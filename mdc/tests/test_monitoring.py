"""Monitoring: the snapshot, the metrics port, Prometheus text, Kafka publishing."""

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import mdc.monitoring.collector as collector_module
from mdc.api.app import create_app
from mdc.app import app as cli_app
from mdc.databases.manager import DatabaseManager
from mdc.model.operation import CreateOperation
from mdc.monitoring import (
    KafkaPublisher,
    KafkaUnavailableError,
    MetricsServer,
    MonitoringSettings,
    RequestStats,
    StatsCollector,
    build_producer,
    render_prometheus,
    settings_from_env,
    start_monitoring,
)
from mdc.schema.loader import load_default_registry
from mdc.security.tokens import TokenStore
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.strategy import StorageTier

KEY = b"\x31" * 32


@pytest.fixture
def manager(tmp_path: Path) -> DatabaseManager:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return DatabaseManager(tmp_path / "databases", store, load_default_registry(), event_master_key=KEY)


@pytest.fixture
def populated(manager: DatabaseManager) -> DatabaseManager:
    handle = manager.get("default")
    for i in range(3):
        handle.engine.create(CreateOperation(collection="merchants", data={"name": f"M{i}", "country": "IN"}))
    svc = handle.object_service
    oid = svc.upload(b"# Notes\n\nbody", filename="n.md")["object_id"]
    svc.move(oid, StorageTier.ARCHIVE)
    svc.upload(b"log line\n" * 5, filename="a.log")
    manager.events("default").create_stream("agent-1", {"mood": "calm"}, ts=datetime(2026, 10, 19, tzinfo=timezone.utc))
    manager.events("default").append("agent-1", state={"mood": "busy"})
    manager.create("shop")
    return manager


def get(url: str, token: str | None = None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode()


# -- the snapshot -----------------------------------------------------------------------


def test_snapshot_describes_the_server_and_every_database(populated):
    snap = StatsCollector(populated, RequestStats(), cache_seconds=0).snapshot()
    assert snap["service"] == "mdc" and snap["version"] and snap["uptime_seconds"] >= 0
    assert snap["process"]["pid"] > 0 and snap["process"]["threads"] >= 1 and snap["process"]["cpu_percent"] >= 0
    assert snap["system"]["cpu_count"] >= 1 and snap["system"]["disk_total_bytes"] > 0
    names = [d["name"] for d in snap["databases"]]
    assert names == ["default", "shop"]
    default = snap["databases"][0]
    assert default["tables"]["merchants"] == 3 and default["rows"] == 3
    assert default["objects"] == 2
    assert default["objects_by_tier"]["ARCHIVE"] == 1 and sum(default["objects_by_tier"].values()) == 2
    assert default["archive_blocks"] >= 1
    assert default["streams"] == 1 and default["events"] == 1
    assert default["file_bytes"] > 0 and default["blocks"] > 0 and default["block_payload_bytes"] > 0
    shop = snap["databases"][1]
    assert shop["rows"] == 0 and shop["objects"] == 0 and shop["streams"] == 0
    assert snap["totals"]["databases"] == 2 and snap["totals"]["rows"] == 3 and snap["totals"]["objects"] == 2
    json.dumps(snap)  # the whole thing is plain JSON


def test_snapshot_never_contains_data_or_keys(populated):
    text = json.dumps(StatsCollector(populated, cache_seconds=0).snapshot())
    for secret in ("calm", "busy", "body", KEY.hex(), "M0"):
        assert secret not in text


def test_snapshot_is_cached_briefly_and_refreshes(populated):
    c = StatsCollector(populated, cache_seconds=60)
    first = c.snapshot()["timestamp"]
    populated.get("default").engine.create(CreateOperation(collection="merchants", data={"name": "X", "country": "US"}))
    assert c.snapshot()["timestamp"] == first and c.snapshot()["totals"]["rows"] == 3
    c.cache_seconds = 0
    assert c.snapshot()["totals"]["rows"] == 4


def test_sections_that_change_fast_are_not_cached(populated):
    stats = RequestStats()
    c = StatsCollector(populated, stats, cache_seconds=60)
    assert c.snapshot()["requests"]["total"] == 0
    stats.finished("GET", 200, 0.01)
    assert c.snapshot()["requests"]["total"] == 1


def test_works_without_psutil(populated, monkeypatch):
    monkeypatch.setattr(collector_module, "psutil", None)
    snap = StatsCollector(populated, cache_seconds=0).snapshot()
    assert snap["source"] == "stdlib"
    assert snap["process"]["memory_peak_rss_bytes"] > 0 and "memory_rss_bytes" not in snap["process"]
    text = render_prometheus(snap)
    assert "mdc_process_peak_resident_memory_bytes" in text and "mdc_process_resident_memory_bytes " not in text


def test_psutil_gives_current_memory_when_installed(populated):
    pytest.importorskip("psutil")
    snap = StatsCollector(populated, cache_seconds=0).snapshot()
    assert snap["source"] == "psutil"
    assert snap["process"]["memory_rss_bytes"] > 0 and snap["system"]["memory_total_bytes"] > 0 and 0 <= snap["system"]["memory_percent"] <= 100


def test_cpu_percent_reflects_work_between_snapshots(populated):
    c = StatsCollector(populated, cache_seconds=0)
    c.snapshot()
    end = time.perf_counter() + 0.3
    while time.perf_counter() < end:
        sum(range(1000))
    assert c.snapshot()["process"]["cpu_percent"] > 20


def test_request_stats_count_by_class_and_method():
    s = RequestStats()
    s.started()
    assert s.snapshot()["in_flight"] == 1
    s.finished("GET", 200, 0.1)
    s.finished("POST", 404, 0.3)
    s.finished("GET", 500, 0.2)
    snap = s.snapshot()
    assert snap["total"] == 3 and snap["in_flight"] == 0
    assert snap["by_status_class"] == {"2xx": 1, "4xx": 1, "5xx": 1} and snap["by_method"] == {"GET": 2, "POST": 1}
    assert snap["seconds_max"] == 0.3 and snap["seconds_avg"] == pytest.approx(0.2)


def test_the_api_counts_its_requests(manager, tmp_path):
    tokens = TokenStore(tmp_path / "t.json")
    web = create_app(manager, token_store=tokens)
    client = TestClient(web)
    client.headers.update({"Authorization": f"Bearer {tokens.issue('c')}"})
    assert client.get("/databases").status_code == 200
    assert client.get("/databases/nope/tables").status_code == 404
    assert TestClient(web).get("/databases").status_code == 401
    snap = web.state.request_stats.snapshot()
    assert snap["total"] >= 3 and snap["by_status_class"]["2xx"] >= 1 and snap["by_status_class"]["4xx"] >= 2
    assert snap["in_flight"] == 0


# -- Prometheus text ----------------------------------------------------------------------


def test_prometheus_output_is_well_formed(populated):
    stats = RequestStats()
    stats.finished("GET", 200, 0.05)
    text = render_prometheus(StatsCollector(populated, stats, cache_seconds=0).snapshot())
    sample = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*",?)*\})? -?[0-9.e+-]+$')
    typed: dict[str, str] = {}
    for line in text.strip().splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split()
            assert kind in ("gauge", "counter") and name not in typed
            typed[name] = kind
        elif not line.startswith("# HELP "):
            assert sample.match(line), line
            assert line.split("{")[0].split(" ")[0] in typed  # every sample was declared first
    assert 'mdc_table_rows{database="default",table="merchants"} 3' in text
    assert 'mdc_database_objects_by_tier{database="default",tier="ARCHIVE"} 1' in text
    assert 'mdc_database_streams{database="default"} 1' in text
    assert 'mdc_http_requests_total{status_class="2xx"} 1' in text
    assert text.endswith("\n")


def test_prometheus_labels_are_escaped():
    from mdc.monitoring.prometheus import _labels

    assert _labels(database='a"b\\c\nd') == '{database="a\\"b\\\\c\\nd"}'


# -- the metrics port -----------------------------------------------------------------------


@pytest.fixture
def server(populated):
    s = MetricsServer(StatsCollector(populated, RequestStats(), cache_seconds=0), "127.0.0.1", 0, interval=0.1).start()
    yield s
    s.stop()


def test_metrics_stats_and_health_endpoints(server):
    base = f"http://127.0.0.1:{server.port}"
    status, ctype, body = get(base + "/metrics")
    assert status == 200 and "version=0.0.4" in ctype and "mdc_database_file_bytes" in body
    status, ctype, body = get(base + "/stats")
    assert ctype == "application/json" and json.loads(body)["totals"]["databases"] == 2
    assert get(base + "/healthz")[2] == "ok"
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base + "/nope")
    assert err.value.code == 404


def test_the_port_is_read_only(server):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/stats", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code in (501, 405)


def test_stream_keeps_publishing_until_the_client_leaves(server):
    events = []
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/stream", timeout=5) as r:
        assert r.headers.get("Content-Type") == "text/event-stream"
        buf = ""
        while len(events) < 3:
            buf += r.read1(65536).decode()
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                assert block.startswith("event: stats\ndata: ")
                events.append(json.loads(block.split("data: ", 1)[1]))
    assert len(events) == 3
    assert all(e["totals"]["databases"] == 2 for e in events)
    assert events[0]["timestamp"] != events[2]["timestamp"]


def test_a_token_can_be_required(populated):
    s = MetricsServer(StatsCollector(populated, cache_seconds=0), "127.0.0.1", 0, token="s3cret", interval=0.1).start()
    try:
        base = f"http://127.0.0.1:{s.port}"
        assert get(base + "/healthz")[0] == 200  # health checks never need it
        for path in ("/metrics", "/stats", "/stream"):
            with pytest.raises(urllib.error.HTTPError) as err:
                get(base + path)
            assert err.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as err:
            get(base + "/metrics", token="wrong")
        assert err.value.code == 401
        assert get(base + "/metrics", token="s3cret")[0] == 200
    finally:
        s.stop()


def test_a_collection_failure_is_a_500_not_a_dead_port(populated):
    c = StatsCollector(populated, cache_seconds=0)
    s = MetricsServer(c, "127.0.0.1", 0).start()
    try:
        original = c._collect
        c._collect = lambda: (_ for _ in ()).throw(RuntimeError("disk gone"))
        with pytest.raises(urllib.error.HTTPError) as err:
            get(f"http://127.0.0.1:{s.port}/stats")
        assert err.value.code == 500 and "disk gone" in err.value.read().decode()
        c._collect = original
        assert get(f"http://127.0.0.1:{s.port}/stats")[0] == 200
    finally:
        s.stop()


def test_the_port_binds_where_asked_and_stops_cleanly(populated):
    s = MetricsServer(StatsCollector(populated), "127.0.0.1", 0).start()
    assert s.address[0] == "127.0.0.1" and s.port > 0
    port = s.port
    s.stop()
    with pytest.raises(OSError):
        get(f"http://127.0.0.1:{port}/healthz")


def test_a_taken_port_is_an_error(populated):
    s = MetricsServer(StatsCollector(populated), "127.0.0.1", 0).start()
    try:
        with pytest.raises(OSError):
            MetricsServer(StatsCollector(populated), "127.0.0.1", s.port)
    finally:
        s.stop()


# -- Kafka -----------------------------------------------------------------------------------


class FakeProducer:
    def __init__(self, fail_first: int = 0):
        self.sent: list[tuple[str, bytes, bytes]] = []
        self.fail = fail_first
        self.flushed = False

    def send(self, topic, key, value):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("broker unreachable")
        self.sent.append((topic, key, value))

    def flush(self, timeout):
        self.flushed = True


def test_kafka_publishes_json_snapshots_keyed_by_host(populated):
    producer = FakeProducer()
    p = KafkaPublisher(StatsCollector(populated, cache_seconds=0), producer, topic="metrics.mdc")
    assert p.publish_once() and p.publish_once()
    topic, key, value = producer.sent[0]
    snap = json.loads(value)
    assert topic == "metrics.mdc" and key.decode() == snap["host"] and snap["totals"]["rows"] == 3
    assert p.status() == {"topic": "metrics.mdc", "published": 2, "errors": 0, "last_error": None}


def test_kafka_failures_are_counted_and_retried_not_fatal(populated):
    producer = FakeProducer(fail_first=2)
    p = KafkaPublisher(StatsCollector(populated, cache_seconds=0), producer)
    assert not p.publish_once() and not p.publish_once() and p.publish_once()
    assert (p.published, p.errors) == (1, 2) and "broker unreachable" in p.last_error


def test_kafka_publisher_runs_in_the_background_and_stops(populated):
    producer = FakeProducer()
    p = KafkaPublisher(StatsCollector(populated, cache_seconds=0), producer, interval=0.05).start()
    deadline = time.time() + 5
    while len(producer.sent) < 3 and time.time() < deadline:
        time.sleep(0.02)
    p.stop()
    assert len(producer.sent) >= 3 and producer.flushed
    n = len(producer.sent)
    time.sleep(0.2)
    assert len(producer.sent) == n  # stopped for good


def test_kafka_counters_show_up_in_the_snapshot_and_prometheus(populated):
    c = StatsCollector(populated, cache_seconds=0)
    p = KafkaPublisher(c, FakeProducer())
    p.publish_once()
    assert c.snapshot()["kafka"]["published"] == 1
    text = render_prometheus(c.snapshot())
    assert "mdc_kafka_messages_total 1" in text and "mdc_kafka_errors_total 0" in text


def test_a_missing_kafka_library_says_what_to_install(monkeypatch):
    import builtins

    real = builtins.__import__

    def refuse(name, *a, **kw):
        if name in ("confluent_kafka", "kafka"):
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(KafkaUnavailableError, match="confluent-kafka"):
        build_producer("localhost:9092")


def test_confluent_adapter_counts_an_unreachable_broker_as_a_failure(populated, monkeypatch):
    pytest.importorskip("confluent_kafka")
    import mdc.monitoring.kafka as kafka_module

    monkeypatch.setattr(kafka_module, "_SEND_TIMEOUT", 1.0)
    producer = build_producer("127.0.0.1:1")  # nothing listens there
    assert type(producer).__name__ == "_ConfluentProducer"
    publisher = KafkaPublisher(StatsCollector(populated, cache_seconds=0), producer)
    assert publisher.publish_once() is False  # not reported as published just because the library accepted it
    assert publisher.published == 0 and publisher.errors == 1 and "did not acknowledge" in publisher.last_error


def test_kafka_python_adapter_does_not_need_a_broker_at_startup(monkeypatch):
    pytest.importorskip("kafka")
    import builtins

    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **kw: (_ for _ in ()).throw(ImportError(name)) if name == "confluent_kafka" else real(name, *a, **kw))
    producer = build_producer("127.0.0.1:1,127.0.0.1:2")
    assert type(producer).__name__ == "_KafkaPythonProducer"
    assert producer._servers == ["127.0.0.1:1", "127.0.0.1:2"] and producer._p is None  # nothing connected yet
    producer.flush(1)  # and flushing before the first send is harmless


# -- settings and startup ----------------------------------------------------------------------


def test_settings_from_the_environment():
    assert not settings_from_env({}).enabled
    s = settings_from_env({"MDC_METRICS_PORT": "9464", "MDC_METRICS_TOKEN": " t ", "MDC_KAFKA_BOOTSTRAP": "k:9092", "MDC_KAFKA_INTERVAL": "2.5"})
    assert s.enabled and s.metrics_port == 9464 and s.metrics_host == "127.0.0.1" and s.metrics_token == "t"
    assert s.kafka_bootstrap == "k:9092" and s.kafka_topic == "mdc.stats" and s.kafka_interval == 2.5
    assert settings_from_env({"MDC_KAFKA_BOOTSTRAP": "k:9092"}).enabled


@pytest.mark.parametrize("env", [{"MDC_METRICS_PORT": "abc"}, {"MDC_METRICS_PORT": "0"}, {"MDC_METRICS_PORT": "70000"}, {"MDC_KAFKA_INTERVAL": "soon"}, {"MDC_METRICS_INTERVAL": "-1"}])
def test_bad_settings_are_rejected_with_the_variable_name(env):
    with pytest.raises(ValueError, match="MDC_"):
        settings_from_env(env)


def test_nothing_starts_unless_asked(manager):
    assert start_monitoring(manager, RequestStats(), settings_from_env({})) is None


def test_start_monitoring_runs_the_port_and_describes_it(manager):
    m = start_monitoring(manager, RequestStats(), MonitoringSettings(metrics_port=0))
    try:
        assert m.server and get(f"http://127.0.0.1:{m.server.port}/healthz")[2] == "ok"
        assert "Metrics: http://127.0.0.1:" in m.describe()[0]
    finally:
        m.stop()


def test_cli_lists_the_monitoring_options():
    out = CliRunner().invoke(cli_app, ["serve", "--help"]).output
    for flag in ("--metrics-port", "--metrics-host", "--kafka-bootstrap", "--kafka-topic", "--kafka-interval"):
        assert flag in out
