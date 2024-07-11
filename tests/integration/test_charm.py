# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm."""

import json
import logging
import subprocess
from pathlib import Path

import aiohttp
import pytest
import requests
import tenacity
import utils
import yaml
from charmed_kubeflow_chisme.testing import (
    assert_alert_rules,
    assert_grafana_dashboards,
    assert_logging,
    assert_metrics_endpoint,
    deploy_and_assert_grafana_agent,
    get_alert_rules,
    get_grafana_dashboards,
)
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.core_v1 import ConfigMap, Namespace, Pod, Service
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = "seldon-controller-manager"
SELDON_DEPLOYMENT = create_namespaced_resource(
    group="machinelearning.seldon.io",
    version="v1",
    kind="seldondeployment",
    plural="seldondeployments",
    verbs=None,
)
SELDON_CM_NAME = "seldon-config"
WORKLOADS_NAMESPACE = "default"

with open("tests/integration/test_data/expected_seldon_cm.json", "r") as json_file:
    SELDON_CONFIG = json.load(json_file)

with open("tests/integration/test_data/expected_changed_seldon_cm.json", "r") as json_file:
    SELDON_CONFIG_CHANGED = json.load(json_file)


@pytest.fixture(scope="session")
def lightkube_client() -> Client:
    client = Client(field_manager=APP_NAME)
    return client


@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy(ops_test: OpsTest):
    """Build and deploy the charm.

    Assert on the unit status.
    """
    charm_under_test = await ops_test.build_charm(".")
    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    resources = {"oci-image": image_path}

    await ops_test.model.deploy(
        charm_under_test, resources=resources, application_name=APP_NAME, trust=True
    )

    # NOTE: idle_period is used to ensure all resources are deployed
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=60 * 10, idle_period=30
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"

    # Deploying grafana-agent-k8s and add all relations
    await deploy_and_assert_grafana_agent(
        ops_test.model, APP_NAME, metrics=True, dashboard=True, logging=True
    )


async def test_alert_rules(ops_test):
    """Test alert_rules are defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    alert_rules = get_alert_rules()
    logger.info("found alert_rules: %s", alert_rules)
    await assert_alert_rules(app, alert_rules)


async def test_metrics_endpoints(ops_test):
    """Test metrics_endpoints are defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    await assert_metrics_endpoint(app, metrics_port=8080, metrics_path="/metrics")


async def test_logging(ops_test):
    """Test logging is defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    await assert_logging(app)


async def test_grafana_dashboards(ops_test):
    """Test Grafana dashboards are defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    dashboards = get_grafana_dashboards()
    logger.info("found dashboards: %s", dashboards)
    # await assert_grafana_dashboards(app, dashboards)


@pytest.mark.abort_on_fail
async def test_configmap_created(lightkube_client: Client, ops_test: OpsTest):
    """Test configmaps contents with default config."""
    seldon_config_cm = lightkube_client.get(
        ConfigMap, SELDON_CM_NAME, namespace=ops_test.model_name
    )

    assert seldon_config_cm.data == SELDON_CONFIG


@pytest.mark.abort_on_fail
async def test_configmap_changes_with_config(lightkube_client: Client, ops_test: OpsTest):
    await ops_test.model.applications[APP_NAME].set_config(
        {
            "custom_images": '{"configmap__predictor__sklearn__v2": "custom:1.0", "configmap_explainer": "custom:2.0"}'  # noqa: E501
        }
    )
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=300
    )
    seldon_config_cm = lightkube_client.get(
        ConfigMap, SELDON_CM_NAME, namespace=ops_test.model_name
    )

    assert seldon_config_cm.data == SELDON_CONFIG_CHANGED

    # Change to default settings
    await ops_test.model.applications[APP_NAME].set_config({"custom_images": "{}"})
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=300
    )


@pytest.mark.abort_on_fail
async def test_seldon_istio_relation(ops_test: OpsTest):
    """Test Seldon/Istio relation."""
    # NOTE: This test is re-using deployment created in test_build_and_deploy()

    # setup Istio
    istio_gateway = "istio-ingressgateway"
    istio_pilot = "istio-pilot"
    await ops_test.model.deploy(
        entity_url="istio-gateway",
        application_name=istio_gateway,
        channel="latest/edge",
        config={"kind": "ingress"},
        trust=True,
    )
    await ops_test.model.deploy(
        istio_pilot,
        channel="latest/edge",
        config={"default-gateway": "test-gateway"},
        trust=True,
    )
    await ops_test.model.integrate(istio_pilot, istio_gateway)

    await ops_test.model.wait_for_idle(
        apps=[istio_pilot, istio_gateway],
        status="active",
        raise_on_blocked=False,
        timeout=60 * 20,
    )

    # add Seldon/Istio relation
    await ops_test.model.integrate(f"{istio_pilot}:gateway-info", f"{APP_NAME}:gateway-info")
    await ops_test.model.wait_for_idle(
        apps=[istio_pilot, istio_gateway, APP_NAME],
        status="active",
        raise_on_blocked=True,
        timeout=60 * 5,
    )


@pytest.mark.asyncio
async def test_seldon_deployment(ops_test: OpsTest):
    """Test Seldon Deployment scenario."""
    # NOTE: This test is re-using deployment created in test_build_and_deploy()
    # Use namespace "default" to create seldon deployments
    # due to https://github.com/canonical/seldon-core-operator/issues/218
    namespace = WORKLOADS_NAMESPACE
    client = Client()

    this_ns = client.get(res=Namespace, name=namespace)
    this_ns.metadata.labels.update({"serving.kubeflow.org/inferenceservice": "enabled"})
    client.patch(res=Namespace, name=this_ns.metadata.name, obj=this_ns)

    with open("tests/assets/crs/test-charm/serve-simple-v1.yaml") as f:
        sdep = SELDON_DEPLOYMENT(yaml.safe_load(f.read()))
        client.create(sdep, namespace=namespace)

    utils.assert_available(logger, client, SELDON_DEPLOYMENT, "seldon-model", namespace)

    service_name = "seldon-model-example-classifier"
    service = client.get(Service, name=service_name, namespace=namespace)
    service_ip = service.spec.clusterIP
    service_port = next(p for p in service.spec.ports if p.name == "http").port

    response = requests.post(
        f"http://{service_ip}:{service_port}/predict",
        json={
            "data": {
                "names": ["a", "b"],
                "tensor": {"shape": [2, 2], "values": [0, 0, 1, 1]},
            }
        },
    )
    response.raise_for_status()

    response = response.json()

    assert response["data"]["names"] == ["proba"]
    assert response["data"]["tensor"]["shape"] == [2, 1]
    assert response["meta"] == {}

    client.delete(SELDON_DEPLOYMENT, name="seldon-model", namespace=namespace, grace_period=0)
    utils.assert_deleted(logger, client, SELDON_DEPLOYMENT, "seldon-model-1", namespace)

    # wait for application to settle
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=120, idle_period=60
    )


@pytest.mark.abort_on_fail
async def test_remove_with_resources_present(ops_test: OpsTest):
    """Test remove with all resources deployed.

    Verify that all deployed resources that need to be removed are removed.
    """
    lightkube_client = Client()

    # remove deployed charm and verify that it is removed
    await ops_test.model.remove_application(APP_NAME, block_until_done=True)
    utils.assert_deleted(
        logger, lightkube_client, Pod, "seldon-controller-manager-0", ops_test.model_name
    )

    # verify that all resources that were deployed are removed
    # verify all CRDs in namespace are removed
    crd_list = lightkube_client.list(
        CustomResourceDefinition,
        labels=[("app.juju.is/created-by", "seldon-controller-manager")],
        namespace=ops_test.model_name,
    )
    assert not list(crd_list)

    # verify that all ConfigMaps are removed
    try:
        _ = lightkube_client.get(
            ConfigMap,
            name="seldon-config",
            namespace=ops_test.model_name,
        )
    except ApiError as error:
        if error.status.code != 404:
            # other error than Not Found
            assert False

    try:
        _ = lightkube_client.get(
            ConfigMap,
            name="a33bd623.machinelearning.seldon.io",
            namespace=ops_test.model_name,
        )
    except ApiError as error:
        if error.status.code != 404:
            # other error than Not Found
            assert False

    # verify that all related Services are removed
    svc_list = lightkube_client.list(
        Service,
        labels=[("app.juju.is/created-by", "seldon-controller-manager")],
        namespace=ops_test.model_name,
    )
    assert not list(svc_list)
