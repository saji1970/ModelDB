"""MDCDataEngine: the concrete Data Engine (CLAUDE.md sections 4, 11, 65).

Sits between the schema registry / storage backend below and every
caller above (CLI today, a future REST API / SDK) - none of which are
allowed to touch `storage` or `schema` directly (section 28). A
record is serialized as JSON and stored as one opaque block per record
via the existing block-addressed `StorageBackend` (`block_id =
"<collection>:<record_id>"`), so swapping in Matrix/DNA storage later
(sections 33-35) needs no change here - only a new `StorageBackend`.

Filtering, sorting, and limiting are decided in Python, so the meaning of
every operator is the engine's own and identical on every backend. What a
backend can do is make that cheap: one that implements `RecordBackend`
(DuckDB does) keeps records in a table of its own, hands back a whole
collection in one query and a record by id directly, and may narrow a filter
inside the database. It only ever returns a superset of the matches, which
the engine then checks with `_matches`, so a backend can speed a query up but
cannot change its answer. Any other backend falls back to one block per
record, read and checked one at a time.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic_core import to_json

from mdc.engine.base import DataEngine, OperationResult
from mdc.engine.errors import DuplicateRecordError, InvalidRecordIdError, RecordNotFoundError
from mdc.model.operation import CountOperation, CreateOperation, DeleteOperation, Filter, ReadOperation, Sort, UpdateOperation
from mdc.model.record import Record, utcnow
from mdc.schema.registry import CollectionNotFoundError, SchemaRegistry
from mdc.storage.interface import RecordBackend, StorageBackend


class MDCDataEngine(DataEngine):
    def __init__(self, storage: StorageBackend, schema: SchemaRegistry):
        self.storage = storage
        self.schema = schema
        self._records: RecordBackend | None = storage if isinstance(storage, RecordBackend) else None

    def create(self, operation: CreateOperation) -> OperationResult:
        record = self._new_record(operation)
        if operation.record_id is not None and self.has_record(operation.collection, record.record_id):
            raise DuplicateRecordError(operation.collection, record.record_id)
        self._put(record, replace=False)
        return OperationResult(kind="CREATE", collection=operation.collection, records=[record], count=1)

    def create_many(self, operations: list[CreateOperation]) -> list[Record]:
        """Create many records as one unit: every record is validated first, and
        if any is invalid, repeats an id, or reuses one that exists, none is
        written. Much faster than calling `create` in a loop on a backend that
        supports it, since they go in with one statement per few hundred."""
        records = [self._new_record(op) for op in operations]
        seen: set[tuple[str, str]] = set()
        for op, record in zip(operations, records):
            key = (record.collection, record.record_id)
            if key in seen:
                raise DuplicateRecordError(record.collection, record.record_id)
            seen.add(key)
        explicit: dict[str, list[str]] = {}
        for op, record in zip(operations, records):
            if op.record_id is not None:
                explicit.setdefault(record.collection, []).append(record.record_id)
        for collection, ids in explicit.items():
            existing = self._records.record_existing_ids(collection, ids) if self._records else {i for i in ids if self.has_record(collection, i)}
            if existing:
                raise DuplicateRecordError(collection, sorted(existing)[0])
        if self._records is not None:
            self._records.record_insert_many([_row(r) for r in records])
        else:
            for record in records:
                self._put(record, replace=False)
        return records

    def get_record(self, collection: str, record_id: str) -> Record | None:
        """One record by its id, found directly rather than by scanning."""
        if not self.schema.has_collection(collection):
            raise CollectionNotFoundError(collection)
        if self._records is not None:
            row = self._records.record_get(collection, record_id)
            return _record_from_row(collection, row) if row else None
        try:
            return Record.model_validate_json(self.storage.get(_block_id(collection, record_id)))
        except KeyError:
            return None

    def _new_record(self, operation: CreateOperation) -> Record:
        validated = self.schema.validate_record(operation.collection, operation.data, partial=False)
        record_id = operation.record_id
        if record_id is None:
            record_id = _generate_record_id(operation.collection)
        elif not RECORD_ID_RE.match(record_id):
            raise InvalidRecordIdError(record_id)
        now = utcnow()
        return Record(
            record_id=record_id,
            collection=operation.collection,
            version=1,
            created_at=now,
            updated_at=now,
            fields=validated,
            checksum=_checksum(validated),
        )

    def read(self, operation: ReadOperation) -> OperationResult:
        records = self._matching(operation.collection, operation.filters, limit=operation.limit if not operation.sort else None)
        records = _sort(records, operation.sort)
        if operation.limit is not None:
            records = records[: operation.limit]
        return OperationResult(kind="READ", collection=operation.collection, records=records, count=len(records))

    def update(self, operation: UpdateOperation) -> OperationResult:
        matches = self._matching(operation.collection, operation.filters)
        if not matches:
            raise RecordNotFoundError(operation.collection, "no record matches the given filters")
        validated = self.schema.validate_record(operation.collection, operation.data, partial=True)

        now = utcnow()
        updated: list[Record] = []
        for record in matches:
            new_fields = {**record.fields, **validated}
            new_record = record.model_copy(
                update={
                    "fields": new_fields,
                    "version": record.version + 1,
                    "updated_at": now,
                    "checksum": _checksum(new_fields),
                }
            )
            self._put(new_record, replace=True)
            updated.append(new_record)
        return OperationResult(kind="UPDATE", collection=operation.collection, records=updated, count=len(updated))

    def delete(self, operation: DeleteOperation) -> OperationResult:
        matches = self._matching(operation.collection, operation.filters)
        if not matches:
            raise RecordNotFoundError(operation.collection, "no record matches the given filters")
        for record in matches:
            self._remove(record.collection, record.record_id)
        return OperationResult(kind="DELETE", collection=operation.collection, records=matches, count=len(matches))

    def count(self, operation: CountOperation) -> OperationResult:
        if not operation.filters and self._records is not None:
            return OperationResult(kind="COUNT", collection=operation.collection, count=self.count_records(operation.collection))
        matches = self._matching(operation.collection, operation.filters)
        return OperationResult(kind="COUNT", collection=operation.collection, count=len(matches))

    def has_record(self, collection: str, record_id: str) -> bool:
        if self._records is not None:
            return self._records.record_exists(collection, record_id)
        return self.storage.exists(_block_id(collection, record_id))

    def count_records(self, collection: str) -> int:
        """How many records a collection holds - by block id, without reading any."""
        if not self.schema.has_collection(collection):
            raise CollectionNotFoundError(collection)
        if self._records is not None:
            return self._records.record_count(collection)
        return len(self.storage.search(collection=collection))

    def delete_record(self, collection: str, record_id: str) -> None:
        """Remove one record by its id (the filters `delete` takes match field
        values, and a record id is not a field)."""
        if not self.schema.has_collection(collection):
            raise CollectionNotFoundError(collection)
        if not self.has_record(collection, record_id):
            raise RecordNotFoundError(collection, f"no record {record_id!r}")
        self._remove(collection, record_id)

    def drop_collection(self, collection: str) -> int:
        """Remove a collection: every record in it, then the collection
        itself. Returns how many records went. Irreversible."""
        if not self.schema.has_collection(collection):
            raise CollectionNotFoundError(collection)
        if self._records is not None:
            removed = self._records.record_delete_collection(collection)
        else:
            block_ids = self.storage.search(collection=collection)
            for block_id in block_ids:
                self.storage.delete(block_id)
            removed = len(block_ids)
        self.schema.delete_collection(collection)
        return removed

    # -- internals ----------------------------------------------------------------

    def _put(self, record: Record, *, replace: bool) -> None:
        if self._records is not None:
            self._records.record_insert_many([_row(record)], replace=replace)
            return
        payload = record.model_dump_json().encode("utf-8")
        self.storage.put(
            _block_id(record.collection, record.record_id),
            payload,
            metadata={"collection": record.collection, "record_id": record.record_id},
        )

    def _remove(self, collection: str, record_id: str) -> None:
        if self._records is not None:
            self._records.record_delete(collection, record_id)
        else:
            self.storage.delete(_block_id(collection, record_id))

    def _matching(self, collection: str, filters: list[Filter], limit: int | None = None) -> list[Record]:
        if not self.schema.has_collection(collection):
            raise CollectionNotFoundError(collection)
        if self._records is not None:
            rows, exact = self._records.record_query(collection, filters, limit)
            out: list[Record] = []
            for row in rows:
                fields = json.loads(row[4])
                if _matches(fields, filters):
                    out.append(_record_from_row(collection, row, fields))
            return out
        block_ids = self.storage.search(collection=collection)
        records = [Record.model_validate_json(self.storage.get(block_id)) for block_id in block_ids]
        return [record for record in records if _matches(record.fields, filters)]


def _row(record: Record) -> tuple:
    return (record.collection, record.record_id, record.version, record.created_at, record.updated_at, to_json(record.fields).decode("utf-8"), record.checksum)


def _record_from_row(collection: str, row: tuple, fields: dict[str, Any] | None = None) -> Record:
    record_id, version, created, updated, fields_json, checksum = row
    return Record(
        record_id=record_id,
        collection=collection,
        version=version,
        created_at=created.replace(tzinfo=timezone.utc),
        updated_at=updated.replace(tzinfo=timezone.utc),
        fields=fields if fields is not None else json.loads(fields_json),
        checksum=bytes(checksum).hex(),
    )


RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _block_id(collection: str, record_id: str) -> str:
    return f"{collection}:{record_id}"


def _generate_record_id(collection: str) -> str:
    prefix = "".join(ch for ch in collection.upper() if ch.isalpha())[:3] or "REC"
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _checksum(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _matches(fields: dict[str, Any], filters: list[Filter]) -> bool:
    return all(_matches_one(fields.get(f.field), f.operator, f.value) for f in filters)


def _matches_one(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "=":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator == ">":
        return actual is not None and actual > expected
    if operator == "<":
        return actual is not None and actual < expected
    if operator == ">=":
        return actual is not None and actual >= expected
    if operator == "<=":
        return actual is not None and actual <= expected
    if operator == "IN":
        return actual in expected
    if operator == "NOT IN":
        return actual not in expected
    if operator == "BETWEEN":
        low, high = expected
        return actual is not None and low <= actual <= high
    if operator == "LIKE":
        return actual is not None and str(expected).replace("%", "").lower() in str(actual).lower()
    if operator == "IS NULL":
        return actual is None
    if operator == "IS NOT NULL":
        return actual is not None
    raise ValueError(f"Unsupported filter operator: {operator!r}")


def _sort(records: list[Record], sorts: list[Sort]) -> list[Record]:
    result = records
    for s in reversed(sorts):
        non_null = [r for r in result if r.fields.get(s.field) is not None]
        null = [r for r in result if r.fields.get(s.field) is None]
        non_null.sort(key=lambda r: r.fields.get(s.field), reverse=(s.direction == "DESC"))
        result = non_null + null
    return result
