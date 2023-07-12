#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# static list
STATIC_IMAGE_LIST=(
# from seldon-core
docker.io/seldonio/engine:1.12.0
seldonio/seldon-core-executor:1.13.1
)
# dynamic list
git checkout origin/track/1.15
IMAGE_LIST=()
IMAGE_LIST+=($(yq e ".data.predictor_servers" src/templates/configmap.yaml.j2 | jq -r '.[]' | jq -r 'select((.protocols.v2)) | "\(.protocols.v2.image):\(.protocols.v2.defaultImageVersion)"'))
IMAGE_LIST+=($(yq e ".data.predictor_servers" src/templates/configmap.yaml.j2 | jq -r '.[]' | jq -r 'select((.protocols.seldon)) | "\(.protocols.seldon.image):\(.protocols.seldon.defaultImageVersion)"'))
IMAGE_LIST+=($(yq '.options | ."executor-container-image-and-version" | .default' config.yaml))
IMAGE_LIST+=($(yq e ".data.storageInitializer" src/templates/configmap.yaml.j2 | jq -r 'select((.image)) | "\(.image)"'))
IMAGE_LIST+=($(yq e ".data.explainer" src/templates/configmap.yaml.j2 | jq -r 'select((.image)) | "\(.image)"'))
IMAGE_LIST+=($(yq e ".data.explainer" src/templates/configmap.yaml.j2 | jq -r 'select((.image_v2)) | "\(.image_v2)"'))

printf "%s\n" "${STATIC_IMAGE_LIST[@]}"
printf "%s\n" "${IMAGE_LIST[@]}"
