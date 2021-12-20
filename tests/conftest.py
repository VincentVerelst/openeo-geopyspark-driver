import os
import sys
import warnings
from pathlib import Path
from importlib.util import find_spec

import flask
import pytest
from _pytest.terminal import TerminalReporter

from openeo_driver.backend import OpenEoBackendImplementation, UserDefinedProcesses
from openeo_driver.testing import ApiTester
from openeo_driver.views import build_app
from .datacube_fixtures import imagecollection_with_two_bands_and_three_dates, \
    imagecollection_with_two_bands_and_one_date, imagecollection_with_two_bands_and_three_dates_webmerc
from .data import get_test_data_file, TEST_DATA_ROOT

os.environ["OPENEO_CATALOG_FILES"] = str(Path(__file__).parent / "layercatalog.json")


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    """Pytest configuration hook"""
    os.environ['PYTEST_CONFIGURE'] = (os.environ.get('PYTEST_CONFIGURE', '') + ':' + __file__).lstrip(':')
    terminal_reporter = config.pluginmanager.get_plugin("terminalreporter")
    _ensure_spark_home()
    _ensure_jep()
    _ensure_geopyspark(terminal_reporter)
    _setup_local_spark(terminal_reporter, verbosity=config.getoption("verbose"))


def _ensure_spark_home():
    if "SPARK_HOME" not in os.environ:
        import pyspark.find_spark_home
        spark_home = pyspark.find_spark_home._find_spark_home()
        warnings.warn("Env var SPARK_HOME was not set, setting it to {h!r}".format(h=spark_home))
        os.environ["SPARK_HOME"] = spark_home


def _ensure_jep():
    if "LD_LIBRARY_PATH" not in os.environ:
        try:
            os.environ["LD_LIBRARY_PATH"] = os.path.dirname(find_spec("jep").origin)
        except ImportError:
            pass


def _ensure_geopyspark(out: TerminalReporter):
    """Make sure GeoPySpark knows where to find Spark (SPARK_HOME) and py4j"""
    try:
        import geopyspark
        out.write_line("[conftest.py] Succeeded to import geopyspark automatically: {p!r}".format(p=geopyspark))
    except KeyError as e:
        # Geopyspark failed to detect Spark home and py4j, let's fix that.
        from pyspark import find_spark_home
        pyspark_home = Path(find_spark_home._find_spark_home())
        out.write_line("[conftest.py] Failed to import geopyspark automatically. "
                       "Will set up py4j path using Spark home: {h}".format(h=pyspark_home))
        py4j_zip = next((pyspark_home / 'python' / 'lib').glob('py4j-*-src.zip'))
        out.write_line("[conftest.py] py4j zip: {z!r}".format(z=py4j_zip))
        sys.path.append(str(py4j_zip))


def _setup_local_spark(out: TerminalReporter, verbosity=0):
    # TODO make a "spark_context" fixture instead of doing this through pytest_configure
    out.write_line("[conftest.py] Setting up local Spark")

    travis_mode = 'TRAVIS' in os.environ
    master_str = "local[2]" if travis_mode else "local[2]"

    if 'PYSPARK_PYTHON' not in os.environ:
        os.environ['PYSPARK_PYTHON'] = sys.executable

    from geopyspark import geopyspark_conf
    from pyspark import SparkContext

    conf = geopyspark_conf(master=master_str, appName="OpenEO-GeoPySpark-Driver-Tests")
    conf.set('spark.kryoserializer.buffer.max', value='1G')
    conf.set(key='spark.kryo.registrator', value='geopyspark.geotools.kryo.ExpandedKryoRegistrator')
    conf.set(key='spark.kryo.classesToRegister', value='org.openeo.geotrellisaccumulo.SerializableConfiguration,ar.com.hjg.pngj.ImageInfo,ar.com.hjg.pngj.ImageLineInt,geotrellis.raster.RasterRegion$GridBoundsRasterRegion')
    # Only show spark progress bars for high verbosity levels
    conf.set('spark.ui.showConsoleProgress', verbosity >= 3)

    if travis_mode:
        conf.set(key='spark.driver.memory', value='2G')
        conf.set(key='spark.executor.memory', value='2G')
        conf.set('spark.ui.enabled', False)
    else:
        conf.set('spark.ui.enabled', True)

    out.write_line("[conftest.py] SparkContext.getOrCreate with {c!r}".format(c=conf.getAll()))
    context = SparkContext.getOrCreate(conf)
    out.write_line("[conftest.py] JVM info: {d!r}".format(d={
        f: context._jvm.System.getProperty(f)
        for f in [
            "java.version", "java.vendor", "java.home",
            "java.class.version",
            # "java.class.path",
        ]
    }))

    out.write_line("[conftest.py] Validating the Spark context")
    dummy = context._jvm.org.openeo.geotrellis.OpenEOProcesses()
    answer = context.parallelize([9, 10, 11, 12]).sum()
    out.write_line("[conftest.py] " + repr((answer, dummy)))

    return context


@pytest.fixture(params=["0.4.0", "1.0.0"])
def api_version(request):
    return request.param


@pytest.fixture
def udf_noop():
    file_name = get_test_data_file("udf_noop.py")
    with open(file_name, "r")  as f:
        udf_code = f.read()

    noop_udf_callback = {
        "udf_process": {
            "arguments": {
                "data": {
                    "from_argument": "dimension_data"
                },
                "udf": udf_code
            },
            "process_id": "run_udf",
            "result": True
        },
    }
    return noop_udf_callback


@pytest.fixture
def backend_implementation() -> 'GeoPySparkBackendImplementation':
    from openeogeotrellis.backend import GeoPySparkBackendImplementation
    return GeoPySparkBackendImplementation()


@pytest.fixture
def flask_app(backend_implementation) -> flask.Flask:
    app = build_app(
        backend_implementation=backend_implementation,
        # error_handling=False,
    )
    app.config['TESTING'] = True
    app.config['SERVER_NAME'] = 'oeo.net'
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def user_defined_process_registry(backend_implementation: OpenEoBackendImplementation) -> UserDefinedProcesses:
    return backend_implementation.user_defined_processes


@pytest.fixture
def api(api_version, client) -> ApiTester:
    return ApiTester(api_version=api_version, client=client, data_root=TEST_DATA_ROOT)


@pytest.fixture
def api100(client) -> ApiTester:
    return ApiTester(api_version="1.0.0", client=client, data_root=TEST_DATA_ROOT)
