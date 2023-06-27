# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
import tenacity
from lightkube import ApiError

"""Common functions for integration tests."""


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(80),
    reraise=True,
)
def assert_available(logger, client, resource_class, resource_name, namespace):
    """Test for available status. Retries multiple times to allow deployment to be created."""
    dep = client.get(resource_class, resource_name, namespace=namespace)
    state = dep.get("status", {}).get("state")

    resource_class_kind = resource_class.__name__
    if state == "Available":
        logger.info(f"{resource_class_kind}/{resource_name} status == {state}")
    else:
        if state == "Failed":
            logger.info("....")
        else:
            logger.info(
                f"{resource_class_kind}/{resource_name} status == {state} (waiting for 'Available')"
            )

    assert state == "Available", f"Waited too long for {resource_class_kind}/{resource_name}!"


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(80),
    reraise=True,
)
def assert_deleted(logger, client, resource_class, resource_name, namespace):
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
