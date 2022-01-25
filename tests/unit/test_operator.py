import pytest
import json
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

from charm import Operator


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == ActiveStatus("")


def test_missing_image(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    assert harness.charm.model.unit.status == BlockedStatus(
        "Missing resource: oci-image"
    )


def test_no_relation(harness):
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

    assert harness.charm.model.unit.status == ActiveStatus("")


def test_prometheus_data_set(harness, mocker):
    harness.set_leader(True)
    harness.set_model_name("kubeflow")
    harness.begin()

    mock_net_get = mocker.patch("ops.testing._TestingModelBackend.network_get")
    mocker.patch("ops.testing._TestingPebbleClient.list_files")

    bind_address = "1.1.1.1"
    fake_network = {
        "bind-addresses": [
            {
                "interface-name": "eth0",
                "addresses": [
                    {"hostname": "cassandra-tester-0", "value": bind_address}
                ],
            }
        ]
    }
    mock_net_get.return_value = fake_network
    rel_id = harness.add_relation("monitoring", "otherapp")
    harness.add_relation_unit(rel_id, "otherapp/0")
    harness.update_relation_data(rel_id, "otherapp", {})

    assert json.loads(
        harness.get_relation_data(rel_id, harness.model.app.name)["scrape_jobs"]
    )[0]["static_configs"][0]["targets"] == ["*:8080"]
