# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm."""

import logging
from pathlib import Path

import aiohttp
import pytest
import requests
import tenacity
import yaml
from lightkube import ApiError, Client
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.core_v1 import ConfigMap, Namespace, Service
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

    # set default series for test model
    await ops_test.juju("model-config", "default-series=focal")

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
        raise_on_blocked=False,
        timeout=60 * 20,
    )

    # add Seldon/Istio relation
    await ops_test.model.add_relation(f"{istio_pilot}:gateway-info", f"{APP_NAME}:gateway-info")
    await ops_test.model.wait_for_idle(status="active", raise_on_blocked=True, timeout=60 * 5)


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(30),
    reraise=True,
)
def assert_available(client, resource_class, resource_name, namespace):
    """Test for available status. Retries multiple times to allow deployment to be created."""
    # NOTE: This test is re-using deployment created in test_build_and_deploy()

    dep = client.get(resource_class, resource_name, namespace=namespace)
    state = dep.get("status", {}).get("state")

    resource_class_kind = resource_class.__name__
    if state == "Available":
        logger.info(f"{resource_class_kind}/{resource_name} status == {state}")
    else:
        logger.info(
            f"{resource_class_kind}/{resource_name} status == {state} (waiting for 'Available')"
        )

    assert state == "Available", f"Waited too long for {resource_class_kind}/{resource_name}!"


async def fetch_url(url):
    """Fetch provided URL and return JSON."""
    result = None
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            result = await response.json()
    return result


@tenacity.retry(wait=tenacity.wait_fixed(30), stop=tenacity.stop_after_attempt(6), reraise=True)
async def check_alert_propagation(url, alert_name):
    """
    Check if given alert's state is propagated to Prometheus.

    Prometheus scraping is done once a minute. Retry for 3 minutes to ensure alert state is
    propagated. Assert if given alert is not in firing state.
    """
    alert_rules_result = await fetch_url(url)
    logger.info("Waiting for alert state to propagate to Prometheus")

    # verify that given alert is firing
    alert_rules = alert_rules_result["data"]["groups"][0]["rules"]
    alert_rule = next((rule for rule in alert_rules if rule["name"] == alert_name))
    assert alert_rule is not None and alert_rule["state"] == "firing"


async def test_seldon_alert_rules(ops_test: OpsTest):
    """Test Seldon alert rules."""
    # NOTE: This test is re-using deployments created in test_build_and_deploy()
    namespace = ops_test.model.name
    client = Client()

    # setup Prometheus
    prometheus = "prometheus-k8s"
    await ops_test.model.deploy(prometheus, channel="latest/stable", trust=True)
    await ops_test.model.relate(prometheus, APP_NAME)
    await ops_test.model.wait_for_idle(
        apps=[prometheus], status="active", raise_on_blocked=True, timeout=60 * 10
    )

    status = await ops_test.model.get_status()
    prometheus_units = status["applications"]["prometheus-k8s"]["units"]
    prometheus_url = prometheus_units["prometheus-k8s/0"]["address"]

    # Test 1: Verify that Prometheus receives the same set of rules as specified.

    # obtain scrape targets from Prometheus
    targets_result = await fetch_url(f"http://{prometheus_url}:9090/api/v1/targets")

    # verify that Seldon is in the target list
    assert targets_result is not None
    assert targets_result["status"] == "success"
    discovered_labels = targets_result["data"]["activeTargets"][0]["discoveredLabels"]
    assert discovered_labels["juju_application"] == "seldon-controller-manager"

    # obtain alert rules from Prometheus
    rules_url = f"http://{prometheus_url}:9090/api/v1/rules"
    alert_rules_result = await fetch_url(rules_url)

    # verify alerts are available in Prometheus
    assert alert_rules_result is not None
    assert alert_rules_result["status"] == "success"
    rules = alert_rules_result["data"]["groups"][0]["rules"]

    # load alert rules from the rules file
    rules_file_alert_names = []
    with open("src/prometheus_alert_rules/seldon_errors.rule") as f:
        seldon_errors = yaml.safe_load(f.read())
        alerts_list = seldon_errors["groups"][0]["rules"]
        for alert in alerts_list:
            rules_file_alert_names.append(alert["alert"])

    # verify number of alerts is the same in Prometheus and in the rules file
    assert len(rules) == len(rules_file_alert_names)

    # verify that all Seldon alert rules are in the list and that alerts obtained from Prometheus
    # match alerts in the rules file
    for rule in rules:
        assert rule["name"] in rules_file_alert_names

    # The following integration test is optional (experimental) and might not be functioning
    # correctly under some conditions due to its reliance on timing of K8S deployments, timing of
    # Prometheus scraping, and rate calculations for alerts.
    # In addition, Seldon Core Operator has one relatively easily triggered alert
    # (SeldonReconcileError) that can be simulated.

    # Test 2: Simulate propagattion of SeldonReconcileError alert by deleting deployment.

    test_alert_name = "SeldonReconcileError"

    # verify that alert SeldonReconcileError is inactive
    seldon_reconcile_error_alert = next(
        (rule for rule in rules if rule["name"] == test_alert_name)
    )
    assert seldon_reconcile_error_alert["state"] == "inactive"

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

    # check Prometheus for propagated alerts
    await check_alert_propagation(rules_url, test_alert_name)

    # obtain updated alert rules from Prometheus
    alert_rules_result = await fetch_url(rules_url)

    # verify that alert SeldonReconcileError is firing
    rules = alert_rules_result["data"]["groups"][0]["rules"]
    seldon_reconcile_error_alert = next(
        (rule for rule in rules if rule["name"] == test_alert_name)
    )
    assert seldon_reconcile_error_alert["state"] == "firing"

    # cleanup SeldonDeployment
    client.delete(seldon_deployment, name="seldon-model-1", namespace=namespace)


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


@pytest.mark.abort_on_fail
async def test_remove_with_resources_present(ops_test: OpsTest):
    """Test remove with all resources deployed.

    Verify that all deployed resources that need to be removed are removed.

    """
    # remove deployed charm and verify that it is removed
    await ops_test.model.remove_application(app_name=APP_NAME, block_until_done=True)
    assert APP_NAME not in ops_test.model.applications

    # verify that all resources that were deployed are removed
    lightkube_client = Client()

    # verify all CRDs in namespace are removed
    crd_list = lightkube_client.list(
        CustomResourceDefinition,
        labels=[("app.juju.is/created-by", "seldon-controller-manager")],
        namespace=ops_test.model.name,
    )
    assert not list(crd_list)

    # verify that ConfigMap is removed
    # TO-DO: test all ConfigMaps with label app.juju.is/created-by=seldon-controller-manager
    try:
        _ = lightkube_client.get(
            ConfigMap,
            name="seldon-config",
            namespace=ops_test.model.name,
        )
    except ApiError as error:
        if error.status.code != 404:
            # other error than Not Found
            assert False

    # verify that all related Services are removed
    svc_list = lightkube_client.list(
        Service,
        labels=[("app.juju.is/created-by", "seldon-controller-manager")],
        namespace=ops_test.model.name,
    )
    assert not list(svc_list)
