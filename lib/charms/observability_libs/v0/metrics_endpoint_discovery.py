# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""# MetricsEndpointDiscovery Library.

This library provides functionality for discovering metrics endpoints exposed
by applications deployed to a Kubernetes cluster.

It comprises:
- A custom event and event source for handling metrics endpoint changes.
- Logic to observe cluster events and emit the events as appropriate.

## Using the Library

### Handling Events

To ensure that your charm can react to changing metrics endpoint events,
use the CharmEvents extension.
```python
import json

from charms.observability_libs.v0.metrics_endpoint_discovery import
    MetricsEndpointCharmEvents,
    MetricsEndpointObserver
)

class MyCharm(CharmBase):

    on = MetricsEndpointChangeCharmEvents()

    def __init__(self, *args):
        super().__init__(*args)

        self._observer = MetricsEndpointObserver(self, {"app.kubernetes.io/name": ["grafana-k8s"]})
        self.framework.observe(self.on.metrics_endpoint_change, self._on_endpoints_change)

    def _on_endpoints_change(self, event):
        self.unit.status = ActiveStatus(json.dumps(event.discovered))
```
"""

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable

from lightkube import Client
from lightkube.resources.core_v1 import Pod
from ops.charm import CharmBase, CharmEvents
from ops.framework import EventBase, EventSource, Object, StoredState

logger = logging.getLogger(__name__)

# The unique Charmhub library identifier, never change it
LIBID = "a141d5620152466781ed83aafb948d03"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 3

# File path where metrics endpoint change data is written for exchange
# between the discovery process and the materialised event.
PAYLOAD_FILE_PATH = "/tmp/metrics-endpoint-payload.json"

# File path for the spawned discovery process to write logs.
LOG_FILE_PATH = "/var/log/discovery.log"


class MetricsEndpointChangeEvent(EventBase):
    """A custom event for metrics endpoint changes."""

    def __init__(self, handle):
        super().__init__(handle)

        with open(PAYLOAD_FILE_PATH, "r") as f:
            self._discovered = json.loads(f.read())

    def snapshot(self):
        """Save the event payload data."""
        return {"payload": self._discovered}

    def restore(self, snapshot):
        """Restore the event payload data."""
        self._discovered = {}

        if snapshot:
            self._discovered = snapshot["payload"]

    @property
    def discovered(self):
        """Return the payload of detected endpoint changes for this event."""
        return self._discovered


class MetricsEndpointChangeCharmEvents(CharmEvents):
    """A CharmEvents extension for metrics endpoint changes.

    Includes :class:`MetricsEndpointChangeEvent` in those that can be handled.
    """

    metrics_endpoint_change = EventSource(MetricsEndpointChangeEvent)


class MetricsEndpointObserver(Object):
    """Observes changing metrics endpoints in the cluster.

    Observed endpoint changes cause :class"`MetricsEndpointChangeEvent` to be emitted.
    """

    # Yes, we need this so we can track it between events
    _stored = StoredState()

    def __init__(self, charm: CharmBase, labels: Dict[str, Iterable]):
        """Constructor for MetricsEndpointObserver.

        Args:
            charm: the charm that is instantiating the library.
            labels: dictionary of label/value to be observed for changing metrics endpoints.
        """
        super().__init__(charm, "metrics-endpoint-observer")
        self._stored.set_default(observer_pid=0)

        self._charm = charm
        self._observer_pid = self._stored.observer_pid

        self._labels = labels
        self.start_observer()

    def start_observer(self):
        """Start the metrics endpoint observer running in a new process."""
        self.stop_observer()

        # We need to trick Juju into thinking that we are not running
        # in a hook context, as Juju will disallow use of juju-run.
        new_env = os.environ.copy()
        if "JUJU_CONTEXT_ID" in new_env:
            new_env.pop("JUJU_CONTEXT_ID")

        tool_prefix = "/usr/bin"
        if Path(tool_prefix, "juju-run").exists():
            tool_path = Path(tool_prefix, "juju-run")
        else:
            tool_path = Path(tool_prefix, "juju-exec")

        pid = subprocess.Popen(
            [
                "/usr/bin/python3",
                "lib/charms/observability_libs/v{}/metrics_endpoint_discovery.py".format(LIBAPI),
                json.dumps(self._labels),
                str(tool_path),
                self._charm.unit.name,
                self._charm.charm_dir,
            ],
            stdout=open(LOG_FILE_PATH, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=new_env,
        ).pid

        self._observer_pid = pid  # type: ignore

    def stop_observer(self):
        """Stop the running observer process if we have previously started it."""
        if not self._observer_pid:  # type: ignore
            return

        try:
            os.kill(self._observer_pid, signal.SIGINT)  # type: ignore
            msg = "Stopped running metrics endpoint observer process with PID {}"
            logging.info(msg.format(self._observer_pid))
        except OSError:
            pass

    @property
    def unit_tag(self):
        """Juju-style tag identifying the unit being run by this charm."""
        unit_num = self._charm.unit.name.split("/")[-1]
        return "unit-{}-{}".format(self._charm.app.name, unit_num)


def write_payload(payload):
    """Write the input event data to event payload file."""
    with open(PAYLOAD_FILE_PATH, "w") as f:
        f.write(json.dumps(payload))


def dispatch(run_cmd, unit, charm_dir):
    """Use the input juju-run command to dispatch a :class:`MetricsEndpointChangeEvent`."""
    dispatch_sub_cmd = "JUJU_DISPATCH_PATH=hooks/metrics_endpoint_change {}/dispatch"
    subprocess.run([run_cmd, "-u", unit, dispatch_sub_cmd.format(charm_dir)])


def main():
    """Main watch and dispatch loop.

    Watch the input k8s service names. When changes are detected, write the
    observed data to the payload file, and dispatch the change event.
    """
    labels, run_cmd, unit, charm_dir = sys.argv[1:]

    client = Client()
    labels = json.loads(labels)

    for change, entity in client.watch(Pod, namespace="*", labels=labels):
        if Path(PAYLOAD_FILE_PATH).exists():
            dispatch(run_cmd, unit, charm_dir)
            Path(PAYLOAD_FILE_PATH).unlink()
        meta = entity.metadata
        metrics_path = ""
        if entity.metadata.annotations.get("prometheus.io/path", ""):
            metrics_path = entity.metadata.annotations.get("prometheus.io/path", "")

        target_ports = []
        for c in filter(lambda c: c.ports is not None, entity.spec.containers):
            for p in filter(lambda p: p.name == "metrics", c.ports):
                target_ports.append("*:{}".format(p.containerPort))

        payload = {
            "change": change,
            "namespace": meta.namespace,
            "name": meta.name,
            "path": metrics_path,
            "targets": target_ports or ["*:80"],
        }

        write_payload(payload)
        dispatch(run_cmd, unit, charm_dir)


if __name__ == "__main__":
    main()
