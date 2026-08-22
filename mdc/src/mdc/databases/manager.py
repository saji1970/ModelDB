"""DatabaseManager: unlimited, independently-created named databases.

Each database is a completely isolated `DuckDBStore` file (its own
`blocks` table, its own `SchemaRegistry`, its own object index/router)
- creating one costs nothing but a new file, so there is no built-in
limit on how many can exist. `"default"` is special only in that it is
always available and pre-populated with the `merchants` collection
(preserving this project's existing behavior) - every other database
starts completely empty; nothing is assumed about what tables/objects
belong in a database a user just created.

Database names come directly from chat/CLI input, and get turned into
a filesystem path (`<databases_dir>/<name>.duckdb`) - `_validate_name`
is a real security boundary, not a formality: without it, a name like
`"../../etc/passwd"` would let a chat message write outside the
databases directory entirely.

Table *data* is already durable - `DuckDBStore` is file-backed, so
records written through `MDCDataEngine` survive a restart on their
own. Table *structure* is not: `SchemaRegistry` is deliberately
storage-agnostic (schema/registry.py's docstring) and holds nothing
but an in-memory dict, so a freshly reopened database would otherwise
forget every collection it ever had - and `MDCDataEngine` refuses to
read/write a collection its schema registry doesn't know about, so
that data would become unreachable even though the bytes are still on
disk. `persist_schema()`/loading a `<name>.schema.json` sidecar next
to `<name>.duckdb` in `get()` closes that gap without teaching
`SchemaRegistry` itself about files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mdc.api.service import ObjectService
from mdc.databases.errors import DatabaseAlreadyExistsError, DatabaseNotFoundError, InvalidDatabaseNameError
from mdc.engine.data_engine import MDCDataEngine
from mdc.schema.registry import SchemaRegistry
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.router import StorageRouter, build_default_router

DEFAULT_DATABASE = "default"
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise InvalidDatabaseNameError(name)


@dataclass
class DatabaseHandle:
    name: str
    store: DuckDBStore
    schema_registry: SchemaRegistry
    engine: MDCDataEngine
    router: StorageRouter
    object_service: ObjectService


class DatabaseManager:
    def __init__(self, databases_dir: Path, default_store: DuckDBStore, default_registry: SchemaRegistry):
        self.databases_dir = databases_dir
        self.databases_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, DatabaseHandle] = {DEFAULT_DATABASE: _build_handle(DEFAULT_DATABASE, default_store, default_registry)}

    def _path_for(self, name: str) -> Path:
        return self.databases_dir / f"{name}.duckdb"

    def _schema_path_for(self, name: str) -> Path:
        return self.databases_dir / f"{name}.schema.json"

    def persist_schema(self, name: str) -> None:
        """Snapshot a database's table structure to disk. Call this after
        any chat/CLI command that mutates `handle.schema_registry` for a
        non-default database, so the tables it defines survive a restart."""
        handle = self.get(name)
        self._schema_path_for(name).write_text(json.dumps(handle.schema_registry.to_dict()))

    def exists(self, name: str) -> bool:
        if name == DEFAULT_DATABASE or name in self._handles:
            return True
        _validate_name(name)
        return self._path_for(name).exists()

    def create(self, name: str) -> DatabaseHandle:
        _validate_name(name)
        if self.exists(name):
            raise DatabaseAlreadyExistsError(name)
        store = DuckDBStore(self._path_for(name))
        store.init_schema()
        handle = _build_handle(name, store, SchemaRegistry())
        self._handles[name] = handle
        return handle

    def get(self, name: str) -> DatabaseHandle:
        if name in self._handles:
            return self._handles[name]
        _validate_name(name)
        if not self._path_for(name).exists():
            raise DatabaseNotFoundError(name)
        store = DuckDBStore(self._path_for(name))
        store.init_schema()
        schema_path = self._schema_path_for(name)
        registry = SchemaRegistry.from_dict(json.loads(schema_path.read_text())) if schema_path.exists() else SchemaRegistry()
        handle = _build_handle(name, store, registry)
        self._handles[name] = handle
        return handle

    def list_names(self) -> list[str]:
        on_disk = {p.stem for p in self.databases_dir.glob("*.duckdb")}
        return sorted({DEFAULT_DATABASE} | on_disk | set(self._handles))


def _build_handle(name: str, store: DuckDBStore, registry: SchemaRegistry) -> DatabaseHandle:
    engine = MDCDataEngine(store, registry)
    router = build_default_router(store)
    service = ObjectService(router)
    return DatabaseHandle(name=name, store=store, schema_registry=registry, engine=engine, router=router, object_service=service)
