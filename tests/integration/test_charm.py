import time
from pathlib import Path

import pytest
import requests
import yaml
from lightkube import Client
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Service
from pytest_operator.plugin import OpsTest

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

    await ops_test.run(
        "kubectl",
        "label",
        "namespace",
        "default",
        "serving.kubeflow.org/inferenceservice=enabled",
        "--overwrite",
        check=True,
    )

    await ops_test.model.deploy(
        charm_under_test, resources=resources, application_name=APP_NAME, trust=True
    )
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=60 * 10
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"

    SeldonDeployment = create_namespaced_resource(
        group="machinelearning.seldon.io",
        version="v1",
        kind="seldondeployment",
        plural="seldondeployments",
        verbs=None,
    )

    client = Client()

    with open("examples/serve-simple-v1.yaml") as f:
        sdep = SeldonDeployment(yaml.safe_load(f.read()))
        client.create(sdep)

    for i in range(30):
        dep = client.get(SeldonDeployment, "seldon-model")

        if dep.get("status", {}).get("state") == "Available":
            break

        time.sleep(5)
    else:
        pytest.fail("Waited too long for seldondeployment/seldon-model!")

    service_name = "seldon-model-example-classifier"
    service = client.get(Service, name=service_name)
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
