"""Load the YAML ontology into `Ontology` objects (CLAUDE.md section 14)."""

from __future__ import annotations

from pathlib import Path

import yaml

from mdc.ontology.ontology import EntityDefinition, FieldDefinition, Ontology

DEFAULT_ONTOLOGY_PATH = Path(__file__).resolve().parent / "payments.yaml"


def load_ontology(path: Path = DEFAULT_ONTOLOGY_PATH) -> Ontology:
    raw = yaml.safe_load(path.read_text())
    entities: dict[str, EntityDefinition] = {}
    for entity_name, entity_data in raw.items():
        fields = {
            field_name: FieldDefinition(
                name=field_name,
                aliases=tuple(field_data.get("aliases", [])),
                datatype=field_data["datatype"],
                source_table=field_data["source_table"],
            )
            for field_name, field_data in entity_data.get("fields", {}).items()
        }
        entities[entity_name] = EntityDefinition(
            name=entity_name,
            aliases=tuple(entity_data.get("aliases", [])),
            fields=fields,
        )
    return Ontology(entities)
