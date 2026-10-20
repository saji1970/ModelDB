"""A snapshot as Prometheus text exposition format (version 0.0.4)."""

from __future__ import annotations

from typing import Any


def _esc(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: Any) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"


def render_prometheus(snap: dict[str, Any]) -> str:
    lines: list[str] = []
    declared: set[str] = set()

    def metric(name: str, kind: str, help_: str, value: Any, **labels: Any) -> None:
        if value is None:
            return
        if name not in declared:
            declared.add(name)
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {kind}")
        text = repr(float(value)) if isinstance(value, float) else str(int(value))
        lines.append(f"{name}{_labels(**labels)} {text}")

    metric("mdc_info", "gauge", "Build information (value is always 1).", 1, version=snap["version"], source=snap["source"], host=snap["host"])
    metric("mdc_uptime_seconds", "gauge", "Seconds since the collector started.", snap["uptime_seconds"])

    p = snap["process"]
    metric("mdc_process_cpu_percent", "gauge", "Process CPU use since the previous snapshot, in percent of one core.", p.get("cpu_percent"))
    metric("mdc_process_cpu_seconds_total", "counter", "Process CPU time.", p.get("cpu_seconds_user"), mode="user")
    metric("mdc_process_cpu_seconds_total", "counter", "Process CPU time.", p.get("cpu_seconds_system"), mode="system")
    metric("mdc_process_resident_memory_bytes", "gauge", "Process resident memory.", p.get("memory_rss_bytes"))
    metric("mdc_process_virtual_memory_bytes", "gauge", "Process virtual memory.", p.get("memory_vms_bytes"))
    metric("mdc_process_peak_resident_memory_bytes", "gauge", "Process peak resident memory (when current memory is unavailable).", p.get("memory_peak_rss_bytes"))
    metric("mdc_process_threads", "gauge", "Process threads.", p.get("threads"))

    s = snap["system"]
    metric("mdc_system_cpu_percent", "gauge", "Whole-machine CPU use in percent.", s.get("cpu_percent"))
    metric("mdc_system_cpu_count", "gauge", "Logical CPUs.", s.get("cpu_count"))
    for window, value in zip(("1m", "5m", "15m"), s.get("load_average") or []):
        metric("mdc_system_load_average", "gauge", "System load average.", value, window=window)
    metric("mdc_system_memory_total_bytes", "gauge", "Machine memory.", s.get("memory_total_bytes"))
    metric("mdc_system_memory_available_bytes", "gauge", "Machine memory available to processes.", s.get("memory_available_bytes"))
    metric("mdc_system_memory_percent", "gauge", "Machine memory in use, percent.", s.get("memory_percent"))
    metric("mdc_system_disk_total_bytes", "gauge", "Size of the disk holding the data.", s.get("disk_total_bytes"))
    metric("mdc_system_disk_used_bytes", "gauge", "Used space on the disk holding the data.", s.get("disk_used_bytes"))
    metric("mdc_system_disk_free_bytes", "gauge", "Free space on the disk holding the data.", s.get("disk_free_bytes"))

    r = snap.get("requests")
    if r:
        metric("mdc_http_requests_in_flight", "gauge", "API requests being handled now.", r["in_flight"])
        for cls, n in sorted(r["by_status_class"].items()):
            metric("mdc_http_requests_total", "counter", "API requests handled, by status class.", n, status_class=cls)
        metric("mdc_http_request_duration_seconds_sum", "counter", "Total time spent handling API requests.", r["seconds_total"])
        metric("mdc_http_request_duration_seconds_count", "counter", "API requests timed.", r["total"])
        metric("mdc_http_request_duration_seconds_max", "gauge", "Slowest API request seen.", r["seconds_max"])

    for db in snap["databases"]:
        n = db["name"]
        metric("mdc_database_file_bytes", "gauge", "Size of the database file.", db["file_bytes"], database=n)
        metric("mdc_database_blocks", "gauge", "Blocks in the block store.", db["blocks"], database=n)
        metric("mdc_database_block_payload_bytes", "gauge", "Bytes of block payloads.", db["block_payload_bytes"], database=n)
        metric("mdc_database_engine_memory_bytes", "gauge", "Memory held by the database engine.", db["engine_memory_bytes"], database=n)
        metric("mdc_database_engine_used_bytes", "gauge", "Bytes the database engine has allocated in its file.", db["engine_used_bytes"], database=n)
        metric("mdc_database_rows", "gauge", "Rows across the database's tables.", db["rows"], database=n)
        for table, rows in db["tables"].items():
            metric("mdc_table_rows", "gauge", "Rows in a table.", rows, database=n, table=table)
        metric("mdc_database_objects", "gauge", "Stored objects.", db["objects"], database=n)
        for tier, count in db["objects_by_tier"].items():
            metric("mdc_database_objects_by_tier", "gauge", "Stored objects by tier.", count, database=n, tier=tier)
        metric("mdc_database_archive_blocks", "gauge", "Blocks in the persistent ARCHIVE tier.", db["archive_blocks"], database=n)
        metric("mdc_database_streams", "gauge", "Event streams.", db["streams"], database=n)
        metric("mdc_database_stream_events", "gauge", "Events across the streams.", db["events"], database=n)

    t = snap["totals"]
    metric("mdc_databases", "gauge", "Databases.", t["databases"])
    metric("mdc_total_rows", "gauge", "Rows across all databases.", t["rows"])
    metric("mdc_total_objects", "gauge", "Objects across all databases.", t["objects"])
    metric("mdc_total_file_bytes", "gauge", "Bytes of all database files.", t["file_bytes"])

    k = snap.get("kafka")
    if k:
        metric("mdc_kafka_messages_total", "counter", "Snapshots published to Kafka.", k["published"])
        metric("mdc_kafka_errors_total", "counter", "Failed Kafka publishes.", k["errors"])
    return "\n".join(lines) + "\n"
