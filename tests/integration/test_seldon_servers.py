# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Servers."""

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
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import create_namespaced_resource
from lightkube.models.meta_v1 import ObjectMeta
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
TEST_LABEL = {"testing-seldon-deployments": "true"}
WORKLOADS_NAMESPACE = "default"


@pytest.fixture(scope="session")
def lightkube_client() -> Client:
    """Return an instantiated lightkube client to use during the session."""
    lightkube_client = Client(field_manager="seldon-tests", trust_env=False)
    return lightkube_client


@pytest.fixture(scope="module")
def patch_namespace_with_seldon_label(lightkube_client: Client, ops_test: OpsTest):
    """Patch the current namespace with Seldon specific labels."""
    this_ns = lightkube_client.get(res=Namespace, name=WORKLOADS_NAMESPACE)
    this_ns.metadata.labels.update({"serving.kubeflow.org/inferenceservice": "enabled"})
    lightkube_client.patch(res=Namespace, name=this_ns.metadata.name, obj=this_ns)


@pytest.fixture()
def remove_seldon_deployment(lightkube_client: Client, ops_test: OpsTest):
    """Remove SeldonDeployment even if the test case fails."""
    yield

    # remove Seldon Deployment
    # Use namespace "default" to create seldon deployments
    # due to https://github.com/canonical/seldon-core-operator/issues/218
    namespace = WORKLOADS_NAMESPACE
    resource_to_delete = lightkube_client.list(
        SELDON_DEPLOYMENT, namespace=namespace, labels=TEST_LABEL
    )
    for obj in resource_to_delete:
        lightkube_client.delete(
            SELDON_DEPLOYMENT, name=obj.metadata.name, namespace=namespace, grace_period=0
        )
        utils.assert_deleted(
            logger, lightkube_client, SELDON_DEPLOYMENT, obj.metadata.name, namespace=namespace
        )


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


@pytest.mark.parametrize(
    # server_name - name of predictor server (should match configmap)
    # server_config - server configuration file
    # url - model prediction URL
    # request_data - data to put into request: JSON object or file with JSON data
    # response_test_data - data expected in response: JSON object or file with JSON data
    # IMAGE:VERSION in below response data is replaced with values found in seldon-config ConfigMap
    "server_name, server_config, url, request_data, response_test_data",
    [
        (
            "SKLEARN_SERVER",
            "sklearn.yaml",
            "api/v1.0/predictions",
            {"data": {"ndarray": [[1, 2, 3, 4]]}},
            {
                "data": {
                    "names": ["t:0", "t:1", "t:2"],
                    "ndarray": [[0.0006985194531162835, 0.00366803903943666, 0.995633441507447]],
                },
                # classifier will be replaced according to configmap
                "meta": {"requestPath": {"classifier": "IMAGE:VERSION"}},
            },
        ),
        (
            "SKLEARN_SERVER",
            "sklearn-v2.yaml",
            "v2/models/classifier/infer",
            {
                "inputs": [
                    {
                        "name": "predict",
                        "shape": [1, 4],
                        "datatype": "FP32",
                        "data": [[1, 2, 3, 4]],
                    },
                ]
            },
            {
                "model_name": "classifier",
                "model_version": "v1",
                "id": "None",  # id needs to be reset in response
                "parameters": {},
                "outputs": [
                    {
                        "name": "predict",
                        "shape": [1, 1],
                        "datatype": "INT64",
                        "parameters": {"content_type": "np"},
                        "data": [2],
                    }
                ],
            },
        ),
        (
            "XGBOOST_SERVER",
            "xgboost.yaml",
            "api/v1.0/predictions",
            {"data": {"ndarray": [[1.0, 2.0, 5.0, 6.0]]}},
            {
                "data": {
                    "names": [],
                    "ndarray": [2.0],
                },
                # classifier will be replaced according to configmap
                "meta": {"requestPath": {"classifier": "IMAGE:VERSION"}},
            },
        ),
        (
            "XGBOOST_SERVER",
            "xgboost-v2.yaml",
            "v2/models/iris/infer",
            {
                "inputs": [
                    {
                        "name": "predict",
                        "shape": [1, 4],
                        "datatype": "FP32",
                        "data": [[1, 2, 3, 4]],
                    },
                ]
            },
            {
                "model_name": "iris",
                "model_version": "v0.1.0",
                "id": "None",  # id needs to be reset in response
                "parameters": {},
                "outputs": [
                    {
                        "name": "predict",
                        "shape": [1, 1],
                        "datatype": "FP32",
                        "parameters": {"content_type": "np"},
                        "data": [2.0],
                    }
                ],
            },
        ),
        (
            "MLFLOW_SERVER",
            "mlflowserver.yaml",
            "api/v1.0/predictions",
            {"data": {"ndarray": [[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1]]}},
            {
                "data": {
                    "names": [],
                    "ndarray": [5.275558760255382],
                },
                # classifier will be replaced according to configmap
                "meta": {"requestPath": {"classifier": "IMAGE:VERSION"}},
            },
        ),
        (
            "MLFLOW_SERVER",
            "mlflowserver-v2.yaml",
            "v2/models/classifier/infer",
            "mlflowserver-request-data.json",
            "mlflowserver-response-data.json",
        ),
        (
            "TENSORFLOW_SERVER",
            "tensorflow-serving.yaml",
            "api/v1.0/predictions",
            "tensorflow-serving-request-data.json",
            "tensorflow-serving-response-data.json",
        ),
        (
            "TENSORFLOW_SERVER",
            "tensorflow.yaml",
            "v1/models/classifier:predict",
            {"instances": [1.0, 2.0, 5.0]},
            {"predictions": [2.5, 3, 4.5]},
        ),
        (
            "HUGGINGFACE_SERVER",
            "huggingface.yaml",
            "v2/models/classifier/infer",
            {
                "inputs": [
                    {
                        "name": "args",
                        "shape": [1],
                        "datatype": "BYTES",
                        "data": ["this is a test"],
                    }
                ],
            },
            {
                "model_name": "classifier",
                "model_version": "v1",
                "id": "None",
                "parameters": {},
                "outputs": [
                    {
                        "name": "output",
                        "shape": [1, 1],
                        "datatype": "BYTES",
                        "parameters": {"content_type": "hg_jsonlist"},
                        # 'data' needs to be reset because GPT returns different results every time
                        "data": "None",
                    }
                ],
            },
        ),
    ],
)
@pytest.mark.abort_on_fail
@pytest.mark.asyncio
async def test_seldon_predictor_server(
    server_name,
    server_config,
    url,
    request_data,
    response_test_data,
    lightkube_client,
    remove_seldon_deployment,
    ops_test: OpsTest,
    patch_namespace_with_seldon_label,
):
    """Test Seldon predictor server.

    Workload deploys Seldon predictor servers defined in ConfigMap.
    Each server is deployed and inference request is triggered, and response is evaluated.
    """
    # Use namespace "default" to create seldon deployments
    # due to https://github.com/canonical/seldon-core-operator/issues/218
    namespace = WORKLOADS_NAMESPACE
    # retrieve predictor server information and create Seldon Depoloyment
    with open(f"tests/assets/crs/{server_config}") as f:
        deploy_yaml = yaml.safe_load(f.read())
        ml_model = deploy_yaml["metadata"]["name"]
        predictor = deploy_yaml["spec"]["predictors"][0]["name"]
        protocol = "seldon"  # default protocol
        if "protocol" in deploy_yaml["spec"]:
            protocol = deploy_yaml["spec"]["protocol"]
        # Add a label to the SeldonDeployment so it is easy to interact with it
        # by simply listing the resources that match the test label.
        sdep = SELDON_DEPLOYMENT(
            deploy_yaml,
            metadata=ObjectMeta(
                name=deploy_yaml["metadata"]["name"], namespace=namespace, labels=TEST_LABEL
            ),
        )
        lightkube_client.create(sdep, namespace=namespace)

    # prepare request data:
    # - if it is string, load it from file specified by that string
    # - otherwise use it as JSON object
    if isinstance(request_data, str):
        # response test data contains file with JSON data
        with open(f"tests/assets/data/{request_data}") as f:
            request_data = json.load(f)

    # prepare test response data:
    # - if it is string, load it from file specified by that string
    # - otherwise use it as JSON object
    if isinstance(response_test_data, str):
        # response test data contains file with JSON data
        with open(f"tests/assets/data/{response_test_data}") as f:
            response_test_data = json.load(f)

    # wait for SeldonDeployment to become available
    utils.assert_available(logger, lightkube_client, SELDON_DEPLOYMENT, ml_model, namespace)

    # obtain prediction service endpoint
    service_name = f"{ml_model}-{predictor}-classifier"
    service = lightkube_client.get(Service, name=service_name, namespace=namespace)
    service_ip = service.spec.clusterIP
    service_port = next(p for p in service.spec.ports if p.name == "http").port

    # post prediction request
    response = requests.post(f"http://{service_ip}:{service_port}/{url}", json=request_data)
    response.raise_for_status()
    response = response.json()

    # reset id in response, if present
    if "id" in response.keys():
        response["id"] = "None"

    # reset data for HUGGINGFACE_SERVER because GPT inference returns different data every time
    if server_name == "HUGGINGFACE_SERVER":
        response["outputs"][0]["data"] = "None"

    # for 'seldon' protocol update test data with correct predictor server image
    if protocol == "seldon":
        # retrieve predictor server image from configmap to implicitly verify that it matches
        # deployed predictor server image
        configmap = lightkube_client.get(
            ConfigMap,
            name="seldon-config",
            namespace=ops_test.model_name,
        )
        configmap_yaml = yaml.safe_load(codecs.dump_all_yaml([configmap]))
        servers = json.loads(configmap_yaml["data"]["predictor_servers"])
        server_image = servers[server_name]["protocols"][protocol]["image"]
        server_version = servers[server_name]["protocols"][protocol]["defaultImageVersion"]
        response_test_data["meta"]["requestPath"][
            "classifier"
        ] = f"{server_image}:{server_version}"

    # verify prediction response
    assert sorted(response.items()) == sorted(response_test_data.items())

    # wait for application to settle
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=120, idle_period=60
    )
