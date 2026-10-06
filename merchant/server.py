#!/usr/bin/env python3
"""Local UCP stub merchant — phase 1.

Serves:
  GET  /.well-known/ucp
  GET  /platform/profile.json
  POST /ucp/v1/checkout-sessions
  GET  /ucp/v1/checkout-sessions/{id}
  POST /ucp/v1/checkout-sessions/{id}/complete
  POST /ucp/v1/checkout-sessions/{id}/cancel

No PSP. No real money. Lab AP2 placeholders only.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PROFILE = ROOT / "platform" / "profile.json"
AP2_DIR = ROOT / "fixtures" / "ap2"
META_PATH = AP2_DIR / "mandates.meta.json"
CHECKOUT_MANDATE_PATH = AP2_DIR / "checkout_mandate.placeholder.txt"
PAYMENT_MANDATE_PATH = AP2_DIR / "payment_mandate.placeholder.txt"

UCP_VERSION = "2026-08-25"
LOCK = threading.Lock()
SESSIONS: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def business_profile(base_url: str) -> dict[str, Any]:
    return {
        "ucp": {
            "version": UCP_VERSION,
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": UCP_VERSION,
                        "spec": "https://ucp.dev/2026-08-25/specification/overview",
                        "transport": "rest",
                        "endpoint": f"{base_url}/ucp/v1",
                        "schema": "https://ucp.dev/2026-08-25/services/shopping/openapi.json",
                    }
                ]
            },
            "capabilities": {
                "dev.ucp.shopping.checkout": [
                    {
                        "version": UCP_VERSION,
                        "spec": "https://ucp.dev/2026-08-25/specification/shopping/checkout",
                        "schema": "https://ucp.dev/2026-08-25/schemas/shopping/checkout.json",
                    }
                ],
                "dev.ucp.common.payment.ap2_mandate": [
                    {
                        "version": UCP_VERSION,
                        "spec": "https://ucp.dev/2026-08-25/specification/payment/extensions/ap2-mandates",
                        "schema": "https://ucp.dev/2026-08-25/schemas/common/payment_ap2_mandate.json",
                        "extends": "dev.ucp.shopping.checkout",
                        "config": {"vp_formats_supported": {"dc+sd-jwt": {}}},
                    }
                ],
            },
            "payment_handlers": {
                "com.example.demo_ap2_handler": [
                    {
                        "id": "demo_ap2_lab",
                        "version": UCP_VERSION,
                        "spec": "https://example.invalid/demo/ap2-handler",
                        "schema": "https://example.invalid/demo/ap2-handler.json",
                        "config": {"environment": "LAB", "money": False},
                    }
                ]
            },
        },
        "signing_keys": [
            {
                "kid": "argentine-demo-merchant-lab-2026",
                "kty": "EC",
                "crv": "P-256",
                "x": "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
                "y": "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
                "use": "sig",
                "alg": "ES256",
                "_demo_note": "LAB PLACEHOLDER — not a real key",
            }
        ],
        "_demo": {
            "phase": 1,
            "money": False,
            "sku": "demo-sticker",
            "note": "Local stub merchant for ArGENTine × UCP × AP2. Not production.",
        },
    }


def merchant_authorization_placeholder(checkout_id: str) -> str:
    # Detached-JWS *shape* only — not verifiable.
    return (
        "eyJhbGciOiJFUzI1NiIsImtpZCI6ImFyZ2VudGluZS1kZW1vLW1lcmNoYW50LWxhYi0yMDI2In0.."
        f"DEMO_MERCHANT_AUTH_{checkout_id.replace('-', '')[:12]}"
    )


def new_checkout(base_url: str, body: dict[str, Any] | None) -> dict[str, Any]:
    meta = _load_json(META_PATH)
    checkout_id = f"chk_demo_{uuid.uuid4().hex[:12]}"
    line_items = []
    if isinstance(body, dict) and isinstance(body.get("line_items"), list) and body["line_items"]:
        line_items = body["line_items"]
    else:
        line_items = [
            {
                "id": "li_demo_sticker",
                "sku": meta.get("sku", "demo-sticker"),
                "title": "ArGENTine demo sticker (no charge)",
                "quantity": 1,
                "amount": meta.get("amount", {"currency": "USD", "value": 0}),
            }
        ]
    session = {
        "id": checkout_id,
        "status": "incomplete",
        "created_at": _now(),
        "updated_at": _now(),
        "line_items": line_items,
        "totals": {
            "currency": "USD",
            "items": 0,
            "tax": 0,
            "grand": 0,
        },
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": UCP_VERSION}],
                "dev.ucp.common.payment.ap2_mandate": [{"version": UCP_VERSION}],
            },
            "payment_handlers": {
                "com.example.demo_ap2_handler": [
                    {"id": "demo_ap2_lab", "version": UCP_VERSION}
                ]
            },
        },
        "ap2": {
            "merchant_authorization": merchant_authorization_placeholder(checkout_id),
            "_demo_note": "LAB placeholder detached JWS shape — not verifiable",
        },
        "_demo": {"base_url": base_url, "money": False},
    }
    with LOCK:
        SESSIONS[checkout_id] = session
    return session


def get_checkout(checkout_id: str) -> dict[str, Any] | None:
    with LOCK:
        session = SESSIONS.get(checkout_id)
        return json.loads(json.dumps(session)) if session else None


def complete_checkout(checkout_id: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    with LOCK:
        session = SESSIONS.get(checkout_id)
        if session is None:
            return 404, {"status": "requires_escalation", "messages": [{"code": "not_found", "message": "unknown checkout"}]}
        if session["status"] == "cancelled":
            return 409, {"status": "requires_escalation", "messages": [{"code": "cancelled", "message": "checkout cancelled"}]}
        if session["status"] == "completed":
            return 200, session

        body = body or {}
        ap2 = body.get("ap2") if isinstance(body.get("ap2"), dict) else {}
        checkout_mandate = ap2.get("checkout_mandate") or body.get("checkout_mandate")
        payment = body.get("payment") if isinstance(body.get("payment"), dict) else {}
        instruments = payment.get("instruments") if isinstance(payment.get("instruments"), list) else []
        payment_mandate = None
        if instruments:
            cred = instruments[0].get("credential") if isinstance(instruments[0], dict) else None
            if isinstance(cred, dict):
                payment_mandate = cred.get("token")

        if not checkout_mandate or not payment_mandate:
            return 400, {
                "status": "requires_escalation",
                "messages": [
                    {
                        "type": "error",
                        "code": "ap2_mandate_required",
                        "message": "complete requires ap2.checkout_mandate and payment instrument credential.token (payment mandate)",
                        "severity": "requires_buyer_input",
                    }
                ],
            }

        session["status"] = "completed"
        session["updated_at"] = _now()
        session["completed_at"] = session["updated_at"]
        session["ap2"]["checkout_mandate_received"] = True
        session["ap2"]["payment_mandate_received"] = True
        session["order"] = {
            "id": f"ord_demo_{uuid.uuid4().hex[:10]}",
            "status": "demo_recorded_no_charge",
            "amount": {"currency": "USD", "value": 0},
        }
        return 200, json.loads(json.dumps(session))


def cancel_checkout(checkout_id: str) -> tuple[int, dict[str, Any]]:
    with LOCK:
        session = SESSIONS.get(checkout_id)
        if session is None:
            return 404, {"status": "requires_escalation", "messages": [{"code": "not_found", "message": "unknown checkout"}]}
        if session["status"] == "completed":
            return 409, {"status": "requires_escalation", "messages": [{"code": "completed", "message": "already completed"}]}
        session["status"] = "cancelled"
        session["updated_at"] = _now()
        session["cancelled_at"] = session["updated_at"]
        return 200, json.loads(json.dumps(session))


def make_handler(host: str, port: int):
    def base_url() -> str:
        return f"http://{host}:{port}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "ArgentineUcpAp2Demo/0.1"

        def log_message(self, fmt: str, *args) -> None:
            # Quiet, structured-ish
            sys_stderr = __import__("sys").stderr
            print(f"[merchant] {self.command} {self.path} -> {fmt % args}", file=sys_stderr)

        def _read_json(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/.well-known/ucp":
                return self._send(200, business_profile(base_url()))
            if path == "/platform/profile.json":
                return self._send(200, _load_json(PLATFORM_PROFILE))
            if path == "/health":
                return self._send(200, {"ok": True, "phase": 1, "money": False, "sessions": len(SESSIONS)})
            if path.startswith("/ucp/v1/checkout-sessions/"):
                checkout_id = path.split("/ucp/v1/checkout-sessions/", 1)[1]
                session = get_checkout(checkout_id)
                if session is None:
                    return self._send(404, {"error": "not_found"})
                return self._send(200, session)
            return self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            body = self._read_json()
            if path == "/ucp/v1/checkout-sessions":
                return self._send(201, new_checkout(base_url(), body))
            if path.startswith("/ucp/v1/checkout-sessions/") and path.endswith("/complete"):
                checkout_id = path[len("/ucp/v1/checkout-sessions/") : -len("/complete")]
                code, payload = complete_checkout(checkout_id, body)
                return self._send(code, payload)
            if path.startswith("/ucp/v1/checkout-sessions/") and path.endswith("/cancel"):
                checkout_id = path[len("/ucp/v1/checkout-sessions/") : -len("/cancel")]
                code, payload = cancel_checkout(checkout_id)
                return self._send(code, payload)
            return self._send(404, {"error": "not_found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local UCP stub merchant (phase 1)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9871)
    args = parser.parse_args(argv)
    handler = make_handler(args.host, args.port)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        json.dumps(
            {
                "event": "merchant_listen",
                "url": f"http://{args.host}:{args.port}",
                "ucp": f"http://{args.host}:{args.port}/.well-known/ucp",
                "money": False,
                "phase": 1,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps({"event": "merchant_stop"}), flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
