# Copyright 2022 Canonical Ltd.
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
import yaml
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
        raise_on_blocked=False,
        timeout=60 * 20,
    )

    # add Seldon/Istio relation
    await ops_test.model.add_relation(f"{istio_pilot}:gateway-info", f"{APP_NAME}:gateway-info")
    await ops_test.model.wait_for_idle(status="active", raise_on_blocked=True, timeout=60 * 5)


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(60),
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


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(60),
    reraise=True,
)
def assert_deleted(client, resource_class, resource_name, namespace):
    """Test for deleted resource. Retries multiple times to allow deployment to be deleted."""
    logger.info(f"Waiting for {resource_class}/{resource_name} to be deleted.")
    deleted = False
    try:
        dep = client.get(resource_class, resource_name, namespace=namespace)
    except ApiError as error:
        logger.info(f"Not found {resource_class}/{resource_name}. Status {error.status.code} ")
        if error.status.code == 404:
            deleted = True

    assert deleted, f"Waited too long for {resource_class}/{resource_name} to be deleted!"


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


@pytest.mark.asyncio
async def test_seldon_alert_rules(ops_test: OpsTest):
    """Test Seldon alert rules."""
    # NOTE: This test is re-using deployments created in test_build_and_deploy()
    namespace = ops_test.model_name
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
    with open("examples/serve-simple-v1.yaml") as f:
        sdep = SELDON_DEPLOYMENT(yaml.safe_load(f.read()))
        sdep["metadata"]["name"] = "seldon-model-1"
        client.create(sdep, namespace=namespace)
    assert_available(client, SELDON_DEPLOYMENT, "seldon-model-1", namespace)

    # remove deployment that was created by Seldon, reconcile alert will fire
    client.delete(
        Deployment, name="seldon-model-1-example-0-classifier", namespace=namespace, grace_period=0
    )

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
    client.delete(SELDON_DEPLOYMENT, name="seldon-model-1", namespace=namespace, grace_period=0)
    assert_deleted(client, SELDON_DEPLOYMENT, "seldon-model-1", namespace)

    # wait for application to settle
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=120, idle_period=60
    )


@pytest.mark.asyncio
async def test_seldon_deployment(ops_test: OpsTest):
    """Test Seldon Deployment scenario."""
    # NOTE: This test is re-using deployment created in test_build_and_deploy()
    namespace = ops_test.model_name
    client = Client()

    this_ns = client.get(res=Namespace, name=namespace)
    this_ns.metadata.labels.update({"serving.kubeflow.org/inferenceservice": "enabled"})
    client.patch(res=Namespace, name=this_ns.metadata.name, obj=this_ns)

    with open("examples/serve-simple-v1.yaml") as f:
        sdep = SELDON_DEPLOYMENT(yaml.safe_load(f.read()))
        client.create(sdep, namespace=namespace)

    assert_available(client, SELDON_DEPLOYMENT, "seldon-model", namespace)

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
    assert_deleted(client, SELDON_DEPLOYMENT, "seldon-model-1", namespace)

    # wait for application to settle
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=120, idle_period=60
    )


@pytest.mark.parametrize(
    # server_name - name of predictor server (should match configmap)
    # server_config - server configuration file
    # url - model prediction URL
    # req_data - data to put into request
    # resp_data - data expected in response
    # IMAGE:VERSION in below response data is replaced with values found in seldon-config ConfigMap
    "server_name, server_config, url, req_data, resp_data",
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
                "id": None,  # id needs to be reset in response
                "parameters": {"content_type": None, "headers": None},
                "outputs": [
                    {
                        "name": "predict",
                        "shape": [1, 1],
                        "datatype": "INT64",
                        "parameters": None,
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
                "id": None,  # id needs to be reset in response
                "parameters": {"content_type": None, "headers": None},
                "outputs": [
                    {
                        "name": "predict",
                        "shape": [1, 1],
                        "datatype": "FP32",
                        "parameters": None,
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
        # Disable test for mlflowserver V2 due to failure in model in test container
        # (
        #     "MLFLOW_SERVER",
        #     "mlflowserver-v2.yaml",
        #     "v2/models/iris/infer",
        #     {
        #         "inputs": [
        #             {
        #                 "name": "predict",
        #                 "shape": [1, 4],
        #                 "datatype": "FP32",
        #                 "data": [[1, 2, 3, 4]],
        #             },
        #         ]
        #     },
        #     {
        #         "model_name": "iris",
        #         "model_version": "v0.1.0",
        #         "id": None,  # id needs to be reset in response
        #         "parameters": {"content_type": None, "headers": None},
        #         "outputs": [
        #             {
        #                 "name": "predict",
        #                 "shape": [1, 1],
        #                 "datatype": "FP32",
        #                 "parameters": None,
        #                 "data": [2.0],
        #             }
        #         ],
        #     },
        # ),
        (
            "TENSORFLOW_SERVER",
            "tensorflow-serving.yaml",
            "api/v1.0/predictions",
            {"meta": {}, "data": {"tensor": {"shape": [1, 784], "values": [0.9998375676089037, 0.6392211092205818, 0.7251277357504603, 0.7808896762945906, 0.9471029273755714, 0.43660688687977034, 0.17739519588149044, 0.2685829334433132, 0.5628708378672913, 0.8612175076530088, 0.2708916996211985, 0.0406158034626205, 0.09456029072837158, 0.2666814341367846, 0.7194599080791999, 0.9452695941148388, 0.5563611274094761, 0.3768816657869556, 0.20350304379689876, 0.8736840386635644, 0.17739603777375101, 0.5882026251989727, 0.1232152208633025, 0.7771061539607746, 0.34367025360840164, 0.2362496171749492, 0.3482959505637404, 0.47976274163712895, 0.6246524750977825, 0.9140435253156701, 0.7937910929365936, 0.38934636623427854, 0.26806514316035146, 0.3893990993199904, 0.49757057620468503, 0.012091057798297222, 0.4044151699262156, 0.7251585100654504, 0.4694628238213354, 0.8651811034421732, 0.5053952381547168, 0.85361227673482, 0.7366099133244872, 0.5180127600188984, 0.2179237298515888, 0.5433329794411055, 0.36930041031018446, 0.7788858414998587, 0.753340821805769, 0.17967511068886155, 0.6145448596582288, 0.16559553195077004, 0.2138529536286481, 0.8427983198788503, 0.8740981689914816, 0.3856242143771802, 0.18371104079163436, 0.4297583987445547, 0.10108105456864513, 0.8431707724572862, 0.6897498937252297, 0.660057146734252, 0.15669945259739615, 0.1285372377408216, 0.30683402720336717, 0.8645433035446762, 0.3695200366707969, 0.9278766778163701, 0.6990669274840023, 0.7738199643415615, 0.3726689316988163, 0.23131303425074046, 0.29464273540103847, 0.44851033406137186, 0.24958522328243227, 0.959718055349001, 0.6957063714598016, 0.47525806718409525, 0.12857671945875881, 0.3012517503948541, 0.07071461194365503, 0.05003219029386197, 0.6661959702260833, 0.8453636967639719, 0.8603493030476621, 0.6154781618016356, 0.17384973466974363, 0.3330026479577167, 0.5580056440950443, 0.6814454446446438, 0.8384207232138043, 0.5550674384177283, 0.1306261038178782, 0.014680840775067261, 0.9181509901035543, 0.7033332205450622, 0.6783174721908556, 0.8806353690204538, 0.46029385882380913, 0.07483606883369387, 0.5738492535113954, 0.09191873181615284, 0.1710354687450769, 0.6074442385836999, 0.6571894909573045, 0.6854921272320373, 0.6471512128723828, 0.6762134519420487, 0.5845542018630464, 0.3960811995818979, 0.7411669850023669, 0.14150103034111605, 0.32866166930243335, 0.2780107810210197, 0.7725567434416791, 0.07129138136851854, 0.45608604142725095, 0.31055509203263054, 0.7940703045116781, 0.8517122049017283, 0.342493184165072, 0.48289569137863053, 0.21321618799616993, 0.8858219313623279, 0.7550908808488739, 0.6943050355825677, 0.16483431391636327, 0.5005854167904734, 0.17238549464611208, 0.2086867478375054, 0.032389489319256004, 0.4347725717199533, 0.8579552070201638, 0.9138605227755245, 0.9758296202010837, 0.6949019761407101, 0.4191053853879734, 0.6659145992525622, 0.7677118645909855, 0.24958121704129033, 0.6508205846666189, 0.4588842664875551, 0.48570748901216254, 0.5937441698499041, 0.37694161792558833, 0.15365476652232424, 0.19167117069921025, 0.3299556262282495, 0.34042673478380925, 0.6577336000051281, 0.5096298336030443, 0.46270681873487773, 0.8632809483728608, 0.9738102075628994, 0.7131382043255359, 0.004079977617932995, 0.2592848651100146, 0.3856602560297604, 0.44087021298398654, 0.5029461657856321, 0.6932019040318891, 0.9675994276804958, 0.7713457068366164, 0.8435094958410188, 0.5289839384467337, 0.735685589615721, 0.7313252352223532, 0.03375290270087983, 0.13840557426864697, 0.4048120155605476, 0.5237633180807841, 0.4355969450907252, 0.14038250002033237, 0.7506403840369514, 0.1450190092792456, 0.4586781275651982, 0.3144058117495878, 0.9673728733287045, 0.22517307893487848, 0.4954757261933921, 0.6244490177357518, 0.2123639772826914, 0.13691828069809142, 0.05650911157497307, 0.2604179633596353, 0.9630038857348265, 0.6812390943133333, 0.34768997997096884, 0.45001420622795685, 0.35174595039916123, 0.9674828830932802, 0.6618377544206178, 0.8477760964345799, 0.9617900873523807, 0.32510636309116403, 0.7416648307261213, 0.9363631285339015, 0.17183020291607165, 0.8475589844039726, 0.224568557892719, 0.2457437399491681, 0.8664006256247765, 0.030028773891941474, 0.4186173023951326, 0.8287920058419038, 0.35973534988896105, 0.6511869173482786, 0.5294770563321308, 0.946412094954597, 0.812482519607099, 0.3672463596036494, 0.7344588610488945, 0.6031783940641463, 0.8656483415839102, 0.43986158402625497, 0.7524420613123411, 0.3965201441779188, 0.1905475008538451, 0.5484707264245137, 0.344203480816045, 0.009900629480145029, 0.6574972440758317, 0.8880546065754985, 0.5949771642765174, 0.7127709047357214, 0.5284806488131697, 0.5571052704197698, 0.19187958117242443, 0.4683002823027661, 0.6953404553051732, 0.15192594776818835, 0.7777480923836222, 0.42668291669449665, 0.8150150912279405, 0.8899240052085794, 0.5838594298538653, 0.4858467641689149, 0.9504536263296325, 0.7862540020148352, 0.642194058242723, 0.41352856151515105, 0.5718923120409027, 0.9927176688561632, 0.6713704439386358, 0.8693793588025164, 0.3134918523498196, 0.4238560309713939, 0.38162953165727087, 0.9358323934496319, 0.9737019888123128, 0.16779542061578134, 0.0463070982913788, 0.013981221400875543, 0.041562368647210635, 0.5591620147767499, 0.40043046533741156, 0.7755514656547978, 0.3737324456822547, 0.6825338279528202, 0.617150828370086, 0.1266471517416432, 0.9138195833364484, 0.3678759592244092, 0.8360064889897666, 0.2723009404655078, 0.16622266035376776, 0.43127338027664575, 0.7376211347031769, 0.1900736849268393, 0.43900843177376225, 0.9955014056647603, 0.89303635195075, 0.1534149135527212, 0.4964767951383465, 0.27829489574883537, 0.5743451500907372, 0.21762350501087535, 0.735034366330862, 0.7251166837090426, 0.664593388340304, 0.2977994030312553, 0.30144048229985254, 0.0845084634448161, 0.995635134851757, 0.9166142444823936, 0.9666806958938474, 0.2626902192330284, 0.6275168705386093, 0.891071449656104, 0.8415206090744147, 0.3818789160170304, 0.3282057734741426, 0.3142930695744207, 0.6971741142393718, 0.2682372295463441, 0.9568228986657156, 0.6186442422980434, 0.1718785257727482, 0.8727239529030081, 0.5478065584520785, 0.05652517300758886, 0.8904410672591415, 0.4384942279260724, 0.6442895976038356, 0.8280932269945421, 0.2834429511658507, 0.9158215603197403, 0.5093282160232573, 0.9080653112016185, 0.43085992170968335, 0.18785259830611656, 0.21612925850903497, 0.9729607119899871, 0.8442927183974572, 0.3132315717225326, 0.9889675720598395, 0.6184690004492538, 0.3234607696590871, 0.7970305933960604, 0.12163738705845795, 0.27102079436280324, 0.29043139927810013, 0.34154193556948464, 0.9806166693093233, 0.7463226794249775, 0.5762567819770816, 0.7799340916363083, 0.53976229047184, 0.5738236203113702, 0.9367811391146843, 0.8882973232082787, 0.9742516762714406, 0.9110159596495111, 0.8603623269075282, 0.3064912817990616, 0.23035242752436524, 0.8057051712575578, 0.40669937967368286, 0.1175110680990441, 0.895500747583005, 0.47009446133562816, 0.9054624459609492, 0.08568836430344529, 0.36626960973579004, 0.5602441915520207, 0.8345101081150171, 0.2228628702962202, 0.3460672112839138, 0.20484292661251136, 0.7467344266988611, 0.5011203578214714, 0.5810559001698333, 0.20042394435456556, 0.2550275459853818, 0.437495834078109, 0.3502670336901408, 0.041331175423582334, 0.7836090848737568, 0.4536330537680159, 0.8188232148429642, 0.3370303529455143, 0.7785899343260836, 0.19675607459979372, 0.5426950762010787, 0.2528998545361786, 0.16864662743535574, 0.07114743764279452, 0.6297021279907552, 0.4239616485533697, 0.30465152398668904, 0.11706584629217565, 0.5613236555392489, 0.561323683670498, 0.2326361313694839, 0.5177396320634163, 0.027835507647529623, 0.3051264320526832, 0.8133355480813557, 0.3580327461764722, 0.4782002780210751, 0.5531330439794514, 0.07914750923295255, 0.5797511130585181, 0.9780696759363786, 0.4863163290020599, 0.4123924698624992, 0.16341325654766192, 0.8917521176620512, 0.8481672612078209, 0.8510981110258017, 0.6564156911261676, 0.564831569959665, 0.8726089325964326, 0.6054518957772484, 0.33597831632445363, 0.35337976356322764, 0.688666794871606, 0.23229962095034895, 0.01484368424479543, 0.4039330793886836, 0.7620534654907409, 0.7169656247854092, 0.548496778834871, 0.3531376017083697, 0.8372522530315838, 0.16247854008417306, 0.40471070910417906, 0.4713888721819638, 0.50951687984169, 0.005333860172462623, 0.6139368993548, 0.5723880896453907, 0.8203885280808363, 0.9193782763206231, 0.772572731410142, 0.5350875924160577, 0.2169185306676037, 0.7818504303350879, 0.6619668563013515, 0.5010961170464322, 0.9657361044475519, 0.029756158670924404, 0.7118587804143762, 0.6302176548849213, 0.5144317764973361, 0.8770089050729013, 0.3357013501902979, 0.35367888873910247, 0.05685006908136636, 0.7420282208200184, 0.5134947040029337, 0.8088826121851742, 0.7639872128642832, 0.12390905073865277, 0.04477475955817767, 0.09364716546752361, 0.372505193919328, 0.7603293685077399, 0.08595211420401605, 0.4089167806327132, 0.2739296937941953, 0.3376259236656729, 0.7967616577087252, 0.23498374601487249, 0.46464819933710766, 0.025945258645318603, 0.284626148444878, 0.7226973370160598, 0.01078023305142306, 0.7666812711560178, 0.5349351960129669, 0.1366648316277128, 0.583999750890187, 0.5774993135897312, 0.7422669600417889, 0.061960152289858184, 0.16333076645775246, 0.0025607209562688027, 0.1951944756741445, 0.6270780823941542, 0.756598651149106, 0.9463960734870384, 0.1657086255376472, 0.8531205083602247, 0.044470829620854135, 0.44754220794858524, 0.7701071014661427, 0.0457811715296752, 0.42638562698722693, 0.27622638518246356, 0.09291471258383588, 0.17890955180023904, 0.4206643721887854, 0.524775511887901, 0.7741425753716078, 0.7814135275537942, 0.7630329593880127, 0.35488034355062537, 0.8194076372776392, 0.6261053193177466, 0.9322812731052698, 0.38371924165132265, 0.2721585234471142, 0.6202344965450154, 0.45398076243834473, 0.8972713225118831, 0.6226872324870506, 0.8705286133573261, 0.9215667189446192, 0.015975433864905186, 0.8714011142134623, 0.6019236078513434, 0.47305557927535624, 0.8481667281315558, 0.3627853794167184, 0.16738472338694899, 0.2814372246525456, 0.4248133011818911, 0.7209725379930139, 0.36049106201516623, 0.8197904407579293, 0.3086107736953896, 0.5891578909647137, 0.8995064496345951, 0.6169467403451937, 0.4044262165234429, 0.8529128104601208, 0.27216230697862387, 0.7263052076140986, 0.7403277918863618, 0.09990368551573803, 0.6511632187066719, 0.9316498214719467, 0.5844478532774479, 0.3240933121684608, 0.05030333741117288, 0.35350034837621014, 0.27227411803326007, 0.5943511364230658, 0.1908244644820457, 0.3052151163831046, 0.780693165747541, 0.45900511964755353, 0.11498890271082429, 0.41051625805273817, 0.385909450189989, 0.11612405915294033, 0.3073784471156583, 0.470253738736087, 0.8791433799603302, 0.27250041875429143, 0.5778628621584133, 0.05774796441444541, 0.19472818245878565, 0.9489467518256041, 0.3874301958766735, 0.5617160963972391, 0.6698918563056502, 0.5037553594619721, 0.8438871660586753, 0.911949614674525, 0.02709585834012629, 0.5051737243183662, 0.994165499192289, 0.20048488690273958, 0.9764821724826116, 0.26805133785379887, 0.8274579636299555, 0.11367551270044307, 0.7729264430047417, 0.6310395873267434, 0.9183742662793112, 0.509632685758568, 0.7921841076637581, 0.46311966693271167, 0.9218382204050641, 0.20080432990625308, 0.9937035382564232, 0.1031189097240055, 0.5058315208668248, 0.13340271825902128, 0.8865742263140193, 0.9889472584165465, 0.6804105676966486, 0.5137657820034426, 0.47123439290699654, 0.9345051895461288, 0.6515179662341368, 0.142084250144058, 0.9972266959163512, 0.0005949651844611159, 0.4179654887357557, 0.3473454066586362, 0.38787060170209986, 0.9366163800009827, 0.43007250933274155, 0.5079131347069591, 0.8189313546965682, 0.6417670249285709, 0.35004745446706753, 0.9420940548921395, 0.5796928186596902, 0.4917695139816023, 0.8554028051017076, 0.44641168508442153, 0.34520503188088203, 0.04504782803523821, 0.869074691034748, 0.46701231219883366, 0.18853528208362247, 0.9188101820250285, 0.13410264026802565, 0.34782581320456807, 0.9171882231045678, 0.5404900935897672, 0.40639607589630355, 0.37176125120164105, 0.8811032356997041, 0.48961219673353495, 0.5703825590798491, 0.657051398088682, 0.2675593162561105, 0.47652403250939546, 0.19139710470166793, 0.2605409612771883, 0.9074323211503212, 0.6887342277718735, 0.6940299152415016, 0.7829857174682875, 0.9064770218462095, 0.7210205246063583, 0.16216145157038797, 0.42486714990458374, 0.798330355432116, 0.9810676267528498, 0.20517514442598328, 0.2558821923676863, 0.8718143213018922, 0.7565757320937324, 0.10555612230287892, 0.5858090594039411, 0.935801600565141, 0.3417964696304636, 0.6226225195551526, 0.46370840313476636, 0.8937991787826053, 0.9780237638935213, 0.7598797634782815, 0.717244702133823, 0.30417432501366426, 0.9393724811606409, 0.6965584229458298, 0.041939794330914104, 0.25187692436921816, 0.416103774213179, 0.9047922009722897, 0.3217013185063887, 0.9901514968215416, 0.9597286917912559, 0.8867989649309458, 0.17699409686257805, 0.024799902490826864, 0.1210076275647205, 0.33817832485501254, 0.36152118155211377, 0.367181400164885, 0.793385079285185, 0.7069148551378989, 0.2904038047458095, 0.079943624013315, 0.16193746530304465, 0.5858733699793525, 0.21764965940738423, 0.9536071992169927, 0.8922159545100463, 0.9834246555811866, 0.018815471892799973, 0.5733699672377677, 0.8714546322697075, 0.6935395071777894, 0.7364544515942502, 0.33131631416218, 0.33145781065527335, 0.4684982014089213, 0.2919329141203564, 0.26101952469081136, 0.2171404518492559, 0.8348086023345749, 0.36270992745041253, 0.6005610261505202, 0.4917410057264371, 0.8148488472442061, 0.9001992401748655, 0.988137158626888, 0.6746429855154066, 0.7069863195180465, 0.0674843892016308, 0.15945105132366055, 0.29830145678205466, 0.6927594156540278, 0.8180672226024285, 0.17308553913232916, 0.9902223621206543, 0.6311861573513877, 0.809690453155234, 0.19192151150992587, 0.18313896190908896, 0.3491543220323795, 0.8254077258226903, 0.9657781337821518, 0.20615961082996803, 0.9899958248765538, 0.8284342100461449, 0.547190740464929, 0.2207811881322581, 0.11291703946465215, 0.29584039066680046, 0.5977371825958736, 0.26434466610073715, 0.15318730727751417, 0.8213628659685003, 0.14930861800812245, 0.39731154998719187, 0.826988152354807, 0.5512198216933386, 0.5440904782941621, 0.48272277164354893, 0.498103775855522, 0.8167376034713866, 0.8605540491540176, 0.9879344543042438, 0.37120859209886437, 0.46537008799759594, 0.9607482672066561, 0.28408748386684746, 0.01370358425114393, 0.9093291604893644, 0.9088707244220318, 0.15267555651701814, 0.5004009522039123, 0.16405911928878225, 0.935093374884947, 0.8094753690907022, 0.6474322516076126, 0.9573794226853352, 0.4026707645689923, 0.5736671866707472, 0.3227778643194148, 0.7789796552741124, 0.5370655611738934, 0.6823021904863539, 0.38692565116892097, 0.8295285173984724, 0.12687844028321082, 0.8129543065350571, 0.9952188380752344, 0.8003417296853862, 0.17019790697596127, 0.5426809411833465, 0.6305504923342433, 0.9723047789348951, 0.5407712933705637, 0.2576613132432922, 0.9236250744400989, 0.20481507765889329, 0.10346661640755417, 0.5156276104682014, 0.06103884933631909, 0.18907227940270432, 0.9136622226739916, 0.9932151006522282, 0.7266653088947709, 0.7300929317396078, 0.4668502288298021, 0.3102837784659479, 0.2659834785299512, 0.13026071841660603, 0.9254075293257326, 0.08529586460698046, 0.5525575307115453, 0.21343858683748318, 0.18060289706324217, 0.8683506637020498, 0.6848930695805433, 0.6755154127436478, 0.2061481738644, 0.4298575389580429, 0.34168578962829554, 0.8454335802901116, 0.9033778297545824, 0.3401244429147189, 0.08965274313171268, 0.1569139124055624, 0.646742011344807, 0.5871233685539055, 0.36616461039778603, 0.6383094108189803, 0.8555840857328585, 0.8645675155149569, 0.6607827923299152, 0.6761344905641318, 0.3017867901904404, 0.4517706542862252, 0.269490128193246]}}},
            {
                "data": {"names": ["t:0", "t:1", "t:2", "t:3", "t:4", "t:5", "t:6", "t:7", "t:8", "t:9"], "tensor": {"shape": [1, 10], "values": [8.49343338e-22, 2.85119398e-35, 0.123584226, 0.0665731356, 1.18265652e-28, 0.809836566, 4.16546084e-13, 1.48641526e-19, 6.06191043e-06, 2.40174282e-20]}},"meta": {"requestPath": {"classifier": "IMAGE:VERSION"}},
            },
        ),
        (
            "TENSORFLOW_SERVER",
            "tensorflow.yaml",
            "v1/models/classifier:predict",
            {"instances": [1.0, 2.0, 5.0]},
            {"predictions": [2.5, 3, 4.5]},
        ),
    ],
)
@pytest.mark.asyncio
async def test_seldon_predictor_server(
    ops_test: OpsTest, server_name, server_config, url, req_data, resp_data
):
    """Test Seldon predictor server.

    Workload deploys Seldon predictor servers defined in ConfigMap.
    Each server is deployed and inference request is triggered, and response is evaluated.
    """
    # NOTE: This test is re-using deployment created in test_build_and_deploy()
    namespace = ops_test.model_name
    client = Client()

    this_ns = client.get(res=Namespace, name=namespace)
    this_ns.metadata.labels.update({"serving.kubeflow.org/inferenceservice": "enabled"})
    client.patch(res=Namespace, name=this_ns.metadata.name, obj=this_ns)

    # retrieve predictor server information and create Seldon Depoloyment
    with open(f"examples/{server_config}") as f:
        deploy_yaml = yaml.safe_load(f.read())
        ml_model = deploy_yaml["metadata"]["name"]
        predictor = deploy_yaml["spec"]["predictors"][0]["name"]
        protocol = "seldon"  # default protocol
        if "protocol" in deploy_yaml["spec"]:
            protocol = deploy_yaml["spec"]["protocol"]
        sdep = SELDON_DEPLOYMENT(deploy_yaml)
        client.create(sdep, namespace=namespace)

    assert_available(client, SELDON_DEPLOYMENT, ml_model, namespace)

    # obtain prediction service endpoint
    service_name = f"{ml_model}-{predictor}-classifier"
    service = client.get(Service, name=service_name, namespace=namespace)
    service_ip = service.spec.clusterIP
    service_port = next(p for p in service.spec.ports if p.name == "http").port

    # post prediction request
    response = requests.post(f"http://{service_ip}:{service_port}/{url}", json=req_data)
    response.raise_for_status()
    response = response.json()

    # reset id in response, if present
    if "id" in response.keys():
        response["id"] = None

    # for 'seldon' protocol update test data with correct predictor server image
    if protocol == "seldon":
        # retrieve predictor server image from configmap to implicitly verify that it matches
        # deployed predictor server image
        configmap = client.get(
            ConfigMap,
            name="seldon-config",
            namespace=ops_test.model_name,
        )
        configmap_yaml = yaml.safe_load(codecs.dump_all_yaml([configmap]))
        servers = json.loads(configmap_yaml["data"]["predictor_servers"])
        server_image = servers[server_name]["protocols"][protocol]["image"]
        server_version = servers[server_name]["protocols"][protocol]["defaultImageVersion"]
        resp_data["meta"]["requestPath"]["classifier"] = f"{server_image}:{server_version}"

    # verify prediction response
    assert sorted(response.items()) == sorted(resp_data.items())

    # remove Seldon Deployment
    client.delete(SELDON_DEPLOYMENT, name=ml_model, namespace=namespace, grace_period=0)
    assert_deleted(client, SELDON_DEPLOYMENT, ml_model, namespace)

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
    assert_deleted(lightkube_client, Pod, "seldon-controller-manager-0", ops_test.model_name)

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
