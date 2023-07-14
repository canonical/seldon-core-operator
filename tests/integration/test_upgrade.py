# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm Upgrade.

Upgrade tests simulate in-field upgrade using local charms. A clean environment is required to test
upgrade/refresh properly without cluttering other integraiton tests.
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


# Skip upgrade test, because it is failing in CI due to authorization issues.
# Manual test instructions for upgrade are provided in corresponding Github issue:
# https://github.com/canonical/seldon-core-operator/issues/101
# Upgrade test can be executed locally.
# TO-DO Ensure upgrade test passes in CI environment.
@pytest.mark.skip(reason="Skip due to authorization issues in CI.")
@pytest.mark.abort_on_fail
async def test_upgrade(ops_test: OpsTest):
    """Test upgrade.

    Verify that all upgrade process succeeds.

    NOTE: There should be no charm with APP_NAME deployed prior to testing upgrade.
          There should be no Seldon related resources present in the cluster.

    Main flow of the test:
    - Build charm to be tested, i.e. to be upgraded to.
    - Deploy stable version of the same charm from Charmhub.
    - Refresh the deployed stable charm to the charm to be tested.
    - Verify that charm is active and all resources are upgraded/intact.
    """

    # build the charm
    charm_under_test = await ops_test.build_charm(".")

    # deploy stable version of the charm
    await ops_test.model.deploy(
        entity_url="seldon-core", application_name=APP_NAME, channel="1.14/stable", trust=True
    )

    # wait for application to be idle for 60 seconds, because seldon-core workload creates an empty
    # configmap that tracks its leadership and expires in 45 seconds
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=60 * 10, idle_period=60
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"

    # refresh (upgrade) using locally built charm
    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    await ops_test.model.applications[APP_NAME].refresh(
        path=f"{charm_under_test}", resources={"oci-image": image_path}
    )

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
    try:
        cluster_crd = lightkube_client.get(
            CustomResourceDefinition,
            name="seldondeployments.machinelearning.seldon.io",
            namespace=ops_test.model_name,
        )
    except ApiError as error:
        logger.error(f"CRD not found {error}")
        assert False
    assert cluster_crd

    # check that cluster CRD is upgraded and version is correct
    test_crd_names = []
    for crd in yaml.safe_load_all(Path("./src/templates/crd-v1.yaml.j2").read_text()):
        test_crd_names.append(crd["metadata"]["name"])
    # there should be single CRD in manifest
    assert len(test_crd_names) == 1
    assert cluster_crd.metadata.name in test_crd_names[0]
    # TO-DO: verify that CRD indeed was updated

    # verify that ConfigMap is installed
    try:
        _ = lightkube_client.get(
            ConfigMap,
            name="seldon-config",
            namespace=ops_test.model_name,
        )
    except ApiError as error:
        logger.error(f"ConfigMap not found {error}")
        assert False
