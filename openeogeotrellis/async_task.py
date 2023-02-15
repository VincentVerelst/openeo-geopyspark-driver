import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
from subprocess import Popen, PIPE
from typing import List, Optional

import kazoo.client
from py4j.java_gateway import OutputConsumer, ProcessConsumer

import openeogeotrellis
from kafka import KafkaProducer
from openeo.util import dict_no_none
from openeo_driver.errors import JobNotFoundException
from openeo_driver.jobregistry import JOB_STATUS
from openeo_driver.util.logging import JSON_LOGGER_DEFAULT_FORMAT
from openeogeotrellis import sentinel_hub
from openeogeotrellis.backend import GpsBatchJobs
from openeogeotrellis.configparams import ConfigParams
from openeogeotrellis.layercatalog import get_layer_catalog
from openeogeotrellis.vault import Vault
from py4j.clientserver import ClientServer, JavaParameters

from openeogeotrellis.job_registry import ZkJobRegistry
from pythonjsonlogger.jsonlogger import JsonFormatter

ARG_BATCH_JOB_ID = 'batch_job_id'
ARG_USER_ID = 'user_id'


# TODO: include job_id in log statements not issued by our own code e.g. Py4J  # 141
_log = logging.getLogger(__name__)

SENTINEL_HUB_BATCH_PROCESSES_POLL_INTERVAL_S = 60

TASK_DELETE_BATCH_PROCESS_DEPENDENCY_SOURCES = 'delete_batch_process_dependency_sources'
TASK_POLL_SENTINELHUB_BATCH_PROCESSES = 'poll_sentinelhub_batch_processes'


def schedule_delete_batch_process_dependency_sources(batch_job_id: str, user_id: str, dependency_sources: List[str]):
    _schedule_task(task_id=TASK_DELETE_BATCH_PROCESS_DEPENDENCY_SOURCES,
                   arguments={
                       ARG_BATCH_JOB_ID: batch_job_id,
                       ARG_USER_ID: user_id,
                       'dependency_sources': dependency_sources
                   }, job_id=batch_job_id, user_id=user_id)


def schedule_poll_sentinelhub_batch_processes(batch_job_id: str, user_id: str, sentinel_hub_client_alias: str,
                                              vault_token: Optional[str]):
    _schedule_task(task_id=TASK_POLL_SENTINELHUB_BATCH_PROCESSES,
                   arguments={
                       ARG_BATCH_JOB_ID: batch_job_id,
                       ARG_USER_ID: user_id,
                       'sentinel_hub_client_alias': sentinel_hub_client_alias,
                       'vault_token': vault_token
                   }, job_id=batch_job_id, user_id=user_id)


def _schedule_task(task_id: str, arguments: dict, job_id: str, user_id: str):
    task = {
        'task_id': task_id,
        'arguments': arguments
    }

    env = ConfigParams().async_task_handler_environment

    def encode(s: str) -> bytes:
        return s.encode('utf-8')

    producer = KafkaProducer(
        bootstrap_servers="epod-master1.vgt.vito.be:6668,epod-master2.vgt.vito.be:6668,epod-master3.vgt.vito.be:6668",
        security_protocol='PLAINTEXT',
        acks='all'
    )

    try:
        producer.send(topic="openeo-async-tasks",
                      value=encode(json.dumps(task)),
                      headers=[('env', encode(env))] if env else None).get(timeout=120)

        _log.info(f"scheduled task {json.dumps(_redact(task))} on env {env}", extra={'job_id': job_id,
                                                                                     'user_id': user_id})
    finally:
        producer.close()


def _redact(task: dict) -> dict:
    def redact(prop: Optional[str], value):
        sensitive = isinstance(prop, str) and any(sensitive_value in prop.lower()
                                                  for sensitive_value in ["secret", "token"])

        if sensitive:
            return "(redacted)"
        elif isinstance(value, dict):
            return {prop: redact(prop, value) for prop, value in value.items()}
        else:
            return value

    return redact(None, task)


def launch_client_server(jarpath, redirect_stdout, redirect_stderr, classpath, javaopts) -> ClientServer:
    # mimics py4j.java_gateway.JavaGateway.launch_gateway
    daemonize_redirect = True

    classpath = os.pathsep.join((jarpath, classpath))

    command = ["java", "-classpath", classpath] + javaopts + ["org.openeo.logging.py4j.ClientServer"]

    stderr = redirect_stderr

    proc = Popen(command, stdout=PIPE, stdin=PIPE, stderr=stderr)

    _port = int(proc.stdout.readline())

    OutputConsumer(redirect_stdout, proc.stdout, daemon=daemonize_redirect).start()
    ProcessConsumer(proc, [redirect_stdout], daemon=daemonize_redirect).start()

    return ClientServer(JavaParameters(port=_port, eager_load=True))


# TODO: DRY this, cleaner.sh and job_tracker.sh
def main():
    import argparse

    logging.basicConfig(level=logging.INFO)
    openeogeotrellis.backend.logger.setLevel(logging.DEBUG)
    kazoo.client.log.setLevel(logging.WARNING)

    # Note: The Java logging is also supposed to match.
    json_formatter = JsonFormatter(JSON_LOGGER_DEFAULT_FORMAT)

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.formatter = json_formatter

    rolling_file_handler = RotatingFileHandler("logs/async_task_python.log", maxBytes=10 * 1024 * 1024, backupCount=1)
    rolling_file_handler.formatter = json_formatter

    root_logger = logging.getLogger()
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(rolling_file_handler)

    _log.info("ConfigParams(): {c}".format(c=ConfigParams()))

    parser = argparse.ArgumentParser(usage="OpenEO AsyncTask --task <task>",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--py4j-jarpath", default="venv/share/py4j/py4j0.10.7.jar", help='Path to the Py4J jar')
    parser.add_argument("--py4j-classpath", default="geotrellis-extensions-2.2.0-SNAPSHOT.jar",
                        help='Classpath used to launch the Java Gateway')
    parser.add_argument("--py4j-maximum-heap-size", default="1G",
                        help='Maximum heap size for the Java Gateway JVM')
    parser.add_argument("--principal", default="openeo@VGT.VITO.BE", help="Principal to be used to login to KDC")
    parser.add_argument("--keytab", default="openeo-deploy/mep/openeo.keytab",
                        help="Path to the file that contains the keytab for the principal")
    parser.add_argument("--task", required=True, dest="task_json", help="The task description in JSON")

    args = parser.parse_args()

    task = json.loads(args.task_json)
    arguments: dict = task.get('arguments', {})

    try:
        task_id = task['task_id']
        if task_id not in [TASK_POLL_SENTINELHUB_BATCH_PROCESSES, TASK_DELETE_BATCH_PROCESS_DEPENDENCY_SOURCES]:
            raise ValueError(f'unsupported task_id "{task_id}"')

        java_opts = [
            "-client",
            f"-Xmx{args.py4j_maximum_heap_size}",
            "-Dsoftware.amazon.awssdk.http.service.impl=software.amazon.awssdk.http.urlconnection.UrlConnectionSdkHttpService",
            "-Dlog4j2.configuration=file:async_task_log4j2.xml"
        ]

        java_gateway = launch_client_server(jarpath=args.py4j_jarpath,
                                            classpath=args.py4j_classpath,
                                            javaopts=java_opts,
                                            redirect_stdout=sys.stdout,
                                            redirect_stderr=sys.stderr)

        try:
            def get_batch_jobs(batch_job_id: str, user_id: str) -> GpsBatchJobs:
                vault = Vault(ConfigParams().vault_addr)
                catalog = get_layer_catalog(vault=vault, opensearch_enrich=True)

                jvm = java_gateway.jvm
                jvm.org.slf4j.MDC.put(jvm.org.openeo.logging.JsonLayout.UserId(), user_id)
                jvm.org.slf4j.MDC.put(jvm.org.openeo.logging.JsonLayout.JobId(), batch_job_id)

                batch_jobs = GpsBatchJobs(catalog, jvm, args.principal, args.keytab, vault=vault)

                default_sentinel_hub_credentials = vault.get_sentinel_hub_credentials(
                    sentinel_hub_client_alias='default',
                    vault_token=vault.login_kerberos(args.principal, args.keytab))

                batch_jobs.set_default_sentinel_hub_credentials(
                    client_id=default_sentinel_hub_credentials.client_id,
                    client_secret=default_sentinel_hub_credentials.client_secret)

                return batch_jobs

            if task_id == TASK_DELETE_BATCH_PROCESS_DEPENDENCY_SOURCES:
                batch_job_id = arguments[ARG_BATCH_JOB_ID]
                user_id = arguments.get(ARG_USER_ID)
                dependency_sources = (arguments.get('dependency_sources')
                                      or [f"s3://{sentinel_hub.OG_BATCH_RESULTS_BUCKET}/{subfolder}"
                                          for subfolder in arguments['subfolders']])

                _log.info(f"removing dependency sources {dependency_sources} for batch job {batch_job_id}...",
                          extra={'job_id': batch_job_id})

                batch_jobs = get_batch_jobs(batch_job_id, user_id)
                batch_jobs.delete_batch_process_dependency_sources(
                    job_id=batch_job_id,
                    dependency_sources=dependency_sources,
                    propagate_errors=True)
            elif task_id == TASK_POLL_SENTINELHUB_BATCH_PROCESSES:
                batch_job_id = arguments[ARG_BATCH_JOB_ID]
                user_id = arguments[ARG_USER_ID]
                sentinel_hub_client_alias = arguments.get('sentinel_hub_client_alias', 'default')
                vault_token = arguments.get('vault_token')

                batch_jobs = get_batch_jobs(batch_job_id, user_id)

                while True:
                    time.sleep(SENTINEL_HUB_BATCH_PROCESSES_POLL_INTERVAL_S)

                    with ZkJobRegistry() as registry:
                        job_info = registry.get_job(batch_job_id, user_id)

                    if job_info.get('dependency_status') not in ['awaiting', "awaiting_retry"]:
                        break
                    else:
                        try:
                            batch_jobs.poll_sentinelhub_batch_processes(job_info, sentinel_hub_client_alias, vault_token)
                        except Exception:
                            # TODO: retry in Nifi? How to mark this job as 'error' then?
                            _log.error("failed to handle polling batch processes", exc_info=True,
                                       extra={'job_id': batch_job_id, 'user_id': user_id})

                            with ZkJobRegistry() as registry:
                                registry.set_status(batch_job_id, user_id, JOB_STATUS.ERROR)

                            raise  # TODO: this will get caught by the exception handler below which will just log it again  # 141
            else:
                raise AssertionError(f'unexpected task_id "{task_id}"')
        except JobNotFoundException as e:
            # TODO: look for "Deleted ..." log entry in Elasticsearch to avoid a false negative?
            _log.warning("job not found; assuming user deleted it in the meanwhile", exc_info=True,
                         extra={'job_id': e.job_id})
        finally:
            java_gateway.shutdown()
    except Exception as e:
        extra = dict_no_none(
            job_id=arguments.get(ARG_BATCH_JOB_ID),
            user_id=arguments.get(ARG_USER_ID)
        )

        _log.error(f"failed to handle task {json.dumps(_redact(task))}", exc_info=True, extra=extra)
        raise e


if __name__ == '__main__':
    main()
