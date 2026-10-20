"""The metrics port.

A small standard-library HTTP server on its own port (not the API's), run in
a background thread:

    GET /metrics   Prometheus text format
    GET /stats     the full snapshot as JSON
    GET /stream    Server-Sent Events: a JSON snapshot every `interval`
                   seconds for as long as the client stays connected
    GET /healthz   "ok", never needs the token

With a `token`, everything except /healthz needs `Authorization: Bearer <token>`.
It only reads; nothing here can change data. Bind it to an internal address.
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mdc.monitoring.collector import StatsCollector
from mdc.monitoring.prometheus import render_prometheus


class MetricsServer:
    def __init__(self, collector: StatsCollector, host: str = "127.0.0.1", port: int = 9464, *, token: str | None = None, interval: float = 5.0):
        self.collector = collector
        self.token = token or None
        self.interval = max(0.05, interval)
        self._stop = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "mdc-metrics"

            def log_message(self, *args):  # keep scrapes out of the server log
                pass

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if outer.token is None:
                    return True
                header = self.headers.get("Authorization", "")
                presented = header[7:] if header.lower().startswith("bearer ") else ""
                return hmac.compare_digest(presented.encode(), outer.token.encode())

            def do_GET(self):  # noqa: N802 - http.server's name
                path = self.path.split("?", 1)[0]
                if path == "/healthz":
                    return self._send(200, b"ok", "text/plain; charset=utf-8")
                if not self._authorized():
                    return self._send(401, b'{"detail":"A bearer token is required."}', "application/json")
                try:
                    if path == "/metrics":
                        return self._send(200, render_prometheus(outer.collector.snapshot()).encode(), "text/plain; version=0.0.4; charset=utf-8")
                    if path in ("/stats", "/"):
                        return self._send(200, json.dumps(outer.collector.snapshot()).encode(), "application/json")
                    if path == "/stream":
                        return self._stream()
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:  # noqa: BLE001 - a bad snapshot must not kill the port
                    return self._send(500, json.dumps({"detail": f"Could not collect statistics: {exc}"}).encode(), "application/json")
                self._send(404, b'{"detail":"Not found. Try /metrics, /stats, /stream or /healthz."}', "application/json")

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    while not outer._stop.is_set():
                        data = json.dumps(outer.collector.snapshot())
                        self.wfile.write(f"event: stats\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                        if outer._stop.wait(outer.interval):
                            break
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._httpd = Server((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]

    @property
    def port(self) -> int:
        return self.address[1]

    def start(self) -> "MetricsServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mdc-metrics", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
