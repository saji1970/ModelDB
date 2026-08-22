"""Load the YAML collection schema into a `SchemaRegistry` (CLAUDE.md section 9)."""

from __future__ import annotations

from pathlib import Path

import yaml

from mdc.schema.registry import FieldSchema, SchemaRegistry

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "collections.yaml"


def load_default_registry(path: Path = DEFAULT_SCHEMA_PATH) -> SchemaRegistry:
    raw = yaml.safe_load(path.read_text())
    registry = SchemaRegistry()
    for collection_name, collection_data in raw.items():
        fields = {
            field_name: FieldSchema(
                name=field_name,
                datatype=field_data["datatype"],
                required=field_data.get("required", False),
            )
            for field_name, field_data in collection_data.get("fields", {}).items()
        }
        registry.create_collection(collection_name, fields)
    return registry
