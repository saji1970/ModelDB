"""Phase J: conversation.interpreter.process_turn - the real NLP
integration for the polymorphic storage system (CLAUDE-STORAGE.md
sections 36-40), including pronoun resolution, delete confirmation,
preference reporting, and the safety rule around STORE-by-path.
"""

from pathlib import Path

import pytest

from mdc.api.service import ObjectService
from mdc.conversation.interpreter import process_turn
from mdc.conversation.state import StorageConversationState
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.router import ObjectNotFoundError, build_default_router
from mdc.storage_intelligence.strategy import StorageTier


@pytest.fixture
def service(tmp_path: Path) -> ObjectService:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return ObjectService(build_default_router(store))


@pytest.fixture
def state() -> StorageConversationState:
    return StorageConversationState(session_id="s1")


def test_unrecognized_text_returns_none(state, service):
    assert process_turn(state, "do a backflip", service) is None


def test_store_without_read_file_reports_unavailable(state, service):
    result = process_turn(state, "store ./model.safetensors", service, read_file=None)
    assert "Upload button" in result.message
    assert state.last_object_id is None


def test_store_with_read_file_uploads_and_remembers_object(state, service, tmp_path: Path):
    sample = tmp_path / "notes.md"
    sample.write_text("# Report\n\nRevenue grew this quarter.\n")

    result = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    assert "Stored" in result.message
    assert state.last_object_id == result.data["object_id"]
    assert result.data["type"] == "DOCUMENT"


def test_store_unreadable_path_reports_error_not_crash(state, service, tmp_path: Path):
    missing = tmp_path / "does-not-exist.md"
    result = process_turn(state, f"store {missing}", service, read_file=Path.read_bytes)
    assert "Couldn't read" in result.message
    assert state.last_object_id is None


def test_pronoun_it_resolves_to_last_object(state, service, tmp_path: Path):
    sample = tmp_path / "a.md"
    sample.write_text("# Doc\n\nBody.")
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    archived = process_turn(state, "archive it", service)
    assert archived.data["storage_tier"] == "ARCHIVE"
    assert archived.data["object_id"] == object_id


def test_pronoun_without_prior_object_asks_for_an_id(state, service):
    result = process_turn(state, "archive it", service)
    assert "Give me an object id" in result.message


def test_retrieve_updates_last_object_id_so_later_pronouns_resolve(state, service, tmp_path: Path):
    sample = tmp_path / "a.md"
    sample.write_text("# Doc\n\nBody.")
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    state.last_object_id = None  # simulate a fresh session that only knows the explicit id
    process_turn(state, f"inspect {object_id}", service)
    assert state.last_object_id == object_id

    archived = process_turn(state, "archive it", service)  # now "it" should resolve
    assert archived.data["object_id"] == object_id


# -- delete confirmation ------------------------------------------------------------

def test_delete_requires_yes_no_confirmation(state, service, tmp_path: Path):
    sample = tmp_path / "a.md"
    sample.write_text("# Doc\n\nBody.")
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    ask = process_turn(state, "delete it", service)
    assert "yes/no" in ask.message
    assert state.pending_delete == object_id
    assert service.get_metadata(object_id)["object_id"] == object_id  # not deleted yet

    confirm = process_turn(state, "yes", service)
    assert "Deleted" in confirm.message
    assert state.last_object_id is None  # cleared since the deleted object was "it"
    with pytest.raises(ObjectNotFoundError):
        service.get_metadata(object_id)


def test_delete_cancelled_keeps_the_object(state, service, tmp_path: Path):
    sample = tmp_path / "a.md"
    sample.write_text("# Doc\n\nBody.")
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    process_turn(state, "delete it", service)
    cancel = process_turn(state, "no", service)
    assert "Cancelled" in cancel.message
    assert service.get_metadata(object_id)["object_id"] == object_id


# -- OPTIMIZE preference reporting (section 38's "propose vs decide") -----------

def test_optimize_reports_requested_preference_separately_from_the_decision(state, service, tmp_path: Path):
    sample = tmp_path / "a.log"
    sample.write_text("2026-01-01 00:00:00 INFO line\n" * 5)
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    result = process_turn(state, f"make {object_id} as compact as possible", service)
    assert "requested: max compression" in result.message
    assert "storage engine decides the actual representation/compression" in result.message


def test_optimize_fast_access_preference_actually_influences_tier(state, service, tmp_path: Path):
    sample = tmp_path / "a.log"
    sample.write_text("2026-01-01 00:00:00 INFO line\n" * 5)
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    result = process_turn(state, f"optimize {object_id} for fast access", service)
    assert result.data["storage_tier"] == "HOT"
    assert "requested: fast access" in result.message


def test_optimize_no_change_needed_is_reported_honestly(state, service, tmp_path: Path):
    sample = tmp_path / "a.log"
    sample.write_text("2026-01-01 00:00:00 INFO line\n" * 5)
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    result = process_turn(state, f"optimize {object_id}", service)
    assert "No change needed" in result.message


# -- MOVE / RESTORE / SEARCH / INSPECT / DESCRIBE --------------------------------

def test_move_to_explicit_tier(state, service, tmp_path: Path):
    sample = tmp_path / "a.log"
    sample.write_text("2026-01-01 00:00:00 INFO line\n" * 5)
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    result = process_turn(state, f"move {object_id} to cold", service)
    assert result.data["storage_tier"] == "COLD"


def test_restore_moves_to_warm(state, service, tmp_path: Path):
    sample = tmp_path / "a.md"
    sample.write_text("# Doc\n\nBody.")
    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    object_id = stored.data["object_id"]

    process_turn(state, "archive it", service)
    result = process_turn(state, "restore it", service)
    assert result.data["storage_tier"] == "WARM"
    assert result.data["object_id"] == object_id


def test_search_by_type(state, service, tmp_path: Path):
    sample = tmp_path / "a.md"
    sample.write_text("# Doc\n\nBody.")
    process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)

    result = process_turn(state, "list documents", service)
    assert "1 DOCUMENT" in result.message
    assert len(result.data) == 1


def test_search_by_text(state, service, tmp_path: Path):
    sample = tmp_path / "a.md"
    sample.write_text("# Report\n\nRevenue grew significantly.")
    process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)

    result = process_turn(state, "search for revenue", service)
    assert "1 document" in result.message.lower()


def test_retrieve_tensor_from_model(state, service, tmp_path: Path):
    import json
    import struct

    w = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    header = {"layer_0.q": {"dtype": "F32", "shape": [4], "data_offsets": [0, len(w)]}}
    header_bytes = json.dumps(header).encode()
    model_bytes = struct.pack("<Q", len(header_bytes)) + header_bytes + w
    sample = tmp_path / "model.safetensors"
    sample.write_bytes(model_bytes)

    stored = process_turn(state, f"store {sample}", service, read_file=Path.read_bytes)
    model_id = stored.data["object_id"]

    result = process_turn(state, f"retrieve tensor layer_0.q from {model_id}", service)
    assert "4 bytes" not in result.message  # sanity: real tensor is 16 bytes (4 floats)
    assert result.data["size"] == len(w)


def test_retrieve_tensor_from_unknown_model_reports_not_found(state, service):
    result = process_turn(state, "retrieve tensor w from AIM-0000000000", service)
    assert "no model found" in result.message.lower() or "no such collection" not in result.message.lower()


def test_unknown_object_reports_not_found_for_every_intent(state, service):
    for text in ["archive AIM-0000000000", "restore AIM-0000000000", "optimize AIM-0000000000",
                 "move AIM-0000000000 to hot", "delete AIM-0000000000", "inspect AIM-0000000000",
                 "describe AIM-0000000000", "retrieve AIM-0000000000"]:
        result = process_turn(state, text, service)
        assert "no object found" in result.message.lower()
