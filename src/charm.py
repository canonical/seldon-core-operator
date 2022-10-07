#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

""" A Juju Charm for Seldon Core Operator """
import logging

from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from lightkube.models.core_v1 import ServicePort
from lightkube import ApiError
from lightkube.generic_resource import load_in_cluster_generic_resources
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus, BlockedStatus
from ops.pebble import ChangeError, Layer

K8S_RESOURCE_FILES = [
    "src/templates/auth_manifests.yaml.j2",
    "src/templates/validate.yaml.j2",
    "src/templates/webhook-service.yaml.j2",
]
CRD_RESOURCE_FILES = [
    "src/templates/crd-v1.yaml.j2",
]
CONFIGMAP_RESOURCE_FILES = [
    "src/templates/configmap.yaml.j2",
]


#
# Helper for raising the exception
#
class CheckFailed(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=None):
        super().__init__()

        self.msg = str(msg)
        self.status_type = status_type
        self.status = status_type(self.msg)


#
# Seldon Core Operator
#
class SeldonCoreOperator(CharmBase):
    """A Juju Charm for Seldon Core Operator"""

    def __init__(self, *args):
        super().__init__(*args)

        # retrieve configuration and base settings
        self.logger = logging.getLogger(__name__)
        self._namespace = self.model.name
        self._lightkube_field_manager = "lightkube"
        self._name = self.model.app.name
        self._metrics_port = self.model.config["metrics-port"]
        self._webhook_port = self.model.config["webhook-port"]
        self._exec_command = (
            "/manager "
            "--enable-leader-election "
            f"--webhook-port {self._webhook_port} "
        )
        self._container_name = "seldon-core"
        self._container = self.unit.get_container(self._container_name)

        # setup context to be used for updating K8S resouces
        self._context = {
            "app_name": self._name,
            "namespace": self._namespace,
            "service": self._name,
            "webhook_port": self._webhook_port,
        }
        self._k8s_resource_handler = None
        self._crd_resource_handler = None
        self._configmap_resource_handler = None

        metrics_port = ServicePort(int(self._metrics_port), name="metrics-port")
        webhook_port = ServicePort(int(self._webhook_port), name="webhook-port")
        self.service_patcher = KubernetesServicePatch(
            self,
            [metrics_port, webhook_port],
            service_name=f"{self.model.app.name}",
        )

        # setup events
        self.framework.observe(self.on.install, self.main)
        self.framework.observe(self.on.upgrade_charm, self.main)
        self.framework.observe(self.on.config_changed, self.main)
        self.framework.observe(self.on.leader_elected, self.main)
        self.framework.observe(self.on.seldon_core_pebble_ready, self.main)

        for rel in self.model.relations.keys():
            self.framework.observe(self.on[rel].relation_changed, self.main)
        self.framework.observe(self.on.remove, self._on_remove)

        # Prometheus related config
        self.prometheus_provider = MetricsEndpointProvider(
            charm=self,
            relation_name="metrics-endpoint",
            jobs=[
                {
                    "metrics_path": self.config["executor-server-metrics-port-name"],
                    "static_configs": [
                        {"targets": ["*:{}".format(self.config["metrics-port"])]}
                    ],
                }
            ],
        )

        # Dashboard related config (Grafana)
        self.dashboard_provider = GrafanaDashboardProvider(
            charm=self,
            relation_name="grafana-dashboard",
        )

    @property
    def container(self):
        return self._container

    @property
    def k8s_resource_handler(self):
        if not self._k8s_resource_handler:
            self._k8s_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=K8S_RESOURCE_FILES,
                context=self._context,
                logger=self.logger,
            )
        load_in_cluster_generic_resources(self._k8s_resource_handler.lightkube_client)
        return self._k8s_resource_handler

    @k8s_resource_handler.setter
    def k8s_resource_handler(self, handler: KubernetesResourceHandler):
        self._k8s_resource_handler = handler

    @property
    def crd_resource_handler(self):
        if not self._crd_resource_handler:
            self._crd_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=CRD_RESOURCE_FILES,
                context=self._context,
                logger=self.logger,
            )
        load_in_cluster_generic_resources(self._crd_resource_handler.lightkube_client)
        return self._crd_resource_handler

    @crd_resource_handler.setter
    def crd_resource_handler(self, handler: KubernetesResourceHandler):
        self._crd_resource_handler = handler

    @property
    def configmap_resource_handler(self):
        if not self._configmap_resource_handler:
            self._configmap_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=CONFIGMAP_RESOURCE_FILES,
                context=self._context,
                logger=self.logger,
            )
        load_in_cluster_generic_resources(self._configmap_resource_handler.lightkube_client)
        return self._configmap_resource_handler

    @configmap_resource_handler.setter
    def configmap_resource_handler(self, handler: KubernetesResourceHandler):
        self._configmap_resource_handler = handler

    #
    # Return environment variables based on model configuration
    #
    def _get_env_vars(self):

        config = self.model.config
        ret_env_vars = {
            "AMBASSADOR_ENABLED": str(bool(self.model.relations["ambassador"])).lower(),
            "AMBASSADOR_SINGLE_NAMESPACE": str(
                config["ambassador-single-namespace"]
            ).lower(),
            "CONTROLLER_ID": config["controller-id"],
            "DEFAULT_USER_ID": config["default-user-id"],
            "EXECUTOR_CONTAINER_IMAGE_AND_VERSION": config[
                "executor-container-image-and-version"
            ],
            "EXECUTOR_CONTAINER_IMAGE_PULL_POLICY": config[
                "executor-container-image-pull-policy"
            ],
            "EXECUTOR_CONTAINER_SERVICE_ACCOUNT_NAME": config[
                "executor-container-service-account-name"
            ],
            "EXECUTOR_CONTAINER_USER": config["executor-container-user"],
            "EXECUTOR_DEFAULT_CPU_LIMIT": config["executor-default-cpu-limit"],
            "EXECUTOR_DEFAULT_CPU_REQUEST": config["executor-default-cpu-request"],
            "EXECUTOR_DEFAULT_MEMORY_LIMIT": config["executor-default-memory-limit"],
            "EXECUTOR_DEFAULT_MEMORY_REQUEST": config[
                "executor-default-memory-request"
            ],
            "EXECUTOR_PROMETHEUS_PATH": config["executor-prometheus-path"],
            "EXECUTOR_REQUEST_LOGGER_DEFAULT_ENDPOINT": config[
                "executor-request-logger-default-endpoint"
            ],
            "EXECUTOR_SERVER_METRICS_PORT_NAME": config[
                "executor-server-metrics-port-name"
            ],
            "EXECUTOR_SERVER_PORT": config["executor-server-port"],
            "ISTIO_ENABLED": str(bool(self.model.relations["istio"])).lower(),
            "ISTIO_GATEWAY": config["istio-gateway"],
            "ISTIO_TLS_MODE": config["istio-tls-mode"],
            "KEDA_ENABLED": str(bool(self.model.relations["keda"])).lower(),
            "MANAGER_CREATE_RESOURCES": "true",
            "POD_NAMESPACE": self.model.name,
            "PREDICTIVE_UNIT_DEFAULT_ENV_SECRET_REF_NAME": config[
                "predictive-unit-default-env-secret-ref-name"
            ],
            "PREDICTIVE_UNIT_METRICS_PORT_NAME": config[
                "predictive-unit-metrics-port-name"
            ],
            "PREDICTIVE_UNIT_SERVICE_PORT": config["predictive-unit-service-port"],
            "RELATED_IMAGE_EXECUTOR": config["related-image-executor"],
            "RELATED_IMAGE_EXPLAINER": config["related-image-explainer"],
            "RELATED_IMAGE_MLFLOWSERVER": config["related-image-mlflowserver"],
            "RELATED_IMAGE_MOCK_CLASSIFIER": config["related-image-mock-classifier"],
            "RELATED_IMAGE_SKLEARNSERVER": config["related-image-sklearnserver"],
            "RELATED_IMAGE_STORAGE_INITIALIZER": config[
                "related-image-storage-initializer"
            ],
            "RELATED_IMAGE_TENSORFLOW": config["related-image-tensorflow"],
            "RELATED_IMAGE_TFPROXY": config["related-image-tfproxy"],
            "RELATED_IMAGE_XGBOOSTSERVER": config["related-image-xgboostserver"],
            "USE_EXECUTOR": str(config["use-executor"]).lower(),
            "WATCH_NAMESPACE": config["watch-namespace"],
        }
        return ret_env_vars

    #
    # Pebble framework layer.
    #
    @property
    def _seldon_core_operator_layer(self) -> Layer:

        env_vars = self._get_env_vars()

        layer_config = {
            "summary": "seldon-core-operator layer",
            "description": "Pebble config layer for seldon-core-operator",
            "services": {
                self._container_name: {
                    "override": "replace",
                    "summary": "Entrypoint of seldon-core-operator image",
                    "command": self._exec_command,
                    "startup": "enabled",
                    "environment": env_vars,
                }
            },
        }

        return Layer(layer_config)

    #
    # Check if connection can be made with container
    #
    def _check_container_connection(self):
        if not self.container.can_connect():
            raise CheckFailed("Pod startup is not complete", MaintenanceStatus)

    #
    # Check for leader
    #
    def _check_leader(self):
        if not self.unit.is_leader():
            self.logger.info("Not a leader, skipping setup")
            raise CheckFailed("Waiting for leadership", WaitingStatus)

    #
    # Update Pebble configuration layer if changed.
    #
    def _update_layer(self) -> None:
        """Updates the Pebble configuration layer if changed."""
        current_layer = self.container.get_plan()
        new_layer = self._seldon_core_operator_layer
        if current_layer.services != new_layer.services:
            self.unit.status = MaintenanceStatus("Applying new pebble layer")
            self.container.add_layer(self._container_name, new_layer, combine=True)
            try:
                self.logger.info(
                    "Pebble plan updated with new configuration, replaning"
                )
                self.container.replan()
            except ChangeError:
                raise CheckFailed("Failed to replan", BlockedStatus)

    #
    # Deploy all K8S resouces
    #
    def _deploy_k8s_resources(self) -> None:
        try:
            self.unit.status = MaintenanceStatus("Creating K8S resources")
            self.k8s_resource_handler.apply()
            self.crd_resource_handler.apply()
            self.configmap_resource_handler.apply()
        except ApiError:
            raise CheckFailed("K8S resources creation failed", BlockedStatus)
        self.model.unit.status = MaintenanceStatus("K8S resources created")

    #
    # Main entry point for the Charm
    #
    def main(self, _) -> None:

        try:
            self._check_container_connection()
            self._check_leader()
            self._deploy_k8s_resources()
            self._update_layer()
        except CheckFailed as error:
            self.model.unit.status = error.status
            return

        self.model.unit.status = ActiveStatus()

    #
    # Remove all resouces
    #
    def _on_remove(self, _):
        self.unit.status = MaintenanceStatus("Removing K8S resources")
        k8s_resources_manifests = self.k8s_resource_handler.render_manifests()
        crd_resources_manifests = self.crd_resource_handler.render_manifests()
        configmap_resources_manifests = (
            self.configmap_resource_handler.render_manifests()
        )
        try:
            delete_many(
                self.crd_resource_handler.lightkube_client, crd_resources_manifests
            )
            delete_many(
                self.k8s_resource_handler.lightkube_client, k8s_resources_manifests
            )
            delete_many(
                self.configmap_resource_handler.lightkube_client,
                configmap_resources_manifests,
            )
        except ApiError as e:
            self.logger.warning(f"Failed to delete K8S resources, with error: {e}")
            raise e
        self.unit.status = MaintenanceStatus("K8S resources removed")


#
# Start main
#
if __name__ == "__main__":
    main(SeldonCoreOperator)
