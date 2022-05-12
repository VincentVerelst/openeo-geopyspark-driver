import kazoo.client
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import sys
from subprocess import CalledProcessError
from typing import Callable, Union
import traceback
import time
from collections import namedtuple
from datetime import datetime

import openeogeotrellis.backend
from openeo.util import date_to_rfc3339
import re
from pythonjsonlogger.jsonlogger import JsonFormatter

from openeo_driver.util.logging import JSON_LOGGER_DEFAULT_FORMAT
from openeogeotrellis.job_registry import JobRegistry
from openeogeotrellis.backend import GpsBatchJobs
from openeogeotrellis.configparams import ConfigParams
from openeogeotrellis import async_task

_log = logging.getLogger(__name__)

# TODO: make this job tracker logic an internal implementation detail of JobRegistry?


class JobTracker:
    class _UnknownApplicationIdException(ValueError):
        pass

    _YarnStatus = namedtuple('YarnStatus', ['state', 'final_state', 'start_time', 'finish_time',
                                            'aggregate_resource_allocation'])
    _KubeStatus = namedtuple('KubeStatus', ['state', 'start_time', 'finish_time'])

    def __init__(self, job_registry: Callable[[], JobRegistry], principal: str, keytab: str):
        self._job_registry = job_registry
        self._principal = principal
        self._keytab = keytab
        self._batch_jobs = GpsBatchJobs(catalog=None, jvm=None, principal=principal, key_tab=keytab)

    def loop_update_statuses(self, interval_s: int = 60):
        with self._job_registry() as registry:
            registry.ensure_paths()

        try:
            i = 0

            while True:
                try:
                    _log.info("tracking statuses...")

                    if i % 60 == 0:
                        self._refresh_kerberos_tgt()

                    self.update_statuses()
                except Exception:
                    _log.warning("scheduling new run after failing to track batch jobs:\n{e}"
                                 .format(e=traceback.format_exc()))

                time.sleep(interval_s)

                i += 1
        except KeyboardInterrupt:
            pass

    def update_statuses(self) -> None:
        with self._job_registry() as registry:
            registry.ensure_paths()

            jobs_to_track = registry.get_running_jobs()

            for job_info in jobs_to_track:
                try:
                    job_id, user_id = job_info['job_id'], job_info['user_id']
                    application_id, current_status = job_info['application_id'], job_info['status']

                    if application_id:
                        try:
                            if ConfigParams().is_kube_deploy:
                                from openeogeotrellis.utils import s3_client, download_s3_dir
                                state, start_time, finish_time = JobTracker._kube_status(job_id, user_id)

                                new_status = JobTracker._kube_status_parser(state)

                                registry.patch(job_id, user_id,
                                               status=new_status,
                                               started=start_time,
                                               finished=finish_time)

                                if current_status != new_status:
                                    _log.info("changed job %s status from %s to %s" %
                                              (job_id, current_status, new_status), extra={'job_id': job_id})

                                if state == "COMPLETED":
                                    # TODO: do we support SHub batch processes in this environment? The AWS
                                    #  credentials conflict.
                                    download_s3_dir("OpenEO-data", "batch_jobs/{j}".format(j=job_id))

                                    result_metadata = self._batch_jobs.get_results_metadata(job_id, user_id)
                                    registry.patch(job_id, user_id, **result_metadata)

                                    registry.mark_done(job_id, user_id)
                                    _log.info("marked %s as done" % job_id, extra={'job_id': job_id})
                            else:
                                state, final_state, start_time, finish_time, aggregate_resource_allocation =\
                                    JobTracker._yarn_status(application_id)

                                memory_time_megabyte_seconds, cpu_time_seconds =\
                                    JobTracker._parse_resource_allocation(aggregate_resource_allocation)

                                new_status = JobTracker._to_openeo_status(state, final_state)

                                registry.patch(job_id, user_id,
                                               status=new_status,
                                               started=JobTracker._to_serializable_datetime(start_time),
                                               finished=JobTracker._to_serializable_datetime(finish_time),
                                               memory_time_megabyte_seconds=memory_time_megabyte_seconds,
                                               cpu_time_seconds=cpu_time_seconds)

                                if current_status != new_status:
                                    _log.info("changed job %s status from %s to %s" %
                                              (job_id, current_status, new_status), extra={'job_id': job_id})

                                if final_state != "UNDEFINED":
                                    result_metadata = self._batch_jobs.get_results_metadata(job_id, user_id)
                                    # TODO: skip patching the job znode and read from this file directly?
                                    registry.patch(job_id, user_id, **result_metadata)

                                    registry.remove_dependencies(job_id, user_id)

                                    # there can be duplicates if batch processes are recycled
                                    dependency_sources = list(set(JobRegistry.get_dependency_sources(job_info)))

                                    if dependency_sources:
                                        async_task.schedule_delete_batch_process_dependency_sources(
                                            job_id, user_id, dependency_sources)

                                    registry.mark_done(job_id, user_id)

                                    _log.info("marked %s as done" % job_id, extra={
                                        'job_id': job_id,
                                        'area': result_metadata.get('area'),
                                        'unique_process_ids': result_metadata.get('unique_process_ids'),
                                        'cpu_time_seconds': cpu_time_seconds
                                    })
                        except JobTracker._UnknownApplicationIdException:
                            registry.mark_done(job_id, user_id)
                except Exception:
                    _log.warning("resuming with remaining jobs after failing to handle batch job {j}:\n{e}"
                                 .format(j=job_id, e=traceback.format_exc()), extra={'job_id': job_id})
                    registry.set_status(job_id, user_id, 'error')
                    registry.mark_done(job_id, user_id)

    @staticmethod
    def yarn_available() -> bool:
        """Check if YARN tools are available."""
        try:
            _log.info("Checking if Hadoop 'yarn' tool is available")
            output = subprocess.check_output(["yarn", "version"]).decode("ascii")
            hadoop_yarn_available = "hadoop" in output.lower()
            _log.info("Hadoop yarn available: {a}".format(a=hadoop_yarn_available))
            return hadoop_yarn_available
        except Exception as e:
            _log.info("Failed to run 'yarn': {e!r}".format(e=e))
            return False

    @staticmethod
    def _kube_status(job_id: str, user_id: str) -> '_KubeStatus':
        from openeogeotrellis.utils import kube_client

        user_id_truncated = user_id.split('@')[0][:20]
        job_id_truncated = job_id.split('-')[0]
        try:
            api_instance = kube_client()
            status = api_instance.get_namespaced_custom_object(
                    "sparkoperator.k8s.io",
                    "v1beta2",
                    "spark-jobs",
                    "sparkapplications",
                    "job-{id}-{user}".format(id=job_id_truncated, user=user_id_truncated))

            return JobTracker._KubeStatus(
                status['status']['applicationState']['state'],
                status['status']['lastSubmissionAttemptTime'],
                status['status']['terminationTime']
            )

        except Exception as e:
            _log.info(e)

    @staticmethod
    def _yarn_status(application_id: str) -> '_YarnStatus':
        """Returns (State, Final-State) of a job as reported by YARN."""

        try:
            application_report = subprocess.check_output(
                ["yarn", "application", "-status", application_id]).decode()

            props = re.findall(r"\t(.+) : (.+)", application_report)

            def prop_value(name: str) -> str:
                return next(value for key, value in props if key == name)

            return JobTracker._YarnStatus(
                prop_value("State"),
                prop_value("Final-State"),
                prop_value("Start-Time"),
                prop_value("Finish-Time"),
                prop_value("Aggregate Resource Allocation")
            )
        except CalledProcessError as e:
            stdout = e.stdout.decode()
            if "doesn't exist in RM or Timeline Server" in stdout:
                raise JobTracker._UnknownApplicationIdException(stdout)
            else:
                raise

    @staticmethod
    def _parse_resource_allocation(aggregate_resource_allocation) -> (int, int):
        match = re.fullmatch(r"^(\d+) MB-seconds, (\d+) vcore-seconds$", aggregate_resource_allocation)

        return int(match.group(1)), int(match.group(2)) if match else (None, None)

    @staticmethod
    def _to_openeo_status(state: str, final_state: str) -> str:
        # TODO: encapsulate status
        if state == 'ACCEPTED':
            new_status = 'queued'
        elif state == 'RUNNING':
            new_status = 'running'
        else:
            new_status = 'created'

        if final_state == 'KILLED':
            new_status = 'canceled'
        elif final_state == 'SUCCEEDED':
            new_status = 'finished'
        elif final_state == 'FAILED':
            new_status = 'error'

        return new_status

    @staticmethod
    def _kube_status_parser(state: str) -> str:
        if state == 'PENDING':
            new_status = 'queued'
        elif state == 'RUNNING':
            new_status = 'running'
        elif state == 'COMPLETED':
            new_status = 'finished'
        elif state == 'FAILED':
            new_status = 'error'
        else:
            new_status = 'created'

        return new_status

    @staticmethod
    def _to_serializable_datetime(epoch_millis: str) -> Union[str, None]:
        if epoch_millis == "0":
            return None

        utc_datetime = datetime.utcfromtimestamp(int(epoch_millis) / 1000)
        return date_to_rfc3339(utc_datetime)

    def _refresh_kerberos_tgt(self):
        if self._keytab and self._principal:
            cmd = ["kinit", "-V", "-kt", self._keytab, self._principal]

            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            for line in p.stdout:
                _log.info(line.rstrip().decode())

            p.wait()
            if p.returncode:
                _log.warning("{c} returned exit code {r}".format(c=" ".join(cmd), r=p.returncode))
        else:
            _log.warning("No Kerberos principal/keytab: will not refresh TGT")


if __name__ == '__main__':
    import argparse

    logging.basicConfig(level=logging.INFO)
    openeogeotrellis.backend.logger.setLevel(logging.DEBUG)
    kazoo.client.log.setLevel(logging.WARNING)

    # Note: The Java logging is also supposed to match.
    json_formatter = JsonFormatter(JSON_LOGGER_DEFAULT_FORMAT)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.formatter = json_formatter

    rolling_file_handler = RotatingFileHandler("logs/job_tracker_python.log", maxBytes=10 * 1024 * 1024, backupCount=1)
    rolling_file_handler.formatter = json_formatter

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.addHandler(rolling_file_handler)

    _log.info("ConfigParams(): {c}".format(c=ConfigParams()))

    parser = argparse.ArgumentParser(usage="OpenEO JobTracker", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--principal", default="openeo@VGT.VITO.BE", help="Principal to be used to login to KDC")
    parser.add_argument("--keytab", default="openeo-deploy/mep/openeo.keytab",
                        help="The full path to the file that contains the keytab for the principal")
    args = parser.parse_args()

    try:
        JobTracker(JobRegistry, args.principal, args.keytab).update_statuses()
    except Exception as e:
        _log.error(e, exc_info=True)
        raise e
