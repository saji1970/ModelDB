"""REST and chat surface of event-sourced agent-state streams."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mdc.api.app import create_app
from mdc.api.chat import ChatEngine
from mdc.databases.manager import DatabaseManager
from mdc.schema.loader import load_default_registry
from mdc.security.roles import Role
from mdc.security.tokens import TokenStore
from mdc.storage.duckdb_store import DuckDBStore

KEY = b"\x21" * 32


def build(tmp_path: Path):
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    # An explicit key, so these tests never create ~/.mdc/dna.key
    manager = DatabaseManager(tmp_path / "databases", store, load_default_registry(), event_master_key=KEY)
    tokens = TokenStore(tmp_path / "api_tokens.json")
    return TestClient(create_app(manager, token_store=tokens)), tokens, manager


def as_role(client: TestClient, tokens: TokenStore, role: Role) -> TestClient:
    other = TestClient(client.app)
    other.headers.update({"Authorization": f"Bearer {tokens.issue(f'{role.value}-client', role=role)}"})
    return other


@pytest.fixture
def parts(tmp_path: Path):
    client, tokens, manager = build(tmp_path)
    return client, tokens, manager, as_role(client, tokens, Role.ADMIN)


URL = "/databases/default/streams/agent-1"


def test_create_append_and_read_back(parts):
    _, _, _, admin = parts
    created = admin.post(URL, json={"state": {"mood": "calm", "level": 1}, "note": "boot"})
    assert created.status_code == 201 and created.json()["seq"] == 0
    appended = admin.post(f"{URL}/events", json={"state": {"mood": "busy", "level": 1}, "note": "got work"})
    assert appended.status_code == 201
    assert appended.json()["seq"] == 1 and appended.json()["payload"] == [{"op": "set", "path": ["mood"], "value": "busy"}]
    now = admin.get(URL).json()
    assert now["state"] == {"mood": "busy", "level": 1} and now["seq"] == 1


def test_time_travel_by_timestamp_and_by_event_number(parts):
    _, _, _, admin = parts
    admin.post(URL, json={"state": {"n": 0}, "at": "2026-10-01T10:00:00Z"})
    for n, hour in ((1, 11), (2, 12), (3, 13)):
        admin.post(f"{URL}/events", json={"state": {"n": n}, "at": f"2026-10-01T{hour}:00:00Z"})
    assert admin.get(URL + "?at=2026-10-01T11:30:00Z").json()["state"] == {"n": 1}
    assert admin.get(URL + "?at=2026-10-01T12:00:00Z").json()["state"] == {"n": 2}  # inclusive
    assert admin.get(URL + "?at=2026-10-01T11:30:00").json()["state"] == {"n": 1}  # no zone = UTC
    assert admin.get(URL + "?seq=0").json()["state"] == {"n": 0}
    assert admin.get(URL + "?at=2027-01-01T00:00:00Z").json()["seq"] == 3


def test_history_endpoint(parts):
    _, _, _, admin = parts
    admin.post(URL, json={"state": {"a": 1}, "at": "2026-10-01T10:00:00Z"})
    admin.post(f"{URL}/events", json={"state": {"a": 2}, "note": "bump", "at": "2026-10-01T11:00:00Z"})
    history = admin.get(f"{URL}/history").json()["events"]
    assert [(e["seq"], e["kind"], e["note"]) for e in history] == [(0, "base", None), (1, "event", "bump")]
    assert history[0]["payload"] == {"a": 1}
    assert [e["seq"] for e in admin.get(f"{URL}/history?since=2026-10-01T10:30:00Z").json()["events"]] == [1]
    assert [e["seq"] for e in admin.get(f"{URL}/history?limit=1").json()["events"]] == [1]


def test_list_and_delete(parts):
    _, _, _, admin = parts
    admin.post("/databases/default/streams/b", json={"state": {}})
    admin.post(URL, json={"state": {"a": 1}})
    admin.post(f"{URL}/events", json={"state": {"a": 2}})
    assert admin.get("/databases/default/streams").json()["streams"] == [{"stream": "agent-1", "events": 1}, {"stream": "b", "events": 0}]
    assert admin.delete(URL).status_code == 204
    assert admin.get(URL).status_code == 404
    assert admin.delete(URL).status_code == 404


def test_append_with_a_raw_payload_and_optimistic_concurrency(parts):
    _, _, _, admin = parts
    admin.post(URL, json={"state": {"a": 1}})
    ok = admin.post(f"{URL}/events", json={"payload": [{"op": "set", "path": ["a"], "value": 5}], "expected_seq": 0})
    assert ok.status_code == 201
    stale = admin.post(f"{URL}/events", json={"state": {"a": 6}, "expected_seq": 0})
    assert stale.status_code == 409 and "Read it again" in stale.json()["detail"]


def test_errors_map_to_clear_statuses(parts):
    _, _, _, admin = parts
    assert admin.get("/databases/default/streams/nope").status_code == 404
    assert admin.get("/databases/nodb/streams/x").status_code == 404
    assert admin.post("/databases/default/streams/bad:id", json={"state": {}}).status_code == 422
    admin.post(URL, json={"state": {"a": 1}, "at": "2026-10-01T10:00:00Z"})
    assert admin.post(URL, json={"state": {}}).status_code == 409  # exists
    assert admin.post(f"{URL}/events", json={"state": {"a": 1}}).status_code == 422  # nothing changed
    assert admin.post(f"{URL}/events", json={}).status_code == 422  # neither state nor payload
    assert admin.post(f"{URL}/events", json={"payload": "junk"}).status_code == 422
    assert admin.get(URL + "?at=2020-01-01T00:00:00Z").status_code == 404  # before it existed
    assert admin.get(URL + "?seq=9").status_code == 422
    assert admin.get(URL + "?seq=0&at=2026-10-01T10:00:00Z").status_code == 422
    assert admin.post(f"{URL}/events", json={"state": {"a": 2}, "at": "2999-01-01T00:00:00Z"}).status_code == 422


def test_tampered_storage_is_reported_without_leaking_content(parts):
    _, _, manager, admin = parts
    admin.post(URL, json={"state": {"secret": "hunter2"}})
    admin.post(f"{URL}/events", json={"state": {"secret": "hunter3"}})
    store = manager.get("default").store
    blob = bytearray(store.get("evt:agent-1:000000000001"))
    blob[15] ^= 1
    store.put("evt:agent-1:000000000001", bytes(blob), metadata={"stream": "agent-1", "kind": "event"})
    response = admin.get(URL)
    assert response.status_code == 500
    assert "integrity check" in response.json()["detail"] and "hunter" not in response.text


def test_roles_viewers_read_editors_write_and_everyone_needs_a_token(parts):
    client, tokens, _, admin = parts
    admin.post(URL, json={"state": {"a": 1}})
    viewer, editor = as_role(client, tokens, Role.VIEWER), as_role(client, tokens, Role.EDITOR)
    assert viewer.get(URL).status_code == 200 and viewer.get(f"{URL}/history").status_code == 200
    assert viewer.get("/databases/default/streams").status_code == 200
    assert viewer.post("/databases/default/streams/x", json={"state": {}}).status_code == 403
    assert viewer.post(f"{URL}/events", json={"state": {"a": 2}}).status_code == 403
    assert viewer.delete(URL).status_code == 403
    assert editor.post(f"{URL}/events", json={"state": {"a": 2}}).status_code == 201
    for call in (client.get(URL), client.post(URL, json={"state": {}}), client.get("/databases/default/streams")):
        assert call.status_code == 401


def test_streams_belong_to_their_database(parts):
    _, _, _, admin = parts
    admin.post("/databases", json={"name": "other"})
    admin.post(URL, json={"state": {"a": 1}})
    assert admin.get("/databases/other/streams/agent-1").status_code == 404
    assert admin.get("/databases/other/streams").json()["streams"] == []


def test_what_the_database_file_holds_is_ciphertext(parts):
    _, _, manager, admin = parts
    admin.post(URL, json={"state": {"secret_goal": "acquire the moon"}})
    admin.post(f"{URL}/events", json={"state": {"secret_goal": "acquire mars"}})
    store = manager.get("default").store
    stored = b"".join(store.get(b) for b in store.search(stream="agent-1"))
    assert b"secret_goal" not in stored and b"moon" not in stored and b"mars" not in stored


# -- chat ---------------------------------------------------------------------------------

@pytest.fixture
def chat(parts) -> ChatEngine:
    _, _, manager, _ = parts
    return ChatEngine(manager)


def test_chat_create_update_show_history(chat: ChatEngine):
    assert "Created stream" in chat.handle("s", 'create stream agent-1 with mood=calm, goal="write, then rest", level=3, done=false').message
    assert "Recorded event 1" in chat.handle("s", "update stream agent-1 set mood=busy, memory.last_tool=search").message
    assert "Recorded event 2" in chat.handle("s", "update stream agent-1 remove goal").message

    latest = chat.handle("s", "show stream agent-1")
    assert "latest state" in latest.message
    assert {r["field"]: r["value"] for r in latest.data} == {"mood": "busy", "level": 3, "done": False, "memory": '{"last_tool":"search"}'}

    after_one = chat.handle("s", "show stream agent-1 after event 1")
    assert "after event 1" in after_one.message and {r["field"]: r["value"] for r in after_one.data}["goal"] == "write, then rest"

    history = chat.handle("s", "show history of stream agent-1")
    assert [(r["seq"], r["kind"]) for r in history.data] == [(0, "base"), (1, "event"), (2, "event")]
    assert history.data[1]["change"] == "set mood; set memory.last_tool" and history.data[2]["change"] == "unset goal"
    assert chat.handle("s", "show stream agent-1 history").data == history.data


def test_chat_as_of_a_time(parts):
    _, _, manager, _ = parts
    chat = ChatEngine(manager)
    chat.handle("s", "create stream agent-1 with n=0")
    chat.handle("s", "update stream agent-1 set n=1")
    assert chat.handle("s", "show stream agent-1 as of 1 hour ago").message.startswith('Stream "agent-1" did not exist yet')
    assert {r["field"]: r["value"] for r in chat.handle("s", "show stream agent-1 as of now").data} == {"n": 1}
    assert {r["field"]: r["value"] for r in chat.handle("s", "show stream agent-1 at 2999-01-01").data} == {"n": 1}
    unreadable = chat.handle("s", "show stream agent-1 as of garbage")
    assert "couldn't read 'garbage' as a time" in unreadable.message


def test_chat_json_state_and_list(chat: ChatEngine):
    assert "No streams" in chat.handle("s", "list streams").message
    chat.handle("s", 'create stream a1 with {"mood": "calm", "memory": {"facts": ["x"]}}')
    chat.handle("s", "create stream b-2 as mood=ok")
    listed = chat.handle("s", "list streams")
    assert listed.data == [{"stream": "a1", "events": 0}, {"stream": "b-2", "events": 0}]
    assert {r["field"]: r["value"] for r in chat.handle("s", "show stream a1").data}["memory"] == '{"facts":["x"]}'


@pytest.mark.parametrize("text, expected", [
    ("create stream x with {not json}", "not valid JSON"),
    ("create stream x with [1, 2]", "expected field=value pairs"),
    ("update stream nope set a=1", "No stream named"),
    ("show stream nope", "No stream named"),
    ("update stream x remove", "expected field=value pairs"),
    ("update stream x remove goal, nope!", "which fields to remove"),
])
def test_chat_stream_errors_say_what_is_wrong(chat: ChatEngine, text, expected):
    chat.handle("s", "create stream x with a=1")
    assert expected.lower() in chat.handle("s", text).message.lower()


def test_chat_streams_follow_the_current_database(parts):
    client, _, manager, admin = parts
    chat = ChatEngine(manager)
    chat.handle("s", "create database lab")
    chat.handle("s", "create stream probe with ok=true")
    assert admin.get("/databases/lab/streams/probe").status_code == 200
    assert admin.get("/databases/default/streams/probe").status_code == 404


def test_chat_and_rest_share_one_stream_store(parts):
    _, _, manager, admin = parts
    ChatEngine(manager).handle("s", "create stream shared with a=1")
    assert admin.post("/databases/default/streams/shared/events", json={"state": {"a": 2}}).json()["seq"] == 1
    assert {r["field"]: r["value"] for r in ChatEngine(manager).handle("s", "show stream shared").data} == {"a": 2}


def test_stream_phrases_do_not_swallow_other_sentences(chat: ChatEngine):
    assert "No merchant" in chat.handle("s", "show me the stream of consciousness").message
    assert "Created MER-" in chat.handle("s", "Create a merchant called Streamline Co in India").message
    assert "No streams" in chat.handle("s", "show streams").message
