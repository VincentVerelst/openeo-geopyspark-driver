import os
from pathlib import Path
from pprint import pformat
from typing import Optional

from openeo_driver.utils import smart_bool


class ConfigParams:
    # TODO: start using real config files instead of this thin wrapper around env vars?
    #       see https://github.com/Open-EO/openeo-geopyspark-driver/issues/285

    def __init__(self, env=os.environ):
        self.openeo_env = env.get("OPENEO_ENV", "unknown")
        self.zookeepernodes = env.get(
            "ZOOKEEPERNODES",
            'epod-master1.vgt.vito.be:2181,epod-master2.vgt.vito.be:2181,epod-master3.vgt.vito.be:2181'
        ).split(',')

        self.batch_jobs_zookeeper_root_path = env.get(
            "BATCH_JOBS_ZOOKEEPER_ROOT_PATH", "/openeo/jobs"
        )
        self.async_task_handler_environment = env.get("ASYNC_TASK_HANDLER_ENV")
        self.cache_shub_batch_results = ConfigParams._as_boolean(env.get("CACHE_SHUB_BATCH_RESULTS"))

        self.async_tasks_kafka_bootstrap_servers = env.get(
            "ASYNC_TASKS_KAFKA_BOOTSTRAP_SERVERS",
            "epod-master1.vgt.vito.be:6668,epod-master2.vgt.vito.be:6668,epod-master3.vgt.vito.be:6668",
        )

        self.yarn_rest_api_base_url = env.get(
            "YARN_REST_API_BASE_URL", "https://epod-master1.vgt.vito.be:8090"
        )

        # TODO #283 using this "is_ci_context" switch is an anti-pattern (induces hard to maintain code and make unit testing difficult)
        # Are we running in a unittest or continuous integration context?
        self.is_ci_context = any(v in env for v in ['PYTEST_CURRENT_TEST', 'PYTEST_CONFIGURE'])

        # TODO: can we avoid using env variables?
        self.layer_catalog_metadata_files = env.get("OPENEO_CATALOG_FILES", "layercatalog.json").split(",")

        self.default_opensearch_endpoint = env.get("OPENSEARCH_ENDPOINT", "https://services.terrascope.be/catalogue")

        self.opensearch_enrich = smart_bool(env.get("OPENSEARCH_ENRICH", True))

        # TODO #283 using this "is_kube_deploy" switch is an anti-pattern (induces hard to maintain code and make unit testing difficult)
        self.is_kube_deploy = env.get("KUBE", False)

        self.s1backscatter_elev_geoid = env.get("OPENEO_S1BACKSCATTER_ELEV_GEOID")

        self.batch_job_output_root = Path(
            env.get("OPENEO_BATCH_JOB_OUTPUT_ROOT")
            or (
                # TODO #283 using this "is_kube_deploy" switch is an anti-pattern (induces hard to maintain code and make unit testing difficult)
                "/batch_jobs"
                if self.is_kube_deploy
                else "/data/projects/OpenEO/"
            )
        )

        self.s3_bucket_name = os.environ.get("SWIFT_BUCKET", "OpenEO-data")

        self.etl_api = os.environ.get("OPENEO_ETL_API", "https://etl.terrascope.be")
        self.etl_api_oidc_issuer = os.environ.get("OPENEO_ETL_API_OIDC_ISSUER", "https://sso.terrascope.be/auth/realms/terrascope")

        self.vault_addr = os.environ.get("VAULT_ADDR", "https://vault.vgt.vito.be")

        # TODO: We need a better system for determining "which backend deploy is this?"
        self.ejr_backend_id = (
            ("creodias" if self.is_kube_deploy else "mep") + "-" + self.openeo_env
        )
        self.ejr_api = env.get("OPENEO_EJR_API", "https://jobregistry.openeo.vito.be")

        self.ejr_credentials_vault_path = env.get(
            "OPENEO_EJR_CREDENTIALS_VAULT_PATH",
            "TAP/big_data_services/openeo/openeo-job-registry-elastic-api",
        )

    def __str__(self) -> str:
        return pformat(vars(self))

    @staticmethod
    def _as_boolean(envar_value: Optional[str]) -> bool:
        # TODO: use `openeo_driver.utils.smart_bool` instead?
        return envar_value is not None and envar_value.lower() == "true"

    @property
    def use_object_storage(self):
        """Whether or not to get the result files / assets from object storage.

        TODO: Give this its own configutation (env var) with sensible default. For now this is basically an alias for is_kube_deploy.
        In the near future we should decouple whether or not we use object storage from is_kube_deploy.
        Reason being that we intend to remove the is_kube_deploy attribute to make the code cleaner and simplify testing.
        See https://github.com/Open-EO/openeo-geopyspark-driver/issues/283
        """
        return self.is_kube_deploy
