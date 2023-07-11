#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# static list
STATIC_IMAGE_LIST=(
# from configmap
tensorflow/serving:2.1.0
seldonio/tfserving-proxy:1.15.0
docker.io/charmedkubeflow/sklearnserver_v1.16.0_20.04_1_amd64:v1.16.0_20.04_1
docker.io/charmedkubeflow/mlserver-sklearn_1.2.0_22.04_1_amd64:1.2.0_22.04_1
seldonio/xgboostserver:1.15.0
docker.io/charmedkubeflow/mlserver-xgboost_1.2.0_22.04_1_amd64:1.2.0_22.04_1
seldonio/mlflowserver:1.15.0
docker.io/charmedkubeflow/mlserver-mlflow_1.2.0_22.04_1:1.2.0_22.04_1
nvcr.io/nvidia/tritonserver:21.08-py3
docker.io/charmedkubeflow/mlserver-huggingface_1.2.4_22.04_1_amd64:1.2.4_22.04_1
seldonio/mlserver:1.2.0-slim
seldonio/rclone-storage-initializer:1.14.1
seldonio/alibiexplainer:1.15.0
seldonio/mlserver:1.2.0-alibi-explain
# from seldon-core
docker.io/seldonio/engine:1.12.0
seldonio/seldon-core-executor:1.13.1
)
# dynamic list
IMAGE_LIST=()
IMAGE_LIST+=$(yq '.options | ."executor-container-image-and-version" | .default' config.yaml)

printf "%s\n" "${STATIC_IMAGE_LIST[@]}"
printf "%s\n" "${IMAGE_LIST[@]}"
