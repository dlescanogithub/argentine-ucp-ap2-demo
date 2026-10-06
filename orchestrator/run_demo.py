#!/usr/bin/env python3
"""Phase-1 orchestrator: local UCP merchant + ArGENTine Railway gate.

Modes:
  smoke  — local UCP full path + gate brief without HITL → expect NEED_HUMAN
           (or diego_off if kill is engaged). Documents that live GO needs Diego.
  demo   — same UCP path + HITL markers for mechanical GO (Diego must open gate).
  abort  — UCP create + optional pre-gate, then admin engage diego_off, then
           gated call must see fails:["diego_off"] (no complete).

Secrets (never printed):
  /home/box/secrets/argentine-partner-caller.env
  /home/box/secrets/argentine-admin.env

Does not modify argentine-a2a. Does not deploy. Local merchant only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARTNER_ENV = Path("/home/box/secrets/argentine-partner-caller.env")
DEFAULT_ADMIN_ENV = Path("/home/box/secrets/argentine-admin.env")
DEFAULT_GATE = "https://argentine-a2a-production.up.railway.app"
AP2_DIR = ROOT / "fixtures" / "ap2"


class SecretError(RuntimeError):
    pass


def _redact_url(url: str) -> str:
    # Never append tokens to URLs; still strip query just in case.
    return url.split("?", 1)[0]


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SecretError(f"missing env file: {path}")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            out[key] = val
    return out


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    data = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            payload: Any
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"_raw": raw[:500]}
            return int(resp.status), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_raw": raw[:500]}
        return int(exc.code), payload


def emit(event: str, **fields: Any) -> None:
    row = {"event": event, "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    row.update(fields)
    print(json.dumps(row, ensure_ascii=False), flush=True)


def start_merchant(host: str, port: int) -> subprocess.Popen:
    cmd = [sys.executable, str(ROOT / "merchant" / "server.py"), "--host", host, "--port", str(port)]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://{host}:{port}"
    deadline = time.time() + 8
    last_err = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"merchant exited early: {out[:400]}")
        try:
            code, _ = http_json("GET", f"{base}/health", timeout=1.5)
            if code == 200:
                emit("merchant_ready", url=base)
                return proc
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.15)
    proc.terminate()
    raise RuntimeError(f"merchant did not become ready: {last_err}")


def stop_merchant(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def gate_health(gate_base: str) -> tuple[int, dict[str, Any]]:
    code, payload = http_json("GET", f"{gate_base.rstrip('/')}/health")
    return code, payload if isinstance(payload, dict) else {"_payload": payload}


def call_gate(gate_base: str, token: str, brief: str, blast_class: str) -> tuple[int, dict[str, Any]]:
    url = f"{gate_base.rstrip('/')}/v1/gate"
    code, payload = http_json(
        "POST",
        url,
        headers={"Authorization": f"Bearer {token}"},
        body={
            "brief": brief,
            "blast_class": blast_class,
            "tools": ["draft", "checklist"],
            "egress": ["reply"],
        },
    )
    if not isinstance(payload, dict):
        payload = {"_payload": payload}
    # Never echo Authorization. Only decision/fails (+ http).
    safe = {
        "decision": payload.get("decision"),
        "fails": payload.get("fails"),
    }
    if "error" in payload:
        safe["error"] = payload.get("error")
    return code, safe


def admin_kill_status(gate_base: str, admin_token: str) -> tuple[int, dict[str, Any]]:
    code, payload = http_json(
        "GET",
        f"{gate_base.rstrip('/')}/admin/kill",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if not isinstance(payload, dict):
        payload = {"_payload": payload}
    # Status has no secrets; still avoid dumping unexpected fields with tokens.
    safe = {
        k: payload.get(k)
        for k in ("diego_off", "env_off", "file_off", "changed_at", "last_kill_pass_at", "server_time", "error")
        if k in payload or k == "error" and "error" in payload
    }
    return code, safe


def admin_kill_engage(gate_base: str, admin_token: str) -> tuple[int, dict[str, Any]]:
    code, payload = http_json(
        "POST",
        f"{gate_base.rstrip('/')}/admin/kill",
        headers={"Authorization": f"Bearer {admin_token}"},
        body={"off": True},
    )
    if not isinstance(payload, dict):
        payload = {"_payload": payload}
    safe = {
        k: payload.get(k)
        for k in ("diego_off", "env_off", "file_off", "changed_at", "last_kill_pass_at", "server_time", "error")
        if k in payload or k == "error" and "error" in payload
    }
    return code, safe


def brief_need_human(checkout_id: str) -> str:
    return (
        f"UCP/AP2 phase-1 smoke: propose completing mock checkout {checkout_id} "
        f"(demo-sticker USD 0). No HITL markers on purpose.\n"
        f"ucp_checkout: {checkout_id}\n"
    )


def brief_hitl_go(checkout_id: str) -> str:
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"digest: Approve dry-run UCP complete for mock checkout {checkout_id}; "
        f"SKU demo-sticker; amount USD 0; AP2 lab placeholders only; no real charge.\n"
        f"HITL: approved\n"
        f"approved_by: diego\n"
        f"approved_until: {until}\n"
        f"kill_path: Diego off-switch ARGENTINE_DIEGO_OFF / diego.off / POST /admin/kill\n"
        f"side_effects: none\n"
        f"ucp_checkout: {checkout_id}\n"
        f"ap2_mandate: lab_placeholder\n"
    )


def ucp_discover(merchant_base: str) -> None:
    code, profile = http_json("GET", f"{merchant_base}/.well-known/ucp")
    emit(
        "ucp_discover",
        http=code,
        version=(profile or {}).get("ucp", {}).get("version") if isinstance(profile, dict) else None,
    )
    if code != 200:
        raise RuntimeError("ucp discovery failed")


def ucp_create(merchant_base: str) -> dict[str, Any]:
    code, created = http_json(
        "POST",
        f"{merchant_base}/ucp/v1/checkout-sessions",
        headers={"UCP-Agent": f'profile="{merchant_base}/platform/profile.json"'},
        body={"line_items": [{"sku": "demo-sticker", "quantity": 1}]},
    )
    emit(
        "ucp_create_checkout",
        http=code,
        id=(created or {}).get("id") if isinstance(created, dict) else None,
        status=(created or {}).get("status") if isinstance(created, dict) else None,
        has_merchant_auth=bool(
            isinstance(created, dict)
            and isinstance(created.get("ap2"), dict)
            and created["ap2"].get("merchant_authorization")
        ),
    )
    if code not in {200, 201} or not isinstance(created, dict):
        raise RuntimeError("create_checkout failed")
    checkout_id = created["id"]
    code, got = http_json("GET", f"{merchant_base}/ucp/v1/checkout-sessions/{checkout_id}")
    emit("ucp_get_checkout", http=code, status=(got or {}).get("status") if isinstance(got, dict) else None)
    return {"checkout_id": checkout_id, "created": created}


def ucp_complete(merchant_base: str, checkout_id: str) -> tuple[int, dict[str, Any]]:
    checkout_mandate = (AP2_DIR / "checkout_mandate.placeholder.txt").read_text(encoding="utf-8").strip()
    payment_mandate = (AP2_DIR / "payment_mandate.placeholder.txt").read_text(encoding="utf-8").strip()
    code, completed = http_json(
        "POST",
        f"{merchant_base}/ucp/v1/checkout-sessions/{checkout_id}/complete",
        body={
            "ap2": {"checkout_mandate": checkout_mandate},
            "payment": {
                "instruments": [
                    {
                        "id": "pm_demo_lab",
                        "handler_id": "demo_ap2_lab",
                        "type": "card",
                        "selected": True,
                        "display": {"brand": "demo", "last_digits": "0000"},
                        "credential": {"type": "ap2_payment_mandate", "token": payment_mandate},
                    }
                ]
            },
        },
    )
    emit(
        "ucp_complete_checkout",
        http=code,
        status=(completed or {}).get("status") if isinstance(completed, dict) else None,
        order_id=((completed or {}).get("order", {}) or {}).get("id") if isinstance(completed, dict) else None,
        money=False,
    )
    return code, completed if isinstance(completed, dict) else {}


def ucp_cancel(merchant_base: str, checkout_id: str) -> tuple[int, dict[str, Any]]:
    code, cancelled = http_json("POST", f"{merchant_base}/ucp/v1/checkout-sessions/{checkout_id}/cancel")
    emit(
        "ucp_cancel_checkout",
        http=code,
        status=(cancelled or {}).get("status") if isinstance(cancelled, dict) else None,
    )
    return code, cancelled if isinstance(cancelled, dict) else {}


def run_smoke(args: argparse.Namespace, partner: dict[str, str], merchant_base: str) -> int:
    emit(
        "mode",
        mode="smoke",
        note="Automated smoke expects NEED_HUMAN (no HITL). Live GO needs Diego approve + gate open.",
    )
    ucp_discover(merchant_base)
    local = ucp_create(merchant_base)
    checkout_id = local["checkout_id"]

    gate_base = partner.get("ARGENTINE_GATE_URL", DEFAULT_GATE).rstrip("/")
    if gate_base.endswith("/v1/gate"):
        gate_base = gate_base[: -len("/v1/gate")]
    token = partner.get("ARGENTINE_CALLER_TOKEN", "")
    caller_id = partner.get("ARGENTINE_CALLER_ID", "")
    if caller_id != "partner":
        emit("refuse", reason="expected ARGENTINE_CALLER_ID=partner")
        return 2
    if not token:
        emit("refuse", reason="empty partner token")
        return 2

    h_code, health = gate_health(gate_base)
    emit("gate_health", http=h_code, diego_off=health.get("diego_off"), endpoint=_redact_url(gate_base))

    brief = brief_need_human(checkout_id)
    g_code, g_body = call_gate(gate_base, token, brief, "high")
    emit("gate_call", case="need_human_smoke", http=g_code, decision=g_body.get("decision"), fails=g_body.get("fails"))

    # Local complete still OK — money false — shows UCP/AP2 shape even if gate blocks live GO.
    c_http, _completed = ucp_complete(merchant_base, checkout_id)
    emit(
        "smoke_summary",
        local_complete_http=c_http,
        gate_decision=g_body.get("decision"),
        gate_fails=g_body.get("fails"),
        note=(
            "If diego_off: Diego must open gate for NEED_HUMAN/GO demos. "
            "If NEED_HUMAN: expected for smoke; live happy path needs HITL GO (Diego approve)."
        ),
    )
    ok_local = c_http == 200
    ok_gate = g_code in {200, 503, 429, 401} and g_body.get("decision") in {"NEED_HUMAN", "NO_GO", "GO"}
    return 0 if ok_local and ok_gate else 1


def run_demo(args: argparse.Namespace, partner: dict[str, str], merchant_base: str) -> int:
    emit(
        "mode",
        mode="demo",
        note="HITL markers included for mechanical GO. Diego must open gate; prefer real approve in live show.",
    )
    ucp_discover(merchant_base)
    local = ucp_create(merchant_base)
    checkout_id = local["checkout_id"]

    gate_base = partner.get("ARGENTINE_GATE_URL", DEFAULT_GATE).rstrip("/")
    if gate_base.endswith("/v1/gate"):
        gate_base = gate_base[: -len("/v1/gate")]
    token = partner.get("ARGENTINE_CALLER_TOKEN", "")
    if partner.get("ARGENTINE_CALLER_ID") != "partner" or not token:
        emit("refuse", reason="partner credentials missing or wrong id")
        return 2

    h_code, health = gate_health(gate_base)
    emit("gate_health", http=h_code, diego_off=health.get("diego_off"), endpoint=_redact_url(gate_base))
    if health.get("diego_off") is True:
        emit(
            "demo_blocked",
            reason="gate diego_off=true; open kill before demo GO; abort mode still works",
        )
        return 3

    brief = brief_hitl_go(checkout_id)
    g_code, g_body = call_gate(gate_base, token, brief, "high")
    emit("gate_call", case="hitl_go", http=g_code, decision=g_body.get("decision"), fails=g_body.get("fails"))

    if g_body.get("decision") != "GO":
        emit("demo_incomplete", reason="gate did not GO; not completing as happy path")
        ucp_cancel(merchant_base, checkout_id)
        return 1

    c_http, _completed = ucp_complete(merchant_base, checkout_id)
    emit("demo_summary", gate="GO", local_complete_http=c_http, money=False)
    return 0 if c_http == 200 else 1


def run_abort(args: argparse.Namespace, partner: dict[str, str], admin: dict[str, str], merchant_base: str) -> int:
    emit(
        "mode",
        mode="abort",
        note="Engage diego_off via admin AFTER a gated call would be next; show 503 diego_off; no complete.",
    )
    ucp_discover(merchant_base)
    local = ucp_create(merchant_base)
    checkout_id = local["checkout_id"]

    gate_base = partner.get("ARGENTINE_GATE_URL", DEFAULT_GATE).rstrip("/")
    if gate_base.endswith("/v1/gate"):
        gate_base = gate_base[: -len("/v1/gate")]
    token = partner.get("ARGENTINE_CALLER_TOKEN", "")
    admin_token = admin.get("ARGENTINE_ADMIN_TOKEN", "")
    if partner.get("ARGENTINE_CALLER_ID") != "partner" or not token:
        emit("refuse", reason="partner credentials missing or wrong id")
        return 2
    if not admin_token:
        emit("refuse", reason="empty admin token")
        return 2

    h_code, health = gate_health(gate_base)
    emit("gate_health_before", http=h_code, diego_off=health.get("diego_off"))

    # Optional probe while open — if already off, skip pre-call and document.
    if health.get("diego_off") is not True:
        brief = brief_need_human(checkout_id)
        g_code, g_body = call_gate(gate_base, token, brief, "high")
        emit(
            "gate_call_before_kill",
            http=g_code,
            decision=g_body.get("decision"),
            fails=g_body.get("fails"),
            note="pre-kill call (would be next before engage)",
        )
    else:
        emit(
            "gate_already_off",
            note="Live gate already diego_off; will still POST /admin/kill (engage-only) then show gated refuse",
        )

    s_code, status_before = admin_kill_status(gate_base, admin_token)
    emit("admin_kill_status_before", http=s_code, **status_before)

    e_code, engaged = admin_kill_engage(gate_base, admin_token)
    emit("admin_kill_engage", http=e_code, **engaged)
    emit(
        "kill_policy",
        note="POST /admin/kill is engage-only; clear is not done by this demo (Diego custodian).",
    )

    brief2 = brief_hitl_go(checkout_id)
    g2_code, g2_body = call_gate(gate_base, token, brief2, "high")
    emit(
        "gate_call_after_kill",
        http=g2_code,
        decision=g2_body.get("decision"),
        fails=g2_body.get("fails"),
        note="expected HTTP 503 fails:[diego_off]; complete skipped",
    )

    # Explicitly do NOT complete.
    c_code, _cancelled = ucp_cancel(merchant_base, checkout_id)
    emit("ucp_cancel_instead_of_complete", http=c_code)

    ok = (
        g2_code == 503
        and g2_body.get("decision") == "NO_GO"
        and isinstance(g2_body.get("fails"), list)
        and "diego_off" in g2_body["fails"]
    )
    emit("abort_summary", ok=ok, money=False, complete=False)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ArGENTine UCP/AP2 phase-1 orchestrator")
    p.add_argument("mode", choices=["smoke", "demo", "abort"])
    p.add_argument("--merchant-host", default="127.0.0.1")
    p.add_argument("--merchant-port", type=int, default=9871)
    p.add_argument("--merchant-url", default="", help="Use existing merchant; do not spawn")
    p.add_argument("--partner-env", type=Path, default=DEFAULT_PARTNER_ENV)
    p.add_argument("--admin-env", type=Path, default=DEFAULT_ADMIN_ENV)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    partner = load_env_file(args.partner_env)
    admin = load_env_file(args.admin_env) if args.mode == "abort" else {}

    proc = None
    try:
        if args.merchant_url:
            merchant_base = args.merchant_url.rstrip("/")
            code, _ = http_json("GET", f"{merchant_base}/health")
            if code != 200:
                emit("refuse", reason="merchant-url health failed", http=code)
                return 2
            emit("merchant_external", url=merchant_base)
        else:
            proc = start_merchant(args.merchant_host, args.merchant_port)
            merchant_base = f"http://{args.merchant_host}:{args.merchant_port}"

        if args.mode == "smoke":
            return run_smoke(args, partner, merchant_base)
        if args.mode == "demo":
            return run_demo(args, partner, merchant_base)
        if args.mode == "abort":
            return run_abort(args, partner, admin, merchant_base)
        return 2
    except SecretError as exc:
        emit("error", message=str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        emit("error", type=type(exc).__name__, message=str(exc)[:300])
        return 1
    finally:
        stop_merchant(proc)


if __name__ == "__main__":
    raise SystemExit(main())
