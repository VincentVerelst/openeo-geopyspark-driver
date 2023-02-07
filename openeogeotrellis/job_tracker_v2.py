"""
V2 implementation of JobTracker
"""
# TODO: when stable enough: eliminate original implementation and get rid of "v2"?

import abc
import argparse
import collections
import datetime as dt
import logging
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from subprocess import CalledProcessError
from typing import Callable, List, NamedTuple, Optional, Union

import requests
from openeo.util import TimingLogger, repr_truncate, rfc3339, url_join, deep_get
from openeo_driver.jobregistry import JOB_STATUS, ElasticJobRegistry
from openeo_driver.util.logging import get_logging_config, setup_logging
import openeo_driver.utils

from openeogeotrellis import async_task
from openeogeotrellis.backend import GpsBatchJobs, get_elastic_job_registry
from openeogeotrellis.configparams import ConfigParams
from openeogeotrellis.integrations.kubernetes import (
    K8S_SPARK_APP_STATE,
    k8s_job_name,
    k8s_state_to_openeo_job_status,
    kube_client,
)
from openeogeotrellis.integrations.etl_api import EtlApi
from openeogeotrellis.integrations.yarn import yarn_state_to_openeo_job_status
from openeogeotrellis.job_registry import ZkJobRegistry
from openeogeotrellis.utils import StatsReporter


# Note: hardcoded logger name as this script is executed directly which kills the usefulness of `__name__`.
_log = logging.getLogger("openeogeotrellis.job_tracker_v2")


class _Usage(NamedTuple):
    cpu_seconds: float
    mb_seconds: float

    def to_dict(self) -> dict:
        return {
            "cpu": {"value": self.cpu_seconds, "unit": "cpu-seconds"},
            "memory": {"value": self.mb_seconds, "unit": "mb-seconds"}
        }


class _JobMetadata(NamedTuple):
    """Simple container for openEO batch job oriented metadata"""

    # Job status, following the openEO API spec (see `openeo_driver.jobregistry.JOB_STATUS`)
    status: str

    # RFC-3339 formatted UTC datetime (or None if not started yet)
    start_time: Optional[str] = None

    # RFC-3339 formatted UTC datetime (or None if not finished yet)
    finish_time: Optional[str] = None

    # Resource usage stats (if any)
    usage: Optional[_Usage] = None


class JobMetadataGetterInterface(metaclass=abc.ABCMeta):
    """
    Interface for implementations that interact with some kind of
    task/app orchestration/management component
    to get a job's status metadata (state, start/finish times, resource usage stats, ...)
    For example: YARN ("yarn applications -status ..."), Kubernetes, ...
    """

    @abc.abstractmethod
    def get_job_metadata(self, job_id: str, user_id: str, app_id: str) -> _JobMetadata:
        raise NotImplementedError


class JobCostsCalculator(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def calculate_costs(self, job_info: dict, job_metadata: _JobMetadata, result_metadata: dict) -> float:
        raise NotImplementedError


class EtlApiJobCostsCalculator(JobCostsCalculator):
    def __init__(self, etl_api: EtlApi, etl_api_access_token: str):
        self._etl_api = etl_api
        self._etl_api_access_token = etl_api_access_token

    def calculate_costs(self, job_info: dict, job_metadata: _JobMetadata, result_metadata: dict) -> float:
        job_id = job_info["job_id"]
        # TODO: make _JobMetadata.start_time a datetime instead
        started_ms = rfc3339.parse_datetime(job_metadata.start_time).timestamp() * 1000
        cpu_seconds = job_metadata.usage.cpu_seconds

        unique_process_ids = result_metadata.get('unique_process_ids', [])

        # TODO: make this usage report handling/logging more generic?
        sentinelhub_processing_units = (
            result_metadata.get("usage", {})
            .get("sentinelhub", {})
            .get("value", 0.0)
        )

        sentinelhub_batch_processing_units = float(ZkJobRegistry.get_dependency_usage(job_info) or Decimal("0.0"))

        with self._etl_api as etl_api:
            resource_costs_in_credits = etl_api.log_resource_usage(
                batch_job_id=job_id,
                title=...,
                application_id=...,
                user_id=...,
                started_ms=started_ms,
                finished_ms=...,
                state=...,
                status=...,
                cpu_seconds=cpu_seconds,
                mb_seconds=...,
                duration_ms=...,
                sentinel_hub_processing_units=sentinelhub_processing_units + sentinelhub_batch_processing_units,
                access_token=self._etl_api_access_token
            )

            area = deep_get(result_metadata, 'area', 'value', default=None)

            added_value_costs_in_credits = sum(etl_api.log_added_value(
                batch_job_id=job_id,
                title=...,
                application_id=...,
                user_id=...,
                started_ms=started_ms,
                finished_ms=...,
                process_id=process_id,
                square_meters=float(area) if area is not None else 0.0,
                access_token=self._etl_api_access_token) for process_id in unique_process_ids)

        return resource_costs_in_credits + added_value_costs_in_credits


class NoJobCostsCalculator(JobCostsCalculator):
    def calculate_costs(self, job_info: dict, job_metadata: _JobMetadata, result_metadata: dict) -> float:
        return 0.0


class JobTrackerException(Exception):
    pass


class AppNotFound(JobTrackerException):
    """Exception to throw when app can not be found in YARN, Kubernetes, ..."""
    pass


class YarnAppReportParseException(Exception):
    pass


class YarnStatusGetter(JobMetadataGetterInterface):
    """YARN app status getter"""

    def get_job_metadata(self, job_id: str, user_id: str, app_id: str) -> _JobMetadata:
        try:
            command = ["yarn", "application", "-status", app_id]
            with TimingLogger(f"Running {command}", logger=_log.debug):
                application_report = subprocess.check_output(
                    command, stderr=subprocess.PIPE
                ).decode("utf-8")
        except CalledProcessError as e:
            stdout = e.stdout.decode()
            if "doesn't exist in RM or Timeline Server" in stdout:
                raise AppNotFound(stdout) from None
            else:
                raise
        return self.parse_application_report(application_report)

    def parse_application_report(self, report: str) -> _JobMetadata:
        try:
            props = dict(re.findall(r"^\t(.+?) : (.+)$", report, flags=re.MULTILINE))

            job_status = yarn_state_to_openeo_job_status(
                state=props["State"], final_state=props["Final-State"]
            )

            def ms_epoch_to_date(epoch_millis: str) -> Union[str, None]:
                """Parse millisecond timestamp from app report and return as rfc3339 date (or None)"""
                if epoch_millis == "0":
                    return None
                utc_datetime = dt.datetime.utcfromtimestamp(int(epoch_millis) / 1000)
                return rfc3339.datetime(utc_datetime)

            start_time = ms_epoch_to_date(props["Start-Time"])
            finish_time = ms_epoch_to_date(props["Finish-Time"])

            allocation = props["Aggregate Resource Allocation"]
            match = re.fullmatch(r"^(\d+) MB-seconds, (\d+) vcore-seconds$", allocation)
            usage = _Usage(cpu_seconds=int(match.group(2)), mb_seconds=int(match.group(1))) if match else None

            return _JobMetadata(
                status=job_status,
                start_time=start_time,
                finish_time=finish_time,
                usage=usage,
            )
        except Exception as e:
            raise YarnAppReportParseException() from e


class K8sException(Exception):
    pass


class K8sStatusGetter(JobMetadataGetterInterface):
    """Kubernetes app status getter"""

    def __init__(self, kubecost_url: Optional[str] = None):
        self._kubernetes_api = kube_client()
        # TODO: get this url from config?
        self._kubecost_url = kubecost_url or "http://kubecost.kube-dev.vgt.vito.be/"

    def get_job_metadata(self, job_id: str, user_id: str, app_id: str) -> _JobMetadata:
        job_status = self._get_job_status(job_id=job_id, user_id=user_id)
        usage = self._get_usage(job_id=job_id, user_id=user_id)
        return _JobMetadata(
            status=job_status.status,
            start_time=job_status.start_time,
            finish_time=job_status.finish_time,
            usage=usage,
        )

    def _get_job_status(self, job_id: str, user_id: str) -> _JobMetadata:
        # Local import to avoid kubernetes dependency when not necessary
        import kubernetes.client.exceptions
        try:
            metadata = self._kubernetes_api.get_namespaced_custom_object(
                group="sparkoperator.k8s.io",
                version="v1beta2",
                namespace="spark-jobs",
                plural="sparkapplications",
                name=k8s_job_name(job_id=job_id, user_id=user_id),
            )
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                # TODO: more precise checking that it was indeed the app that was not found (instead of k8s api itself).
                raise AppNotFound() from e
            raise

        if "status" in metadata:
            app_state = metadata["status"]["applicationState"]["state"]
            start_time = metadata["status"]["lastSubmissionAttemptTime"]
            finish_time = metadata["status"]["terminationTime"]
        else:
            _log.warning("No K8s app status found, assuming new app")
            app_state = K8S_SPARK_APP_STATE.NEW
            start_time = finish_time = None

        job_status = k8s_state_to_openeo_job_status(app_state)
        return _JobMetadata(
            status=job_status, start_time=start_time, finish_time=finish_time
        )

    def _get_usage(self, job_id: str, user_id: str) -> Union[_Usage, None]:
        try:
            url = url_join(self._kubecost_url, "/model/allocation")
            namespace = "spark-jobs"
            window = "5d"
            pod = k8s_job_name(job_id=job_id, user_id=user_id) + "*"
            params = (
                ("aggregate", "namespace"),
                ("filterNamespaces", namespace),
                ("filterPods", pod),
                ("window", window),
                ("accumulate", "true"),
            )
            response = requests.get(url, params=params)
            response.raise_for_status()
            total_cost = response.json()
            if not (
                total_cost["code"] == 200
                and len(total_cost["data"]) > 0
                and namespace in total_cost["data"][0]
            ):
                raise K8sException(
                    f"Unexpected response {repr_truncate(total_cost, width=200)}"
                )

            cost = total_cost["data"][0][namespace]
            # TODO: need to iterate through "data" list?
            _log.info(f"Successfully retrieved total cost {cost}")
            return _Usage(cpu_seconds=cost["cpuCoreHours"] * 60 * 60,
                          mb_seconds=cost["ramByteHours"] * 60 * 60 / (1024 * 1024))
        except Exception:
            _log.error(
                f"Failed to retrieve usage stats from kubecost",
                exc_info=True,
                extra={"job_id": job_id},
            )


class JobTracker:
    def __init__(
        self,
        app_state_getter: JobMetadataGetterInterface,
        zk_job_registry: ZkJobRegistry,
        principal: str,
        keytab: str,
        job_costs_calculator: JobCostsCalculator = NoJobCostsCalculator(),
        output_root_dir: Optional[Union[str, Path]] = None,
        elastic_job_registry: Optional[ElasticJobRegistry] = None
    ):
        self._app_state_getter = app_state_getter
        self._zk_job_registry = zk_job_registry
        self._principal = principal
        self._keytab = keytab
        self._job_costs_calculator = job_costs_calculator
        # TODO: inject GpsBatchJobs (instead of constructing it here and requiring all its constructor args to be present)
        #       Also note that only `get_results_metadata` is actually used, so dragging a complete GpsBatchJobs might actuall be overkill in the first place.
        self._batch_jobs = GpsBatchJobs(
            catalog=None,
            jvm=None,
            principal=principal,
            key_tab=keytab,
            vault=None,
            output_root_dir=output_root_dir,
            elastic_job_registry=elastic_job_registry,
        )
        self._elastic_job_registry = elastic_job_registry

    def update_statuses(self, fail_fast: bool = False) -> None:
        """Iterate through all known (ongoing) jobs and update their status"""
        with self._zk_job_registry as zk_job_registry, StatsReporter(
            name="JobTracker.update_statuses stats", report=_log.info
        ) as stats, TimingLogger("JobTracker.update_statuses", logger=_log.info):

            with TimingLogger(title="Fetching jobs to track", logger=_log.info):
                jobs_to_track = zk_job_registry.get_running_jobs()
            _log.info(f"Collected {len(jobs_to_track)} jobs to track")
            stats["collected jobs"] = len(jobs_to_track)

            for job_info in jobs_to_track:
                if not (
                    isinstance(job_info, dict)
                    and job_info.get("job_id")
                    and job_info.get("user_id")
                ):
                    _log.error(
                        f"Invalid job info: {repr_truncate(job_info, width=200)}"
                    )
                    stats["invalid job_info"] += 1
                    continue

                job_id = job_info["job_id"]
                user_id = job_info["user_id"]

                try:
                    application_id = job_info.get("application_id")
                    status = job_info.get("status")

                    if not application_id:
                        # No application_id typically means that job hasn't been started yet.
                        created = job_info.get("created")
                        if created:
                            age = dt.datetime.utcnow() - rfc3339.parse_datetime(created)
                        else:
                            age = "unknown"
                        # TODO: handle very old, non-started jobs? E.g. mark as error?
                        _log.info(
                            f"Skipping job without application_id: {job_id=}, {created=}, {age=}, {status=}"
                        )
                        stats[f"skip due to no application_id ({status=})"] += 1
                        continue

                    self._sync_job_status(
                        job_id=job_id,
                        user_id=user_id,
                        application_id=application_id,
                        job_info=job_info,
                        zk_job_registry=zk_job_registry,
                        stats=stats,
                    )
                except Exception as e:
                    _log.exception(
                        f"Failed status sync for {job_id=}: unexpected {type(e).__name__}: {e}",
                        extra={"job_id": job_id, "user_id": user_id},
                    )
                    stats["failed sync"] += 1
                    if fail_fast:
                        raise

    def _sync_job_status(
        self,
        job_id: str,
        user_id: str,
        application_id: str,
        job_info: dict,
        zk_job_registry: ZkJobRegistry,
        stats: collections.Counter,
    ):
        """Sync job status for a single job"""
        # Local logger with default `extra`
        log = logging.LoggerAdapter(_log, extra={"job_id": job_id, "user_id": user_id})

        assert application_id == job_info.get("application_id")
        previous_status = job_info.get("status")
        log.debug(
            f"About to sync status for {job_id=} {user_id=} {application_id=} {previous_status=}"
        )
        stats[f"job with {previous_status=}"] += 1

        try:
            stats["get metadata attempt"] += 1
            job_metadata: _JobMetadata = self._app_state_getter.get_job_metadata(
                job_id=job_id, user_id=user_id, app_id=application_id
            )
            stats["new metadata"] += 1
        except AppNotFound:
            log.warning(f"App not found: {job_id=} {application_id=}", exc_info=True)
            # TODO: handle status setting generically with logic below (e.g. dummy job_metadata)?
            zk_job_registry.set_status(job_id, user_id, JOB_STATUS.ERROR)
            with ElasticJobRegistry.just_log_errors("job_tracker app not found"):
                if self._elastic_job_registry:
                    # TODO: also set started/finished, exception/error info ...
                    self._elastic_job_registry.set_status(
                        job_id=job_id, status=JOB_STATUS.ERROR
                    )
            stats["app not found"] += 1
            return

        if previous_status != job_metadata.status:
            log.info(
                f"job {job_id}: status change from {previous_status} to {job_metadata.status}",
            )
            stats["status change"] += 1
            stats[f"status change {previous_status!r} -> {job_metadata.status!r}"] += 1
        else:
            stats["status same"] += 1
            stats[f"status same {job_metadata.status!r}"] += 1

        zk_job_registry.patch(
            job_id=job_id,
            user_id=user_id,
            status=job_metadata.status,
            started=job_metadata.start_time,
            finished=job_metadata.finish_time,
            usage=job_metadata.usage.to_dict(),
        )
        with ElasticJobRegistry.just_log_errors(
            f"job_tracker {job_metadata.status=} from {type(self._app_state_getter).__name__}"
        ):
            if self._elastic_job_registry:
                self._elastic_job_registry.set_status(
                    job_id=job_id,
                    status=job_metadata.status,
                    started=job_metadata.start_time,
                    finished=job_metadata.finish_time,
                    # TODO: also record usage data
                )

        if job_metadata.status in {
            JOB_STATUS.FINISHED,
            JOB_STATUS.ERROR,
            JOB_STATUS.CANCELED,
        }:
            stats[f"reached final status {job_metadata.status}"] += 1
            result_metadata = self._batch_jobs.get_results_metadata(job_id, user_id)

            zk_job_registry.remove_dependencies(job_id, user_id)

            # there can be duplicates if batch processes are recycled
            dependency_sources = list(
                set(ZkJobRegistry.get_dependency_sources(job_info))
            )

            if dependency_sources:
                async_task.schedule_delete_batch_process_dependency_sources(
                    job_id, user_id, dependency_sources
                )

            # Note: setting the status is already done with a `patch` higher,
            #       but we do it here again with `set_status` for the "auto_mark_done" feature
            zk_job_registry.set_status(
                job_id=job_id, user_id=user_id, status=job_metadata.status, auto_mark_done=True
            )

            job_costs = self._job_costs_calculator.calculate_costs(job_info, job_metadata, result_metadata)

            # TODO: skip patching the job znode and read from this file directly?
            # TODO: don't add costs if there are none ()?
            zk_job_registry.patch(job_id, user_id, **dict(result_metadata, costs=job_costs))


class CliApp:
    def main(self, *, args: Optional[List[str]] = None):

        args = self.parse_cli_args(args=args)

        self.setup_logging(
            basic_logging=args.basic_logging,
            rotating_file=(
                "logs/job_tracker_python.log"
                if not ConfigParams().is_kube_deploy and Path("logs").is_dir()
                else None
            ),
        )

        _log.info(f"job_tracker_v2 cli {args=}")
        _log.info(f"job_tracker_v2 cli {ConfigParams()=!s}")
        package_versions = openeo_driver.utils.get_package_versions(
            ["openeo", "openeo_driver", "openeo-geopyspark", "kubernetes"]
        )
        _log.info(f"job_tracker_v2 cli {package_versions=}")

        with TimingLogger(logger=_log.info, title=f"job_tracker_v2 cli"):
            try:
                # ZooKeeper Job Registry
                zk_root_path = args.zk_job_registry_root_path
                _log.info(f"Using {zk_root_path=}")
                zk_job_registry = ZkJobRegistry(root_path=zk_root_path)
                etl_api = EtlApi(ConfigParams().etl_api)
                etl_api_access_token = ...  # FIXME

                # Elastic Job Registry (EJR)
                elastic_job_registry = get_elastic_job_registry()

                # YARN or Kubernetes?
                app_cluster = args.app_cluster
                if app_cluster == "auto":
                    # TODO: eliminate (need for) auto-detection.
                    app_cluster = "k8s" if ConfigParams().is_kube_deploy else "yarn"
                if app_cluster == "yarn":
                    app_state_getter = YarnStatusGetter()
                elif app_cluster == "k8s":
                    app_state_getter = K8sStatusGetter()
                else:
                    raise ValueError(app_cluster)
                job_tracker = JobTracker(
                    app_state_getter=app_state_getter,
                    zk_job_registry=zk_job_registry,
                    principal=args.principal,
                    keytab=args.keytab,
                    elastic_job_registry=elastic_job_registry,
                    job_costs_calculator=EtlApiJobCostsCalculator(etl_api, etl_api_access_token)
                )
                job_tracker.update_statuses(fail_fast=args.fail_fast)
            except Exception as e:
                _log.error(e, exc_info=True)
                raise e

    def parse_cli_args(self, args: Optional[List[str]] = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            description="JobTracker from openeo-geopyspark-driver.",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        parser.add_argument(
            "--principal",
            default="openeo@VGT.VITO.BE",
            help="Principal to be used to login to KDC",
        )
        parser.add_argument(
            "--keytab",
            default="openeo-deploy/mep/openeo.keytab",
            help="The full path to the file that contains the keytab for the principal",
        )

        parser.add_argument(
            "--fail-fast",
            action="store_true",
            default=False,
            help="Stop immediately on unexpected errors while tracking a certain job, instead of skipping to next job.",
        )
        parser.add_argument(
            "--app-cluster",
            choices=["yarn", "k8s", "auto"],
            default="auto",
            help="Application cluster to get job/app status from.",
        )
        parser.add_argument(
            "--basic-logging",
            action="store_true",
            help="Use basic logging on stderr instead of JSON formatted logs.",
        )
        parser.add_argument(
            "--zk-job-registry-root-path",
            default=ConfigParams().batch_jobs_zookeeper_root_path,
            help="ZooKeeper root path for the job registry",
        )
        # TODO: also allow setting zk_root_path through "env" setting (prod,dev, integrationtests, ...)?

        return parser.parse_args(args=args)

    def setup_logging(
        self,
        *,
        basic_logging: bool = False,
        rotating_file: Optional[Union[str, Path]] = None,
    ):
        logging_config = get_logging_config(
            # TODO: better handler than "wsgi"?
            root_handlers=["wsgi"] if basic_logging else ["stderr_json"],
            loggers={
                "openeo": {"level": "DEBUG"},
                "openeo_driver": {"level": "DEBUG"},
                "openeogeotrellis": {"level": "DEBUG"},
                _log.name: {"level": "DEBUG"},
            },
        )

        if rotating_file:
            # TODO: support this appending directly in get_logging_config
            logging_config["handlers"]["rotating_file"] = {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(rotating_file),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 1,
                "formatter": "json",
            }
            logging_config["root"]["handlers"].append("rotating_file")

        setup_logging(logging_config, force=True)


if __name__ == "__main__":
    CliApp().main()
