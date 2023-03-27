#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""A Juju Charm for Seldon Core Operator."""

import logging
import tempfile
from base64 import b64encode
from pathlib import Path
from subprocess import DEVNULL, check_call

from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.istio_pilot.v0.istio_gateway_info import GatewayRelationError, GatewayRequirer
from charms.observability_libs.v0.metrics_endpoint_discovery import (
    MetricsEndpointChangeCharmEvents,
    MetricsEndpointObserver,
)
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from lightkube import ApiError
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.models.core_v1 import ServicePort
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, Container, MaintenanceStatus, WaitingStatus
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
SSL_CONFIG_FILE = "src/templates/ssl.conf.j2"
CONTAINER_CERTS_DEST = "/tmp/k8s-webhook-server/serving-certs/"


class SeldonCoreOperator(CharmBase):
    """A Juju Charm for Seldon Core Operator."""

    _stored = StoredState()
    on = MetricsEndpointChangeCharmEvents()

    def __init__(self, *args):
        """Initialize charm and setup the container."""
        super().__init__(*args)

        # retrieve configuration and base settings
        self.logger = logging.getLogger(__name__)
        self._gateway = GatewayRequirer(self)
        self._namespace = self.model.name
        self._lightkube_field_manager = "lightkube"
        self._name = self.model.app.name
        self._metrics_port = self.model.config["metrics-port"]
        self._webhook_port = self.model.config["webhook-port"]
        self._exec_command = (
            "/manager " "--enable-leader-election " f"--webhook-port {self._webhook_port} "
        )
        self._container_name = "seldon-core"
        self._container = self.unit.get_container(self._container_name)

        # generate certs
        self._stored.set_default(**self._gen_certs(), targets={})

        # setup context to be used for updating K8S resources
        self._context = {
            "app_name": self._name,
            "namespace": self._namespace,
            "service": self._name,
            "webhook_port": self._webhook_port,
            "ca_bundle": b64encode(self._stored.ca.encode("ascii")).decode("utf-8"),
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

        # setup events to be handled by main event handler
        self.framework.observe(self.on.config_changed, self._on_event)
        for rel in self.model.relations.keys():
            self.framework.observe(self.on[rel].relation_changed, self._on_event)

        # setup events to be handled by specific event handlers
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.seldon_core_pebble_ready, self._on_pebble_ready)
        self.framework.observe(self.on.remove, self._on_remove)

        # Prometheus related config
        self.prometheus_provider = MetricsEndpointProvider(
            charm=self,
            relation_name="metrics-endpoint",
            jobs=[
                {
                    "metrics_path": self.config["executor-server-metrics-port-name"],
                    "static_configs": [{"targets": ["*:{}".format(self.config["metrics-port"])]}],
                }
            ],
            lookaside_jobs_callable=self.return_list_of_running_models,
        )

        # Dashboard related config (Grafana)
        self.dashboard_provider = GrafanaDashboardProvider(
            charm=self,
            relation_name="grafana-dashboard",
        )

        self.metrics_server_observer = MetricsEndpointObserver(
            self, {"seldon-deployment-id": None}
        )
        self.framework.observe(
            self.on.metrics_endpoint_change,
            self._metrics_endpoint_change,
        )

    @property
    def container(self):
        """Return container."""
        return self._container

    @property
    def k8s_resource_handler(self):
        """Update K8S with K8S resources."""
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
        """Update K8S with CRD resources."""
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
        """Update K8S with ConfigMap resources."""
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

    def _get_env_vars(self):
        """Return environment variables based on model configuration."""
        config = self.model.config

        istio_gateway = self._get_istio_gateway()
        if istio_gateway is None:
            istio_gateway = ""

        ret_env_vars = {
            "AMBASSADOR_ENABLED": str(bool(self.model.relations["ambassador"])).lower(),
            "AMBASSADOR_SINGLE_NAMESPACE": str(config["ambassador-single-namespace"]).lower(),
            "CONTROLLER_ID": config["controller-id"],
            "DEFAULT_USER_ID": config["default-user-id"],
            "EXECUTOR_CONTAINER_IMAGE_AND_VERSION": config["executor-container-image-and-version"],
            "EXECUTOR_CONTAINER_IMAGE_PULL_POLICY": config["executor-container-image-pull-policy"],
            "EXECUTOR_CONTAINER_SERVICE_ACCOUNT_NAME": config[
                "executor-container-service-account-name"
            ],
            "EXECUTOR_CONTAINER_USER": config["executor-container-user"],
            "EXECUTOR_DEFAULT_CPU_LIMIT": config["executor-default-cpu-limit"],
            "EXECUTOR_DEFAULT_CPU_REQUEST": config["executor-default-cpu-request"],
            "EXECUTOR_DEFAULT_MEMORY_LIMIT": config["executor-default-memory-limit"],
            "EXECUTOR_DEFAULT_MEMORY_REQUEST": config["executor-default-memory-request"],
            "EXECUTOR_PROMETHEUS_PATH": config["executor-prometheus-path"],
            "EXECUTOR_REQUEST_LOGGER_DEFAULT_ENDPOINT": config[
                "executor-request-logger-default-endpoint"
            ],
            "EXECUTOR_SERVER_METRICS_PORT_NAME": config["executor-server-metrics-port-name"],
            "EXECUTOR_SERVER_PORT": config["executor-server-port"],
            "ISTIO_ENABLED": str(bool(self.model.relations["gateway-info"])).lower(),
            "ISTIO_GATEWAY": istio_gateway,
            "ISTIO_TLS_MODE": config["istio-tls-mode"],
            "KEDA_ENABLED": str(bool(self.model.relations["keda"])).lower(),
            "MANAGER_CREATE_RESOURCES": "true",
            "POD_NAMESPACE": self.model.name,
            "PREDICTIVE_UNIT_DEFAULT_ENV_SECRET_REF_NAME": config[
                "predictive-unit-default-env-secret-ref-name"
            ],
            "PREDICTIVE_UNIT_METRICS_PORT_NAME": config["predictive-unit-metrics-port-name"],
            "PREDICTIVE_UNIT_SERVICE_PORT": config["predictive-unit-service-port"],
            "RELATED_IMAGE_EXECUTOR": config["related-image-executor"],
            "RELATED_IMAGE_EXPLAINER": config["related-image-explainer"],
            "RELATED_IMAGE_MLFLOWSERVER": config["related-image-mlflowserver"],
            "RELATED_IMAGE_MOCK_CLASSIFIER": config["related-image-mock-classifier"],
            "RELATED_IMAGE_SKLEARNSERVER": config["related-image-sklearnserver"],
            "RELATED_IMAGE_STORAGE_INITIALIZER": config["related-image-storage-initializer"],
            "RELATED_IMAGE_TENSORFLOW": config["related-image-tensorflow"],
            "RELATED_IMAGE_TFPROXY": config["related-image-tfproxy"],
            "RELATED_IMAGE_XGBOOSTSERVER": config["related-image-xgboostserver"],
            "USE_EXECUTOR": str(config["use-executor"]).lower(),
            "WATCH_NAMESPACE": config["watch-namespace"],
        }
        return ret_env_vars

    @property
    def _seldon_core_operator_layer(self) -> Layer:
        """Create and return Pebble framework layer."""
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

    def _check_leader(self):
        """Check if this unit is a leader."""
        if not self.unit.is_leader():
            self.logger.info("Not a leader, skipping setup")
            raise ErrorWithStatus("Waiting for leadership", WaitingStatus)

    def _update_layer(self) -> None:
        """Update the Pebble configuration layer (if changed)."""
        current_layer = self.container.get_plan()
        new_layer = self._seldon_core_operator_layer
        if current_layer.services != new_layer.services:
            self.unit.status = MaintenanceStatus("Applying new pebble layer")
            self.container.add_layer(self._container_name, new_layer, combine=True)
            try:
                self.logger.info("Pebble plan updated with new configuration, replaning")
                self.container.replan()
            except ChangeError as e:
                raise GenericCharmRuntimeError("Failed to replan") from e

    def _check_container_connection(self, container: Container) -> None:
        """Check if connection can be made with container.

        Args:
            container: the named container in a unit to check.

        Raises:
            ErrorWithStatus if the connection cannot be made.
        """
        if not container.can_connect():
            raise ErrorWithStatus("Pod startup is not complete", MaintenanceStatus)

    def _upload_certs_to_container(self):
        """Upload generated certs to container."""
        try:
            self._check_container_connection(self.container)
        except ErrorWithStatus as error:
            self.model.unit = error.status
            return

        self.container.push(CONTAINER_CERTS_DEST + "tls.key", self._stored.key, make_dirs=True)
        self.container.push(CONTAINER_CERTS_DEST + "tls.crt", self._stored.cert, make_dirs=True)
        self.container.push(CONTAINER_CERTS_DEST + "ca.crt", self._stored.ca, make_dirs=True)

    def _check_and_report_k8s_conflict(self, error):
        """Return True if error status code is 409 (conflict), False otherwise."""
        if error.status.code == 409:
            self.logger.warning(f"Encountered a conflict: {error}")
            return True
        return False

    def _apply_k8s_resources(self, force_conflicts: bool = False) -> None:
        """Apply K8S resources.

        Args:
            force_conflicts (bool): *(optional)* Will "force" apply requests causing conflicting
                                    fields to change ownership to the field manager used in this
                                    charm.
                                    NOTE: This will only be used if initial regular apply() fails.
        """
        self.unit.status = MaintenanceStatus("Creating K8S resources")
        try:
            self.k8s_resource_handler.apply()
        except ApiError as error:
            if self._check_and_report_k8s_conflict(error) and force_conflicts:
                # conflict detected when applying K8S resources
                # re-apply K8S resources with forced conflict resolution
                self.unit.status = MaintenanceStatus("Force applying K8S resources")
                self.logger.warning("Apply K8S resources with forced changes against conflicts")
                self.k8s_resource_handler.apply(force=force_conflicts)
            else:
                raise GenericCharmRuntimeError("K8S resources creation failed") from error
        try:
            self.crd_resource_handler.apply()
        except ApiError as error:
            if self._check_and_report_k8s_conflict(error) and force_conflicts:
                # conflict detected when applying CRD resources
                # re-apply CRD resources with forced conflict resolution
                self.unit.status = MaintenanceStatus("Force applying CRD resources")
                self.logger.warning("Apply CRD resources with forced changes against conflicts")
                self.crd_resource_handler.apply(force=force_conflicts)
            else:
                raise GenericCharmRuntimeError("CRD resources creation failed") from error
        try:
            self.configmap_resource_handler.apply()
        except ApiError as error:
            if self._check_and_report_k8s_conflict(error) and force_conflicts:
                # conflict detected when applying ConfigMap resources
                # re-apply ConfigMap resources with forced conflict resolution
                self.unit.status = MaintenanceStatus("Force applying ConfigMap resources")
                self.logger.warning("Apply ConfigMap with forced changes against conflicts")
                self.configmap_resource_handler.apply(force=force_conflicts)
            else:
                raise GenericCharmRuntimeError("ConfigMap resources creation failed") from error
        self.model.unit.status = MaintenanceStatus("K8S resources created")

    def _on_install(self, _):
        """Installation only tasks."""
        # deploy K8S resources to speed up deployment
        self._apply_k8s_resources()

    def _on_pebble_ready(self, event):
        """Configure started container."""
        # TODO: extract try/except to _check_container_connection()
        try:
            self._check_container_connection(self.container)
        except ErrorWithStatus as error:
            self.model.unit = error.status
            return

        # container is ready
        # upload certs to container
        self._upload_certs_to_container()

        # proceed with other actions
        self._on_event(event)

    def _on_upgrade(self, _):
        """Perform upgrade steps."""
        # upload certs to container
        if self.container.can_connect():
            self._upload_certs_to_container()

        # force conflict resolution in K8S resources update
        self._on_event(_, force_conflicts=True)

    def _on_remove(self, _):
        """Remove all resources."""
        self.unit.status = MaintenanceStatus("Removing K8S resources")
        k8s_resources_manifests = self.k8s_resource_handler.render_manifests()
        crd_resources_manifests = self.crd_resource_handler.render_manifests()
        configmap_resources_manifests = self.configmap_resource_handler.render_manifests()
        try:
            delete_many(self.crd_resource_handler.lightkube_client, crd_resources_manifests)
            delete_many(self.k8s_resource_handler.lightkube_client, k8s_resources_manifests)
            delete_many(
                self.configmap_resource_handler.lightkube_client,
                configmap_resources_manifests,
            )
        except ApiError as error:
            # do not log/report when resources were not found
            if error.status.code != 404:
                self.logger.error(f"Failed to delete K8S resources, with error: {error}")
                raise error
        self.unit.status = MaintenanceStatus("K8S resources removed")

    def _gen_certs(self):
        """Generate certificates."""
        # generate SSL configuration based on template
        model = self.model.name

        try:
            ssl_conf_template = open(SSL_CONFIG_FILE)
            ssl_conf = ssl_conf_template.read()
        except IOError as err:
            self.logger.warning(f"Failed to open SSL config file, error: {err}")
            return

        ssl_conf = ssl_conf.replace("{{ model }}", str(model))
        with tempfile.TemporaryDirectory() as tmp_dir:
            Path(tmp_dir + "/seldon-cert-gen-ssl.conf").write_text(ssl_conf)

            # execute OpenSSL commands
            check_call(["openssl", "genrsa", "-out", tmp_dir + "/seldon-cert-gen-ca.key", "2048"])
            check_call(
                ["openssl", "genrsa", "-out", tmp_dir + "/seldon-cert-gen-server.key", "2048"],
                stdout=DEVNULL,
            )
            check_call(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-new",
                    "-sha256",
                    "-nodes",
                    "-days",
                    "3650",
                    "-key",
                    tmp_dir + "/seldon-cert-gen-ca.key",
                    "-subj",
                    "/CN=127.0.0.1",
                    "-out",
                    tmp_dir + "/seldon-cert-gen-ca.crt",
                ],
                stdout=DEVNULL,
            )
            check_call(
                [
                    "openssl",
                    "req",
                    "-new",
                    "-sha256",
                    "-key",
                    tmp_dir + "/seldon-cert-gen-server.key",
                    "-out",
                    tmp_dir + "/seldon-cert-gen-server.csr",
                    "-config",
                    tmp_dir + "/seldon-cert-gen-ssl.conf",
                ],
                stdout=DEVNULL,
            )
            check_call(
                [
                    "openssl",
                    "x509",
                    "-req",
                    "-sha256",
                    "-in",
                    tmp_dir + "/seldon-cert-gen-server.csr",
                    "-CA",
                    tmp_dir + "/seldon-cert-gen-ca.crt",
                    "-CAkey",
                    tmp_dir + "/seldon-cert-gen-ca.key",
                    "-CAcreateserial",
                    "-out",
                    tmp_dir + "/seldon-cert-gen-cert.pem",
                    "-days",
                    "365",
                    "-extensions",
                    "v3_ext",
                    "-extfile",
                    tmp_dir + "/seldon-cert-gen-ssl.conf",
                ],
                stdout=DEVNULL,
            )

            ret_certs = {
                "cert": Path(tmp_dir + "/seldon-cert-gen-cert.pem").read_text(),
                "key": Path(tmp_dir + "/seldon-cert-gen-server.key").read_text(),
                "ca": Path(tmp_dir + "/seldon-cert-gen-ca.crt").read_text(),
            }

            # cleanup temporary files
            check_call(["rm", "-f", tmp_dir + "/seldon-cert-gen-*"])

        return ret_certs

    def _metrics_endpoint_change(self, event):
        k = f"{event.discovered['namespace']}-{event.discovered['name']}"
        if event.discovered["change"] == "DELETED" and self._stored.targets.get(k, ""):
            del self._stored.targets[k]
        else:
            self._stored.targets[k] = event.discovered["targets"]
        self.prometheus_provider.set_scrape_job_spec()

    def _get_istio_gateway(self):
        """Parse 'gateway' relation and return Istio gateway definition in appropriate format."""
        try:
            gateway_info = self._gateway.get_relation_data()
        except GatewayRelationError:
            self.model.unit.status = WaitingStatus("Waiting for gateway info relation")
            return None
        istio_gateway = None
        if gateway_info["gateway_namespace"] and gateway_info["gateway_name"]:
            istio_gateway = gateway_info["gateway_namespace"] + "/" + gateway_info["gateway_name"]
        return istio_gateway

    def _on_event(self, event, force_conflicts: bool = False) -> None:
        """Perform all required actions for the Charm.

        Args:
            force_conflicts (bool): Should only be used when need to resolved conflicts on K8S
                                    resources.
        """
        try:
            self._check_leader()
            self._apply_k8s_resources(force_conflicts=force_conflicts)
            self._update_layer()
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            self.logger.info(f"Failed to handle {event} with error: {str(err)}")
            return

        self.model.unit.status = ActiveStatus()

    def return_list_of_running_models(self):
        """Return the models and targets for endpoint discovery."""
        return [{"running-models": [{"targets": [p for p in self._stored.targets.values()]}]}]


#
# Start main
#
if __name__ == "__main__":
    main(SeldonCoreOperator)
