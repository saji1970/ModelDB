"""ObjectService: browsing/listing/search behavior that backs the
Storage Explorer UI - the primary-vs-companion object filtering in
particular (list_objects() must not leak a model's tensor blocks or a
document's/image's internal companion objects as if they were separate
top-level uploads).
"""

from pathlib import Path

import pytest

from mdc.api.service import ObjectService
from mdc.classification.data_type import DataType
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.router import build_default_router
from mdc.storage_intelligence.strategy import StorageTier


@pytest.fixture
def service(tmp_path: Path) -> ObjectService:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return ObjectService(build_default_router(store))


def _safetensors_bytes() -> bytes:
    import json
    import struct

    header = {"w": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]}}
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"\x00" * 8


def _png_bytes() -> bytes:
    import struct

    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 10, 10) + b"\x08\x02\x00\x00\x00" + b"\x00" * 20


def test_list_objects_excludes_document_record_companion(service: ObjectService):
    service.upload(b"# Title\n\nSome body text.", filename="notes.md")
    results = service.list_objects()
    assert len(results) == 1
    assert results[0]["type"] == "DOCUMENT"
    assert not results[0]["object_id"].endswith(":record")


def test_list_objects_excludes_image_metadata_companion(service: ObjectService):
    service.upload(_png_bytes(), filename="photo.png")
    results = service.list_objects()
    assert len(results) == 1
    assert not results[0]["object_id"].endswith(":metadata")


def test_list_objects_excludes_model_tensor_blocks(service: ObjectService):
    service.upload(_safetensors_bytes(), filename="model.safetensors")
    results = service.list_objects()
    assert len(results) == 1
    assert results[0]["type"] == "AI_MODEL"


def test_list_objects_can_include_companions_explicitly(service: ObjectService):
    service.upload(b"# Title\n\nBody.", filename="notes.md")
    results = service.list_objects(include_companions=True)
    assert len(results) == 2  # original + :record


def test_list_objects_filters_by_type(service: ObjectService):
    service.upload(b"# Doc\n\nBody.", filename="a.md")
    service.upload(_png_bytes(), filename="b.png")

    documents = service.list_objects(object_type=DataType.DOCUMENT)
    images = service.list_objects(object_type=DataType.IMAGE)
    assert len(documents) == 1
    assert len(images) == 1
    assert documents[0]["type"] == "DOCUMENT"
    assert images[0]["type"] == "IMAGE"


def test_list_objects_filters_by_tier(service: ObjectService):
    entry = service.upload(b"just some log text\n" * 5, filename="a.log")
    service.move(entry["object_id"], StorageTier.HOT)

    hot = service.list_objects(storage_tier=StorageTier.HOT)
    warm = service.list_objects(storage_tier=StorageTier.WARM)
    assert len(hot) == 1
    assert len(warm) == 0


def test_search_documents_finds_matching_text(service: ObjectService):
    service.upload(b"# Report\n\nRevenue grew significantly this quarter.", filename="report.md")
    service.upload(b"# Notes\n\nUnrelated content about gardening.", filename="notes.md")

    results = service.search_documents("revenue")
    assert len(results) == 1
    assert "revenue" in results[0]["snippet"].lower() or "Revenue" in results[0]["snippet"]


def test_search_documents_is_case_insensitive_and_returns_snippet(service: ObjectService):
    service.upload(b"# Doc\n\nThe quick brown fox jumps over the lazy dog.", filename="a.md")
    results = service.search_documents("QUICK BROWN")
    assert len(results) == 1
    assert "quick brown" in results[0]["snippet"].lower()


def test_search_documents_no_match_returns_empty(service: ObjectService):
    service.upload(b"# Doc\n\nNothing relevant here.", filename="a.md")
    assert service.search_documents("nonexistent-term-xyz") == []


# -- optimize() must not lose the object's known type on re-evaluation ----------

def test_optimize_preserves_known_type_for_content_that_does_not_self_announce_it(service: ObjectService):
    # A model manifest is stored as plain JSON - re-classifying those raw
    # bytes from scratch reads as a generic DATABASE_RECORD (mutable by
    # default), not AI_MODEL (immutable by default), which would silently
    # flip the tier `optimize()` recommends. With zero access/mutation, an
    # AI_MODEL correctly settles at COLD; a misclassified DATABASE_RECORD
    # would instead fall to WARM's mutable-default path. optimize() must
    # use the type already recorded in the index, not re-guess it from
    # bytes that don't self-announce it.
    manifest = service.upload(_safetensors_bytes(), filename="model.safetensors")
    model_id = manifest["object_id"]
    assert service.get_metadata(model_id)["type"] == "AI_MODEL"

    result = service.optimize(model_id)
    final_tier = result["storage_tier"] if result["changed"] else service.get_metadata(model_id)["storage_tier"]
    assert final_tier == "COLD"
