#!/usr/bin/env python3
"""Serve the read-only UCP/AP2 interaction dashboard.

Reads reports/ and logs/ written by the orchestrator. Never calls the gate,
clears the kill switch, or sends a payment.

  python3 dashboard/server.py
  python3 dashboard/server.py --port 9872
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.reports import list_runs, load_run  # noqa: E402

STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9872


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ArgentineUcpDashboard/0.1"
    protocol_version = "HTTP/1.1"

    root: Path = ROOT
    index_path: Path = STATIC_INDEX

    def log_message(self, fmt: str, *args: object) -> None:
        path = urlparse(self.path).path
        sys.stderr.write(f"[dashboard] {self.command} {path}\n")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send_html()
            return
        if path == "/api/runs":
            self._send_json({"runs": list_runs(self.root)})
            return
        if path.startswith("/api/runs/"):
            run_id = unquote(path[len("/api/runs/") :]).strip("/")
            if not run_id or "/" in run_id:
                self._send_json({"error": "not_found"}, status=404)
                return
            run = load_run(self.root, run_id)
            if run is None:
                self._send_json({"error": "not_found"}, status=404)
                return
            self._send_json(run)
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_write()

    def _reject_write(self) -> None:
        self._send_json(
            {"error": "read_only", "message": "The dashboard only reads local report files."},
            status=405,
        )

    def _send_html(self) -> None:
        try:
            body = self.index_path.read_bytes()
        except OSError:
            self._send_json({"error": "dashboard_missing"}, status=500)
            return
        self._send(200, "text/html; charset=utf-8", body)

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
        )
        if status == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        self.wfile.write(body)


def make_server(root: Path, host: str, port: int, index_path: Path | None = None) -> ThreadingHTTPServer:
    # Bind instance attributes via a subclass so tests can point at a temp root.
    index = index_path or STATIC_INDEX

    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.root = root
    BoundHandler.index_path = index
    return ThreadingHTTPServer((host, port), BoundHandler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only dashboard for UCP/AP2 demo reports")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repo root that contains reports/ and logs/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        server = make_server(root, args.host, args.port)
    except OSError as exc:
        print(json.dumps({"event": "dashboard_bind_failed", "error": str(exc)}), flush=True)
        return 1
    url = f"http://{args.host}:{args.port}"
    print(
        json.dumps(
            {
                "event": "dashboard_listen",
                "url": url,
                "reports": str(root / "reports"),
                "logs": str(root / "logs"),
                "read_only": True,
                "money": False,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps({"event": "dashboard_stop"}), flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
