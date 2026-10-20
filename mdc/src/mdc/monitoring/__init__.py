"""Monitoring: live statistics about the server and its databases, published
on a separate port and optionally streamed to Kafka.

    collector.py    one snapshot: process and system CPU/memory, request
                    counts, and per database size, tables, objects, streams
    prometheus.py   a snapshot as Prometheus text (GET /metrics)
    server.py       the metrics port: /metrics, /stats (JSON), /stream
                    (Server-Sent Events), /healthz
    kafka.py        a background publisher of JSON snapshots to a topic

Off unless asked for (`mdc serve --metrics-port`, `MDC_METRICS_PORT`,
`MDC_KAFKA_BOOTSTRAP`): the port is separate from the API so it can be kept on
an internal network and scraped without an API token.
"""

from mdc.monitoring.collector import RequestStats, StatsCollector
from mdc.monitoring.kafka import KafkaPublisher, KafkaUnavailableError, build_producer
from mdc.monitoring.prometheus import render_prometheus
from mdc.monitoring.runtime import Monitoring, MonitoringSettings, settings_from_env, start_monitoring
from mdc.monitoring.server import MetricsServer

__all__ = [
    "KafkaPublisher", "KafkaUnavailableError", "MetricsServer", "Monitoring", "MonitoringSettings", "RequestStats",
    "StatsCollector", "build_producer", "render_prometheus", "settings_from_env", "start_monitoring",
]
