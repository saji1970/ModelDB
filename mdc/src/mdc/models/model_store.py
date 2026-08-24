"""ModelStore (CLAUDE-STORAGE.md sections 11-14, success criteria in
section 56: "retrieve tensor layer_27.attention.q returns only the
requested tensor/block data").

Ties Phase A (classification/profiling) + Phase B (strategy selection)
+ Phase C (`StorageRouter`/`ObjectIndex`) together for the one concrete
case CLAUDE-STORAGE.md insists on handling specially: a model is never
one giant blob. Every tensor gets its own `DataProfile` and its own
`StorageStrategy` - two tensors in the same model can legitimately end
up chunked differently or on different tiers, exactly like two whole
objects can (see Phase B). The manifest itself is stored the same way
as everything else in this system (via the router, indexed like any
other object) rather than kept in a private in-memory dict, so it
survives independently of any one Python process.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.models.errors import ModelNotFoundError, TensorNotFoundError
from mdc.models.extractor import ExtractedTensor, parse_safetensors, split_tensor_into_blocks
from mdc.models.manifest import ModelManifest
from mdc.models.tensor import TensorDescriptor
from mdc.model.object import MDCObject, generate_object_id
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.policy import DEFAULT_POLICY
from mdc.storage_intelligence.router import ObjectNotFoundError, StorageRouter
from mdc.storage_intelligence.strategy import StorageStrategyEngine, StorageTier


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _element_count(shape: list[int]) -> int:
    count = 1
    for dim in shape:
        count *= dim
    return count


class ModelStore:
    def __init__(self, router: StorageRouter, strategy_engine: StorageStrategyEngine | None = None):
        self.router = router
        self.strategy_engine = strategy_engine or StorageStrategyEngine()

    def store_model(self, content: bytes, model_name: str, access: AccessProfile | None = None) -> ModelManifest:
        access = access or AccessProfile()
        parsed = parse_safetensors(content)
        model_id = generate_object_id(DataType.AI_MODEL)

        descriptors: list[TensorDescriptor] = []
        total_parameters = 0
        for tensor in parsed.tensors:
            descriptor, parameter_count = self._store_tensor(model_id, tensor, access)
            descriptors.append(descriptor)
            total_parameters += parameter_count

        manifest = ModelManifest(
            model_id=model_id,
            model_name=model_name,
            architecture=parsed.metadata.get("architecture"),
            framework=parsed.metadata.get("format"),
            dtype=parsed.tensors[0].dtype if parsed.tensors else None,
            tokenizer=parsed.metadata.get("tokenizer"),
            tensor_count=len(parsed.tensors),
            total_parameters=total_parameters,
            total_size=len(content),
            checksum=hashlib.sha256(content).hexdigest(),
        )
        self._store_manifest(manifest, access)
        return manifest

    def _store_tensor(self, model_id: str, tensor: ExtractedTensor, access: AccessProfile) -> tuple[TensorDescriptor, int]:
        tensor_id = f"{model_id}:{tensor.name}"
        profile = build_profile(DataType.TENSOR, tensor.data, dimensions=tensor.shape)
        strategy = self.strategy_engine.select(profile, access)
        chunk_size = strategy.chunk_size or DEFAULT_POLICY.chunk_size_bytes

        for index, block in enumerate(split_tensor_into_blocks(tensor, chunk_size)):
            block_id = f"{tensor_id}:block_{index:03d}"
            block_object = MDCObject(
                object_id=block_id.replace(":", "_").replace(".", "_"),
                object_type=DataType.TENSOR,
                size=len(block.data),
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            self.router.store(block_object, block.data, strategy, tensor_id=tensor_id, tensor_name=tensor.name, block_id=block_id)

        descriptor = TensorDescriptor(
            tensor_id=tensor_id,
            tensor_name=tensor.name,
            shape=tensor.shape,
            dtype=tensor.dtype,
            quantization=None,
            compression=strategy.compression,
            block_size=chunk_size,
            checksum=hashlib.sha256(tensor.data).hexdigest(),
        )
        return descriptor, _element_count(tensor.shape)

    def _store_manifest(self, manifest: ModelManifest, access: AccessProfile) -> None:
        payload = manifest.model_dump_json().encode("utf-8")
        profile = build_profile(DataType.AI_MODEL, payload)
        strategy = self.strategy_engine.select(profile, access)
        manifest_object = MDCObject(
            object_id=manifest.model_id,
            object_type=DataType.AI_MODEL,
            size=len(payload),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.router.store(manifest_object, payload, strategy)

    def get_manifest(self, model_id: str) -> ModelManifest:
        try:
            payload = self.router.retrieve(model_id)
        except ObjectNotFoundError:
            raise ModelNotFoundError(model_id) from None
        return ModelManifest.model_validate_json(payload)

    def retrieve_tensor(self, model_id: str, tensor_name: str) -> bytes:
        # Confirms the model itself exists (a clearer error than "no
        # tensor found" when the typo is actually in the model id).
        self.get_manifest(model_id)

        tensor_id = f"{model_id}:{tensor_name}"
        entries = self.router.index.search(tensor_id=tensor_id)
        if not entries:
            raise TensorNotFoundError(model_id, tensor_name)

        ordered = sorted(entries, key=lambda entry: entry.block_id or "")
        return b"".join(self.router.retrieve(entry.object_id) for entry in ordered)

    def move_model(self, model_id: str, tier: StorageTier) -> int:
        """Move a model's manifest AND every block of every one of its
        tensors to `tier` in one call.

        A model is really N+1 separate index entries - the manifest plus
        one entry per tensor block (each tensor gets its own tier
        decision at upload time, `_store_tensor` above) - so a plain
        `router.move(model_id, tier)` only relocates the small manifest
        JSON, silently leaving the actual weight bytes wherever they
        already were. Returns the total number of objects actually
        moved (manifest + every tensor block).
        """
        self.get_manifest(model_id)  # ModelNotFoundError before touching anything
        self.router.move(model_id, tier)
        moved = 1

        tensor_prefix = f"{model_id}:"
        for entry in self.router.index.search(object_type=DataType.TENSOR):
            if entry.tensor_id is not None and entry.tensor_id.startswith(tensor_prefix):
                self.router.move(entry.object_id, tier)
                moved += 1
        return moved
