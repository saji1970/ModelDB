"""Phase I: the Universal Object REST API (CLAUDE-STORAGE.md sections
34-35, 41, required tests in section 48's API category, now exercised
over real HTTP instead of calling the router directly).
"""

import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mdc.api.app import create_app
from mdc.databases.manager import DatabaseManager
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 40


def _safetensors(tensors: dict[str, tuple[list[int], bytes]]) -> bytes:
    header = {}
    body = b""
    offset = 0
    for name, (shape, data) in tensors.items():
        header[name] = {"dtype": "F32", "shape": shape, "data_offsets": [offset, offset + len(data)]}
        body += data
        offset += len(data)
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + body


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    manager = DatabaseManager(tmp_path / "databases", store, load_default_registry())
    return TestClient(create_app(manager))


# -- section 48 required tests: upload / retrieve / delete, now over HTTP -------

def test_upload_object(client: TestClient):
    response = client.post("/objects?filename=notes.txt", content=b"hello from the API")
    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "DOCUMENT"
    assert "object_id" in body


def test_retrieve_object(client: TestClient):
    upload = client.post("/objects?filename=notes.txt", content=b"payload content")
    object_id = upload.json()["object_id"]

    metadata = client.get(f"/objects/{object_id}")
    assert metadata.status_code == 200
    assert metadata.json()["object_id"] == object_id

    read = client.post(f"/objects/{object_id}/read")
    assert read.status_code == 200
    assert read.content == b"payload content"


def test_delete_object(client: TestClient):
    upload = client.post("/objects?filename=notes.txt", content=b"delete me")
    object_id = upload.json()["object_id"]

    delete = client.delete(f"/objects/{object_id}")
    assert delete.status_code == 204

    assert client.get(f"/objects/{object_id}").status_code == 404


# -- 404s for everything that should 404 -------------------------------------------

def test_unknown_object_returns_404_everywhere(client: TestClient):
    assert client.get("/objects/nope").status_code == 404
    assert client.post("/objects/nope/read").status_code == 404
    assert client.delete("/objects/nope").status_code == 404
    assert client.post("/objects/nope/move", json={"tier": "HOT"}).status_code == 404
    assert client.post("/objects/nope/optimize").status_code == 404
    assert client.get("/objects/nope/strategy").status_code == 404


# -- move / optimize / explain ------------------------------------------------------

def test_move_changes_tier(client: TestClient):
    upload = client.post("/objects?filename=notes.txt", content=b"movable content")
    object_id = upload.json()["object_id"]

    moved = client.post(f"/objects/{object_id}/move", json={"tier": "HOT"})
    assert moved.status_code == 200
    assert moved.json()["storage_tier"] == "HOT"
    assert client.get(f"/objects/{object_id}").json()["storage_tier"] == "HOT"


def test_optimize_without_access_reverts_to_policy_default(client: TestClient):
    upload = client.post("/objects?filename=notes.txt", content=b"optimizable content")
    object_id = upload.json()["object_id"]
    client.post(f"/objects/{object_id}/move", json={"tier": "HOT"})

    result = client.post(f"/objects/{object_id}/optimize")
    assert result.status_code == 200
    assert result.json()["changed"] is True
    assert result.json()["storage_tier"] == "WARM"


def test_optimize_with_reported_high_access_keeps_hot(client: TestClient):
    upload = client.post("/objects?filename=notes.txt", content=b"frequently used content")
    object_id = upload.json()["object_id"]
    client.post(f"/objects/{object_id}/move", json={"tier": "HOT"})

    result = client.post(f"/objects/{object_id}/optimize", json={"access_frequency": 100.0})
    assert result.status_code == 200
    assert result.json()["changed"] is False
    assert result.json()["storage_tier"] == "HOT"


def test_explain_returns_a_real_explanation_matching_the_recorded_decision(client: TestClient):
    upload = client.post("/objects?filename=data.log", content=b"2026-01-01 00:00:00 INFO x\n" * 10)
    object_id = upload.json()["object_id"]

    explanation = client.get(f"/objects/{object_id}/strategy")
    assert explanation.status_code == 200
    body = explanation.json()
    assert body["storage_tier"] in ("HOT", "WARM", "COLD", "ARCHIVE")
    assert body["compression"] in body["explanation"] or "uncompressed" in body["explanation"]


# -- PUT replace --------------------------------------------------------------------

def test_put_replaces_content_and_reclassifies(client: TestClient):
    upload = client.post("/objects?filename=notes.txt", content=b"original text")
    object_id = upload.json()["object_id"]

    png = _png(64, 64)
    replaced = client.put(f"/objects/{object_id}?filename=photo.png", content=png)
    assert replaced.status_code == 200
    assert replaced.json()["type"] == "IMAGE"

    read = client.post(f"/objects/{object_id}/read")
    assert read.content == png


# -- image upload dispatches to ImageStore -----------------------------------------

def test_upload_image_returns_real_dimensions(client: TestClient):
    response = client.post("/objects?filename=photo.png", content=_png(200, 150))
    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "IMAGE"
    assert body["format"] == "png"
    assert body["width"] == 200
    assert body["height"] == 150


# -- model upload dispatches to ModelStore, tensor-level retrieval works --------

def test_upload_model_and_retrieve_manifest_and_tensor(client: TestClient):
    w = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    content = _safetensors({"layer_0.q": ([4], w)})

    upload = client.post("/objects?filename=model.safetensors", content=content)
    assert upload.status_code == 201
    body = upload.json()
    assert body["type"] == "AI_MODEL"
    assert body["tensor_count"] == 1
    assert body["total_parameters"] == 4
    model_id = body["object_id"]

    manifest = client.get(f"/models/{model_id}")
    assert manifest.status_code == 200
    assert manifest.json()["model_id"] == model_id
    assert manifest.json()["tensor_count"] == 1

    tensor = client.get(f"/models/{model_id}/tensors/layer_0.q")
    assert tensor.status_code == 200
    assert tensor.content == w


def test_unknown_model_and_tensor_return_404(client: TestClient):
    assert client.get("/models/NOPE-0000000000").status_code == 404

    w = struct.pack("<2f", 1.0, 2.0)
    content = _safetensors({"w": ([2], w)})
    upload = client.post("/objects?filename=model.safetensors", content=content)
    model_id = upload.json()["object_id"]

    assert client.get(f"/models/{model_id}/tensors/does.not.exist").status_code == 404


def test_move_on_a_model_cascades_to_every_tensor_block(client: TestClient):
    q = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    k = struct.pack("<256f", *range(256))
    content = _safetensors({"layer_0.q": ([4], q), "layer_0.k": ([64, 4], k)})

    upload = client.post("/objects?filename=model.safetensors", content=content)
    model_id = upload.json()["object_id"]

    moved = client.post(f"/objects/{model_id}/move", json={"tier": "HOT"})
    assert moved.status_code == 200
    body = moved.json()
    assert body["storage_tier"] == "HOT"
    assert body["objects_moved"] >= 3  # manifest + at least one block per tensor

    # The manifest's own metadata reflects the move too.
    assert client.get(f"/objects/{model_id}").json()["storage_tier"] == "HOT"

    # And the tensor content is still correct after moving.
    tensor = client.get(f"/models/{model_id}/tensors/layer_0.q")
    assert tensor.content == q


def test_move_on_a_model_stuck_at_hot_after_a_restart_is_a_clean_404_not_a_500(tmp_path: Path):
    # Reproduces the exact crash found by hand: a model at HOT whose
    # in-memory backend was emptied (simulating a process restart -
    # `MemoryStorageBackend` is deliberately not persisted) used to raise
    # an unhandled KeyError instead of a clean, catchable error.
    from mdc.storage.memory_store import MemoryStorageBackend
    from mdc.storage_intelligence.strategy import StorageTier

    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    manager = DatabaseManager(tmp_path / "databases", store, load_default_registry())
    client = TestClient(create_app(manager))

    q = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    content = _safetensors({"layer_0.q": ([4], q)})
    upload = client.post("/objects?filename=model.safetensors", content=content)
    model_id = upload.json()["object_id"]
    client.post(f"/objects/{model_id}/move", json={"tier": "HOT"})

    router = manager.get("default").router
    router.backends[StorageTier.HOT] = MemoryStorageBackend()  # simulate a restart

    read = client.post(f"/objects/{model_id}/read")
    assert read.status_code == 404
    assert "restart" in read.json()["detail"].lower()

    move_again = client.post(f"/objects/{model_id}/move", json={"tier": "WARM"})
    assert move_again.status_code == 404  # not 500 - a clean, actionable error


def test_non_safetensors_ai_model_extension_falls_back_to_generic_storage(client: TestClient):
    # Classified as AI_MODEL by extension (.pt), but not real safetensors
    # content - upload() must fall through to generic storage rather than error.
    response = client.post("/objects?filename=weights.pt", content=b"not actually a real checkpoint")
    assert response.status_code == 201
    assert "object_id" in response.json()


# -- the Storage Explorer's browser UI: GET /objects, POST /chat, GET / --------

def test_root_serves_the_explorer_html(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "MDC Storage Explorer" in response.text


def test_list_objects_endpoint(client: TestClient):
    client.post("/objects?filename=a.md", content=b"# Doc\n\nBody.")
    client.post("/objects?filename=photo.png", content=_png(10, 10))

    all_objects = client.get("/objects").json()["objects"]
    assert len(all_objects) == 2

    documents = client.get("/objects?type=DOCUMENT").json()["objects"]
    assert len(documents) == 1
    assert documents[0]["type"] == "DOCUMENT"


def test_chat_endpoint_lists_and_inspects(client: TestClient):
    log_lines = b"2026-01-01 00:00:00 INFO line\n" * 5
    upload = client.post("/objects?filename=a.log", content=log_lines)
    object_id = upload.json()["object_id"]

    list_reply = client.post("/chat", json={"message": "list logs"})
    assert list_reply.status_code == 200
    assert "1 LOG" in list_reply.json()["message"]

    inspect_reply = client.post("/chat", json={"message": f"show {object_id}"})
    assert object_id in inspect_reply.json()["message"]


def test_chat_endpoint_delete_confirmation_flow(client: TestClient):
    upload = client.post("/objects?filename=a.log", content=b"log line\n" * 5)
    object_id = upload.json()["object_id"]

    ask = client.post("/chat", json={"message": f"delete {object_id}", "session_id": "http-session"})
    assert "yes/no" in ask.json()["message"]

    confirm = client.post("/chat", json={"message": "yes", "session_id": "http-session"})
    assert "deleted" in confirm.json()["message"].lower()
    assert client.get(f"/objects/{object_id}").status_code == 404


# -- browsing a non-default database's tables/rows over HTTP --------------------

def test_list_databases_endpoint(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    names = client.get("/databases").json()["databases"]
    assert "default" in names
    assert "mytest" in names


def test_create_database_endpoint_rejects_duplicate(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    duplicate = client.post("/databases", json={"name": "mytest"})
    assert duplicate.status_code == 409


def test_database_tables_endpoint_is_empty_for_a_fresh_database(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    tables = client.get("/databases/mytest/tables").json()["tables"]
    assert tables == []


def test_database_tables_endpoint_unknown_database_404s(client: TestClient):
    assert client.get("/databases/nope/tables").status_code == 404


def test_browse_a_table_created_and_populated_via_chat(client: TestClient):
    session_id = "browse-session"
    client.post("/databases", json={"name": "mytest"})
    client.post("/chat", json={"message": "use database mytest", "session_id": session_id})
    client.post("/chat", json={"message": "create table products with sku string, price decimal", "session_id": session_id})
    client.post("/chat", json={"message": "insert into products sku=ABC123, price=9.99", "session_id": session_id})

    tables = client.get("/databases/mytest/tables").json()["tables"]
    assert tables == [{"name": "products", "fields": 2}]

    table = client.get("/databases/mytest/tables/products").json()
    field_names = {f["name"] for f in table["fields"]}
    assert field_names == {"sku", "price"}

    rows = client.get("/databases/mytest/tables/products/rows").json()
    assert rows["count"] == 1
    assert rows["rows"][0]["sku"] == "ABC123"


def test_table_endpoints_unknown_table_404(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    assert client.get("/databases/mytest/tables/nope").status_code == 404
    assert client.get("/databases/mytest/tables/nope/rows").status_code == 404


# -- structured REST endpoints for external NLU/integration callers -------------
# (a caller that already extracted structured intent - a RASA custom action,
# a hand-rolled UI - shouldn't have to reconstruct chat's sentence syntax)

def test_create_table_via_rest(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    response = client.post(
        "/databases/mytest/tables/products", json={"fields": {"sku": "string", "price": "decimal"}}
    )
    assert response.status_code == 201
    assert response.json() == {"database": "mytest", "table": "products", "fields": ["sku", "price"]}

    fields = client.get("/databases/mytest/tables/products").json()["fields"]
    assert {f["name"] for f in fields} == {"sku", "price"}


def test_create_table_via_rest_rejects_unknown_field_type(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    response = client.post("/databases/mytest/tables/products", json={"fields": {"sku": "not-a-real-type"}})
    assert response.status_code == 422


def test_create_table_via_rest_duplicate_is_409(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    client.post("/databases/mytest/tables/products", json={"fields": {"sku": "string"}})
    response = client.post("/databases/mytest/tables/products", json={"fields": {"sku": "string"}})
    assert response.status_code == 409


def test_create_table_via_rest_unknown_database_404(client: TestClient):
    response = client.post("/databases/nope/tables/products", json={"fields": {"sku": "string"}})
    assert response.status_code == 404


def test_insert_row_via_rest(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    client.post("/databases/mytest/tables/products", json={"fields": {"sku": "string", "price": "decimal"}})

    response = client.post(
        "/databases/mytest/tables/products/rows", json={"values": {"sku": "ABC123", "price": "9.99"}}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["sku"] == "ABC123"
    assert "record_id" in body

    rows = client.get("/databases/mytest/tables/products/rows").json()["rows"]
    assert rows[0]["sku"] == "ABC123"


def test_insert_row_via_rest_unknown_table_404(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    response = client.post("/databases/mytest/tables/nope/rows", json={"values": {"a": "b"}})
    assert response.status_code == 404


def test_find_endpoint_matches_a_price_constrained_term_across_databases(client: TestClient):
    client.post("/databases", json={"name": "mytest"})
    client.post("/databases/mytest/tables/products", json={"fields": {"name": "string", "price": "decimal"}})
    client.post("/databases/mytest/tables/products/rows", json={"values": {"name": "Widget", "price": "9.99"}})
    client.post("/databases/mytest/tables/products/rows", json={"values": {"name": "Gadget", "price": "25000"}})

    response = client.get("/find", params={"q": "widget", "max": 20000})
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["name"] == "Widget"


def test_find_endpoint_scoped_to_one_database_and_table(client: TestClient):
    client.post("/databases", json={"name": "db_a"})
    client.post("/databases/db_a/tables/products", json={"fields": {"name": "string"}})
    client.post("/databases/db_a/tables/products/rows", json={"values": {"name": "Widget"}})
    client.post("/databases", json={"name": "db_b"})
    client.post("/databases/db_b/tables/products", json={"fields": {"name": "string"}})
    client.post("/databases/db_b/tables/products/rows", json={"values": {"name": "Widget"}})

    response = client.get("/find", params={"q": "widget", "database": "db_a"})
    results = response.json()["results"]
    assert all(r["database"] == "db_a" for r in results)


def test_find_endpoint_no_matches_is_still_200(client: TestClient):
    response = client.get("/find", params={"q": "nonexistent-term-xyz"})
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_cors_allows_a_cross_origin_request(client: TestClient):
    # A third-party NLU/custom UI calling from a different origin (a
    # RASA action server, a chat UI hosted elsewhere) needs this header
    # present, or the browser rejects the response before it ever
    # reaches the caller's own code.
    response = client.options(
        "/chat",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
    )
    assert response.headers.get("access-control-allow-origin") == "*"


# -- the exact concurrency shape that exposed the DuckDBStore threading bug -----

def test_concurrent_metadata_and_strategy_requests_agree(client: TestClient):
    # Reproduces the exact shape of the bug found by hand in the Explorer
    # UI: two DIFFERENT endpoints reading the SAME object, fired truly
    # concurrently (not one batch after another) via a mix of futures on
    # one pool - this is what `Promise.all([...])` does from the browser.
    import concurrent.futures

    upload = client.post("/objects?filename=a.log", content=b"log line\n" * 5)
    object_id = upload.json()["object_id"]

    def fetch_metadata() -> int:
        return client.get(f"/objects/{object_id}").status_code

    def fetch_strategy() -> int:
        return client.get(f"/objects/{object_id}/strategy").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for _ in range(20):
            futures.append(pool.submit(fetch_metadata))
            futures.append(pool.submit(fetch_strategy))
        codes = [f.result() for f in futures]

    assert all(code == 200 for code in codes)
