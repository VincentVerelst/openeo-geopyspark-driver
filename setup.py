from setuptools import setup,find_packages
import os
import datetime

# Load the openeo version info.
#
# Note that we cannot simply import the module, since dependencies listed
# in setup() will very likely not be installed yet when setup.py run.
#
# See:
#   https://packaging.python.org/guides/single-sourcing-package-version

__version__ = None
date = datetime.datetime.today().strftime('%Y%m%d')

with open('openeogeotrellis/_version.py') as fp:
    exec(fp.read())

if os.environ.get('BUILD_NUMBER') and os.environ.get('BRANCH_NAME'):
    if os.environ.get('BRANCH_NAME') == 'develop':
        version = __version__ + '.' + date + '.' + os.environ['BUILD_NUMBER']
    else:
        version = __version__ + '.' + date + '.' + os.environ['BUILD_NUMBER'] + '+' + os.environ['BRANCH_NAME']
else:
    version = __version__

setup(
    name='openeo-geopyspark',
    version=version,
    packages=find_packages(exclude=('tests', 'scripts')),
    include_package_data = True,
    scripts = ['scripts/submit_batch_job.sh'],
    data_files=['layercatalog.json','scripts/log4j.properties'],
    setup_requires=['pytest-runner'],
    tests_require=['pytest','mock'],
    install_requires=[
        'flask',
        'openeo-api',
        'geopandas==0.3.0',
        'geopyspark==0.4.2'
    ],
)
