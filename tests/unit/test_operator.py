# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Unit tests for Seldon Core Operator/Charm."""

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Harness

from charm import SeldonCoreOperator


@pytest.fixture(scope="function")
def harness() -> Harness:
    """Create and return Harness for testing."""
    harness = Harness(SeldonCoreOperator)

    # setup container networking simulation
    harness.set_can_connect("seldon-core", True)

    return harness


class TestCharm:
    """Test class for SeldonCoreOperator."""

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.SeldonCoreOperator.k8s_resource_handler")
    @patch("charm.SeldonCoreOperator.configmap_resource_handler")
    @patch("charm.SeldonCoreOperator.crd_resource_handler")
    def test_not_leader(
        self,
        _: MagicMock,  # k8s_resource_handler
        __: MagicMock,  # configmap_resource_handler
        ___: MagicMock,  # crd_resource_handler
        harness: Harness,
    ):
        """Test not a leader scenario."""
        harness.begin_with_initial_hooks()
        harness.container_pebble_ready("seldon-core")
        assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.SeldonCoreOperator.k8s_resource_handler")
    @patch("charm.SeldonCoreOperator.configmap_resource_handler")
    @patch("charm.SeldonCoreOperator.crd_resource_handler")
    def test_no_relation(
        self,
        _: MagicMock,  # k8s_resource_handler
        __: MagicMock,  # configmap_resource_handler
        ___: MagicMock,  # crd_resource_handler
        harness: Harness,
    ):
        """Test no relation scenario."""
        harness.set_leader(True)
        harness.add_oci_resource(
            "oci-image",
            {
                "registrypath": "ci-test",
                "username": "",
                "password": "",
            },
        )

        harness.begin_with_initial_hooks()
        harness.container_pebble_ready("seldon-core")
        assert harness.charm.model.unit.status == ActiveStatus("")

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    def test_prometheus_data_set(self, harness: Harness, mocker):
        """Test Prometheus data setting."""
        harness.set_leader(True)
        harness.set_model_name("test_kubeflow")

        mock_net_get = mocker.patch("ops.testing._TestingModelBackend.network_get")
        mocker.patch("ops.testing._TestingPebbleClient.list_files")

        bind_address = "1.1.1.1"
        fake_network = {
            "bind-addresses": [
                {
                    "interface-name": "eth0",
                    "addresses": [{"hostname": "cassandra-tester-0", "value": bind_address}],
                }
            ]
        }
        mock_net_get.return_value = fake_network
        rel_id = harness.add_relation("metrics-endpoint", "otherapp")
        harness.add_relation_unit(rel_id, "otherapp/0")
        harness.update_relation_data(rel_id, "otherapp", {})
        harness.begin()

        # basic data
        assert json.loads(
            harness.get_relation_data(rel_id, harness.model.app.name)["scrape_jobs"]
        )[0]["static_configs"][0]["targets"] == ["*:8080"]

        # load alert rules from rules file
        test_alerts = []
        with open("src/prometheus_alert_rules/seldon_errors.rule") as f:
            seldon_errors = yaml.safe_load(f.read())
            alerts_list = seldon_errors["groups"][0]["rules"]
            for alert in alerts_list:
                test_alerts.append(alert["alert"])

        # alert rules
        alert_rules = json.loads(
            harness.get_relation_data(rel_id, harness.model.app.name)["alert_rules"]
        )

        assert alert_rules is not None
        assert alert_rules["groups"] is not None
        # there are two groups of alerts:
        # - Seldon errors has 5 alerts
        # - Unit unavailable has 1 alert
        assert alert_rules["groups"][0] is not None
        assert alert_rules["groups"][1] is not None
        rules = []
        if len(alert_rules["groups"][0]["rules"]) == len(test_alerts):
            rules = alert_rules["groups"][0]["rules"]
        elif len(alert_rules["groups"][1]["rules"]) == len(test_alerts):
            rules = alert_rules["groups"][1]["rules"]
        else:
            # Seldon error alerts not found
            assert 0

        # verify number of alerts is the same in relation and in the rules file
        assert len(rules) == len(test_alerts)

        # verify alerts in relation match alerts in the rules file
        for rule in rules:
            assert rule["alert"] in test_alerts

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.SeldonCoreOperator.k8s_resource_handler")
    @patch("charm.SeldonCoreOperator.configmap_resource_handler")
    @patch("charm.SeldonCoreOperator.crd_resource_handler")
    def test_pebble_layer(
        self,
        _: MagicMock,  # k8s_resource_handler
        __: MagicMock,  # configmap_resource_handler
        ___: MagicMock,  # crd_resource_handler
        harness: Harness,
    ):
        """Test creation of Pebble layer. Only testing specific items."""
        harness.set_leader(True)
        harness.set_model_name("test_kubeflow")
        harness.begin_with_initial_hooks()
        harness.container_pebble_ready("seldon-core")
        pebble_plan = harness.get_container_pebble_plan("seldon-core")
        assert pebble_plan
        assert pebble_plan._services
        pebble_plan_info = pebble_plan.to_dict()
        assert (
            pebble_plan_info["services"]["seldon-core"]["command"] == "/manager "
            "--enable-leader-election "
            f"--webhook-port {harness.charm._webhook_port} "
        )
        test_env = pebble_plan_info["services"]["seldon-core"]["environment"]
        # there should be 36 environment variables
        assert 36 == len(test_env)
        assert "test_kubeflow" == test_env["POD_NAMESPACE"]

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.SeldonCoreOperator.k8s_resource_handler")
    @patch("charm.SeldonCoreOperator.configmap_resource_handler")
    @patch("charm.SeldonCoreOperator.crd_resource_handler")
    def test_deploy_k8s_resources_success(
        self,
        k8s_resource_handler: MagicMock,
        configmap_resource_handler: MagicMock,
        crd_resource_handler: MagicMock,
        harness: Harness,
    ):
        """Test if K8S resource handler is executed as expected."""
        harness.begin()
        harness.charm._deploy_k8s_resources()
        crd_resource_handler.apply.assert_called()
        k8s_resource_handler.apply.assert_called()
        configmap_resource_handler.apply.assert_called()
        assert isinstance(harness.charm.model.unit.status, MaintenanceStatus)

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    def test_get_certs(self, harness: Harness):
        """Test certs generation."""
        harness.begin()

        # obtain certs and verify contents
        cert_info = harness.charm._gen_certs()
        assert cert_info is not None
        assert len(cert_info) == 3
        for cert in cert_info.items():
            assert len(str(cert[1])) != 0

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.SeldonCoreOperator.k8s_resource_handler")
    @patch("charm.SeldonCoreOperator.configmap_resource_handler")
    @patch("charm.SeldonCoreOperator.crd_resource_handler")
    def test_istio_relation(
        self,
        _: MagicMock,  # k8s_resource_handler
        __: MagicMock,  # configmap_resource_handler
        ___: MagicMock,  # crd_resource_handler
        harness: Harness,
        mocker,
    ):
        """Test Istio relation addition."""
        harness.set_leader(True)
        test_model_name = "test-kubeflow"
        test_gateway = "test-gateway"
        harness.set_model_name(test_model_name)

        rel_id = harness.add_relation("gateway-info", "istio-pilot")
        harness.update_relation_data(
            rel_id,
            "istio-pilot",
            {"gateway_namespace": test_model_name, "gateway_name": test_gateway},
        )
        harness.add_relation_unit(rel_id, "istio-pilot/0")
        harness.begin_with_initial_hooks()

        istio_gateway = harness.charm._get_istio_gateway()
        assert istio_gateway is not None
        assert istio_gateway == test_model_name + "/" + test_gateway
