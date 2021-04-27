import numpy as np
import pytest
from numpy.testing import assert_equal

from openeo_driver.backend import OpenEoBackendImplementation, UserDefinedProcesses
from openeo_driver.testing import ApiTester, TEST_USER
from openeo_driver.views import app
from .data import TEST_DATA_ROOT


@pytest.fixture
def backend_implementation() -> OpenEoBackendImplementation:
    import openeo_driver.views
    return openeo_driver.views.backend_implementation


@pytest.fixture
def user_defined_process_registry(backend_implementation: OpenEoBackendImplementation) -> UserDefinedProcesses:
    return backend_implementation.user_defined_processes


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SERVER_NAME'] = 'oeo.net'
    return app.test_client()


@pytest.fixture
def api(api_version, client) -> ApiTester:
    return ApiTester(api_version=api_version, client=client, data_root=TEST_DATA_ROOT)


@pytest.fixture
def api100(client) -> ApiTester:
    return ApiTester(api_version="1.0.0", client=client, data_root=TEST_DATA_ROOT)


def test_execute_math_basic(api100):
    res = api100.check_result({"add": {"process_id": "add", "arguments": {"x": 3, "y": 5}, "result": True}})
    assert res.json == 8


def test_load_collection_json_basic(api100):
    res = api100.check_result({
        "lc": {
            "process_id": "load_collection",
            "arguments": {
                "id": "TestCollection-LonLat4x4",
                "temporal_extent": ["2021-01-01", "2021-01-10"],
                "spatial_extent": {"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0},
                "bands": ["Flat:1", "TileRow", "Longitude", "Day"]
            },
        },
        "save": {
            "process_id": "save_result",
            "arguments": {"data": {"from_node": "lc"}, "format": "json"},
            "result": True,
        }
    })
    res.assert_status_code(200)
    data = res.json

    assert data["dims"] == ["t", "bands", "x", "y"]
    assert_equal(data["data"], [[
        np.ones((4, 4)),
        np.zeros((4, 4)),
        [[0, 0, 0, 0], [0.25, 0.25, 0.25, 0.25], [0.5, 0.5, 0.5, 0.5], [0.75, 0.75, 0.75, 0.75]],
        5 * np.ones((4, 4))
    ]])


def test_udp_simple_temporal_reduce(api100, user_defined_process_registry):
    """Test calling a UDP with simple temporal reduce operation"""
    udp_spec = {
        "id": "reduce_t_max",
        "parameters": [
            {"name": "data", "schema": {"type": "object", "subtype": "raster-cube"}}
        ],
        "process_graph": {
            "reduce": {
                "process_id": "reduce_dimension",
                "arguments": {
                    "data": {"from_parameter": "data"},
                    "dimension": "t",
                    "reducer": {
                        "process_graph": {
                            "max": {
                                "process_id": "max", "arguments": {"data": {"from_parameter": "data"}}, "result": True
                            }
                        }
                    }
                },
                "result": True
            }
        }
    }
    user_defined_process_registry.save(user_id=TEST_USER, process_id="reduce_t_max", spec=udp_spec)

    res = api100.check_result({
        "lc": {
            "process_id": "load_collection",
            "arguments": {
                "id": "TestCollection-LonLat4x4",
                "temporal_extent": ["2021-01-01", "2021-02-01"],
                "spatial_extent": {"west": 0.0, "south": 0.0, "east": 1.0, "north": 1.0},
                "bands": ["Longitude", "Day"]
            },
        },
        "udp": {
            "process_id": "reduce_t_max", "arguments": {"data": {"from_node": "lc"}}
        },
        "save": {
            "process_id": "save_result",
            "arguments": {"data": {"from_node": "udp"}, "format": "json"},
            "result": True,
        }
    })
    res.assert_status_code(200)
    data = res.json
    assert data["dims"] == ["bands", "x", "y"]
    assert_equal(data["data"], np.array([
        np.array([[0, .25, .5, .75]] * 4).T,
        np.full((4, 4), fill_value=25)
    ]))
