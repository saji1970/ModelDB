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
from mdc.databases.errors import (
    CannotDropDefaultDatabaseError,
    DatabaseAlreadyExistsError,
    DatabaseNotFoundError,
    InvalidDatabaseNameError,
    ProtectedTableError,
)
from mdc.engine.data_engine import MDCDataEngine
from mdc.events import EventStore
from mdc.events.store import list_streams_in
from mdc.schema.registry import SchemaRegistry
from mdc.security.keys import load_or_create_dna_key
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
    def __init__(self, databases_dir: Path, default_store: DuckDBStore, default_registry: SchemaRegistry, event_master_key: bytes | None = None):
        self.databases_dir = databases_dir
        # Master key for sealed event streams. Loaded on first use, not at
        # startup, so a deployment that never records a stream never touches it.
        self._event_master_key = event_master_key
        self._event_stores: dict[str, EventStore] = {}
        self.databases_dir.mkdir(parents=True, exist_ok=True)
        # Tables added to "default" at runtime are snapshotted to its sidecar
        # like any other database's; without reading it back here they were
        # forgotten on restart, leaving their rows on disk but unreachable.
        sidecar = self._schema_path_for(DEFAULT_DATABASE)
        if sidecar.exists():
            saved = SchemaRegistry.from_dict(json.loads(sidecar.read_text()))
            for name in saved.list_collections():
                default_registry.create_collection(name, saved.get_collection(name).fields)
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

    def summarize(self, name: str) -> dict:
        """What a database holds - used to tell someone exactly what dropping
        it would destroy: {"tables": {name: rows}, "rows", "objects", "streams"}."""
        handle = self.get(name)
        tables = {t: handle.engine.count_records(t) for t in handle.schema_registry.list_collections()}
        return {
            "tables": tables,
            "rows": sum(tables.values()),
            "objects": len(handle.object_service.list_objects()),
            "streams": len(list_streams_in(handle.store)),
        }

    def drop(self, name: str) -> dict:
        """Permanently delete a database: its tables, rows, objects, streams
        and files. Returns what it held. The default database is refused."""
        if name == DEFAULT_DATABASE:
            raise CannotDropDefaultDatabaseError()
        if not self.exists(name):
            raise DatabaseNotFoundError(name)
        held = self.summarize(name)
        handle = self.get(name)
        handle.store.close()
        self._handles.pop(name, None)
        self._event_stores.pop(name, None)
        for path in (self._path_for(name), Path(f"{self._path_for(name)}.wal"), self._schema_path_for(name)):
            path.unlink(missing_ok=True)
        return held

    @staticmethod
    def is_protected_table(name: str, table: str) -> bool:
        return name == DEFAULT_DATABASE and table == "merchants"

    def drop_table(self, name: str, table: str) -> int:
        """Permanently delete a table and all its rows; returns the row count.
        The built-in merchants table of the default database is refused."""
        if self.is_protected_table(name, table):
            raise ProtectedTableError(table)
        handle = self.get(name)
        removed = handle.engine.drop_collection(table)
        self.persist_schema(name)
        return removed

    def events(self, name: str) -> EventStore:
        """The event-sourced streams of database `name` - one EventStore per
        database, shared by the REST API and chat so they use one lock."""
        handle = self.get(name)
        store = self._event_stores.get(name)
        if store is None:
            if self._event_master_key is None:
                self._event_master_key = load_or_create_dna_key()
            store = self._event_stores[name] = EventStore(handle.store, self._event_master_key)
        return store

    def list_names(self) -> list[str]:
        on_disk = {p.stem for p in self.databases_dir.glob("*.duckdb")}
        return sorted({DEFAULT_DATABASE} | on_disk | set(self._handles))


def _build_handle(name: str, store: DuckDBStore, registry: SchemaRegistry) -> DatabaseHandle:
    engine = MDCDataEngine(store, registry)
    router = build_default_router(store)
    service = ObjectService(router)
    return DatabaseHandle(name=name, store=store, schema_registry=registry, engine=engine, router=router, object_service=service)
