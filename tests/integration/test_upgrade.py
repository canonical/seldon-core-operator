# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm Upgrade.

Upgrade tests simulate in-field upgrade using local charms. As a result, a clean environment is
required to test upgrade/refresh properly without cluttering other integraiton tests.
"""

import logging
from pathlib import Path

import pytest
import yaml
from lightkube import ApiError, Client
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.core_v1 import ConfigMap
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = "seldon-controller-manager"


@pytest.mark.abort_on_fail
async def test_upgrade(ops_test: OpsTest):
    """Test upgrade.

    Verify that all upgrade process succeeds.

    NOTE: There should be no charm with APP_NAME deployed prior to testing upgrade, because this
    test deploys local stable version of this charm which has revision 0 and peforms upgrade to
    locally built charm which will have revision 1. If prior deployment of locally build or remote
    charm is done, initial revision will be different and this mismatch which will cause upgrade to
    fail.
    There should be no Seldon related resources present in the cluster.

    Main flow of the test:
    - Build charm to be tested, i.e. to be upgraded to.
    - Download and deploy stable version of the same charm and store it in the same location as the
      charm to be tested (juju refresh of local charm requires local charms to be in the same
      path). Note that stable/1.14 version should be deployed without "trust"
    - Enable trust for the stable charm.
    - Refresh the deployed stable charm to the charm to be tested.
    - Verify that charm is active and all resources are upgraded/intact.
    """

    # build the charm
    charm_under_test = await ops_test.build_charm(".")

    # download and deploy stable version of the charm
    stable_charm_resources = {"oci-image": "docker.io/seldonio/seldon-core-operator:1.14.0"}
    stable_charm = str(charm_under_test.parent) + "/seldon-core-stable.charm"
    juju_download_result, _, __ = await ops_test.juju(
        "download", "seldon-core", f"--filepath={stable_charm}", "--channel=1.14/stable"
    )
    # check that download succeeded
    assert juju_download_result == 0

    await ops_test.model.deploy(
        stable_charm, resources=stable_charm_resources, application_name=APP_NAME, trust=True
    )

    # wait for application to be idle for 60 seconds, because seldon-core workload creates an empty
    # configmap that tracks its leadership and expires in 45 seconds
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=60 * 10, idle_period=60
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"

    # enable trust (needed because 1.14 was deployed without trust)
    juju_trust_result, _, __ = await ops_test.juju("trust", APP_NAME, f"--scope=cluster")
    assert juju_trust_result == 0

    # refresh (upgrade) using locally built charm
    # NOTE: using ops_test.juju() because there is no functionality to refresh in ops_test
    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    juju_refresh_result, _, __ = await ops_test.juju(
        "refresh", APP_NAME, f"--path={charm_under_test}", f'--resource="oci-image={image_path}"'
    )
    # check that refresh was started successfully
    # there is no guarantee that refresh has completed
    assert juju_refresh_result == 0

    # wait for updated charm to become active and idle for 120 seconds to ensure upgrade-charm
    # event has been handled and all resources were installed/upgraded
    # NOTE: when charm reaches active-idle state, upgrade-event might not have been handled yet
    # and/or installation of resources might not be completed
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=60 * 10, idle_period=120
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"

    # verify that cluster CRD is installed
    lightkube_client = Client()
    crd_list = lightkube_client.list(
        CustomResourceDefinition,
        labels=[("app.juju.is/created-by", "seldon-controller-manager")],
        namespace=ops_test.model.name,
    )
    cluster_crd = None
    try:
        cluster_crd = next(crd_list)
    except StopIteration:
        # no CRDs retrieved
        logger.error(f"CRD not found")
        assert 0

    # check that cluster CRD is upgraded and version is correct
    test_crd_names = []
    for crd in yaml.safe_load_all(Path("./src/templates/crd-v1.yaml.j2").read_text()):
        test_crd_names.append(crd["metadata"]["name"])
    # there should be single CRD in manifest
    assert len(test_crd_names) == 1
    assert cluster_crd.metadata.name in test_crd_names[0]
    # there should be no 'annotations' in this version of CRD
    assert cluster_crd.metadata.annotations

    # verify that ConfigMap is installed
    try:
        _ = lightkube_client.get(
            ConfigMap,
            name="seldon-config",
            namespace=ops_test.model.name,
        )
    except ApiError as error:
        logger.error(f"ConfigMap not found {error}")
        assert False
