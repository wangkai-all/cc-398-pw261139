"""Small dependency-free HTTP toolkit shared by the three services."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 2 * 1024 * 1024


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return body


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], str]] = []

    def add(self, method: str, pattern: str, func_name: str) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), func_name))

    def match(self, method: str, path: str) -> tuple[str, dict[str, str]] | None:
        for route_method, pattern, func_name in self.routes:
            if route_method != method:
                continue
            m = pattern.match(path)
            if m:
                return func_name, m.groupdict()
        return None


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }.get(ext, "application/octet-stream")


class BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service_name = "service"
    static_dir: str | None = None

    # Subclasses set a Router instance in `routes`.
    routes: Router
    # Prefix -> upstream base URL, forwarded verbatim (same-origin web app).
    proxy_map: dict[str, str] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter, structured-ish
        sys.stderr.write(
            f"{time.strftime('%H:%M:%S')} {self.service_name} "
            f"{self.address_string()} {fmt % args}\n"
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _handle(self, method: str) -> None:
        started = time.perf_counter()
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        self.query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.json_body = None
        try:
            for prefix, upstream in self.proxy_map.items():
                if path == prefix or path.startswith(prefix + "/"):
                    self._proxy(prefix, upstream, method, parsed)
                    return
            if path.startswith("/api/") or path.startswith("/internal/") or path == "/health":
                match = self.routes.match(method, path)
                if not match:
                    raise AppError(404, "not_found", f"No route for {method} {path}")
                func_name, params = match
                self.params = params
                if method in {"POST", "PUT"}:
                    self.json_body = self._read_json()
                result = getattr(self, func_name)()
                status, payload = result if isinstance(result, tuple) else (200, result)
                self._send_json(status, payload)
            else:
                if self.static_dir is None:
                    raise AppError(404, "not_found", "Not found")
                self._serve_static(path)
        except AppError as exc:
            self._send_json(exc.status, exc.payload())
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._send_json(
                500,
                {"error": {"code": "internal_error", "message": "Unhandled service error"}},
            )
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if elapsed_ms > 250:
                sys.stderr.write(
                    f"{self.service_name} slow request {method} {self.path} {elapsed_ms:.0f}ms\n"
                )

    def _proxy(self, prefix: str, upstream: str, method: str, parsed: Any) -> None:
        """Forward a request to another service and relay its response.

        Used only so the browser can talk to one origin; the services remain
        independently reachable on their own ports.
        """
        rest = self.path[len(prefix):]
        target = upstream.rstrip("/") + "/" + rest.split("?", 1)[0].lstrip("/")
        if parsed.query:
            target += "?" + parsed.query
        payload = None
        if method in {"POST", "PUT", "DELETE"}:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise AppError(400, "invalid_json", "Request body must be valid UTF-8 JSON")
        try:
            status, body = http_json(target, method=method, payload=payload, timeout=20)
        except AppError as exc:
            if exc.code == "upstream_rejected" and isinstance(exc.details, dict):
                self._send_json(exc.status, exc.details)
                return
            raise
        self._send_json(status, body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        if length > MAX_BODY:
            raise AppError(413, "body_too_large", "Request body exceeds 2 MiB")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AppError(400, "invalid_json", "Request body must be valid UTF-8 JSON")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        assert self.static_dir is not None
        rel = path.lstrip("/")
        if rel == "":
            rel = "index.html"
        target = os.path.realpath(os.path.join(self.static_dir, rel))
        root = os.path.realpath(self.static_dir)
        if not target.startswith(root + os.sep) and target != root:
            raise AppError(403, "forbidden", "Path escapes static root")
        if not os.path.isfile(target):
            raise AppError(404, "not_found", "File not found")
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", _content_type(target))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # helpers used by route handlers
    def q(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def q_int(self, name: str, default: int | None = None, *, required: bool = False) -> int | None:
        raw = self.q(name)
        if raw is None or raw == "":
            if required:
                raise AppError(400, "missing_parameter", f"Query parameter '{name}' is required")
            return default
        try:
            return int(raw)
        except ValueError:
            raise AppError(400, "invalid_parameter", f"Query parameter '{name}' must be an integer")

    def body_dict(self) -> dict:
        if not isinstance(self.json_body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        return self.json_body


def serve(service_name: str, handler_cls: type[BaseHandler], port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    server.daemon_threads = True
    sys.stderr.write(f"{service_name} listening on http://127.0.0.1:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write(f"{service_name} stopping\n")
    finally:
        server.server_close()


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def http_json(
    url: str,
    method: str = "GET",
    payload: Any | None = None,
    timeout: float = 6.0,
    *,
    ok_statuses: tuple[int, ...] = (200, 201, 204),
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            body = json.loads(raw.decode("utf-8")) if raw else None
            return resp.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            detail = {"error": {"code": "upstream_error", "message": raw.decode("utf-8", "replace")}}
        message = "Upstream service rejected the request"
        if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
            message = detail["error"].get("message", message)
        raise AppError(exc.code, "upstream_rejected", message, detail)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise AppError(
            503,
            "upstream_unavailable",
            f"Cannot reach upstream {url}: {getattr(exc, 'reason', exc)}",
        )


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", default=os.environ.get("LEDGER_DATA_DIR", "data"))
    parser.add_argument("--web-dir", default=os.environ.get("LEDGER_WEB_DIR", "web"))
    return parser.parse_args()


def minute_iso(tick: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(tick * 60))


