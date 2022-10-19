"""Library for sharing istio gateway information

This library wraps the relation endpoints using the `istio-gateway-name`
interface. It provides a Python API for both requesting and providing 
gateway information.

## Getting Started

### Fetch library with charmcraft
You can fetch the library using the following commands with charmcraft.
```shell
cd some-charm
charmcraft fetch-lib charms.istio_pilot.v0.istio_gateway_info
```
### Add relation to metadata.yaml
```yaml
requires:
    gateway-info:
        interface: istio_gateway_info
        limit: 1
```

### Initialise the library in charm.py
```python
from charms.istio_pilot.v0.istio_gateway_info import GatewayProvider, GatewayRelationError

Class SomeCharm(CharmBase):
    def __init__(self, *args):
        self.gateway = GatewayProvider(self)
        self.framework.observe(self.on.some_event_emitted, self.some_event_function)

    def some_event_function():
        # use the getter function wherever the info is needed
        try:
            gateway_data = self.gateway_relation.get_relation_data()
            except GatewayRelationError as error:
            ...
```
"""

import logging
from ops.framework import Object
from ops.model import Application

# The unique Charmhub library identifier, never change it
LIBID = "354103422e7a43e2870e4203fbb5a649"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


RELATION_NAME = "gateway-info"
INTERFACE_NAME = "istio-gateway-info"

logger = logging.getLogger(__name__)


class GatewayRelationError(Exception):
    pass


class GatewayRelationMissingError(GatewayRelationError):
    def __init__(self):
        self.message = "Missing gateway-info relation with istio-pilot"
        super().__init__(self.message)


class GatewayRelationTooManyError(GatewayRelationError):
    def __init__(self):
        self.message = "Too many istio-gateway-info relations"
        super().__init__(self.message)


class GatewayRelationDataMissingError(GatewayRelationError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class GatewayRequirer(Object):
    def __init__(self, charm, relation_name: str = RELATION_NAME):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.relation_name = relation_name

    def get_relation_data(self):
        if not self.model.unit.is_leader():
            return
        gateway = self.model.relations[self.relation_name]
        if len(gateway) == 0:
            raise GatewayRelationMissingError()
        if len(gateway) > 1:
            raise GatewayRelationTooManyError()

        remote_app = [
            app
            for app in gateway[0].data.keys()
            if isinstance(app, Application) and not app._is_our_app
        ][0]

        data = gateway[0].data[remote_app]

        if not "gateway_name" in data:
            logger.error(
                "Missing gateway name in gateway-info relation data. Waiting for gateway creation in istio-pilot"
            )
            raise GatewayRelationDataMissingError(
                "Missing gateway name in gateway-info relation data. Waiting for gateway creation in istio-pilot"
            )

        if not "gateway_namespace" in data:
            logger.error("Missing gateway namespace in gateway-info relation data")
            raise GatewayRelationDataMissingError(
                "Missing gateway namespace in gateway-info relation data"
            )

        return {
            "gateway_name": data["gateway_name"],
            "gateway_namespace": data["gateway_namespace"],
        }

