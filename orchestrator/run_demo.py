#!/usr/bin/env python3
"""Orchestrator: local UCP stub + live hemanth UCP + ArGENTine Railway gate.

Modes:
  smoke     — local UCP full path + gate brief without HITL → expect NEED_HUMAN
              (or diego_off if kill is engaged). Documents that live GO needs Diego.
  demo      — same UCP path + HITL markers for mechanical GO (Diego must open gate).
  abort     — UCP create + optional pre-gate, then admin engage diego_off, then
              gated call must see fails:["diego_off"] (no complete).
  live-ucp  — hemanth live merchant (discovery + Worker API). create→get→complete(mock).
              Gate stays CLOSED for this smoke (expect 503 diego_off). No Railway open.
              No real money (mock-payment-handler / success_token only). No AP2 claim.

Secrets (never printed):
  /home/box/secrets/argentine-partner-caller.env
  /home/box/secrets/argentine-admin.env

Does not modify argentine-a2a. Does not deploy this stub to Railway.

After each run, writes a redacted interaction report (no extra flag):
  reports/latest.md     presenter narrative
  reports/latest.json   same entries as JSON
  logs/latest.jsonl     one interaction entry per line
Timestamped copies sit beside latest.*. Tokens, mandates, and signing keys
are redacted. Logging does not open the gate or clear the kill switch.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from interaction_log import (
        InteractionLog,
        select_catalog_item,
        summarize_ap2_request,
        summarize_ap2_response,
        summarize_catalog,
        summarize_checkout_created,
        summarize_checkout_get,
        summarize_complete,
        summarize_discovery,
    )
except ImportError:  # `python -m orchestrator.run_demo`
    from orchestrator.interaction_log import (
        InteractionLog,
        select_catalog_item,
        summarize_ap2_request,
        summarize_ap2_response,
        summarize_catalog,
        summarize_checkout_created,
        summarize_checkout_get,
        summarize_complete,
        summarize_discovery,
    )

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARTNER_ENV = Path("/home/box/secrets/argentine-partner-caller.env")
DEFAULT_ADMIN_ENV = Path("/home/box/secrets/argentine-admin.env")
DEFAULT_GATE = "https://argentine-a2a-production.up.railway.app"
AP2_DIR = ROOT / "fixtures" / "ap2"

# hemanth/ucp-demo live sandbox (phase 2)
LIVE_UCP_DISCOVERY = "https://ucp-demo.web.app/.well-known/ucp"
LIVE_UCP_API = "https://ucp-demo-api.hemanthhm.workers.dev"
LIVE_UCP_MOCK_HANDLER = "mock-payment-handler"
LIVE_UCP_FORBIDDEN_HANDLERS = frozenset({"card-handler"})


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
    # Cloudflare (and many CDNs) ban the default Python-urllib UA (error 1010).
    req_headers = {
        "Accept": "application/json",
        "User-Agent": (
            "ArgentineUcpAp2Demo/0.2 (+https://github.com/dlescanogithub/argentine-ucp-ap2-demo; "
            "research-smoke; compatible)"
        ),
    }
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


_INTERACTION: InteractionLog | None = None


def _log_req(step: str, method: str, url: str, summary: str, body: Any = None) -> None:
    if _INTERACTION is None:
        return
    payload: dict[str, Any] = {"method": method, "url": _redact_url(url)}
    if body is not None:
        payload["body"] = body
    _INTERACTION.record(
        step=step,
        direction="request",
        summary=summary,
        status=None,
        payload=payload,
    )


def _log_res(step: str, summary: str, status: int | None, payload: Any) -> None:
    if _INTERACTION is None:
        return
    _INTERACTION.record(
        step=step,
        direction="response",
        summary=summary,
        status=status,
        payload=payload,
    )


def _note(text: str) -> None:
    if _INTERACTION is not None:
        _INTERACTION.set_result(text)


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
    _log_req("merchant", "GET", f"{base}/health", "Start the local UCP stub and wait until /health responds.")
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
                _log_res(
                    "merchant",
                    f"Local stub merchant is ready at {base}. Money is false.",
                    200,
                    {"url": base, "money": False},
                )
                return proc
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.15)
    proc.terminate()
    _log_res(
        "merchant",
        f"Local stub merchant did not become ready: {last_err}",
        None,
        {"url": base},
    )
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
    url = f"{gate_base.rstrip('/')}/health"
    _log_req("gate_health", "GET", url, "Check whether the ArGENTine gate is open (diego_off).")
    code, payload = http_json("GET", url)
    body = payload if isinstance(payload, dict) else {"_payload": payload}
    safe = {"diego_off": body.get("diego_off")} if isinstance(payload, dict) else {"diego_off": None}
    _log_res(
        "gate_health",
        f"Gate health HTTP {code}; diego_off={safe.get('diego_off')}.",
        code,
        safe,
    )
    return code, body


def call_gate(gate_base: str, token: str, brief: str, blast_class: str) -> tuple[int, dict[str, Any]]:
    url = f"{gate_base.rstrip('/')}/v1/gate"
    _log_req(
        "gate",
        "POST",
        url,
        "Ask the ArGENTine gate for a decision. The brief and caller credential are not stored in the report.",
    )
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
    _log_res(
        "gate",
        f"Gate HTTP {code}; decision {safe.get('decision')}; fails {safe.get('fails')}.",
        code,
        safe,
    )
    return code, safe


def admin_kill_status(gate_base: str, admin_token: str) -> tuple[int, dict[str, Any]]:
    url = f"{gate_base.rstrip('/')}/admin/kill"
    _log_req("kill_switch", "GET", url, "Read the Diego off-switch. The admin credential is not stored.")
    code, payload = http_json(
        "GET",
        url,
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
    _log_res(
        "kill_switch",
        f"Kill status HTTP {code}; diego_off={safe.get('diego_off')}.",
        code,
        safe,
    )
    return code, safe


def admin_kill_engage(gate_base: str, admin_token: str) -> tuple[int, dict[str, Any]]:
    url = f"{gate_base.rstrip('/')}/admin/kill"
    _log_req(
        "kill_switch",
        "POST",
        url,
        "Engage the Diego off-switch (off=true). This demo never clears the kill.",
        body={"off": True},
    )
    code, payload = http_json(
        "POST",
        url,
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
    _log_res(
        "kill_switch",
        f"Kill engage HTTP {code}; diego_off={safe.get('diego_off')}. Clear is not done by this demo.",
        code,
        safe,
    )
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
    url = f"{merchant_base}/.well-known/ucp"
    _log_req(
        "discovery",
        "GET",
        url,
        "GET /.well-known/ucp for version, services, capabilities, and payment handlers.",
    )
    code, profile = http_json("GET", url)
    emit(
        "ucp_discover",
        http=code,
        version=(profile or {}).get("ucp", {}).get("version") if isinstance(profile, dict) else None,
    )
    _log_res("discovery", summarize_discovery(profile, code), code, profile)
    if code != 200:
        raise RuntimeError("ucp discovery failed")


def ucp_catalog(merchant_base: str) -> dict[str, Any]:
    url = f"{merchant_base}/ucp/v1/products"
    _log_req("catalog", "GET", url, "GET the merchant catalog and pick an in-stock product.")
    code, catalog = http_json("GET", url)
    chosen = select_catalog_item(catalog)
    sku = None
    name = None
    if isinstance(chosen, dict):
        sku = chosen.get("sku") or chosen.get("id")
        name = chosen.get("name") or chosen.get("title")
    emit("ucp_catalog", http=code, sku=sku, name=name)
    _log_res("catalog", summarize_catalog(catalog, code), code, catalog)
    if code != 200 or not isinstance(chosen, dict) or not sku:
        raise RuntimeError("catalog failed")
    return chosen


def ucp_create(merchant_base: str, sku: str = "demo-sticker") -> dict[str, Any]:
    url = f"{merchant_base}/ucp/v1/checkout-sessions"
    body = {"line_items": [{"sku": sku, "quantity": 1}]}
    _log_req(
        "checkout_create",
        "POST",
        url,
        f"Create a checkout for sku {sku}, quantity 1. Mock only — no charge.",
        body=body,
    )
    code, created = http_json(
        "POST",
        url,
        headers={"UCP-Agent": f'profile="{merchant_base}/platform/profile.json"'},
        body=body,
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
    _log_res("checkout_create", summarize_checkout_created(created, code), code, created)
    if code not in {200, 201} or not isinstance(created, dict):
        raise RuntimeError("create_checkout failed")
    checkout_id = created["id"]
    get_url = f"{merchant_base}/ucp/v1/checkout-sessions/{checkout_id}"
    _log_req("checkout_get", "GET", get_url, f"Read checkout {checkout_id} back from the merchant.")
    code, got = http_json("GET", get_url)
    emit("ucp_get_checkout", http=code, status=(got or {}).get("status") if isinstance(got, dict) else None)
    _log_res("checkout_get", summarize_checkout_get(got, code), code, got)
    return {"checkout_id": checkout_id, "created": created, "sku": sku}


def ucp_complete(merchant_base: str, checkout_id: str) -> tuple[int, dict[str, Any]]:
    meta_raw = json.loads((AP2_DIR / "mandates.meta.json").read_text(encoding="utf-8"))
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    checkout_mandate = (AP2_DIR / "checkout_mandate.placeholder.txt").read_text(encoding="utf-8").strip()
    payment_mandate = (AP2_DIR / "payment_mandate.placeholder.txt").read_text(encoding="utf-8").strip()
    url = f"{merchant_base}/ucp/v1/checkout-sessions/{checkout_id}/complete"
    _log_req(
        "ap2_mandate",
        "POST",
        url,
        summarize_ap2_request(meta),
        body={
            "source": "fixtures/ap2",
            "verifiable": meta.get("verifiable"),
            "money": False,
            "vct_checkout": meta.get("vct_checkout"),
            "vct_payment": meta.get("vct_payment"),
            "handler_id": meta.get("handler_id"),
            "amount": meta.get("amount"),
            "checkout_mandate_present": bool(checkout_mandate),
            "payment_mandate_present": bool(payment_mandate),
        },
    )
    complete_body = {
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
    }
    _log_req(
        "checkout_complete",
        "POST",
        url,
        "POST mock complete with the lab AP2 instrument. No card charge.",
        body=complete_body,
    )
    code, completed = http_json("POST", url, body=complete_body)
    completed_body = completed if isinstance(completed, dict) else {}
    emit(
        "ucp_complete_checkout",
        http=code,
        status=completed_body.get("status"),
        order_id=(completed_body.get("order") or {}).get("id") if isinstance(completed_body.get("order"), dict) else None,
        money=False,
    )
    _log_res("checkout_complete", summarize_complete(completed_body, code), code, completed_body)
    order = completed_body.get("order") if isinstance(completed_body.get("order"), dict) else {}
    _log_res(
        "ap2_mandate",
        summarize_ap2_response(completed_body, code),
        code,
        {
            "ap2": completed_body.get("ap2"),
            "order": {
                "id": order.get("id"),
                "status": order.get("status"),
                "amount": order.get("amount"),
            },
        },
    )
    return code, completed_body


def ucp_cancel(merchant_base: str, checkout_id: str) -> tuple[int, dict[str, Any]]:
    url = f"{merchant_base}/ucp/v1/checkout-sessions/{checkout_id}/cancel"
    _log_req(
        "checkout_cancel",
        "POST",
        url,
        f"Cancel checkout {checkout_id} instead of completing it. No charge.",
    )
    code, cancelled = http_json("POST", url)
    cancelled_body = cancelled if isinstance(cancelled, dict) else {}
    emit(
        "ucp_cancel_checkout",
        http=code,
        status=cancelled_body.get("status"),
    )
    _log_res(
        "checkout_cancel",
        f"Cancel HTTP {code}; status {cancelled_body.get('status')}. No charge.",
        code,
        cancelled_body,
    )
    return code, cancelled_body


def _local_checkout(merchant_base: str) -> dict[str, Any]:
    ucp_discover(merchant_base)
    chosen = ucp_catalog(merchant_base)
    sku = str(chosen.get("sku") or chosen.get("id"))
    return ucp_create(merchant_base, sku)


def run_smoke(args: argparse.Namespace, partner: dict[str, str], merchant_base: str) -> int:
    emit(
        "mode",
        mode="smoke",
        note="Automated smoke expects NEED_HUMAN (no HITL). Live GO needs Diego approve + gate open.",
    )
    local = _local_checkout(merchant_base)
    checkout_id = local["checkout_id"]

    gate_base = partner.get("ARGENTINE_GATE_URL", DEFAULT_GATE).rstrip("/")
    if gate_base.endswith("/v1/gate"):
        gate_base = gate_base[: -len("/v1/gate")]
    token = partner.get("ARGENTINE_CALLER_TOKEN", "")
    caller_id = partner.get("ARGENTINE_CALLER_ID", "")
    if caller_id != "partner":
        emit("refuse", reason="expected ARGENTINE_CALLER_ID=partner")
        _note("Refused: expected caller id partner. No charge.")
        return 2
    if not token:
        emit("refuse", reason="empty partner token")
        _note("Refused: empty partner token. No charge.")
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
    _note(
        f"Local mock complete HTTP {c_http}. Gate decision {g_body.get('decision')} "
        f"fails {g_body.get('fails')}. Smoke does not require GO. No real charge."
    )
    return 0 if ok_local and ok_gate else 1


def run_demo(args: argparse.Namespace, partner: dict[str, str], merchant_base: str) -> int:
    emit(
        "mode",
        mode="demo",
        note="HITL markers included for mechanical GO. Diego must open gate; prefer real approve in live show.",
    )
    local = _local_checkout(merchant_base)
    checkout_id = local["checkout_id"]

    gate_base = partner.get("ARGENTINE_GATE_URL", DEFAULT_GATE).rstrip("/")
    if gate_base.endswith("/v1/gate"):
        gate_base = gate_base[: -len("/v1/gate")]
    token = partner.get("ARGENTINE_CALLER_TOKEN", "")
    if partner.get("ARGENTINE_CALLER_ID") != "partner" or not token:
        emit("refuse", reason="partner credentials missing or wrong id")
        _note("Refused: partner credentials missing or wrong id. No charge.")
        return 2

    h_code, health = gate_health(gate_base)
    emit("gate_health", http=h_code, diego_off=health.get("diego_off"), endpoint=_redact_url(gate_base))
    if health.get("diego_off") is True:
        emit(
            "demo_blocked",
            reason="gate diego_off=true; open kill before demo GO; abort mode still works",
        )
        _note("Gate is closed (diego_off). Discovery and checkout were recorded; mock complete was not called. No charge.")
        return 3

    brief = brief_hitl_go(checkout_id)
    g_code, g_body = call_gate(gate_base, token, brief, "high")
    emit("gate_call", case="hitl_go", http=g_code, decision=g_body.get("decision"), fails=g_body.get("fails"))

    if g_body.get("decision") != "GO":
        emit("demo_incomplete", reason="gate did not GO; not completing as happy path")
        ucp_cancel(merchant_base, checkout_id)
        _note(
            f"Gate decision was {g_body.get('decision')} (fails {g_body.get('fails')}). "
            "Checkout cancelled. No charge."
        )
        return 1

    c_http, _completed = ucp_complete(merchant_base, checkout_id)
    emit("demo_summary", gate="GO", local_complete_http=c_http, money=False)
    if c_http == 200:
        _note("Gate returned GO. Mock checkout completed with AP2 lab placeholders. No real charge.")
    else:
        _note(f"Gate returned GO but mock complete HTTP was {c_http}. No real charge.")
    return 0 if c_http == 200 else 1


def run_abort(args: argparse.Namespace, partner: dict[str, str], admin: dict[str, str], merchant_base: str) -> int:
    emit(
        "mode",
        mode="abort",
        note="Engage diego_off via admin AFTER a gated call would be next; show 503 diego_off; no complete.",
    )
    local = _local_checkout(merchant_base)
    checkout_id = local["checkout_id"]

    gate_base = partner.get("ARGENTINE_GATE_URL", DEFAULT_GATE).rstrip("/")
    if gate_base.endswith("/v1/gate"):
        gate_base = gate_base[: -len("/v1/gate")]
    token = partner.get("ARGENTINE_CALLER_TOKEN", "")
    admin_token = admin.get("ARGENTINE_ADMIN_TOKEN", "")
    if partner.get("ARGENTINE_CALLER_ID") != "partner" or not token:
        emit("refuse", reason="partner credentials missing or wrong id")
        _note("Refused: partner credentials missing or wrong id. No charge.")
        return 2
    if not admin_token:
        emit("refuse", reason="empty admin token")
        _note("Refused: empty admin token. No charge.")
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
    if ok:
        _note("Kill engaged. Next gated call refused. Checkout cancelled instead of completed. No charge.")
    else:
        _note("Abort path did not observe the expected diego_off refusal. Complete was still skipped. No charge.")
    return 0 if ok else 1



def _live_ucp_headers(idem: str | None = None) -> dict[str, str]:
    h = {
        "UCP-Agent": f'profile="{LIVE_UCP_DISCOVERY}"',
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; ArgentineUcpAp2Demo/0.2; +https://github.com/dlescanogithub/argentine-ucp-ap2-demo)",
    }
    if idem:
        h["Idempotency-Key"] = idem
    return h


def _assert_mock_only(instruments: list[Any] | None) -> None:
    for inst in instruments or []:
        if not isinstance(inst, dict):
            continue
        hid = str(inst.get("handler_id") or "")
        if hid in LIVE_UCP_FORBIDDEN_HANDLERS or hid.startswith("card"):
            raise RuntimeError(f"refusing non-mock payment handler: {hid}")


def run_live_ucp(args: argparse.Namespace, partner: dict[str, str]) -> int:
    """Phase-2 smoke against hemanth live UCP. Gate CLOSED expected (503 diego_off)."""
    emit(
        "mode",
        mode="live-ucp",
        discovery=LIVE_UCP_DISCOVERY,
        api=LIVE_UCP_API,
        note=(
            "Gate must stay CLOSED for this smoke (expect 503 diego_off). "
            "GO needs a later Diego-approved open window. "
            "No AP2 claim (merchant has no ap2_mandate). "
            "Mock payment only; Worker get/cancel/complete may 404 (in-memory Map across isolates)."
        ),
        ap2=False,
    )

    # 1) Discovery (Firebase + Worker)
    _log_req(
        "discovery",
        "GET",
        LIVE_UCP_DISCOVERY,
        "GET /.well-known/ucp on the live hemanth merchant.",
    )
    code, disc = http_json("GET", LIVE_UCP_DISCOVERY)
    shopping = None
    if isinstance(disc, dict):
        shopping = ((disc.get("ucp") or {}).get("services") or {}).get("dev.ucp.shopping")
    rest_ep = None
    if isinstance(shopping, dict):
        rest_ep = ((shopping.get("rest") or {}).get("endpoint"))
    emit(
        "live_ucp_discover",
        http=code,
        version=(disc or {}).get("ucp", {}).get("version") if isinstance(disc, dict) else None,
        rest_endpoint=rest_ep,
        has_ap2_mandate=False,
    )
    _log_res("discovery", summarize_discovery(disc, code), code, disc)
    if code != 200:
        emit("live_ucp_fail", reason="discovery_failed", http=code)
        _note(f"Stopped after discovery HTTP {code}. Gate was not opened. No charge.")
        return 1

    worker_url = f"{LIVE_UCP_API}/.well-known/ucp"
    _log_req("discovery_worker", "GET", worker_url, "GET the Worker copy of /.well-known/ucp.")
    w_code, w_disc = http_json("GET", worker_url)
    emit("live_ucp_worker_discover", http=w_code, version=((w_disc or {}).get("ucp") or {}).get("version") if isinstance(w_disc, dict) else None)
    _log_res("discovery_worker", summarize_discovery(w_disc, w_code), w_code, w_disc)
    health_url = f"{LIVE_UCP_API}/health"
    _log_req("merchant_health", "GET", health_url, "Check the live UCP API health endpoint.")
    h_code, health_api = http_json("GET", health_url)
    emit("live_ucp_api_health", http=h_code, body=health_api if isinstance(health_api, dict) else None)
    _log_res(
        "merchant_health",
        f"Live UCP API health HTTP {h_code}.",
        h_code,
        health_api if isinstance(health_api, dict) else {"_payload": health_api},
    )

    # 2) Catalog → pick in-stock SKU
    catalog_url = f"{LIVE_UCP_API}/api/catalog/search"
    catalog_body = {"query": "", "limit": 10, "filters": {"availability": "in_stock"}}
    _log_req("catalog", "POST", catalog_url, "Search the live catalog for an in-stock SKU.", body=catalog_body)
    c_code, catalog = http_json(
        "POST",
        catalog_url,
        headers=_live_ucp_headers(),
        body=catalog_body,
    )
    items = (catalog or {}).get("items") if isinstance(catalog, dict) else None
    if not isinstance(items, list):
        items = []
    sku = None
    product_name = None
    price = None
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("availability") == "out_of_stock":
            continue
        sku = it.get("id")
        product_name = it.get("name")
        price = it.get("price")
        break
    emit(
        "live_ucp_catalog",
        http=c_code,
        count=len(items),
        sku=sku,
        product_name=product_name,
        price=price,
    )
    _log_res("catalog", summarize_catalog(catalog, c_code), c_code, catalog)
    if c_code != 200 or not sku:
        emit("live_ucp_fail", reason="catalog_or_sku_missing")
        _note("Stopped after catalog: no in-stock SKU. Gate was not opened. No charge.")
        return 1

    # 3) Create checkout
    idem = f"argentine-live-ucp-{int(time.time())}"
    create_url = f"{LIVE_UCP_API}/api/shopping/checkout-sessions"
    create_body = {"line_items": [{"item": {"id": sku}, "quantity": 1}]}
    _log_req(
        "checkout_create",
        "POST",
        create_url,
        f"Create a live checkout for sku {sku}, quantity 1. Mock payment only.",
        body=create_body,
    )
    cr_code, created = http_json(
        "POST",
        create_url,
        headers=_live_ucp_headers(idem),
        body=create_body,
    )
    checkout_id = created.get("id") if isinstance(created, dict) else None
    instruments = None
    if isinstance(created, dict):
        instruments = ((created.get("payment") or {}).get("instruments"))
    try:
        _assert_mock_only(instruments if isinstance(instruments, list) else [])
    except RuntimeError as exc:
        emit("live_ucp_fail", reason=str(exc))
        _log_res("checkout_create", str(exc), cr_code, created if isinstance(created, dict) else {})
        _note(f"{exc} No charge.")
        return 1
    _log_res("checkout_create", summarize_checkout_created(created, cr_code), cr_code, created)
    emit(
        "live_ucp_create",
        http=cr_code,
        id=checkout_id,
        status=created.get("status") if isinstance(created, dict) else None,
        totals=created.get("totals") if isinstance(created, dict) else None,
        line_item_ids=[
            (li.get("item") or {}).get("id")
            for li in (created.get("line_items") or [])
            if isinstance(li, dict)
        ]
        if isinstance(created, dict)
        else [],
        mock_handlers=[
            i.get("handler_id")
            for i in (instruments or [])
            if isinstance(i, dict)
        ],
    )
    if cr_code not in {200, 201} or not checkout_id:
        emit("live_ucp_fail", reason="create_failed")
        _note("Stopped after checkout create failed. No complete was sent. No charge.")
        return 1

    # 4) GET with retries (known flaky on CF Worker in-memory Map)
    got = None
    g_code = 0
    get_ok = False
    for attempt in range(1, 9):
        get_url = f"{LIVE_UCP_API}/api/shopping/checkout-sessions/{checkout_id}"
        _log_req(
            "checkout_get",
            "GET",
            get_url,
            f"Read checkout {checkout_id} (attempt {attempt}). Worker storage is an in-memory map and may 404.",
        )
        g_code, got = http_json("GET", get_url)
        emit("live_ucp_get_attempt", attempt=attempt, http=g_code, id=checkout_id)
        _log_res("checkout_get", summarize_checkout_get(got, g_code, attempt=attempt), g_code, got)
        if g_code == 200 and isinstance(got, dict) and got.get("id") == checkout_id:
            get_ok = True
            break
        time.sleep(0.2)
    emit(
        "live_ucp_get",
        http=g_code,
        ok=get_ok,
        flaky_note="Worker uses in-memory Map; create/get often hit different isolates → 404",
    )

    complete_ok = False
    complete_http = None
    order_id = None
    if get_ok:
        # Select mock instrument only (never card-handler)
        update_url = f"{LIVE_UCP_API}/api/shopping/checkout-sessions/{checkout_id}"
        update_body = {
            "payment": {
                "selected_instrument_id": "mock-instrument-1",
                "instruments": [
                    {
                        "id": "mock-instrument-1",
                        "handler_id": LIVE_UCP_MOCK_HANDLER,
                        "type": "token",
                        "display_name": "Test Payment",
                    }
                ],
            }
        }
        _log_req(
            "checkout_update",
            "PUT",
            update_url,
            "Select mock-payment-handler only. Card handlers are refused.",
            body=update_body,
        )
        put_code, updated = http_json(
            "PUT",
            update_url,
            headers=_live_ucp_headers(),
            body=update_body,
        )
        emit(
            "live_ucp_select_mock",
            http=put_code,
            status=updated.get("status") if isinstance(updated, dict) else None,
        )
        _log_res(
            "checkout_update",
            f"Mock instrument select HTTP {put_code}; status "
            f"{updated.get('status') if isinstance(updated, dict) else None}. No card handler.",
            put_code,
            updated if isinstance(updated, dict) else {},
        )
        # Complete with success_token only (mock path; fail_token would decline)
        complete_url = f"{LIVE_UCP_API}/api/shopping/checkout-sessions/{checkout_id}/complete"
        complete_body = {"payment_data": {"token": "success_token"}}
        _log_req(
            "checkout_complete",
            "POST",
            complete_url,
            "Complete with the mock success path only. No card, no AP2, no real charge.",
            body=complete_body,
        )
        co_code, completed = http_json(
            "POST",
            complete_url,
            headers=_live_ucp_headers(),
            body=complete_body,
        )
        complete_http = co_code
        if isinstance(completed, dict):
            order_id = ((completed.get("order") or {}) or {}).get("id")
            complete_ok = co_code == 200 and completed.get("status") == "completed"
        emit(
            "live_ucp_complete",
            http=co_code,
            ok=complete_ok,
            status=completed.get("status") if isinstance(completed, dict) else None,
            order_id=order_id,
            money=False,
            handler=LIVE_UCP_MOCK_HANDLER,
        )
        _log_res("checkout_complete", summarize_complete(completed, co_code), co_code, completed)
    else:
        # Still attempt complete once to document 404 flaky; never card handler
        complete_url = f"{LIVE_UCP_API}/api/shopping/checkout-sessions/{checkout_id}/complete"
        complete_body = {"payment_data": {"token": "success_token"}}
        _log_req(
            "checkout_complete",
            "POST",
            complete_url,
            "Get was flaky, so complete is attempted once on the mock path only to record the HTTP result. No card, no AP2.",
            body=complete_body,
        )
        co_code, completed = http_json(
            "POST",
            complete_url,
            headers=_live_ucp_headers(),
            body=complete_body,
        )
        complete_http = co_code
        emit(
            "live_ucp_complete",
            http=co_code,
            ok=False,
            skipped_ready_check=True,
            error=(completed or {}).get("error") if isinstance(completed, dict) else None,
            note="get flaky → complete almost always 404 on this Worker; create body still proves merchant path",
            money=False,
        )
        _log_res("checkout_complete", summarize_complete(completed, co_code), co_code, completed)

    # 5) Gate CLOSED smoke — do NOT open Railway
    gate_base = partner.get("ARGENTINE_GATE_URL", DEFAULT_GATE).rstrip("/")
    if gate_base.endswith("/v1/gate"):
        gate_base = gate_base[: -len("/v1/gate")]
    token = partner.get("ARGENTINE_CALLER_TOKEN", "")
    if partner.get("ARGENTINE_CALLER_ID") != "partner" or not token:
        emit("refuse", reason="partner credentials missing or wrong id")
        _note("Refused: partner credentials missing or wrong id. No charge.")
        return 2

    gh_code, ghealth = gate_health(gate_base)
    emit(
        "gate_health",
        http=gh_code,
        diego_off=ghealth.get("diego_off"),
        endpoint=_redact_url(gate_base),
        expect_closed=True,
    )
    # Closed-gate smoke: NEED_HUMAN-shaped brief; diego_off should win with 503 first.
    g_code, g_body = call_gate(gate_base, token, brief_need_human(str(checkout_id)), "high")
    emit(
        "gate_call",
        case="live_ucp_closed_gate",
        http=g_code,
        decision=g_body.get("decision"),
        fails=g_body.get("fails"),
        note="Expected 503 diego_off while kill engaged. GO requires a later open window + HITL.",
    )

    gate_ok = (
        g_code == 503
        and g_body.get("decision") == "NO_GO"
        and isinstance(g_body.get("fails"), list)
        and "diego_off" in g_body["fails"]
    )
    merchant_ok = cr_code in {200, 201} and bool(checkout_id) and bool(sku)
    emit(
        "live_ucp_summary",
        merchant_ok=merchant_ok,
        sku=sku,
        product_name=product_name,
        checkout_id=checkout_id,
        get_ok=get_ok,
        complete_ok=complete_ok,
        complete_http=complete_http,
        order_id=order_id,
        gate_ok=gate_ok,
        gate_http=g_code,
        ap2=False,
        money=False,
        note=(
            "Smoke OK if merchant create+catalog work and gate returns diego_off. "
            "get/complete may be flaky (documented). Live GO = later open window, not this mode."
        ),
    )
    _note(
        "Live UCP smoke finished. "
        f"Catalog sku {sku}, checkout {checkout_id}, get_ok={get_ok}, complete_ok={complete_ok}, "
        f"gate_ok={gate_ok} (closed-gate check). AP2 was not used. No real charge."
    )
    return 0 if merchant_ok and gate_ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ArGENTine UCP/AP2 phase-1 orchestrator")
    p.add_argument("mode", choices=["smoke", "demo", "abort", "live-ucp"])
    p.add_argument("--merchant-host", default="127.0.0.1")
    p.add_argument("--merchant-port", type=int, default=9871)
    p.add_argument("--merchant-url", default="", help="Use existing merchant; do not spawn")
    p.add_argument("--partner-env", type=Path, default=DEFAULT_PARTNER_ENV)
    p.add_argument("--admin-env", type=Path, default=DEFAULT_ADMIN_ENV)
    return p


def main(argv: list[str] | None = None) -> int:
    global _INTERACTION
    args = build_parser().parse_args(argv)
    _INTERACTION = InteractionLog(mode=args.mode, root=ROOT)
    exit_code = 1
    proc = None
    try:
        partner = load_env_file(args.partner_env)
        admin = load_env_file(args.admin_env) if args.mode == "abort" else {}

        if args.mode == "live-ucp":
            exit_code = run_live_ucp(args, partner)
            return exit_code

        if args.merchant_url:
            merchant_base = args.merchant_url.rstrip("/")
            health_url = f"{merchant_base}/health"
            _log_req("merchant", "GET", health_url, "Use an already running merchant.")
            code, _ = http_json("GET", health_url)
            _log_res(
                "merchant",
                f"External merchant health HTTP {code} at {merchant_base}.",
                code,
                {"url": merchant_base, "money": False},
            )
            if code != 200:
                emit("refuse", reason="merchant-url health failed", http=code)
                _note(f"Refused: merchant health HTTP {code}. No charge.")
                exit_code = 2
                return exit_code
            emit("merchant_external", url=merchant_base)
        else:
            proc = start_merchant(args.merchant_host, args.merchant_port)
            merchant_base = f"http://{args.merchant_host}:{args.merchant_port}"

        if args.mode == "smoke":
            exit_code = run_smoke(args, partner, merchant_base)
        elif args.mode == "demo":
            exit_code = run_demo(args, partner, merchant_base)
        elif args.mode == "abort":
            exit_code = run_abort(args, partner, admin, merchant_base)
        else:
            exit_code = 2
        return exit_code
    except SecretError as exc:
        exit_code = 2
        _note(str(exc)[:300])
        emit("error", message=str(exc))
        return exit_code
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        _note(f"{type(exc).__name__}: {str(exc)[:300]}")
        emit("error", type=type(exc).__name__, message=str(exc)[:300])
        return exit_code
    finally:
        stop_merchant(proc)
        if _INTERACTION is not None:
            try:
                paths = _INTERACTION.write(exit_code)
                emit(
                    "demo_report",
                    markdown=paths["markdown_latest"],
                    markdown_archive=paths["markdown"],
                    json=paths["json_latest"],
                    interactions=paths["interactions_latest"],
                    narrative=_INTERACTION.narrative_lines(),
                    result=_INTERACTION.result_text(exit_code),
                )
            except Exception as exc:  # noqa: BLE001
                emit("demo_report_error", message=str(exc)[:300])


if __name__ == "__main__":
    raise SystemExit(main())
