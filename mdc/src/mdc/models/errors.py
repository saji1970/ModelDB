"""Model/tensor storage errors."""

from __future__ import annotations


class ModelNotFoundError(Exception):
    def __init__(self, model_id: str):
        super().__init__(f"No model found with model_id={model_id!r}")
        self.model_id = model_id


class TensorNotFoundError(Exception):
    def __init__(self, model_id: str, tensor_name: str):
        super().__init__(f"No tensor {tensor_name!r} found in model {model_id!r}")
        self.model_id = model_id
        self.tensor_name = tensor_name
