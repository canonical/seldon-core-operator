# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm."""

import logging
from pathlib import Path
from time import sleep

import aiohttp
import pytest
import requests
import tenacity
import yaml
from lightkube import Client
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.core_v1 import Namespace, Service
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = "seldon-controller-manager"


@pytest.mark.abort_on_fail
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
    await ops_test.model.add_relation(istio_pilot, istio_gateway)

    await ops_test.model.wait_for_idle(
        apps=[istio_pilot, istio_gateway],
        status="active",
        raise_on_blocked=True,
        timeout=60 * 10 * 2,
    )

    # add Seldon/Istio relation
    await ops_test.model.add_relation(f"{istio_pilot}:gateway-info", f"{APP_NAME}:gateway-info")
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=60 * 10
    )


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(30),
    reraise=True,
)
def assert_available(client, seldon_deployment, deploy_name, namespace):
    """Test for available status. Retries multiple times to allow deployment to be created."""
    # NOTE: This test is re-using deployment created in test_build_and_deploy()

    dep = client.get(seldon_deployment, deploy_name, namespace=namespace)
    state = dep.get("status", {}).get("state")

    if state == "Available":
        logger.info(f"SeldonDeployment/{deploy_name} status == {state}")
    else:
        logger.info(f"SeldonDeployment/{deploy_name} status == {state} (waiting for 'Available')")

    assert state == "Available", f"Waited too long for seldondeployment/{deploy_name}!"


async def test_seldon_alert_rules(ops_test: OpsTest):
    """Test Seldon alert rules."""
    # NOTE: This test is re-using deployments created in test_build_and_deploy()
    namespace = ops_test.model.name
    client = Client()

    # setup Prometheus
    prometheus = "prometheus-k8s"
    await ops_test.model.deploy(prometheus, channel="latest/edge", trust=True)
    await ops_test.model.relate(prometheus, APP_NAME)
    await ops_test.model.wait_for_idle(
        apps=[prometheus], status="active", raise_on_blocked=True, timeout=60 * 10
    )

    status = await ops_test.model.get_status()
    prometheus_units = status["applications"]["prometheus-k8s"]["units"]
    prometheus_url = prometheus_units["prometheus-k8s/0"]["address"]

    # obtain scrape targets from Prometheus
    targets_result = None
    targets_url = f"http://{prometheus_url}:9090/api/v1/targets"
    async with aiohttp.ClientSession() as session:
        async with session.get(targets_url) as response:
            targets_result = await response.json()

    # verify that Seldon is in the target list
    assert targets_result is not None
    assert targets_result["status"] == "success"
    discovered_labels = targets_result["data"]["activeTargets"][0]["discoveredLabels"]
    assert discovered_labels["juju_application"] == "seldon-controller-manager"

    # obtain alert rules from Prometheus
    alert_rules_result = None
    rules_url = f"http://{prometheus_url}:9090/api/v1/rules"
    async with aiohttp.ClientSession() as session:
        async with session.get(rules_url) as response:
            alert_rules_result = await response.json()

    # verify that all Seldon alert rules are in the list
    assert alert_rules_result is not None
    assert alert_rules_result["status"] == "success"
    rules = alert_rules_result["data"]["groups"][0]["rules"]
    assert rules[0]["name"] == "SeldonWorkqueueTooManyRetries"
    assert rules[1]["name"] == "SeldonHTTPError"
    assert rules[2]["name"] == "SeldonReconcileError"
    assert rules[3]["name"] == "SeldonUnfinishedWorkIncrease"
    assert rules[4]["name"] == "SeldonWebhookError"

    # verify that alert SeldonReconcileError is inactive
    assert rules[2]["name"] == "SeldonReconcileError" and rules[2]["state"] == "inactive"

    # simulate scenario where alert will fire
    # create SeldonDeployment
    seldon_deployment = create_namespaced_resource(
        group="machinelearning.seldon.io",
        version="v1",
        kind="seldondeployment",
        plural="seldondeployments",
        verbs=None,
    )
    with open("examples/serve-simple-v1.yaml") as f:
        sdep = seldon_deployment(yaml.safe_load(f.read()))
        sdep["metadata"]["name"] = "seldon-model-1"
        client.create(sdep, namespace=namespace)
    assert_available(client, seldon_deployment, "seldon-model-1", namespace)

    # remove deployment that was created by Seldon, reconcile alert will fire
    client.delete(Deployment, name="seldon-model-1-example-0-classifier", namespace=namespace)

    # Prometheus scraping is done once a minute
    # wait for 90 seconds to ensure alert state is propagated
    logger.info("Waiting for alert state to propagate to Prometheus.")
    sleep(90)

    # obtain updated alert rules from Prometheus
    async with aiohttp.ClientSession() as session:
        async with session.get(rules_url) as response:
            alert_rules_result = await response.json()

    # verify that alert SeldonReconcileError is firing
    rules = alert_rules_result["data"]["groups"][0]["rules"]
    assert rules[2]["name"] == "SeldonReconcileError" and rules[2]["state"] == "firing"


async def test_seldon_deployment(ops_test: OpsTest):
    """Test Seldon Deployment scenario."""
    # NOTE: This test is re-using deployment created in test_build_and_deploy()
    namespace = ops_test.model_name
    client = Client()

    this_ns = client.get(res=Namespace, name=namespace)
    this_ns.metadata.labels.update({"serving.kubeflow.org/inferenceservice": "enabled"})
    client.patch(res=Namespace, name=this_ns.metadata.name, obj=this_ns)

    seldon_deployment = create_namespaced_resource(
        group="machinelearning.seldon.io",
        version="v1",
        kind="seldondeployment",
        plural="seldondeployments",
        verbs=None,
    )

    with open("examples/serve-simple-v1.yaml") as f:
        sdep = seldon_deployment(yaml.safe_load(f.read()))
        client.create(sdep, namespace=namespace)

    assert_available(client, seldon_deployment, "seldon-model", namespace)

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
