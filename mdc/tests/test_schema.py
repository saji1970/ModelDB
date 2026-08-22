"""Schema registry: coercion, required fields, unknown fields (CLAUDE.md sections 9-10)."""

import pytest

from mdc.schema.loader import load_default_registry
from mdc.schema.registry import CollectionNotFoundError, FieldSchema, SchemaRegistry, SchemaValidationError


def test_default_registry_loads_merchants_collection():
    registry = load_default_registry()
    collection = registry.get_collection("merchants")
    assert "name" in collection.fields
    assert collection.fields["name"].required is True
    assert collection.fields["settlement_balance"].datatype == "decimal"


def test_unknown_collection_raises():
    registry = SchemaRegistry()
    with pytest.raises(CollectionNotFoundError):
        registry.get_collection("nope")


def test_validate_record_coerces_declared_datatypes():
    registry = SchemaRegistry()
    registry.create_collection("merchants", {"balance": FieldSchema(name="balance", datatype="decimal")})
    validated = registry.validate_record("merchants", {"balance": "15000"})
    assert validated["balance"] == 15000.0
    assert isinstance(validated["balance"], float)


def test_validate_record_rejects_unknown_field():
    registry = SchemaRegistry()
    registry.create_collection("merchants", {"name": FieldSchema(name="name", datatype="string")})
    with pytest.raises(SchemaValidationError):
        registry.validate_record("merchants", {"nickname": "ABC"})


def test_validate_record_requires_required_fields_on_full_record():
    registry = SchemaRegistry()
    registry.create_collection(
        "merchants",
        {"name": FieldSchema(name="name", datatype="string", required=True)},
    )
    with pytest.raises(SchemaValidationError):
        registry.validate_record("merchants", {})


def test_partial_validation_skips_required_check():
    registry = SchemaRegistry()
    registry.create_collection(
        "merchants",
        {
            "name": FieldSchema(name="name", datatype="string", required=True),
            "balance": FieldSchema(name="balance", datatype="decimal"),
        },
    )
    validated = registry.validate_record("merchants", {"balance": 500}, partial=True)
    assert validated == {"balance": 500.0}
