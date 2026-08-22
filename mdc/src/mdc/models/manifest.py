"""ModelManifest (CLAUDE-STORAGE.md section 13)."""

from __future__ import annotations

from pydantic import BaseModel


class ModelManifest(BaseModel):
    model_id: str
    model_name: str
    version: str = "1"
    architecture: str | None = None
    framework: str | None = None
    dtype: str | None = None
    # Never inferred automatically (section 16: "quantization must be
    # explicit") - stays None until an explicit quantize() operation
    # (a later phase) runs and records what it did.
    quantization: str | None = None
    tokenizer: str | None = None
    tensor_count: int
    total_parameters: int
    total_size: int
    checksum: str
