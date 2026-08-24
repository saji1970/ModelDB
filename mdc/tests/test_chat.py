"""ChatEngine: the deterministic command parser behind the Storage
Explorer's chat panel (CLAUDE-STORAGE.md sections 36-38's minimal
slice - see mdc/api/chat.py's module docstring for the scoping note).
"""

from pathlib import Path

import pytest

from mdc.api.chat import ChatEngine
from mdc.api.service import ObjectService
from mdc.databases.manager import DatabaseManager
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore


@pytest.fixture
def manager(tmp_path: Path) -> DatabaseManager:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return DatabaseManager(tmp_path / "databases", store, load_default_registry())


@pytest.fixture
def service(manager: DatabaseManager) -> ObjectService:
    return manager.get("default").object_service


@pytest.fixture
def chat(manager: DatabaseManager) -> ChatEngine:
    return ChatEngine(manager)


def test_list_by_type(chat: ChatEngine, service: ObjectService):
    service.upload(b"# Doc\n\nBody.", filename="a.md")
    service.upload(b"just some plain prose, no markup or structure here", filename="b.dat")

    reply = chat.handle("s1", "list documents")
    assert "1 DOCUMENT" in reply.message
    assert len(reply.data) == 1


def test_list_by_tier_requires_in_phrasing(chat: ChatEngine, service: ObjectService):
    service.upload(b"log line\n" * 3, filename="a.log")
    reply = chat.handle("s1", "show objects in warm")
    assert "WARM" in reply.message
    assert len(reply.data) == 1


def test_unrecognized_type_word_falls_through_to_merchants_pipeline(chat: ChatEngine):
    # Not a real object-storage type word, so this falls all the way
    # through to the merchants CRUD/analytics fallback (see chat.py's
    # module docstring) - which resolves it as a by-name lookup that
    # finds nothing, rather than a bare "I didn't understand".
    reply = chat.handle("s1", "list unicorns")
    assert "no merchant found" in reply.message.lower()


def test_inspect(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"# Doc\n\nBody.", filename="a.md")
    object_id = upload["object_id"]
    reply = chat.handle("s1", f"show {object_id}")
    assert object_id in reply.message
    assert reply.data["object_id"] == object_id


def test_inspect_unknown_object(chat: ChatEngine):
    reply = chat.handle("s1", "show NOPE-0000000000")
    assert "no object found" in reply.message.lower()


def test_read_text_content(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"# Doc\n\nHello world.", filename="a.md")
    reply = chat.handle("s1", f"read {upload['object_id']}")
    assert "Hello world" in reply.message


def test_read_binary_content_is_not_shown_as_text(chat: ChatEngine, service: ObjectService):
    upload = service.upload(bytes(range(256)) * 4, filename="photo.jpg")
    reply = chat.handle("s1", f"read {upload['object_id']}")
    assert "bytes" in reply.message
    assert "not shown as text" in reply.message


def test_explain(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"log line\n" * 3, filename="a.log")
    reply = chat.handle("s1", f"explain {upload['object_id']}")
    assert len(reply.message) > 0
    assert reply.data["object_id"] == upload["object_id"]


def test_archive(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"log line\n" * 3, filename="a.log")
    reply = chat.handle("s1", f"archive {upload['object_id']}")
    assert "archived" in reply.message.lower()
    assert reply.data["storage_tier"] == "ARCHIVE"


def test_move_to_specific_tier(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"log line\n" * 3, filename="a.log")
    reply = chat.handle("s1", f"move {upload['object_id']} to hot")
    assert "moved" in reply.message.lower()
    assert reply.data["storage_tier"] == "HOT"


def test_delete_requires_confirmation(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"log line\n" * 3, filename="a.log")
    object_id = upload["object_id"]

    ask = chat.handle("s1", f"delete {object_id}")
    assert "yes/no" in ask.message
    # not deleted yet
    assert service.get_metadata(object_id)["object_id"] == object_id

    confirm = chat.handle("s1", "yes")
    assert "deleted" in confirm.message.lower()
    from mdc.storage_intelligence.router import ObjectNotFoundError
    with pytest.raises(ObjectNotFoundError):
        service.get_metadata(object_id)


def test_delete_can_be_cancelled(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"log line\n" * 3, filename="a.log")
    object_id = upload["object_id"]

    chat.handle("s1", f"delete {object_id}")
    cancel = chat.handle("s1", "no")
    assert "cancelled" in cancel.message.lower()
    assert service.get_metadata(object_id)["object_id"] == object_id


def test_delete_confirmation_is_scoped_per_session(chat: ChatEngine, service: ObjectService):
    upload = service.upload(b"log line\n" * 3, filename="a.log")
    object_id = upload["object_id"]

    chat.handle("session-a", f"delete {object_id}")
    # a different session's next message is NOT treated as a confirmation
    reply = chat.handle("session-b", "yes")
    assert "yes/no" not in reply.message  # not re-asked; just handled as a normal (unrecognized) message


def test_search(chat: ChatEngine, service: ObjectService):
    service.upload(b"# Report\n\nRevenue grew this quarter.", filename="r.md")
    reply = chat.handle("s1", "search for revenue")
    assert "1 document" in reply.message.lower()


def test_search_no_matches(chat: ChatEngine, service: ObjectService):
    service.upload(b"# Doc\n\nUnrelated.", filename="a.md")
    reply = chat.handle("s1", "search for nonexistent-term-xyz")
    assert "no documents matched" in reply.message.lower()


def test_empty_message_returns_help(chat: ChatEngine):
    reply = chat.handle("s1", "   ")
    assert "try" in reply.message.lower()


def test_unrecognized_command_falls_through_to_merchants_pipeline(chat: ChatEngine):
    reply = chat.handle("s1", "do a backflip")
    assert "no merchant found" in reply.message.lower()


# -- merchants CRUD/analytics fallback (closes the gap chat.py's docstring names) --

def test_merchants_crud_works_through_chat(chat: ChatEngine):
    created = chat.handle("s1", "Create a merchant called ABC Store in India")
    assert "Created" in created.message

    shown = chat.handle("s1", "Show me ABC Store")
    assert shown.data[0]["name"] == "ABC Store"
    assert shown.data[0]["country"] == "IN"


def test_merchants_analytics_query_asks_for_clarification_on_bare_balance(chat: ChatEngine):
    # "balance" alone is ambiguous among ledger/available/settlement -
    # this is the exact phrasing that used to hit the (no longer
    # existent) "I didn't understand that" fallback; it should now reach
    # the real clarification dialogue, symbol operator (">") included.
    reply = chat.handle("s1", "list all merchants with balance > 6000")
    assert "clarify" in reply.message.lower()
    assert "ledger balance" in reply.message.lower()
    assert "settlement balance" in reply.message.lower()


def test_merchants_insert_via_db_admin_is_visible_to_merchants_crud(chat: ChatEngine):
    # "insert into merchants ..." (database-admin path) and the merchants
    # natural-language path must read/write the same underlying engine -
    # see chat.py's module docstring.
    chat.handle("s1", "insert into merchants name=Global Mart, country=US, settlement_balance=5000")
    reply = chat.handle("s1", "Show me Global Mart")
    assert reply.data[0]["name"] == "Global Mart"
