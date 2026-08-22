"""Phase D: Model/Tensor storage (CLAUDE-STORAGE.md sections 12-14,
success criteria in section 56).
"""

import array
import json
import struct
from pathlib import Path

import pytest

from mdc.models.errors import ModelNotFoundError, TensorNotFoundError
from mdc.models.extractor import ExtractedTensor, InvalidSafetensorsError, parse_safetensors, split_tensor_into_blocks
from mdc.models.model_store import ModelStore
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.policy import StoragePolicy
from mdc.storage_intelligence.router import build_default_router
from mdc.storage_intelligence.strategy import StorageStrategyEngine


def _floats(values) -> bytes:
    return array.array("f", values).tobytes()


def _build_safetensors(tensors: dict[str, tuple[list[int], bytes]], metadata: dict | None = None) -> bytes:
    header: dict = dict(metadata and {"__metadata__": metadata} or {})
    offset = 0
    body = b""
    for name, (shape, data) in tensors.items():
        header[name] = {"dtype": "F32", "shape": shape, "data_offsets": [offset, offset + len(data)]}
        body += data
        offset += len(data)
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + body


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    duckdb_store = DuckDBStore(tmp_path / "mdc.duckdb")
    duckdb_store.init_schema()
    return duckdb_store


@pytest.fixture
def model_store(store: DuckDBStore) -> ModelStore:
    router = build_default_router(store)
    # A tiny chunk size so the "large" fixture tensor genuinely splits
    # into multiple blocks, exercising the block-reassembly path.
    policy = StoragePolicy(chunk_threshold_bytes=1, chunk_size_bytes=4096)
    return ModelStore(router, StorageStrategyEngine(policy=policy))


# -- safetensors extraction ------------------------------------------------------

def test_parse_safetensors_extracts_real_tensor_bytes():
    q_data = _floats([1.0, 2.0, 3.0, 4.0])
    content = _build_safetensors({"layer_0.q": ([4], q_data)})
    parsed = parse_safetensors(content)
    assert len(parsed.tensors) == 1
    assert parsed.tensors[0].name == "layer_0.q"
    assert parsed.tensors[0].shape == [4]
    assert parsed.tensors[0].data == q_data


def test_parse_safetensors_separates_metadata_from_tensors():
    content = _build_safetensors({"w": ([1], _floats([1.0]))}, metadata={"format": "pt"})
    parsed = parse_safetensors(content)
    assert parsed.metadata == {"format": "pt"}
    assert [t.name for t in parsed.tensors] == ["w"]  # "__metadata__" is not treated as a tensor


def test_parse_safetensors_rejects_truncated_content():
    with pytest.raises(InvalidSafetensorsError):
        parse_safetensors(b"\x08\x00")


# -- block splitting ---------------------------------------------------------------

def test_split_small_tensor_produces_one_block():
    tensor = ExtractedTensor(name="q", dtype="F32", shape=[4], data=_floats([1.0, 2.0, 3.0, 4.0]))
    blocks = split_tensor_into_blocks(tensor, chunk_size_bytes=4096)
    assert len(blocks) == 1
    assert blocks[0].data == tensor.data
    assert blocks[0].shape == [4]


def test_split_large_tensor_produces_multiple_row_aligned_blocks():
    rows, cols = 100, 64
    tensor = ExtractedTensor(name="k", dtype="F32", shape=[rows, cols], data=_floats(range(rows * cols)))
    row_bytes = cols * 4
    blocks = split_tensor_into_blocks(tensor, chunk_size_bytes=row_bytes * 16)  # 16 rows/block

    assert len(blocks) == 7  # ceil(100 / 16)
    assert sum(b.shape[0] for b in blocks) == rows
    assert b"".join(b.data for b in blocks) == tensor.data
    assert blocks[0].shape == [16, cols]
    assert blocks[-1].shape == [4, cols]  # 100 - 6*16 = 4 remainder rows


# -- ModelStore: store, manifest, tensor-level retrieval ---------------------------

def test_store_model_builds_a_correct_manifest(model_store: ModelStore):
    q = _floats([1.0, 2.0, 3.0, 4.0])
    k = _floats(range(100 * 64))
    content = _build_safetensors({"layer_0.q": ([4], q), "layer_0.k": ([100, 64], k)}, metadata={"format": "pt"})

    manifest = model_store.store_model(content, "tiny-model")

    assert manifest.model_name == "tiny-model"
    assert manifest.tensor_count == 2
    assert manifest.total_parameters == 4 + 100 * 64
    assert manifest.total_size == len(content)
    assert manifest.framework == "pt"
    assert manifest.dtype == "F32"
    assert manifest.quantization is None  # never inferred automatically


def test_get_manifest_round_trips(model_store: ModelStore):
    content = _build_safetensors({"w": ([2], _floats([1.0, 2.0]))})
    manifest = model_store.store_model(content, "m")
    assert model_store.get_manifest(manifest.model_id) == manifest


def test_retrieve_tensor_returns_only_that_tensor_not_the_whole_model(model_store: ModelStore):
    q = _floats([1.0, 2.0, 3.0, 4.0])
    k = _floats(range(100 * 64))
    content = _build_safetensors({"layer_0.q": ([4], q), "layer_0.k": ([100, 64], k)})
    manifest = model_store.store_model(content, "tiny-model")

    retrieved_q = model_store.retrieve_tensor(manifest.model_id, "layer_0.q")
    assert retrieved_q == q
    assert retrieved_q != k

    retrieved_k = model_store.retrieve_tensor(manifest.model_id, "layer_0.k")
    assert retrieved_k == k


def test_retrieve_tensor_reassembles_multiple_blocks_in_order(model_store: ModelStore):
    rows, cols = 100, 64
    k = _floats(range(rows * cols))
    content = _build_safetensors({"layer_0.k": ([rows, cols], k)})
    manifest = model_store.store_model(content, "tiny-model")

    entries = model_store.router.index.search(tensor_id=f"{manifest.model_id}:layer_0.k")
    assert len(entries) > 1  # actually exercised multi-block reassembly

    assert model_store.retrieve_tensor(manifest.model_id, "layer_0.k") == k


def test_retrieve_unknown_tensor_raises(model_store: ModelStore):
    content = _build_safetensors({"w": ([1], _floats([1.0]))})
    manifest = model_store.store_model(content, "m")
    with pytest.raises(TensorNotFoundError):
        model_store.retrieve_tensor(manifest.model_id, "does.not.exist")


def test_unknown_model_raises(model_store: ModelStore):
    with pytest.raises(ModelNotFoundError):
        model_store.get_manifest("NOPE-0000000000")
    with pytest.raises(ModelNotFoundError):
        model_store.retrieve_tensor("NOPE-0000000000", "w")


def test_two_tensors_in_the_same_model_get_independent_checksums(model_store: ModelStore):
    content = _build_safetensors({
        "a": ([2], _floats([1.0, 2.0])),
        "b": ([2], _floats([3.0, 4.0])),
    })
    manifest = model_store.store_model(content, "m")
    assert manifest.total_parameters == 4
    a = model_store.retrieve_tensor(manifest.model_id, "a")
    b = model_store.retrieve_tensor(manifest.model_id, "b")
    assert a != b
