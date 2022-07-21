#!/usr/bin/env python3

import logging

from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus

from oci_image import OCIImageResource, OCIImageResourceError

log = logging.getLogger()


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        if not self.model.unit.is_leader():
            log.info("Not a leader, skipping set_pod_spec")
            self.model.unit.status = ActiveStatus()
            return

        self.image = OCIImageResource(self, "oci-image")

        self.framework.observe(self.on.install, self.set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        self.framework.observe(self.on.config_changed, self.set_pod_spec)

        for rel in self.model.relations.keys():
            self.framework.observe(
                self.on[rel].relation_changed,
                self.set_pod_spec,
            )

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

        self.dashboard_provider = GrafanaDashboardProvider(
            charm=self,
            relation_name="grafana-dashboard",
        )

    def set_pod_spec(self, event):
        if not self.model.unit.is_leader():
            log.info("Not a leader, skipping set_pod_spec")
            self.model.unit.status = ActiveStatus()
            return

        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            self.model.unit.status = e.status
            log.info(e)
            return

        config = self.model.config
        tconfig = {k.replace("-", "_"): v for k, v in config.items()}
        tconfig["service"] = self.model.app.name
        tconfig["namespace"] = self.model.name
        env = Environment(
            loader=FileSystemLoader("src/templates/"),
        )
        envs = {
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

        self.model.unit.status = MaintenanceStatus("Setting pod spec")
        self.model.pod.set_spec(
            {
                "version": 3,
                "serviceAccount": {
                    "roles": [
                        {
                            "global": True,
                            "rules": [
                                {
                                    "apiGroups": ["coordination.k8s.io"],
                                    "resources": ["leases"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["events"],
                                    "verbs": ["create", "patch"],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["namespaces"],
                                    "verbs": ["get", "list", "watch"],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["services"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["apps"],
                                    "resources": ["deployments"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["apps"],
                                    "resources": ["deployments/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["autoscaling"],
                                    "resources": ["horizontalpodautoscalers"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["autoscaling"],
                                    "resources": ["horizontalpodautoscalers/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["keda.sh"],
                                    "resources": ["scaledobjects"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["keda.sh"],
                                    "resources": ["scaledobjects/finalizers"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["keda.sh"],
                                    "resources": ["scaledobjects/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["machinelearning.seldon.io"],
                                    "resources": ["seldondeployments"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["machinelearning.seldon.io"],
                                    "resources": ["seldondeployments/finalizers"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["machinelearning.seldon.io"],
                                    "resources": ["seldondeployments/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["networking.istio.io"],
                                    "resources": ["destinationrules"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["networking.istio.io"],
                                    "resources": ["destinationrules/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["networking.istio.io"],
                                    "resources": ["virtualservices"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["networking.istio.io"],
                                    "resources": ["virtualservices/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["policy"],
                                    "resources": ["poddisruptionbudgets"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["policy"],
                                    "resources": ["poddisruptionbudgets/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["v1"],
                                    "resources": ["namespaces"],
                                    "verbs": ["get", "list", "watch"],
                                },
                                {
                                    "apiGroups": ["v1"],
                                    "resources": ["services"],
                                    "verbs": [
                                        "create",
                                        "delete",
                                        "get",
                                        "list",
                                        "patch",
                                        "update",
                                        "watch",
                                    ],
                                },
                                {
                                    "apiGroups": ["v1"],
                                    "resources": ["services/status"],
                                    "verbs": ["get", "patch", "update"],
                                },
                                {
                                    "apiGroups": ["apiextensions.k8s.io"],
                                    "resources": ["customresourcedefinitions"],
                                    "verbs": ["create", "get", "list"],
                                },
                                {
                                    "apiGroups": ["admissionregistration.k8s.io"],
                                    "resources": ["validatingwebhookconfigurations"],
                                    "verbs": [
                                        "get",
                                        "list",
                                        "create",
                                        "delete",
                                        "update",
                                    ],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["configmaps"],
                                    "verbs": [
                                        "get",
                                        "list",
                                        "watch",
                                        "create",
                                        "update",
                                        "patch",
                                        "delete",
                                    ],
                                },
                            ],
                        }
                    ]
                },
                "containers": [
                    {
                        "name": "seldon-core",
                        "command": ["/manager"],
                        "args": [
                            "--enable-leader-election",
                            "--webhook-port",
                            config["webhook-port"],
                            "--create-resources",
                            "true",
                        ],
                        "imageDetails": image_details,
                        "ports": [
                            {
                                "name": "metrics",
                                "containerPort": int(config["metrics-port"]),
                            },
                            {
                                "name": "webhook",
                                "containerPort": int(config["webhook-port"]),
                            },
                        ],
                        "envConfig": envs,
                        "volumeConfig": [
                            {
                                "name": "operator-resources",
                                "mountPath": "/tmp/operator-resources",
                                "files": [
                                    {
                                        "path": f"{name}.yaml",
                                        "content": env.get_template(
                                            f"{name}.yaml"
                                        ).render(tconfig),
                                    }
                                    for name in (
                                        "configmap",
                                        "crd-v1",
                                        "service",
                                        "validate",
                                    )
                                ],
                            }
                        ],
                    }
                ],
            },
        )
        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(Operator)
