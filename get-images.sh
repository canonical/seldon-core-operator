#!/bin/bash
STATIC_IMAGE_LIST=(
docker.io/seldonio/engine:1.12.0
docker.io/charmedkubeflow/sklearnserver_v1.16.0_20.04_1_amd64:v1.16.0_20.04_1
seldonio/mlserver:1.2.0-sklearn
seldonio/xgboostserver:1.15.0
seldonio/mlflowserver:1.15.0
docker.io/charmedkubeflow/mlserver-mlflow_1.2.0_22.04_1:1.2.0_22.04_1
nvcr.io/nvidia/tritonserver:21.08-py3
seldonio/mlserver:1.2.0-huggingface
seldonio/mlserver:1.2.0-slim
)
IMAGE_LIST=()
IMAGE_LIST+=$(yq '.options | ."executor-container-image-and-version" | .default' config.yaml)

printf "%s\n" "${STATIC_IMAGE_LIST[@]}"
printf "%s\n" "${IMAGE_LIST[@]}"
