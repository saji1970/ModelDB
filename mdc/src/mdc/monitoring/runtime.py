"""Starting monitoring from settings: the CLI flags and environment variables
share this, so `mdc serve` and the container behave the same."""

from __future__ import annotations

import os
from dataclasses import dataclass

from mdc.databases.manager import DatabaseManager
from mdc.monitoring.collector import RequestStats, StatsCollector
from mdc.monitoring.kafka import KafkaPublisher, build_producer
from mdc.monitoring.server import MetricsServer


@dataclass(frozen=True)
class MonitoringSettings:
    metrics_port: int | None = None  # None = no metrics port
    metrics_host: str = "127.0.0.1"
    metrics_token: str | None = None
    metrics_interval: float = 5.0  # seconds between /stream events
    kafka_bootstrap: str | None = None  # None = no Kafka
    kafka_topic: str = "mdc.stats"
    kafka_interval: float = 10.0

    @property
    def enabled(self) -> bool:
        return self.metrics_port is not None or bool(self.kafka_bootstrap)


def settings_from_env(env: dict[str, str] | None = None) -> MonitoringSettings:
    e = os.environ if env is None else env

    def num(name: str, default: float) -> float:
        raw = (e.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number, got {raw!r}") from None
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")
        return value

    port_raw = (e.get("MDC_METRICS_PORT") or "").strip()
    port: int | None = None
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            raise ValueError(f"MDC_METRICS_PORT must be a port number, got {port_raw!r}") from None
        if not 0 < port < 65536:
            raise ValueError("MDC_METRICS_PORT must be between 1 and 65535")
    return MonitoringSettings(
        metrics_port=port,
        metrics_host=(e.get("MDC_METRICS_HOST") or "127.0.0.1").strip(),
        metrics_token=(e.get("MDC_METRICS_TOKEN") or "").strip() or None,
        metrics_interval=num("MDC_METRICS_INTERVAL", 5.0),
        kafka_bootstrap=(e.get("MDC_KAFKA_BOOTSTRAP") or "").strip() or None,
        kafka_topic=(e.get("MDC_KAFKA_TOPIC") or "mdc.stats").strip(),
        kafka_interval=num("MDC_KAFKA_INTERVAL", 10.0),
    )


class Monitoring:
    def __init__(self, collector: StatsCollector, server: MetricsServer | None, publisher: KafkaPublisher | None):
        self.collector, self.server, self.publisher = collector, server, publisher

    def describe(self) -> list[str]:
        lines = []
        if self.server:
            host, port = self.server.address
            lines.append(f"Metrics: http://{host}:{port}/metrics (also /stats, /stream, /healthz)" + (" - bearer token required" if self.server.token else ""))
        if self.publisher:
            lines.append(f"Kafka: publishing a snapshot every {self.publisher.interval:g}s to topic {self.publisher.topic}")
        return lines

    def stop(self) -> None:
        if self.publisher:
            self.publisher.stop()
        if self.server:
            self.server.stop()


def start_monitoring(manager: DatabaseManager, request_stats: RequestStats | None, settings: MonitoringSettings) -> Monitoring | None:
    """Start what `settings` asks for; None if nothing is asked for. Raises
    (before anything is left running) if the port is taken or the Kafka
    client library is missing."""
    if not settings.enabled:
        return None
    collector = StatsCollector(manager, request_stats)
    producer = build_producer(settings.kafka_bootstrap) if settings.kafka_bootstrap else None
    server = None
    if settings.metrics_port is not None:
        server = MetricsServer(collector, settings.metrics_host, settings.metrics_port, token=settings.metrics_token, interval=settings.metrics_interval).start()
    publisher = KafkaPublisher(collector, producer, settings.kafka_topic, settings.kafka_interval).start() if producer else None
    return Monitoring(collector, server, publisher)
