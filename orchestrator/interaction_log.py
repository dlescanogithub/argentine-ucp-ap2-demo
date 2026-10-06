"""Redacted interaction log and presenter report for the UCP/AP2 demo.

Each protocol step is stored as a request and/or response entry:

  timestamp, step, direction, summary, status, payload_snippet

After a run the orchestrator writes:

  logs/interactions-<mode>-<utc>.jsonl   and logs/latest.jsonl
  reports/demo-report-<mode>-<utc>.md    and reports/latest.md
  reports/demo-report-<mode>-<utc>.json  and reports/latest.json

Snippets drop tokens, mandate material, signing keys, and contact fields.
Summaries stay short enough to read out loud in a demo.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Capability names such as dev.ucp.common.payment.ap2_mandate and flags such as
# checkout_mandate_received must stay visible. Only credential-bearing keys match.
_EXACT_SENSITIVE = {
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
    "cookie",
    "set_cookie",
    "last_digits",
    "card_number",
    "pan",
    "cvv",
    "cvc",
    "email",
    "phone",
    "phone_number",
    "signing_keys",
    "checkout_mandate",
    "payment_mandate",
    "merchant_authorization",
    "private_key",
    "client_secret",
}
_JWK_COORD_KEYS = {"x", "y", "d", "k"}
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)+(?:~[A-Za-z0-9_.-]*)*")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
# Require ":" or "=" so ordinary prose ("authorization placeholder") stays readable.
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*\S+"
)
_LITERAL_TOKENS = {"success_token", "fail_token"}

PROTOCOL_STEPS = (
    "discovery",
    "catalog",
    "checkout_create",
    "checkout_complete",
    "ap2_mandate",
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> str:
    return _iso(datetime.now(timezone.utc))


def _key_sensitive(key: str) -> bool:
    lk = key.lower().replace("-", "_")
    if lk in _EXACT_SENSITIVE:
        return True
    if lk.endswith(("_token", "_secret", "_password", "_authorization", "_api_key")):
        return True
    if "credential" in lk or "signing_key" in lk:
        return True
    return False


def redact_text(text: str) -> str:
    for literal in _LITERAL_TOKENS:
        text = text.replace(literal, "[redacted]")
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _JWT_RE.sub("[redacted]", text)
    text = _EMAIL_RE.sub("[redacted-email]", text)
    text = _LABELED_SECRET_RE.sub(lambda match: f"{match.group(1)} [redacted]", text)
    if len(text) > 1200:
        text = text[:1200] + "…"
    return text


def redact_payload(value: Any, depth: int = 0) -> Any:
    """Return a JSON-safe copy with secrets replaced by ``[redacted]``."""
    # UCP profiles nest capabilities → config → vp_formats. Keep that visible.
    if depth > 12:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        items = list(value.items())
        extra = 0
        if len(items) > 24:
            extra = len(items) - 24
            items = items[:24]
        for key, item in items:
            name = str(key)
            lk = name.lower().replace("-", "_")
            if _key_sensitive(name):
                out[name] = "[redacted]"
            elif lk in _JWK_COORD_KEYS and isinstance(item, str) and len(item) >= 16:
                out[name] = "[redacted]"
            else:
                out[name] = redact_payload(item, depth + 1)
        if extra:
            out["_truncated_keys"] = extra
        return out
    if isinstance(value, list):
        shown = [redact_payload(item, depth + 1) for item in value[:12]]
        if len(value) > 12:
            shown.append(f"… {len(value) - 12} more")
        return shown
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def snippet(value: Any) -> Any:
    redacted = redact_payload(value)
    try:
        encoded = json.dumps(redacted, ensure_ascii=False)
    except TypeError:
        return redact_text(str(value))[:1800]
    if len(encoded) <= 4000:
        return redacted
    return encoded[:4000] + "…"


def _catalog_items(payload: Any) -> list[dict[str, Any]]:
    raw: Any = None
    if isinstance(payload, dict):
        raw = payload.get("items")
        if not isinstance(raw, list):
            raw = payload.get("products")
    elif isinstance(payload, list):
        raw = payload
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def select_catalog_item(payload: Any) -> dict[str, Any] | None:
    """First catalog row that is not explicitly out of stock."""
    for item in _catalog_items(payload):
        if item.get("availability") == "out_of_stock":
            continue
        return item
    return None


def _shown(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return str(value)


def _price_text(price: Any) -> str:
    if isinstance(price, dict):
        # Keep the merchant's own fields. Do not guess whether "amount" is cents.
        return _shown(price)
    if price is None:
        return "unpriced"
    return str(price)


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def _endpoint_from_service(value: Any) -> str | None:
    if isinstance(value, dict):
        rest = value.get("rest")
        if isinstance(rest, dict) and rest.get("endpoint"):
            return _strip_query(str(rest["endpoint"]))
        if value.get("endpoint"):
            return _strip_query(str(value["endpoint"]))
        for child in value.values():
            found = _endpoint_from_service(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _endpoint_from_service(child)
            if found:
                return found
    return None


def _dedupe(names: list[str]) -> list[str]:
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _capability_names_in(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        caps = node.get("capabilities")
        if isinstance(caps, dict):
            found.extend(str(name) for name in caps)
        elif isinstance(caps, list):
            for cap in caps:
                if isinstance(cap, dict) and cap.get("name"):
                    found.append(str(cap["name"]))
                elif isinstance(cap, str):
                    found.append(cap)
        for key, child in node.items():
            if key == "capabilities":
                continue
            if isinstance(child, (dict, list)):
                found.extend(_capability_names_in(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_capability_names_in(child))
    return found


def _service_lines(services: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(services, dict):
        for name, value in services.items():
            endpoint = _endpoint_from_service(value)
            if endpoint:
                lines.append(f"{name} at {endpoint}")
    elif isinstance(services, list):
        endpoint = _endpoint_from_service(services)
        if endpoint:
            lines.append(endpoint)
    return lines


def _handler_ids(profile: dict[str, Any]) -> list[str]:
    found: list[str] = []
    ucp = profile.get("ucp") if isinstance(profile.get("ucp"), dict) else {}
    handlers = ucp.get("payment_handlers")
    values: list[Any]
    if isinstance(handlers, dict):
        values = list(handlers.values())
    elif isinstance(handlers, list):
        values = handlers
    else:
        values = []
    for value in values:
        seq = value if isinstance(value, list) else [value]
        for entry in seq:
            if isinstance(entry, dict) and entry.get("id"):
                found.append(str(entry["id"]))
            elif isinstance(entry, str):
                found.append(entry)
    payment = profile.get("payment") if isinstance(profile.get("payment"), dict) else {}
    listed = payment.get("handlers")
    if isinstance(listed, list):
        for entry in listed:
            if isinstance(entry, dict) and entry.get("id"):
                found.append(str(entry["id"]))
    return _dedupe(found)


def summarize_discovery(profile: Any, http: int) -> str:
    if not isinstance(profile, dict):
        return f"Discovery HTTP {http} did not return a UCP profile."
    ucp = profile.get("ucp") if isinstance(profile.get("ucp"), dict) else {}
    version = ucp.get("version") or "unknown"
    caps = _dedupe(_capability_names_in(ucp))
    services = _service_lines(ucp.get("services"))
    handlers = _handler_ids(profile)
    ap2 = any("ap2" in name for name in caps)
    parts = [f"Discovered UCP version {version} (HTTP {http})"]
    if services:
        parts.append("services: " + "; ".join(services))
    parts.append("capabilities: " + (", ".join(caps) if caps else "none listed"))
    if handlers:
        parts.append("payment handlers: " + ", ".join(handlers))
    demo = profile.get("_demo") if isinstance(profile.get("_demo"), dict) else {}
    if demo.get("money") is False:
        parts.append("merchant marks money=false")
    if ap2:
        parts.append("AP2 mandate capability is advertised (lab/stub until a real mandate is presented)")
    else:
        parts.append("no AP2 mandate capability on this profile")
    return "; ".join(parts) + "."


def summarize_catalog(payload: Any, http: int) -> str:
    items = _catalog_items(payload)
    chosen = select_catalog_item(payload)
    if chosen is None:
        return f"Catalog HTTP {http} did not include an in-stock product ({len(items)} row(s) seen)."
    sku = chosen.get("sku") or chosen.get("id")
    name = chosen.get("name") or chosen.get("title") or sku
    return (
        f"Catalog HTTP {http} lists {len(items)} product(s). "
        f"Selected in-stock {name} (sku {sku}) at {_price_text(chosen.get('price'))}. "
        "Mock catalog only — no charge."
    )


def _line_labels(payload: dict[str, Any]) -> list[str]:
    items = payload.get("line_items")
    if not isinstance(items, list):
        return []
    labels: list[str] = []
    for line in items:
        if not isinstance(line, dict):
            continue
        nested = line.get("item") if isinstance(line.get("item"), dict) else {}
        label = line.get("sku") or nested.get("sku") or nested.get("id") or line.get("id")
        if label:
            labels.append(str(label))
    return labels


def summarize_checkout_created(payload: Any, http: int) -> str:
    if not isinstance(payload, dict) or not payload.get("id"):
        err = payload.get("error") if isinstance(payload, dict) else None
        return f"Checkout create HTTP {http} did not return a session id. {err or ''}".strip()
    labels = ", ".join(_line_labels(payload)) or "n/a"
    ap2_note = ""
    if isinstance(payload.get("ap2"), dict):
        ap2_note = " Merchant returned an AP2 authorization placeholder (redacted in the snippet)."
    return (
        f"Created checkout {payload.get('id')} status {payload.get('status')} (HTTP {http}); "
        f"items {labels}; totals {_shown(payload.get('totals'))}.{ap2_note} No charge."
    )


def summarize_checkout_get(payload: Any, http: int, *, attempt: int | None = None) -> str:
    prefix = f"Get attempt {attempt}: " if attempt else ""
    if http == 404:
        return (
            f"{prefix}Checkout get HTTP 404. "
            "On the live Worker this often means create and get hit different isolates."
        )
    if not isinstance(payload, dict):
        return f"{prefix}Checkout get HTTP {http}."
    return (
        f"{prefix}Checkout {payload.get('id')} status {payload.get('status')} (HTTP {http})."
    )


def summarize_complete(payload: Any, http: int) -> str:
    if not isinstance(payload, dict):
        return f"Complete HTTP {http}. No real charge."
    status = payload.get("status")
    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    order_id = order.get("id")
    order_status = order.get("status")
    if http == 200 and status in {"completed", "complete"}:
        return (
            f"Mock complete succeeded (HTTP {http}). "
            f"Order {order_id} status {order_status or status}. No real charge."
        )
    err = payload.get("error")
    if err is None and isinstance(payload.get("messages"), list):
        err = payload.get("messages")
    return f"Complete HTTP {http}, checkout status {status}, order {order_id}. Detail: {err}. No real charge."


def summarize_ap2_request(meta: dict[str, Any]) -> str:
    return (
        "Decided to attach AP2 lab placeholders only: "
        f"checkout {meta.get('vct_checkout')} and payment {meta.get('vct_payment')} "
        f"via handler {meta.get('handler_id')}, amount {_shown(meta.get('amount'))}. "
        "Fixtures are not verifiable and are not a real mandate. No charge."
    )


def summarize_ap2_response(payload: Any, http: int) -> str:
    ap2 = payload.get("ap2") if isinstance(payload, dict) and isinstance(payload.get("ap2"), dict) else {}
    checkout_ok = bool(ap2.get("checkout_mandate_received"))
    payment_ok = bool(ap2.get("payment_mandate_received"))
    if checkout_ok and payment_ok:
        return (
            f"Merchant accepted the lab AP2 placeholders (HTTP {http}): "
            "checkout mandate received and payment mandate received. "
            "Still not verifiable. No charge."
        )
    return (
        f"AP2 lab step finished with HTTP {http}. "
        f"checkout_mandate_received={checkout_ok}, payment_mandate_received={payment_ok}. No charge."
    )


class InteractionLog:
    def __init__(self, mode: str, root: Path):
        self.mode = mode
        self.root = root
        self.started = datetime.now(timezone.utc)
        self.entries: list[dict[str, Any]] = []
        self.result_note = ""

    def record(
        self,
        *,
        step: str,
        direction: str,
        summary: str,
        status: int | None,
        payload: Any,
    ) -> None:
        if direction not in {"request", "response"}:
            raise ValueError(f"direction must be request or response, got {direction!r}")
        self.entries.append(
            {
                "timestamp": _now(),
                "step": step,
                "direction": direction,
                "summary": redact_text(summary),
                "status": status,
                "payload_snippet": snippet(payload),
            }
        )

    def set_result(self, text: str) -> None:
        self.result_note = redact_text(text)

    def result_text(self, exit_code: int) -> str:
        if self.result_note:
            return self.result_note
        labels = {
            0: "Run finished successfully. No real charge.",
            1: "Run finished incomplete. No real charge.",
            2: "Run refused before the protocol walk finished. No real charge.",
            3: "Demo blocked because the gate is closed. No real charge.",
        }
        return labels.get(exit_code, f"Run exited {exit_code}. No real charge.")

    def narrative_lines(self) -> list[str]:
        lines: list[str] = []
        for index, entry in enumerate(self.entries, 1):
            status = "—" if entry["status"] is None else f"HTTP {entry['status']}"
            lines.append(
                f"{index}. {entry['step']} ({entry['direction']}, {status}) — {entry['summary']}"
            )
        return lines

    def _document(self, exit_code: int, finished: datetime) -> dict[str, Any]:
        seen: list[str] = []
        for entry in self.entries:
            if entry["step"] not in seen:
                seen.append(entry["step"])
        covered = {step: step in seen for step in PROTOCOL_STEPS}
        if self.mode == "live-ucp" and not covered["ap2_mandate"]:
            ap2_note = "not run — this merchant does not advertise AP2"
        elif not covered["ap2_mandate"]:
            ap2_note = "not run"
        else:
            ap2_note = "yes — lab placeholders only, not verifiable"
        return {
            "mode": self.mode,
            "started_at": _iso(self.started),
            "finished_at": _iso(finished),
            "exit_code": exit_code,
            "money": False,
            "result": self.result_text(exit_code),
            "steps": seen,
            "protocol_steps": covered,
            "ap2_mandate": ap2_note,
            "narrative": self.narrative_lines(),
            "entries": self.entries,
        }

    def _markdown(self, doc: dict[str, Any]) -> str:
        lines = [
            f"# Demo report — {doc['mode']}",
            "",
            "Walk the numbered list top to bottom during the demo "
            "(discovery, catalog, checkout, complete, and the AP2 lab step when this run sent one). "
            "Payload snippets are redacted: no tokens, mandate material, signing keys, or contact fields.",
            "",
            f"- **Mode:** `{doc['mode']}`",
            f"- **Started:** {doc['started_at']}",
            f"- **Finished:** {doc['finished_at']}",
            f"- **Exit code:** {doc['exit_code']}",
            "- **Money:** false (mock only, no charge)",
            f"- **Result:** {doc['result']}",
            f"- **AP2:** {doc['ap2_mandate']}",
            "",
            "## Protocol steps",
            "",
            "| Step | In this run |",
            "| --- | --- |",
        ]
        labels = {
            "discovery": "discovery (`/.well-known/ucp`)",
            "catalog": "catalog / products",
            "checkout_create": "checkout create",
            "checkout_complete": "mock complete",
            "ap2_mandate": "AP2 mandate (stub)",
        }
        for step, label in labels.items():
            flag = "yes" if doc["protocol_steps"].get(step) else "no"
            lines.append(f"| {label} | {flag} |")
        lines.extend(["", "## What the agent did", ""])
        if doc["narrative"]:
            lines.extend(doc["narrative"])
        else:
            lines.append("_No protocol steps were recorded._")
        lines.extend(["", "## Interaction log", ""])
        if not doc["entries"]:
            lines.append("_No interaction entries._")
        for index, entry in enumerate(doc["entries"], 1):
            status = "—" if entry["status"] is None else str(entry["status"])
            lines.append(f"### {index}. {entry['step']} · {entry['direction']}")
            lines.append("")
            lines.append(f"- **Timestamp:** {entry['timestamp']}")
            lines.append(f"- **Summary:** {entry['summary']}")
            lines.append(f"- **Status:** {status}")
            lines.append("- **Payload snippet:**")
            lines.append("")
            payload = entry["payload_snippet"]
            if isinstance(payload, str):
                fenced = payload.replace("```", "'''")
                lines.append("```")
                lines.append(fenced)
                lines.append("```")
            else:
                fenced = json.dumps(payload, indent=2, ensure_ascii=False).replace("```", "'''")
                lines.append("```json")
                lines.append(fenced)
                lines.append("```")
            lines.append("")
        lines.extend(
            [
                "---",
                "",
                "Newest copy for the next demo: `reports/latest.md`. "
                "Archives are `reports/demo-report-<mode>-<utc>.md`. "
                "Machine-readable copies: `reports/latest.json` and `logs/latest.jsonl`.",
                "",
            ]
        )
        return "\n".join(lines)

    def write(self, exit_code: int) -> dict[str, str]:
        finished = datetime.now(timezone.utc)
        logs = self.root / "logs"
        reports = self.root / "reports"
        logs.mkdir(parents=True, exist_ok=True)
        reports.mkdir(parents=True, exist_ok=True)
        stamp = self.started.strftime("%Y%m%dT%H%M%SZ")
        jsonl_name = f"interactions-{self.mode}-{stamp}.jsonl"
        md_name = f"demo-report-{self.mode}-{stamp}.md"
        json_name = f"demo-report-{self.mode}-{stamp}.json"
        doc = self._document(exit_code, finished)
        md = self._markdown(doc)
        json_text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        jsonl = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in doc["entries"])
        (logs / jsonl_name).write_text(jsonl, encoding="utf-8")
        (logs / "latest.jsonl").write_text(jsonl, encoding="utf-8")
        (reports / md_name).write_text(md, encoding="utf-8")
        (reports / "latest.md").write_text(md, encoding="utf-8")
        (reports / json_name).write_text(json_text, encoding="utf-8")
        (reports / "latest.json").write_text(json_text, encoding="utf-8")
        return {
            "markdown": f"reports/{md_name}",
            "markdown_latest": "reports/latest.md",
            "json": f"reports/{json_name}",
            "json_latest": "reports/latest.json",
            "interactions": f"logs/{jsonl_name}",
            "interactions_latest": "logs/latest.jsonl",
        }
