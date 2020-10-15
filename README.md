## OpenEO Geopyspark Driver

[![Status](https://img.shields.io/badge/Status-proof--of--concept-yellow.svg)]()
[![Build Status](https://travis-ci.org/Open-EO/openeo-geopyspark-driver.svg?branch=master)](https://travis-ci.org/Open-EO/openeo-geopyspark-driver)

Python version: 3.6

This driver implements the GeoPySpark/Geotrellis specific backend for OpenEO.

It does this by implementing a direct (non-REST) version of the OpenEO client API on top 
of [GeoPySpark](https://github.com/locationtech-labs/geopyspark/). 

A REST service based on Flask translates incoming calls to this local API.

![Technology stack](openeo-geotrellis-techstack.png?raw=true "Technology stack")

### Operating environment dependencies
This backend has been tested with:
- Something that runs Spark: Kubernetes or YARN (Hadoop), standalone or on your laptop
- Accumulo as the tile storage backend for Geotrellis
- Reading GeoTiff files directly from disk or object storage

### Public endpoint
https://openeo.vito.be/openeo/

### Running locally
Preparation:
A few custom Scala classes are needed to run this project, these can be found in this jar:
https://artifactory.vgt.vito.be/libs-snapshot-public/org/openeo/geotrellis-extensions/1.4.0-SNAPSHOT/geotrellis-extensions-1.4.0-SNAPSHOT.jar
Geopyspark will search for any jar in the 'jars' directory and add it to the classpath. So make
sure that this jar can be found in the correct location.
 
For development, you can run the service:

    export SPARK_HOME=$(find_spark_home.py)
    export HADOOP_CONF_DIR=/etc/hadoop/conf
    export FLASK_DEBUG=1
    export DRIVER_IMPLEMENTATION_PACKAGE=openeogeotrellis
    python openeogeotrellis/deploy/local.py


For production, a gunicorn server script is available:
PYTHONPATH=. python openeogeotrellis/server.py 

### Running on the Proba-V MEP
The web application can be deployed by running:
sh scripts/submit.sh
This will package the application and it's dependencies from source, and submit it on the cluster. The application will register itself with an NginX reverse proxy using Zookeeper.


### Running the unit tests

The unit tests expect that environment variable `SPARK_HOME` is set,
which can easily be done from within you development virtual environment as follows:

    export SPARK_HOME=$(find_spark_home.py)
    pytest
