"""The Data Engine's record fast path: it must give exactly the answers the
generic block path gives, migrate old data safely, and be much cheaper."""

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mdc.engine.data_engine import MDCDataEngine
from mdc.engine.errors import DuplicateRecordError, InvalidRecordIdError, RecordNotFoundError
from mdc.model.operation import CountOperation, CreateOperation, DeleteOperation, Filter, ReadOperation, Sort, UpdateOperation
from mdc.model.record import Record
from mdc.schema.registry import CollectionNotFoundError, FieldSchema, SchemaRegistry, SchemaValidationError
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage.interface import RecordBackend
from mdc.storage.memory_store import MemoryStorageBackend

WEIRD = 'we"ird name'


def registry() -> SchemaRegistry:
    r = SchemaRegistry()
    r.create_collection(
        "t",
        {
            "name": FieldSchema("name", "string", required=True),
            "country": FieldSchema("country", "string"),
            "amount": FieldSchema("amount", "decimal"),
            "count": FieldSchema("count", "integer"),
            "active": FieldSchema("active", "boolean"),
            "when": FieldSchema("when", "datetime"),
            "a b.c": FieldSchema("a b.c", "string"),
            WEIRD: FieldSchema(WEIRD, "string"),
        },
    )
    return r


@pytest.fixture
def fast(tmp_path: Path):
    store = DuckDBStore(tmp_path / "f.duckdb")
    store.init_schema()
    yield MDCDataEngine(store, registry())
    store.close()


@pytest.fixture
def generic() -> MDCDataEngine:
    return MDCDataEngine(MemoryStorageBackend(), registry())


def rows(n: int = 300) -> list[dict]:
    rnd = random.Random(7)
    out = []
    for i in range(n):
        row = {"name": f"Name {i % 40:02d} é{i}"}
        if rnd.random() < 0.85:
            row["country"] = rnd.choice(["IN", "US", "GB", "DE", "", "in", "10", "5"])
        if rnd.random() < 0.8:
            row["amount"] = round(rnd.uniform(-50, 1000), 2)
        if rnd.random() < 0.7:
            row["count"] = rnd.randint(-5, 25)
        if rnd.random() < 0.5:
            row["active"] = rnd.random() < 0.5
        if rnd.random() < 0.3:
            row["when"] = datetime(2026, 1, 1 + i % 28, tzinfo=timezone.utc)
        if rnd.random() < 0.3:
            row["a b.c"] = rnd.choice(["x", "y"])
        if rnd.random() < 0.3:
            row[WEIRD] = rnd.choice(["p", "q"])
        out.append(row)
    return out


def load(engine: MDCDataEngine, data: list[dict]) -> None:
    for i, row in enumerate(data):
        engine.create(CreateOperation(collection="t", data=row, record_id=f"R{i:04d}"))


def view(result):
    return [(r.record_id, r.version, r.fields, r.checksum) for r in result.records]


FILTERS = [
    [],
    [Filter(field="country", operator="=", value="IN")],
    [Filter(field="country", operator="=", value="10")],
    [Filter(field="country", operator="=", value=10)],
    [Filter(field="count", operator="=", value=5)],
    [Filter(field="count", operator="=", value=5.0)],
    [Filter(field="amount", operator=">", value=500)],
    [Filter(field="amount", operator="<", value=0)],
    [Filter(field="amount", operator=">=", value=100.5), Filter(field="amount", operator="<=", value=200)],
    [Filter(field="count", operator="BETWEEN", value=[0, 10])],
    [Filter(field="name", operator="BETWEEN", value=["Name 10", "Name 20"])],
    [Filter(field="name", operator=">", value="Name 30")],
    [Filter(field="country", operator="IN", value=["IN", "US"])],
    [Filter(field="count", operator="IN", value=[1, 2, 3])],
    [Filter(field="country", operator="IN", value=["IN", 5])],
    [Filter(field="country", operator="!=", value="IN")],
    [Filter(field="country", operator="NOT IN", value=["IN", "US"])],
    [Filter(field="name", operator="LIKE", value="%name 1%")],
    [Filter(field="country", operator="IS NULL")],
    [Filter(field="country", operator="IS NOT NULL")],
    [Filter(field="active", operator="=", value=True)],
    [Filter(field="a b.c", operator="=", value="x")],
    [Filter(field=WEIRD, operator="IN", value=["p"])],
    [Filter(field="nope", operator="=", value="x")],
    [Filter(field="country", operator="=", value="IN"), Filter(field="amount", operator=">", value=100), Filter(field="count", operator="<", value=20)],
    [Filter(field="name", operator="=", value="Name 07 é7")],
    [Filter(field="amount", operator=">", value=1e12)],
]


@pytest.mark.parametrize("filters", FILTERS, ids=lambda f: "|".join(f"{x.field}{x.operator}{x.value}" for x in f) or "all")
def test_the_fast_path_answers_exactly_like_the_generic_one(fast, generic, filters):
    data = rows()
    load(fast, data)
    load(generic, data)
    try:
        expected = generic.read(ReadOperation(collection="t", filters=filters))
    except TypeError as exc:
        # the generic path raises on a type clash (a string where a number was compared);
        # the fast path must not return anything the generic one would have refused
        with pytest.raises(TypeError):
            fast.read(ReadOperation(collection="t", filters=filters))
        return
    got = fast.read(ReadOperation(collection="t", filters=filters))
    assert sorted(r.record_id for r in got.records) == sorted(r.record_id for r in expected.records)
    assert {r.record_id: (r.fields, r.version, r.checksum) for r in got.records} == {r.record_id: (r.fields, r.version, r.checksum) for r in expected.records}
    assert fast.count(CountOperation(collection="t", filters=filters)).count == expected.count


def test_insertion_order_sort_and_limit_agree(fast, generic):
    data = rows(120)
    load(fast, data)
    load(generic, data)
    assert [r.record_id for r in fast.read(ReadOperation(collection="t", filters=[])).records] == [f"R{i:04d}" for i in range(120)]
    for sorts in ([Sort(field="amount", direction="DESC")], [Sort(field="country", direction="ASC"), Sort(field="count", direction="DESC")]):
        for limit in (None, 7):
            a = fast.read(ReadOperation(collection="t", filters=[], sort=sorts, limit=limit))
            b = generic.read(ReadOperation(collection="t", filters=[], sort=sorts, limit=limit))
            assert [r.record_id for r in a.records] == [r.record_id for r in b.records]
    only = fast.read(ReadOperation(collection="t", filters=[], limit=5))
    assert [r.record_id for r in only.records] == [f"R{i:04d}" for i in range(5)]


def test_no_true_match_is_ever_excluded_by_the_database_prefilter(fast):
    rnd = random.Random(3)
    data = rows(400)
    load(fast, data)
    every = {r.record_id: r.fields for r in fast.read(ReadOperation(collection="t", filters=[])).records}
    from mdc.engine.data_engine import _matches

    for _ in range(300):
        f = rnd.choice([x for x in FILTERS if x and not any(y.operator in ("LIKE",) for y in x)])
        wanted = set()
        try:
            wanted = {rid for rid, fields in every.items() if _matches(fields, f)}
        except TypeError:
            continue
        rows_, _ = fast.storage.record_query("t", f)
        assert wanted <= {r[0] for r in rows_}


def test_values_survive_the_round_trip(fast, generic):
    when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    data = {"name": "é€𝄞 \"q\" \\", "amount": 12345678.9, "count": -3, "active": False, "when": when, "a b.c": "ok"}
    made = fast.create(CreateOperation(collection="t", data=data, record_id="X1")).records[0]
    generic.create(CreateOperation(collection="t", data=data, record_id="X1"))
    got = fast.get_record("t", "X1")
    assert got.fields == generic.get_record("t", "X1").fields  # the same stored form as before, datetimes included
    assert got.fields["when"].startswith("2026-03-04T05:06:07") and got.fields["name"] == data["name"] and got.fields["amount"] == 12345678.9
    assert got.version == 1 and got.checksum == made.checksum and got.created_at == made.created_at.replace(microsecond=made.created_at.microsecond)
    assert got.created_at.tzinfo is not None and got.updated_at >= got.created_at


def test_get_record_finds_by_id(fast, generic):
    for engine in (fast, generic):
        engine.create(CreateOperation(collection="t", data={"name": "a"}, record_id="A1"))
        assert engine.get_record("t", "A1").fields == {"name": "a"}
        assert engine.get_record("t", "missing") is None
        with pytest.raises(CollectionNotFoundError):
            engine.get_record("nope", "A1")


def test_update_keeps_the_record_in_place_and_bumps_the_version(fast):
    load(fast, rows(5))
    before = [r.record_id for r in fast.read(ReadOperation(collection="t", filters=[])).records]
    fast.update(UpdateOperation(collection="t", filters=[Filter(field="name", operator="=", value=fast.get_record("t", "R0001").fields["name"])], data={"country": "ZZ"}))
    after = fast.read(ReadOperation(collection="t", filters=[]))
    assert [r.record_id for r in after.records] == before
    r1 = fast.get_record("t", "R0001")
    assert r1.version == 2 and r1.fields["country"] == "ZZ" and r1.updated_at >= r1.created_at


def test_delete_count_and_drop(fast):
    load(fast, rows(20))
    name = fast.get_record("t", "R0003").fields["name"]
    fast.delete(DeleteOperation(collection="t", filters=[Filter(field="name", operator="=", value=name)]))
    assert fast.get_record("t", "R0003") is None and fast.count_records("t") == 19
    fast.delete_record("t", "R0004")
    with pytest.raises(RecordNotFoundError):
        fast.delete_record("t", "R0004")
    assert fast.count_records("t") == 18 and fast.count(CountOperation(collection="t", filters=[])).count == 18
    assert fast.drop_collection("t") == 18
    assert not fast.schema.has_collection("t")


def test_duplicate_and_bad_ids(fast):
    fast.create(CreateOperation(collection="t", data={"name": "a"}, record_id="A1"))
    with pytest.raises(DuplicateRecordError):
        fast.create(CreateOperation(collection="t", data={"name": "b"}, record_id="A1"))
    with pytest.raises(InvalidRecordIdError):
        fast.create(CreateOperation(collection="t", data={"name": "b"}, record_id="bad id!"))
    assert fast.count_records("t") == 1


# -- create_many ---------------------------------------------------------------------


def test_create_many_inserts_everything_in_order(fast):
    made = fast.create_many([CreateOperation(collection="t", data={"name": f"n{i}"}, record_id=f"B{i:05d}") for i in range(2500)])
    assert len(made) == 2500 and fast.count_records("t") == 2500
    got = fast.read(ReadOperation(collection="t", filters=[], limit=3))
    assert [r.record_id for r in got.records] == ["B00000", "B00001", "B00002"]
    assert fast.get_record("t", "B02499").fields == {"name": "n2499"}


def test_create_many_writes_nothing_if_any_row_is_wrong(fast):
    fast.create(CreateOperation(collection="t", data={"name": "old"}, record_id="OLD"))
    cases = {
        "invalid": [CreateOperation(collection="t", data={"name": "ok"}, record_id="N1"), CreateOperation(collection="t", data={"country": "no name"}, record_id="N2")],
        "repeats an id": [CreateOperation(collection="t", data={"name": "a"}, record_id="N1"), CreateOperation(collection="t", data={"name": "b"}, record_id="N1")],
        "reuses an existing id": [CreateOperation(collection="t", data={"name": "a"}, record_id="N1"), CreateOperation(collection="t", data={"name": "b"}, record_id="OLD")],
    }
    errors = {"invalid": SchemaValidationError, "repeats an id": DuplicateRecordError, "reuses an existing id": DuplicateRecordError}
    for label, ops in cases.items():
        with pytest.raises(errors[label]):
            fast.create_many(ops)
        assert fast.count_records("t") == 1, label


def test_create_many_works_on_the_generic_backend_too(generic):
    generic.create_many([CreateOperation(collection="t", data={"name": f"n{i}"}, record_id=f"G{i}") for i in range(50)])
    assert generic.count_records("t") == 50
    with pytest.raises(DuplicateRecordError):
        generic.create_many([CreateOperation(collection="t", data={"name": "x"}, record_id="G1")])


def test_create_many_is_far_cheaper_than_one_at_a_time(fast, tmp_path):
    n = 1500
    t = time.perf_counter()
    fast.create_many([CreateOperation(collection="t", data={"name": f"n{i}", "amount": float(i)}, record_id=f"M{i}") for i in range(n)])
    bulk = time.perf_counter() - t
    store = DuckDBStore(tmp_path / "s.duckdb")
    store.init_schema()
    slow = MDCDataEngine(store, registry())
    t = time.perf_counter()
    for i in range(n):
        slow.create(CreateOperation(collection="t", data={"name": f"n{i}", "amount": float(i)}, record_id=f"M{i}"))
    one_by_one = time.perf_counter() - t
    store.close()
    assert bulk * 5 < one_by_one


def test_reads_do_not_grow_with_the_table_the_way_they_used_to(fast):
    fast.create_many([CreateOperation(collection="t", data={"name": f"n{i}", "country": "IN" if i % 100 == 0 else "US"}, record_id=f"P{i}") for i in range(20000)])
    t = time.perf_counter()
    for _ in range(5):
        hit = fast.read(ReadOperation(collection="t", filters=[Filter(field="name", operator="=", value="n12345")]))
    filtered = (time.perf_counter() - t) / 5
    t = time.perf_counter()
    for _ in range(50):
        fast.get_record("t", "P12345")
    by_id = (time.perf_counter() - t) / 50
    assert hit.count == 1
    assert by_id < 0.005  # the old engine needed about a second for this at 10,000 records
    assert filtered < 0.25


# -- migration -----------------------------------------------------------------------


def old_block(store: DuckDBStore, collection: str, record_id: str, fields: dict, version: int = 1) -> dict:
    """Write a record the way the engine used to: one JSON block."""
    record = Record(
        record_id=record_id, collection=collection, version=version,
        created_at=datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc), updated_at=datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        fields=fields, checksum="ab" * 32,
    )
    store.put(f"{collection}:{record_id}", record.model_dump_json().encode(), metadata={"collection": collection, "record_id": record_id})
    return json.loads(record.model_dump_json())


def test_records_stored_by_the_old_engine_are_migrated_on_open(tmp_path):
    path = tmp_path / "old.duckdb"
    store = DuckDBStore(path)
    store.init_schema()
    store.conn.execute("DELETE FROM records")
    olds = [old_block(store, "t", f"O{i}", {"name": f"old {i}", "amount": i * 1.5}, version=1 + i % 3) for i in range(30)]
    store.put("OBJ:keep-me", b"not a record", metadata={"object_id": "keep-me"})
    store.put("evt:s:000000000000", b"sealed", metadata={"stream": "s", "kind": "event"})
    store.close()

    store = DuckDBStore(path)
    store.init_schema()
    engine = MDCDataEngine(store, registry())
    assert engine.count_records("t") == 30
    for old in olds:
        got = engine.get_record("t", old["record_id"])
        assert got.fields == old["fields"] and got.version == old["version"] and got.checksum == "ab" * 32
        assert got.created_at == datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc) and got.updated_at == datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    assert [r.record_id for r in engine.read(ReadOperation(collection="t", filters=[], limit=3)).records] == ["O0", "O1", "O2"]
    assert store.search(collection="t") == []  # the old blocks are gone
    assert store.get("OBJ:keep-me") == b"not a record" and store.exists("evt:s:000000000000")  # nothing else was touched
    store.close()

    again = DuckDBStore(path)
    again.init_schema()  # a second open changes nothing
    assert MDCDataEngine(again, registry()).count_records("t") == 30
    again.close()


def test_a_failed_migration_leaves_the_old_data_in_place(tmp_path, monkeypatch):
    path = tmp_path / "old2.duckdb"
    store = DuckDBStore(path)
    store.init_schema()
    old_block(store, "t", "O1", {"name": "a"})
    old_block(store, "t", "O2", {"name": "b"})
    store.close()

    import mdc.storage.duckdb_store as mod

    monkeypatch.setattr(mod, "_naive_utc", lambda v: (_ for _ in ()).throw(RuntimeError("boom")))
    broken = DuckDBStore(path)
    with pytest.raises(RuntimeError, match="boom"):
        broken.init_schema()
    monkeypatch.undo()
    assert broken.exists("t:O1") and broken.exists("t:O2")  # still there
    assert broken.conn.execute("SELECT count(*) FROM records").fetchone()[0] == 0
    broken.init_schema()  # and it works once the cause is gone
    assert MDCDataEngine(broken, registry()).count_records("t") == 2 and not broken.exists("t:O1")
    broken.close()


def test_the_store_advertises_the_capability_and_others_do_not(tmp_path):
    store = DuckDBStore(tmp_path / "c.duckdb")
    assert isinstance(store, RecordBackend) and not isinstance(MemoryStorageBackend(), RecordBackend)
    store.close()
