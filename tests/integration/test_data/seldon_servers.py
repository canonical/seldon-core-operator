from pytest import param

SELDON_SERVERS = [
    param(
        "SKLEARN_SERVER",
        "sklearn-v1.yaml",
        "api/v1.0/predictions",
        {"data": {"ndarray": [[1, 2, 3, 4]]}},
        {
            "data": {
                "names": ["t:0", "t:1", "t:2"],
                "ndarray": [[0.0006985194531162835, 0.00366803903943666, 0.995633441507447]],
            },
            # classifier will be replaced according to configmap
            "meta": {"requestPath": {"classifier": "IMAGE:VERSION"}},
        },
        id="sklearn-v1",
    ),
    param(
        "SKLEARN_SERVER",
        "sklearn-v2.yaml",
        "v2/models/classifier/infer",
        {
            "inputs": [
                {
                    "name": "predict",
                    "shape": [1, 4],
                    "datatype": "FP32",
                    "data": [[1, 2, 3, 4]],
                },
            ]
        },
        {
            "model_name": "classifier",
            "model_version": "v1",
            "id": "None",  # id needs to be reset in response
            "parameters": {},
            "outputs": [
                {
                    "name": "predict",
                    "shape": [1, 1],
                    "datatype": "INT64",
                    "parameters": {"content_type": "np"},
                    "data": [2],
                }
            ],
        },
        id="sklearn-v2",
    ),
    param(
        "XGBOOST_SERVER",
        "xgboost-v1.yaml",
        "api/v1.0/predictions",
        {"data": {"ndarray": [[1.0, 2.0, 5.0, 6.0]]}},
        {
            "data": {
                "names": [],
                "ndarray": [2.0],
            },
            # classifier will be replaced according to configmap
            "meta": {"requestPath": {"classifier": "IMAGE:VERSION"}},
        },
        id="xgboost-v1",
    ),
    param(
        "XGBOOST_SERVER",
        "xgboost-v2.yaml",
        "v2/models/iris/infer",
        {
            "inputs": [
                {
                    "name": "predict",
                    "shape": [1, 4],
                    "datatype": "FP32",
                    "data": [[1, 2, 3, 4]],
                },
            ]
        },
        {
            "model_name": "iris",
            "model_version": "v0.1.0",
            "id": "None",  # id needs to be reset in response
            "parameters": {},
            "outputs": [
                {
                    "name": "predict",
                    "shape": [1, 1],
                    "datatype": "FP32",
                    "parameters": {"content_type": "np"},
                    "data": [2.0],
                }
            ],
        },
        id="xgboost-v2",
    ),
    param(
        "MLFLOW_SERVER",
        "mlflowserver-v1.yaml",
        "api/v1.0/predictions",
        {"data": {"ndarray": [[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1]]}},
        {
            "data": {
                "names": [],
                "ndarray": [5.275558760255382],
            },
            # classifier will be replaced according to configmap
            "meta": {"requestPath": {"classifier": "IMAGE:VERSION"}},
        },
        id="mlflowserver-v1",
    ),
    param(
        "MLFLOW_SERVER",
        "mlflowserver-v2.yaml",
        "v2/models/classifier/infer",
        "mlflowserver-request-data.json",
        "mlflowserver-response-data.json",
        id="mlflowserver-v2",
    ),
    param(
        "TENSORFLOW_SERVER",
        "tf-serving.yaml",
        "api/v1.0/predictions",
        "tensorflow-serving-request-data.json",
        "tensorflow-serving-response-data.json",
        id="tf-serving",
    ),
    param(
        "TENSORFLOW_SERVER",
        "tensorflow.yaml",
        "v1/models/classifier:predict",
        {"instances": [1.0, 2.0, 5.0]},
        {"predictions": [2.5, 3, 4.5]},
        id="tensorflow",
    ),
    param(
        "HUGGINGFACE_SERVER",
        "huggingface.yaml",
        "v2/models/classifier/infer",
        {
            "inputs": [
                {
                    "name": "args",
                    "shape": [1],
                    "datatype": "BYTES",
                    "data": ["this is a test"],
                }
            ],
        },
        {
            "model_name": "classifier",
            "model_version": "v1",
            "id": "None",
            "parameters": {},
            "outputs": [
                {
                    "name": "output",
                    "shape": [1, 1],
                    "datatype": "BYTES",
                    "parameters": {"content_type": "hg_jsonlist"},
                    # 'data' needs to be reset because GPT returns different results every time
                    "data": "None",
                }
            ],
        },
        id="huggingface",
    ),
]
