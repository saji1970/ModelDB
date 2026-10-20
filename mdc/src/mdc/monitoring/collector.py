"""One snapshot of how the server is doing.

Everything is read-only and cheap to repeat: the database part (row and
object counts) scans each database, so a snapshot is cached for a couple of
seconds and every consumer inside that window - a Prometheus scrape, an SSE
client, the Kafka publisher - shares it.

Process and system numbers use `psutil` when it is installed (current memory,
CPU percent, system totals); without it the snapshot falls back to the
standard library (peak memory, CPU seconds, load average) and says so in
`"source"`.
"""

from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mdc

try:  # optional: richer numbers when available
    import psutil
except ImportError:  # pragma: no cover - exercised by monkeypatching in tests
    psutil = None

from mdc.databases.manager import DatabaseManager
from mdc.events.store import list_streams_in
from mdc.storage_intelligence.strategy import StorageTier


class RequestStats:
    """API request counters, fed by a middleware. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total = 0
        self.by_class: Counter[str] = Counter()
        self.by_method: Counter[str] = Counter()
        self.seconds_total = 0.0
        self.seconds_max = 0.0
        self.in_flight = 0

    def started(self) -> None:
        with self._lock:
            self.in_flight += 1

    def finished(self, method: str, status: int, seconds: float) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.total += 1
            self.by_class[f"{status // 100}xx"] += 1
            self.by_method[method] += 1
            self.seconds_total += seconds
            self.seconds_max = max(self.seconds_max, seconds)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self.total,
                "in_flight": self.in_flight,
                "by_status_class": dict(self.by_class),
                "by_method": dict(self.by_method),
                "seconds_total": round(self.seconds_total, 6),
                "seconds_max": round(self.seconds_max, 6),
                "seconds_avg": round(self.seconds_total / self.total, 6) if self.total else 0.0,
            }


class StatsCollector:
    def __init__(
        self,
        manager: DatabaseManager,
        request_stats: RequestStats | None = None,
        *,
        data_dir: Path | None = None,
        cache_seconds: float = 2.0,
    ):
        self.manager = manager
        self.request_stats = request_stats
        self.data_dir = data_dir or manager.databases_dir
        self.cache_seconds = cache_seconds
        self.started_at = time.time()
        self.extra: dict[str, dict[str, Any]] = {}  # sections other parts add, e.g. the Kafka publisher
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None
        self._last_cpu: tuple[float, float] | None = None  # (wall, process cpu seconds)
        self._process = psutil.Process() if psutil else None
        if psutil:
            psutil.cpu_percent(interval=None)  # the first call has nothing to compare with

    # -- public -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached[0] < self.cache_seconds:
                data = dict(self._cached[1])
            else:
                data = self._collect()
                self._cached = (now, data)
                data = dict(data)
        # sections that change faster than the cache are never cached
        data["requests"] = self.request_stats.snapshot() if self.request_stats else None
        for name, section in self.extra.items():
            data[name] = section() if callable(section) else section
        return data

    # -- collection ---------------------------------------------------------------

    def _collect(self) -> dict[str, Any]:
        databases = [self._database(name) for name in self.manager.list_names()]
        totals = {
            "databases": len(databases),
            "rows": sum(d["rows"] for d in databases),
            "objects": sum(d["objects"] for d in databases),
            "streams": sum(d["streams"] for d in databases),
            "events": sum(d["events"] for d in databases),
            "file_bytes": sum(d["file_bytes"] for d in databases),
        }
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "mdc",
            "version": mdc.__version__,
            "host": platform.node(),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "source": "psutil" if psutil else "stdlib",
            "process": self._process_stats(),
            "system": self._system_stats(),
            "databases": databases,
            "totals": totals,
        }

    def _database(self, name: str) -> dict[str, Any]:
        handle = self.manager.get(name)
        store = handle.store
        file_bytes = store.path.stat().st_size if store.path.exists() else 0
        blocks, payload_bytes = store.query("SELECT count(*), coalesce(sum(octet_length(payload)), 0) FROM blocks")[0]
        engine_memory = store.query("SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory()")[0][0]
        used_blocks, block_size = store.query("SELECT used_blocks, block_size FROM pragma_database_size()")[0]
        tables = {t: handle.engine.count_records(t) for t in handle.schema_registry.list_collections()}
        objects = handle.object_service.list_objects()
        by_tier = Counter(o["storage_tier"] for o in objects)
        by_type = Counter(o["type"] for o in objects)
        streams = list_streams_in(store)
        archive = handle.router.backends[StorageTier.ARCHIVE]
        return {
            "name": name,
            "file_bytes": file_bytes,
            "blocks": int(blocks),
            "block_payload_bytes": int(payload_bytes),
            "engine_memory_bytes": int(engine_memory or 0),
            "engine_used_bytes": int((used_blocks or 0) * (block_size or 0)),
            "tables": tables,
            "rows": sum(tables.values()),
            "objects": len(objects),
            "objects_by_tier": {tier.value: by_tier.get(tier.value, 0) for tier in StorageTier},
            "objects_by_type": dict(by_type),
            "archive_blocks": len(archive.search()),
            "streams": len(streams),
            "events": sum(s["events"] for s in streams),
        }

    def _process_stats(self) -> dict[str, Any]:
        wall = time.monotonic()
        if self._process is not None:
            cpu = self._process.cpu_times()
            cpu_seconds = cpu.user + cpu.system
            mem = self._process.memory_info()
            stats = {
                "pid": self._process.pid,
                "cpu_seconds_user": round(cpu.user, 3),
                "cpu_seconds_system": round(cpu.system, 3),
                "memory_rss_bytes": mem.rss,
                "memory_vms_bytes": mem.vms,
                "threads": self._process.num_threads(),
            }
        else:
            import resource

            t = os.times()
            cpu_seconds = t.user + t.system
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            stats = {
                "pid": os.getpid(),
                "cpu_seconds_user": round(t.user, 3),
                "cpu_seconds_system": round(t.system, 3),
                # ru_maxrss is bytes on macOS and kilobytes on Linux; it is a peak, not the current value
                "memory_peak_rss_bytes": peak if platform.system() == "Darwin" else peak * 1024,
                "threads": threading.active_count(),
            }
        if self._last_cpu is not None and wall > self._last_cpu[0]:
            stats["cpu_percent"] = round(max(0.0, (cpu_seconds - self._last_cpu[1]) / (wall - self._last_cpu[0]) * 100), 2)
        else:
            stats["cpu_percent"] = 0.0
        self._last_cpu = (wall, cpu_seconds)
        return stats

    def _system_stats(self) -> dict[str, Any]:
        disk = shutil.disk_usage(self.data_dir if Path(self.data_dir).exists() else Path.cwd())
        stats: dict[str, Any] = {
            "cpu_count": os.cpu_count(),
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "disk_total_bytes": disk.total,
            "disk_used_bytes": disk.used,
            "disk_free_bytes": disk.free,
        }
        if psutil:
            vm = psutil.virtual_memory()
            stats.update(
                cpu_percent=psutil.cpu_percent(interval=None),
                memory_total_bytes=vm.total,
                memory_available_bytes=vm.available,
                memory_percent=vm.percent,
            )
        return stats
