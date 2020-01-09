#!/bin/sh -e

if [ -z "${OPENEO_VENV_ZIP}" ]; then
    >&2 echo "Environment variable OPENEO_VENV_ZIP is not set, falling back to default.\n"
    OPENEO_VENV_ZIP=https://artifactory.vgt.vito.be/auxdata-public/openeo/venv36.zip
fi

if [ "$#" -lt 5 ]; then
    >&2 echo "Usage: $0 <job name> <process graph input file> <results output file> <principal> <key tab file> [api version]"
    exit 1
fi

export LOG4J_CONFIGURATION_FILE="./log4j.properties"

if [ ! -f ${LOG4J_CONFIGURATION_FILE} ]; then
    LOG4J_CONFIGURATION_FILE='scripts/log4j.properties'
    if [ ! -f ${LOG4J_CONFIGURATION_FILE} ]; then
        >&2 echo "${LOG4J_CONFIGURATION_FILE} is missing"
        exit 1
    fi

fi

jobName=$1
processGraphFile=$2
outputFile=$3
principal=$4
keyTab=$5
apiVersion=$6
drivermemory=${7-22G}
executormemory=${8-4G}

pysparkPython="venv/bin/python"

kinit -kt ${keyTab} ${principal} || true

export HDP_VERSION=3.0.0.0-1634
export SPARK_HOME=/usr/hdp/$HDP_VERSION/spark2
export PATH="$SPARK_HOME/bin:$PATH"
export SPARK_SUBMIT_OPTS="-Dlog4j.configuration=file:${LOG4J_CONFIGURATION_FILE}"
export LD_LIBRARY_PATH="venv/lib64"

export PYTHONPATH="venv/lib64/python3.6/site-packages:venv/lib/python3.6/site-packages"

extensions=$(ls geotrellis-extensions-*.jar)
backend_assembly=$(ls geotrellis-backend-assembly-*.jar) || true
if [ ! -f ${backend_assembly} ]; then
   backend_assembly=https://artifactory.vgt.vito.be/auxdata-public/openeo/geotrellis-backend-assembly-0.4.5-openeo.jar
fi

pyfiles = "--py-files cropsar*.whl"
if [ -f custom_processes.py ]; then
   pyfiles = ${pyfiles},custom_processes.py
fi

main_py_file='venv/lib64/python3.6/site-packages/openeogeotrellis/deploy/batch_job.py'

spark-submit \
 --master yarn --deploy-mode cluster \
 --principal ${principal} --keytab ${keyTab} \
 --conf spark.yarn.submit.waitAppCompletion=false \
 --driver-memory ${drivermemory} \
 --executor-memory ${executormemory} \
 --driver-java-options "-Dscala.concurrent.context.maxThreads=12" \
 --conf spark.serializer=org.apache.spark.serializer.KryoSerializer \
 --conf spark.kryo.classesToRegister=org.openeo.geotrellisaccumulo.SerializableConfiguration \
 --conf spark.rdd.compress=true \
 --conf spark.driver.cores=4 \
 --conf spark.driver.maxResultSize=5g \
 --conf spark.driver.memoryOverhead=8g \
 --conf spark.executor.memoryOverhead=2g \
 --conf spark.speculation=false \
 --conf spark.dynamicAllocation.minExecutors=20 \
 --conf "spark.yarn.appMasterEnv.SPARK_HOME=$SPARK_HOME" --conf spark.yarn.appMasterEnv.PYTHON_EGG_CACHE=./ \
 --conf "spark.yarn.appMasterEnv.PYSPARK_PYTHON=$pysparkPython" \
 --conf spark.executorEnv.LD_LIBRARY_PATH=venv/lib64 \
 --conf spark.yarn.appMasterEnv.LD_LIBRARY_PATH=venv/lib64 \
 --conf spark.executorEnv.DRIVER_IMPLEMENTATION_PACKAGE=openeogeotrellis --conf spark.yarn.appMasterEnv.DRIVER_IMPLEMENTATION_PACKAGE=openeogeotrellis \
 --conf spark.executorEnv.AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID} --conf spark.yarn.appMasterEnv.AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID} \
 --conf spark.executorEnv.AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY} --conf spark.yarn.appMasterEnv.AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY} \
 --conf spark.yarn.appMasterEnv.OPENEO_REQUIRE_BOUNDS=False \
 --conf spark.shuffle.service.enabled=true --conf spark.dynamicAllocation.enabled=true \
 --conf spark.ui.view.acls.groups=vito \
 --files layercatalog.json,"${processGraphFile}" ${pyfiles} \
 --archives "${OPENEO_VENV_ZIP}#venv" \
 --conf spark.hadoop.security.authentication=kerberos --conf spark.yarn.maxAppAttempts=1 \
 --jars "${extensions}","${backend_assembly}" \
 --name "${jobName}" "${main_py_file}" "$(basename "${processGraphFile}")" "${outputFile}" "${apiVersion}"
