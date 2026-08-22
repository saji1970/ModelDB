"""MDCDataEngine: the concrete Data Engine (CLAUDE.md sections 4, 11, 65).

Sits between the schema registry / storage backend below and every
caller above (CLI today, a future REST API / SDK) - none of which are
allowed to touch `storage` or `schema` directly (section 28). A
record is serialized as JSON and stored as one opaque block per record
via the existing block-addressed `StorageBackend` (`block_id =
"<collection>:<record_id>"`), so swapping in Matrix/DNA storage later
(sections 33-35) needs no change here - only a new `StorageBackend`.

Filtering, sorting, and limiting all happen in Python over the
records in a collection rather than as compiled backend queries. That
keeps this engine backend-agnostic (a future DNA backend won't have a
query planner at all) and is deliberately simple - correct for the
collection sizes a CRUD prototype deals with, not optimized for the
500k-row synthetic payments tables the read-only NLP pipeline
(mce/cql) still queries directly via SQL.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from mdc.engine.base import DataEngine, OperationResult
from mdc.engine.errors import RecordNotFoundError
from mdc.model.operation import CountOperation, CreateOperation, DeleteOperation, Filter, ReadOperation, Sort, UpdateOperation
from mdc.model.record import Record, utcnow
from mdc.schema.registry import CollectionNotFoundError, SchemaRegistry
from mdc.storage.interface import StorageBackend


class MDCDataEngine(DataEngine):
    def __init__(self, storage: StorageBackend, schema: SchemaRegistry):
        self.storage = storage
        self.schema = schema

    def create(self, operation: CreateOperation) -> OperationResult:
        validated = self.schema.validate_record(operation.collection, operation.data, partial=False)
        now = utcnow()
        record = Record(
            record_id=_generate_record_id(operation.collection),
            collection=operation.collection,
            version=1,
            created_at=now,
            updated_at=now,
            fields=validated,
            checksum=_checksum(validated),
        )
        self._put(record)
        return OperationResult(kind="CREATE", collection=operation.collection, records=[record], count=1)

    def read(self, operation: ReadOperation) -> OperationResult:
        records = self._matching(operation.collection, operation.filters)
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
            self._put(new_record)
            updated.append(new_record)
        return OperationResult(kind="UPDATE", collection=operation.collection, records=updated, count=len(updated))

    def delete(self, operation: DeleteOperation) -> OperationResult:
        matches = self._matching(operation.collection, operation.filters)
        if not matches:
            raise RecordNotFoundError(operation.collection, "no record matches the given filters")
        for record in matches:
            self.storage.delete(_block_id(record.collection, record.record_id))
        return OperationResult(kind="DELETE", collection=operation.collection, records=matches, count=len(matches))

    def count(self, operation: CountOperation) -> OperationResult:
        matches = self._matching(operation.collection, operation.filters)
        return OperationResult(kind="COUNT", collection=operation.collection, count=len(matches))

    # -- internals ----------------------------------------------------------------

    def _put(self, record: Record) -> None:
        payload = record.model_dump_json().encode("utf-8")
        self.storage.put(
            _block_id(record.collection, record.record_id),
            payload,
            metadata={"collection": record.collection, "record_id": record.record_id},
        )

    def _matching(self, collection: str, filters: list[Filter]) -> list[Record]:
        if not self.schema.has_collection(collection):
            raise CollectionNotFoundError(collection)
        block_ids = self.storage.search(collection=collection)
        records = [Record.model_validate_json(self.storage.get(block_id)) for block_id in block_ids]
        return [record for record in records if _matches(record.fields, filters)]


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
