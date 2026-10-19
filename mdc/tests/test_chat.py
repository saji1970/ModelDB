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


@pytest.mark.parametrize("text", ["show objects", "list objects", "show all files"])
def test_listing_every_object_is_not_read_as_a_merchant_lookup(chat: ChatEngine, service: ObjectService, text: str):
    service.upload(b"# Doc\n\nBody.", filename="a.md")
    service.upload(b"log line\n" * 3, filename="a.log")
    reply = chat.handle("s1", text)
    assert reply.message == "2 object(s)." and "merchant" not in reply.message.lower()
    assert len(reply.data) == 2


def test_listing_every_object_when_there_are_none(chat: ChatEngine):
    assert chat.handle("s1", "show objects").message == "0 object(s)."


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


# -- a pending clarification must not trap the whole chat ------------------------

def _ask_ambiguous_balance(chat: ChatEngine) -> str:
    reply = chat.handle("s1", "total balance of all merchants")
    assert "clarify" in reply.message
    return reply.message


def test_pending_clarification_traps_unrelated_prompts_until_cancelled(chat: ChatEngine):
    # Reproduces "every prompt returns the same response": once the merchants
    # pipeline asks which balance is meant, a free-form prompt is read as an
    # answer to that question and gets the identical re-ask back.
    _ask_ambiguous_balance(chat)
    first = chat.handle("s1", "hello").message
    second = chat.handle("s1", "how are you").message
    assert first == second
    assert "didn't understand that answer" in first
    assert 'Say "cancel"' in first  # and it now says how to leave


def test_cancel_drops_the_pending_question(chat: ChatEngine):
    _ask_ambiguous_balance(chat)
    reply = chat.handle("s1", "cancel")
    assert "dropped" in reply.message
    # The same free-form prompt no longer gets the re-ask.
    after = chat.handle("s1", "hello").message
    assert "didn't understand that answer" not in after


def test_reset_clears_pending_question_like_the_cli(chat: ChatEngine):
    _ask_ambiguous_balance(chat)
    assert "cleared" in chat.handle("s1", "/reset").message
    assert "didn't understand that answer" not in chat.handle("s1", "hello").message


def test_cancel_with_nothing_pending_is_harmless(chat: ChatEngine):
    assert "Nothing is pending" in chat.handle("s1", "never mind").message


def test_cancel_also_drops_a_pending_delete_confirmation(chat: ChatEngine):
    chat.handle("s1", "Create a merchant called Acme in India")
    assert "(yes/no)" in chat.handle("s1", "Delete merchant Acme").message
    assert "dropped" in chat.handle("s1", "cancel").message
    # "yes" now is not read as confirming the delete.
    chat.handle("s1", "yes")
    shown = chat.handle("s1", "Show Acme")
    assert [row["name"] for row in shown.data] == ["Acme"]  # still there


# -- export / import phrasing ----------------------------------------------------------

@pytest.mark.parametrize("text, fmt, scope, database, table", [
    ("export database as csv", "csv", None, None, None),
    ("download the database as excel", "xlsx", None, None, None),
    ("export database default as xls", "xlsx", None, "default", None),
    ("export schema as markdown", "md", "schema", None, None),
    ("export design document", "md", "schema", None, None),
    ("export schema as er diagram", "er", "schema", None, None),
    ("export database with data", None, "all", None, None),
    ("export data and structure", None, "all", None, None),
    ("export data only as sql", "sql", "data", None, None),
    ("export table merchants as csv", "csv", None, None, "merchants"),
    ("export database sales as sql script", "sql", None, "sales", None),
    ("Please export everything", None, "all", None, None),
])
def test_export_phrasings(text, fmt, scope, database, table):
    from mdc.nlp.db_command import DatabaseIntent, parse_database_command

    command = parse_database_command(text)
    assert command is not None and command.intent == DatabaseIntent.EXPORT
    assert (command.export_format, command.export_scope, command.database_name, command.table_name) == (fmt, scope, database, table)


@pytest.mark.parametrize("text", ["download report", "export nothing here", "downloading is slow", "important notes"])
def test_other_sentences_are_not_taken_for_an_export(text):
    from mdc.nlp.db_command import parse_database_command

    assert parse_database_command(text) is None


def test_bare_export_and_import_explain_how_to_use_them(chat: ChatEngine):
    for text in ("export", "import", "import a csv"):
        reply = chat.handle("s1", text)
        assert "paperclip" in reply.message and "export database as csv" in reply.message
        assert reply.download is None


def test_export_of_a_database_with_no_tables_says_so(chat: ChatEngine):
    chat.handle("s1", "create database empty")
    assert "no tables yet" in chat.handle("s1", "export database as sql").message


# -- building a schema from a prompt or an ER diagram -----------------------------------

DESIGN_PROMPT = "create database shop with customers(name, email, joined), orders(customer_id, total decimal, paid)"


def test_design_prompt_is_previewed_and_creates_nothing_until_yes(chat: ChatEngine, manager: DatabaseManager):
    reply = chat.handle("s1", DESIGN_PROMPT)
    assert 'new database "shop"' in reply.message
    assert "customers: name string, email string, joined datetime" in reply.message
    assert "orders: customer_id string, total decimal, paid boolean" in reply.message
    assert "Guessed from the field names" in reply.message and "joined -> datetime" in reply.message
    assert not manager.exists("shop")

    created = chat.handle("s1", "yes")
    assert "Imported into new database" in created.message
    assert manager.get("shop").schema_registry.list_collections() == ["customers", "orders"]
    assert chat.session_database("s1") == "shop"  # and the session moved into it
    assert chat.handle("s1", "insert into customers name=Ada").message.startswith("Inserted")


def test_design_no_drops_the_proposal(chat: ChatEngine, manager: DatabaseManager):
    chat.handle("s1", DESIGN_PROMPT)
    assert "nothing was created" in chat.handle("s1", "no").message
    chat.handle("s1", "yes")  # nothing is pending any more
    assert not manager.exists("shop")


def test_anything_else_drops_the_proposal_and_runs_as_a_new_request(chat: ChatEngine, manager: DatabaseManager):
    chat.handle("s1", DESIGN_PROMPT)
    assert "database(s)" in chat.handle("s1", "list databases").message  # ran as a normal command
    chat.handle("s1", "yes")
    assert not manager.exists("shop")


def test_cancel_drops_a_pending_design(chat: ChatEngine, manager: DatabaseManager):
    chat.handle("s1", DESIGN_PROMPT)
    assert "dropped" in chat.handle("s1", "cancel").message
    chat.handle("s1", "yes")
    assert not manager.exists("shop")


def test_design_adds_tables_to_the_current_database_when_none_is_named(chat: ChatEngine, manager: DatabaseManager):
    chat.handle("s1", "create database lab")
    reply = chat.handle("s1", "create tables samples(label, weight, taken_at)")
    assert 'database "lab"' in reply.message and "new database" not in reply.message
    chat.handle("s1", "yes")
    assert manager.get("lab").schema_registry.list_collections() == ["samples"]


def test_design_conflict_is_reported_not_proposed(chat: ChatEngine):
    reply = chat.handle("s1", "create tables merchants(name)")
    assert "already exists" in reply.message and "Create it?" not in reply.message


def test_design_prompts_do_not_swallow_other_sentences(chat: ChatEngine):
    assert "Created MER-" in chat.handle("s1", "Create a merchant called Foo (Bar) in India").message
    assert 'Created database "plain"' in chat.handle("s1", "create database plain").message
    assert "Create it?" not in chat.handle("s1", "create table x with a string, b integer").message


def test_er_diagram_pasted_into_chat_is_previewed_then_created(chat: ChatEngine, manager: DatabaseManager):
    # Mermaid wants one attribute per line, so lay it out that way
    diagram = "create the schema for database library from this:\nerDiagram\n  AUTHOR ||--o{ BOOK : writes\n  AUTHOR {\n    string name PK\n  }\n  BOOK {\n    string title\n  }\n"
    reply = chat.handle("s1", diagram)
    assert 'new database "library"' in reply.message
    assert "author: name string*" in reply.message and "book: title string, author_id string" in reply.message
    chat.handle("s1", "yes")
    assert manager.get("library").schema_registry.list_collections() == ["author", "book"]


def test_bad_er_diagram_in_chat_explains_what_is_wrong(chat: ChatEngine):
    reply = chat.handle("s1", "build this\nerDiagram\n  A {\n    ???\n  }\n")
    assert "couldn't read that ER diagram" in reply.message and "cannot read the attribute" in reply.message


def test_roles_limit_what_a_chat_prompt_may_create(manager: DatabaseManager):
    from mdc.security.roles import Role

    engine = ChatEngine(manager)
    refused = engine.handle("e", DESIGN_PROMPT, role=Role.EDITOR)  # editors cannot create databases
    assert "create_database" in refused.message and "Create it?" not in refused.message
    assert "create_database" in engine.handle("e", "create database viachat", role=Role.EDITOR).message
    assert not manager.exists("viachat")
    # ...but may add tables to an existing database
    assert "Create it?" in engine.handle("e", "create tables extra(name)", role=Role.EDITOR).message
    # a database admin may do both
    assert "Create it?" in engine.handle("d", DESIGN_PROMPT, role=Role.DB_ADMIN).message
    assert 'Created database "viachat2"' in engine.handle("d", "create database viachat2", role=Role.DB_ADMIN).message
