from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .data import RunRepository


_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def serve(
    runs: str | Path = "runs",
    *,
    host: str = "127.0.0.1",
    port: int,
) -> None:
    repository = RunRepository(runs)
    handler = _handler(repository)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Collector Monitor: http://{host}:{port}")
    print(f"Runs: {repository.root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler(repository: RunRepository) -> type[BaseHTTPRequestHandler]:
    class MonitorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/runs":
                    self._json({"runs": repository.list_runs()})
                    return
                if parsed.path == "/api/summary":
                    self._json(repository.overview())
                    return
                if parsed.path.startswith("/api/runs/"):
                    self._run_api(parsed.path, parse_qs(parsed.query))
                    return
                self._static(parsed.path)
            except FileNotFoundError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            except (ValueError, json.JSONDecodeError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except OSError as error:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def log_message(self, format: str, *args: object) -> None:
            return

        def _run_api(self, path: str, query: dict[str, list[str]]) -> None:
            relative = path.removeprefix("/api/runs/")
            parts = relative.split("/")
            run_key = unquote(parts[0])
            view = repository.get(run_key)
            if len(parts) == 1:
                self._json(repository.detail(run_key))
                return
            if len(parts) == 2 and parts[1] == "online":
                self._json(repository.online_view(run_key))
                return
            if len(parts) == 2 and parts[1] == "events":
                self._json(
                    view.query(
                        offset=_integer(query, "offset", 0),
                        limit=_integer(query, "limit", 200),
                        agent=_value(query, "agent"),
                        record_type=_value(query, "type"),
                        tool=_value(query, "tool"),
                        text=_value(query, "q"),
                        anchor=_optional_integer(query, "anchor"),
                    )
                )
                return
            raise FileNotFoundError(f"Monitor endpoint not found: {path}")

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.removeprefix("/")
            if Path(name).name != name:
                raise FileNotFoundError(f"Static asset not found: {path}")
            resource = files("core.monitor").joinpath("static").joinpath(name)
            if not resource.is_file():
                raise FileNotFoundError(f"Static asset not found: {path}")
            content_type = _STATIC_TYPES.get(Path(name).suffix, "application/octet-stream")
            body = resource.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._headers("application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json({"error": message}, status)

        def _headers(self, content_type: str, length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'",
            )

    return MonitorHandler


def _value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def _integer(query: dict[str, list[str]], key: str, default: int) -> int:
    value = _value(query, key)
    return int(value) if value else default


def _optional_integer(query: dict[str, list[str]], key: str) -> int | None:
    value = _value(query, key)
    return int(value) if value else None
