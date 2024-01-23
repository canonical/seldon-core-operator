# Contributing

## Overview

This document outlines the processes and practices recommended for contributing enhancements to `seldon-core`.

## Talk to us First

Before developing enhancements to this charm, you should [open an issue](/../../issues) explaining your use case. If you would like to chat with us about your use-cases or proposed implementation, you can reach us at [MLOps Mattermost public channel](https://chat.charmhub.io/charmhub/channels/mlops-documentation) or on [Discourse](https://discourse.charmhub.io/).

## Pull Requests

Please help us out in ensuring easy to review branches by rebasing your pull request branch onto the `main` branch. This also avoids merge commits and creates a linear Git commit history.

All pull requests require review before being merged. Code review typically examines:
  - code quality
  - test coverage
  - user experience for Juju administrators of this charm.

## Recommended Knowledge

Familiarising yourself with the [Charmed Operator Framework](https://juju.is/docs/sdk) library will help you a lot when working on new features or bug fixes.

## Build Charm

To build `seldon-core` run:

```shell
charmcraft pack
```

## Developing

You can use the environments created by `tox` for development. For example, to load the `unit` environment into your shell, run:

```shell
tox --notest -e unit
source .tox/unit/bin/activate
```

### Testing

Use tox for testing. For example to test the `lint` environment, run:

```shell
tox -e lint
```

See `tox.ini` for all available environments.

#### Seldon server integration tests

`test_seldon_servers.py` test ensures that SeldonDeployments CRs can be deployed successfully. The test initially deploys the charm and then applies the SeldonDeployment CRs defined under `assets/crs` directory.

* **Test all SeldonDeployment at once**: In order to test all SeldonDeployments at once, deploying the charm once, and then all the CRs consecutively (deploying one and once test succeeds, deleting it) use the `seldon-servers-integration` environment like this
```
# -- --model testing is optional in order to the charm to deployed to model `testing`
tox -e seldon-servers-integration -- --model testing
```

* **Test one server at a time**: In order to test only one SeldonDeployment each time, use the `seldon-servers-integration` environment with `pytest` flag `-k`, alongside the CR's corresponding keyword e.g.
```
# --model testing is optional in order to the charm to deployed to model `testing`
tox -e seldon-servers-integration -- -k sklearn-v1 --model testing
```
The available keywords are:
```
sklearn-v1
sklearn-v2
xgboost-v1
xgboost-v2
mlflowserver-v1
mlflowserver-v2
tf-serving
tensorflow
huggingface
```
These derive from the `id` field of objects in the `seldon_servers.py` file.

### Deploy

```bash
# Create a model
juju add-model dev
# Enable DEBUG logging
juju model-config logging-config="<root>=INFO;unit=DEBUG"
# Deploy the charm
juju deploy ./seldon-core_ubuntu-20.04-amd64.charm \
    --resource oci-image=$(yq '.resources."oci-image"."upstream-source"' metadata.yaml)
```

### Update manifests
Taking into account issue [#222](https://github.com/canonical/seldon-core-operator/issues/222), we need to manually update resources manifests for seldon. In order to do this:
1. In a clean cluster, apply upstream KF manifests using `kustomize build seldon-core-operator/base | kubectl apply -n kubeflow -f -` from the [kubeflow/manifests/contrib/seldon](https://github.com/kubeflow/manifests/tree/master/contrib/seldon) directory.
1. Check what resources have been created in the cluster (verify those by looking in the [initializer function](https://github.com/SeldonIO/seldon-core/blob/master/operator/utils/k8s/initializer.go#L46))
1. Go ahead and update our manifests accordingly

Do not forget to follow also the standard process (described in the Release Handbook) by doing `diff` between old and new version KF manifests for other manifests apart from those resources, like `auth_manifests`.

Note that, as with all sidecar charms, we do not include the `deployment` resource in our manifests since Pebble is responsible for creating the workload container in our case. However, we should consult any changes in the deployment that affect the container (e.g. its environment or command arguments).

## Canonical Contributor Agreement

Canonical welcomes contributions to this charm. Please check out our [contributor agreement](https://ubuntu.com/legal/contributors) if you're interested in contributing.
