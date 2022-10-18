# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm."""

import logging
from pathlib import Path

import pytest
import requests
import tenacity
import yaml
from lightkube import Client
from lightkube.generic_resource import create_namespaced_resource
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

    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=60 * 10
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(30),
    reraise=True,
)
def assert_available(client, seldon_deployment, namespace):
    """Test for available status. Retries multiple times to allow deployment to be created."""
    dep = client.get(seldon_deployment, "seldon-model", namespace=namespace)
    state = dep.get("status", {}).get("state")

    if state == "Available":
        logger.info(f"SeldonDeployment status == {state}")
    else:
        logger.info(f"SeldonDeployment status == {state} (waiting for 'Available')")

    assert state == "Available", "Waited too long for seldondeployment/seldon-model!"


async def test_seldon_deployment(ops_test: OpsTest):
    """Test Seldon Deployment scenario."""
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

    assert_available(client, seldon_deployment, namespace)

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
