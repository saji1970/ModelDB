"""Stream snapshots to Kafka.

A background thread publishes one JSON snapshot every `interval` seconds to a
topic. It is a client only: it needs a broker you run, and a Kafka client
library, neither of which MDC bundles - install `confluent-kafka` (preferred)
or `kafka-python`. A failed publish is counted and retried on the next tick;
it never stops the server.

The message value is the same JSON as `GET /stats`; the key is the host name,
so a topic shared by several servers keeps each one's snapshots in order.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol

from mdc.monitoring.collector import StatsCollector


class KafkaUnavailableError(RuntimeError):
    pass


class Producer(Protocol):
    def send(self, topic: str, key: bytes, value: bytes) -> None: ...
    def flush(self, timeout: float) -> None: ...


_SEND_TIMEOUT = 10.0


class _ConfluentProducer:
    def __init__(self, producer):
        self._p = producer

    def send(self, topic: str, key: bytes, value: bytes) -> None:
        errors: list[Any] = []
        self._p.produce(topic, value=value, key=key, on_delivery=lambda err, _msg: err and errors.append(err))
        remaining = self._p.flush(_SEND_TIMEOUT)
        if errors:
            raise RuntimeError(str(errors[0]))
        if remaining:
            # Not acknowledged in time. Cancel it, so a broker that comes back does
            # not suddenly receive a pile of stale snapshots.
            self._p.purge()
            self._p.flush(1)
            raise RuntimeError(f"the broker did not acknowledge the message within {_SEND_TIMEOUT:g}s")

    def flush(self, timeout: float) -> None:
        self._p.flush(timeout)


class _KafkaPythonProducer:
    """kafka-python connects when constructed and fails if no broker answers, so
    the producer is created on first use and retried until one does - a broker
    that is down when MDC starts must not stop MDC."""

    def __init__(self, bootstrap_servers: list[str], options: dict[str, Any]):
        self._servers, self._options, self._p = bootstrap_servers, options, None

    def _producer(self):
        if self._p is None:
            from kafka import KafkaProducer

            self._p = KafkaProducer(bootstrap_servers=self._servers, **self._options)
        return self._p

    def send(self, topic: str, key: bytes, value: bytes) -> None:
        self._producer().send(topic, key=key, value=value).get(timeout=_SEND_TIMEOUT)

    def flush(self, timeout: float) -> None:
        if self._p is not None:
            self._p.flush(timeout)


def build_producer(bootstrap_servers: str, **options: Any) -> Producer:
    """A producer for `host:port[,host:port]`. `options` are passed to the client library."""
    try:
        from confluent_kafka import Producer as ConfluentProducer

        return _ConfluentProducer(ConfluentProducer({"bootstrap.servers": bootstrap_servers, "message.timeout.ms": 15000, **options}))
    except ImportError:
        pass
    try:
        import kafka  # noqa: F401  (only to find out it is installed)
    except ImportError:
        raise KafkaUnavailableError("Publishing to Kafka needs a client library: pip install confluent-kafka (or kafka-python).") from None
    return _KafkaPythonProducer(bootstrap_servers.split(","), options)


class KafkaPublisher:
    def __init__(self, collector: StatsCollector, producer: Producer, topic: str = "mdc.stats", interval: float = 10.0):
        self.collector = collector
        self.producer = producer
        self.topic = topic
        self.interval = max(0.05, interval)
        self.published = 0
        self.errors = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        collector.extra["kafka"] = self.status

    def status(self) -> dict[str, Any]:
        return {"topic": self.topic, "published": self.published, "errors": self.errors, "last_error": self.last_error}

    def publish_once(self) -> bool:
        try:
            snap = self.collector.snapshot()
            self.producer.send(self.topic, str(snap["host"]).encode(), json.dumps(snap).encode())
        except Exception as exc:  # noqa: BLE001 - never take the server down for a broker problem
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.published += 1
        return True

    def start(self) -> "KafkaPublisher":
        def run() -> None:
            while not self._stop.is_set():
                self.publish_once()
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=run, name="mdc-kafka", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self.producer.flush(5)
        except Exception:  # noqa: BLE001
            pass
