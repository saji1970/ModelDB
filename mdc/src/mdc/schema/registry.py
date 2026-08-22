"""Schema registry (CLAUDE.md sections 9-10).

Independent of DuckDB by construction: it only knows field names and
logical datatypes (string/decimal/integer/boolean/datetime), and
validates/coerces plain dicts. `MDCDataEngine` consults it before
every write; nothing here executes SQL or touches a storage backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

Datatype = str  # "string" | "decimal" | "integer" | "boolean" | "datetime"

_COERCERS: dict[str, Any] = {
    "string": str,
    "decimal": float,
    "integer": int,
    "boolean": bool,
    "datetime": lambda v: v if isinstance(v, datetime) else datetime.fromisoformat(str(v)),
}


class SchemaError(Exception):
    """Base class for schema-registry errors."""


class CollectionNotFoundError(SchemaError):
    def __init__(self, collection: str):
        super().__init__(f"No such collection: {collection!r}")
        self.collection = collection


class SchemaValidationError(SchemaError):
    def __init__(self, message: str, *, collection: str, field: str | None = None):
        super().__init__(message)
        self.collection = collection
        self.field = field


@dataclass(frozen=True)
class FieldSchema:
    name: str
    datatype: Datatype
    required: bool = False


@dataclass
class CollectionSchema:
    name: str
    fields: dict[str, FieldSchema] = field(default_factory=dict)


class SchemaRegistry:
    def __init__(self) -> None:
        self._collections: dict[str, CollectionSchema] = {}

    def create_collection(self, name: str, fields: dict[str, FieldSchema]) -> None:
        self._collections[name] = CollectionSchema(name=name, fields=dict(fields))

    def delete_collection(self, name: str) -> None:
        self._collections.pop(name, None)

    def get_collection(self, name: str) -> CollectionSchema:
        try:
            return self._collections[name]
        except KeyError:
            raise CollectionNotFoundError(name) from None

    def has_collection(self, name: str) -> bool:
        return name in self._collections

    def list_collections(self) -> list[str]:
        return sorted(self._collections)

    def add_field(self, collection: str, field_schema: FieldSchema) -> None:
        self.get_collection(collection).fields[field_schema.name] = field_schema

    def remove_field(self, collection: str, field_name: str) -> None:
        self.get_collection(collection).fields.pop(field_name, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            name: {
                "fields": {
                    field_name: {"datatype": f.datatype, "required": f.required}
                    for field_name, f in collection.fields.items()
                }
            }
            for name, collection in self._collections.items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchemaRegistry":
        registry = cls()
        for name, collection in data.items():
            fields = {
                field_name: FieldSchema(name=field_name, datatype=info["datatype"], required=info.get("required", False))
                for field_name, info in collection.get("fields", {}).items()
            }
            registry.create_collection(name, fields)
        return registry

    def validate_record(self, collection: str, data: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
        """Coerce `data` to the collection's declared datatypes and check
        required fields (for a full record) / that every key is known.

        `partial=True` skips the required-field check - used for UPDATE,
        where only the fields being changed are present.
        """
        schema = self.get_collection(collection)
        validated: dict[str, Any] = {}

        for key, value in data.items():
            field_schema = schema.fields.get(key)
            if field_schema is None:
                raise SchemaValidationError(
                    f'Collection {collection!r} has no field {key!r}', collection=collection, field=key
                )
            validated[key] = _coerce(value, field_schema)

        if not partial:
            missing = [f.name for f in schema.fields.values() if f.required and f.name not in validated]
            if missing:
                raise SchemaValidationError(
                    f"Missing required field(s) for {collection!r}: {', '.join(missing)}",
                    collection=collection,
                    field=missing[0],
                )

        return validated


def _coerce(value: Any, field_schema: FieldSchema) -> Any:
    if value is None:
        return None
    coercer = _COERCERS.get(field_schema.datatype)
    if coercer is None:
        return value
    try:
        return coercer(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(
            f"Field {field_schema.name!r} expects {field_schema.datatype}, got {value!r}",
            collection="", field=field_schema.name,
        ) from exc
