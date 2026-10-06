"""Load orchestrator reports for the presenter dashboard.

Reads the files ``run_demo`` already writes:

  reports/latest.json
  reports/demo-report-<mode>-<utc>.json
  logs/latest.jsonl
  logs/interactions-<mode>-<utc>.jsonl

Does not call the merchant, the gate, or any payment endpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from orchestrator.interaction_log import redact_payload, redact_text

_ARCHIVE_JSON = re.compile(r"^demo-report-(.+)-(\d{8}T\d{6}Z)\.json$")
_ARCHIVE_JSONL = re.compile(r"^interactions-(.+)-(\d{8}T\d{6}Z)\.jsonl$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,160}$")

_GATE_STEPS = {"gate", "gate_health", "kill_switch"}
_MAX_ENTRIES = 500
_MAX_SNIPPET_CHARS = 12000


def protocol_for(step: str) -> str:
    """Classify a log step as UCP, AP2, or the ArGENTine gate."""
    name = (step or "").lower()
    if name == "ap2_mandate" or name.startswith("ap2"):
        return "ap2"
    if name in _GATE_STEPS or name.startswith("gate"):
        return "gate"
    return "ucp"


def clean_url(url: str | None) -> str | None:
    """Keep scheme, host, port, and path. Drop userinfo and query strings."""
    if not isinstance(url, str):
        return None
    raw = url.split("#", 1)[0].split("?", 1)[0].strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    path = parts.path or ""
    if len(path) > 1:
        path = path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def _netloc(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _safe_child(directory: Path, path: Path) -> Path | None:
    try:
        root = directory.resolve()
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return None
        if isinstance(row, dict):
            entries.append(row)
    return entries


def _payload_url(payload: Any) -> str | None:
    if isinstance(payload, dict):
        url = payload.get("url")
        if isinstance(url, str):
            return url
    return None


def _payload_method(payload: Any) -> str | None:
    if isinstance(payload, dict):
        method = payload.get("method")
        if isinstance(method, str) and method.strip():
            return method.strip().upper()[:16]
    return None


def _coerce_status(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _snippet_for_display(payload: Any, url: str | None) -> Any:
    safe = redact_payload(payload)
    if isinstance(safe, str) and len(safe) > _MAX_SNIPPET_CHARS:
        safe = safe[:_MAX_SNIPPET_CHARS] + "…"
    if isinstance(safe, dict) and url:
        safe = dict(safe)
        safe["url"] = url
    if isinstance(safe, (dict, list)):
        encoded = json.dumps(safe, ensure_ascii=False)
        if len(encoded) > _MAX_SNIPPET_CHARS:
            return encoded[:_MAX_SNIPPET_CHARS] + "…"
    return safe


def _harvest_advertised(payload: Any, found: list[str], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "endpoint" and isinstance(value, str):
                found.append(value)
            elif key != "url":
                _harvest_advertised(value, found, depth + 1)
    elif isinstance(payload, list):
        for item in payload:
            _harvest_advertised(item, found, depth + 1)


def _origin_label(origin: str, protocols: set[str]) -> str:
    host = urlsplit(origin).hostname or ""
    if "gate" in protocols:
        return "ArGENTine gate"
    if host in {"127.0.0.1", "localhost"}:
        return "Local UCP stub"
    if host.endswith(".workers.dev"):
        return "Live UCP API"
    if host.endswith(".web.app"):
        return "Live UCP discovery"
    return "Connected host"


def annotate_entries(entries: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach URL, host, and protocol to each entry. Return connections too."""
    pending: dict[str, list[str]] = {}
    annotated: list[dict[str, Any]] = []
    called: dict[str, dict[str, Any]] = {}
    advertised_raw: list[str] = []

    for raw in entries[:_MAX_ENTRIES]:
        if not isinstance(raw, dict):
            continue
        step = str(raw.get("step") or "unknown")
        direction = raw.get("direction") if raw.get("direction") in {"request", "response"} else "event"
        payload = raw.get("payload_snippet")
        cleaned = clean_url(_payload_url(payload))
        if direction == "request":
            if cleaned:
                pending.setdefault(step, []).append(cleaned)
            url = cleaned
        elif direction == "response":
            queue = pending.get(step) or []
            url = queue.pop(0) if queue else cleaned
            if step.startswith("discovery"):
                _harvest_advertised(payload, advertised_raw)
        else:
            url = cleaned

        protocol = protocol_for(step)
        method = _payload_method(payload) if direction == "request" else None
        row = {
            "timestamp": redact_text(str(raw.get("timestamp") or "")),
            "step": step,
            "direction": direction,
            "summary": redact_text(str(raw.get("summary") or "")),
            "status": _coerce_status(raw.get("status")),
            "protocol": protocol,
            "method": method,
            "url": url,
            "origin": _origin(url) if url else None,
            "host": _netloc(url) if url else None,
            "payload_snippet": _snippet_for_display(payload, url),
        }
        annotated.append(row)

        if direction == "request" and url:
            origin = row["origin"]
            bucket = called.get(origin)
            if bucket is None:
                bucket = {
                    "origin": origin,
                    "host": row["host"],
                    "protocols": set(),
                    "steps": [],
                    "endpoints": {},
                }
                called[origin] = bucket
            bucket["protocols"].add(protocol)
            if step not in bucket["steps"]:
                bucket["steps"].append(step)
            key = (method or "", url)
            endpoint = bucket["endpoints"].get(key)
            if endpoint is None:
                bucket["endpoints"][key] = {"method": method, "url": url, "calls": 1}
            else:
                endpoint["calls"] += 1

    connections: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for bucket in called.values():
        protocols = set(bucket["protocols"])
        endpoints = list(bucket["endpoints"].values())
        for endpoint in endpoints:
            seen_urls.add(endpoint["url"])
        connections.append(
            {
                "origin": bucket["origin"],
                "host": bucket["host"],
                "label": _origin_label(bucket["origin"], protocols),
                "protocols": sorted(protocols),
                "steps": bucket["steps"],
                "endpoints": endpoints,
            }
        )

    called_origins = set(called)
    advertised: list[dict[str, str]] = []
    seen_adv: set[str] = set()
    for raw_url in advertised_raw:
        url = clean_url(raw_url)
        if not url or url in seen_urls or url in seen_adv:
            continue
        # The connection cards already list hosts the bot called.
        if _origin(url) in called_origins:
            continue
        seen_adv.add(url)
        advertised.append({"url": url, "origin": _origin(url), "host": _netloc(url)})

    return annotated, connections, advertised


def _covered(entries: list[dict[str, Any]], existing: Any) -> dict[str, bool]:
    from orchestrator.interaction_log import PROTOCOL_STEPS

    seen = {entry["step"] for entry in entries}
    covered = {step: step in seen for step in PROTOCOL_STEPS}
    if isinstance(existing, dict):
        for step in PROTOCOL_STEPS:
            if existing.get(step) is True:
                covered[step] = True
    return covered


def prepare_document(doc: dict[str, Any], *, run_id: str, source: str, is_latest: bool) -> dict[str, Any]:
    raw_entries = doc.get("entries") if isinstance(doc.get("entries"), list) else []
    entries, connections, advertised = annotate_entries(raw_entries)
    narrative = doc.get("narrative") if isinstance(doc.get("narrative"), list) else []
    safe_narrative = [redact_text(str(line)) for line in narrative[:_MAX_ENTRIES]]
    exit_code = doc.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    return {
        "id": run_id,
        "source": source,
        "is_latest": is_latest,
        "mode": redact_text(str(doc.get("mode") or "unknown")),
        "started_at": redact_text(str(doc.get("started_at") or "")),
        "finished_at": redact_text(str(doc.get("finished_at") or "")),
        "exit_code": exit_code,
        "money": doc.get("money") if isinstance(doc.get("money"), bool) else False,
        "result": redact_text(str(doc.get("result") or "")),
        "ap2_mandate": redact_text(str(doc.get("ap2_mandate") or "")),
        "protocol_steps": _covered(entries, doc.get("protocol_steps")),
        "narrative": safe_narrative,
        "entries": entries,
        "entry_count": len(entries),
        "truncated": len(raw_entries) > _MAX_ENTRIES,
        "connections": connections,
        "advertised_endpoints": advertised,
    }


def _summary(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["id"],
        "source": doc["source"],
        "is_latest": doc["is_latest"],
        "mode": doc["mode"],
        "started_at": doc["started_at"],
        "finished_at": doc["finished_at"],
        "exit_code": doc["exit_code"],
        "money": doc["money"],
        "result": doc["result"],
        "ap2_mandate": doc["ap2_mandate"],
        "entry_count": doc["entry_count"],
    }


def _identity(doc: dict[str, Any]) -> tuple[str, str, str]:
    return (str(doc.get("mode") or ""), str(doc.get("started_at") or ""), str(doc.get("finished_at") or ""))


def _jsonl_document(mode: str, stamp: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    started = entries[0].get("timestamp") if entries else ""
    finished = entries[-1].get("timestamp") if entries else ""
    return {
        "mode": mode,
        "started_at": started or stamp,
        "finished_at": finished or stamp,
        "exit_code": None,
        "money": False,
        "result": "Loaded from the interaction log. The JSON report for this run was not beside it.",
        "ap2_mandate": "",
        "entries": entries,
    }


def load_index(root: Path) -> list[dict[str, Any]]:
    """Newest-first run summaries from reports/ and, if needed, logs/."""
    reports = root / "reports"
    logs = root / "logs"
    found: list[tuple[str, str, dict[str, Any]]] = []

    if reports.is_dir():
        for path in sorted(reports.iterdir(), key=lambda item: item.name):
            if not _ARCHIVE_JSON.match(path.name):
                continue
            safe = _safe_child(reports, path)
            if safe is None:
                continue
            doc = _read_json(safe)
            if doc is None:
                found.append((safe.stem, f"reports/{safe.name}", {"mode": "unreadable", "entries": [], "result": "Could not read this report."}))
            else:
                found.append((safe.stem, f"reports/{safe.name}", doc))

    seen_ids = {run_id for run_id, _, _ in found}
    if logs.is_dir():
        for path in sorted(logs.iterdir(), key=lambda item: item.name):
            match = _ARCHIVE_JSONL.match(path.name)
            if not match:
                continue
            json_id = f"demo-report-{match.group(1)}-{match.group(2)}"
            if json_id in seen_ids:
                continue
            safe = _safe_child(logs, path)
            if safe is None:
                continue
            entries = _read_jsonl(safe)
            if entries is None:
                continue
            doc = _jsonl_document(match.group(1), match.group(2), entries)
            found.append((safe.stem, f"logs/{safe.name}", doc))

    latest_identity: tuple[str, str, str] | None = None
    latest_only: dict[str, Any] | None = None
    latest_path = reports / "latest.json"
    if reports.is_dir():
        safe_latest = _safe_child(reports, latest_path)
        if safe_latest is not None:
            latest_doc = _read_json(safe_latest)
            if latest_doc is not None:
                latest_identity = _identity(latest_doc)
                latest_only = latest_doc

    prepared: list[dict[str, Any]] = []
    matched_latest = False
    for run_id, source, doc in found:
        is_latest = latest_identity is not None and _identity(doc) == latest_identity
        if is_latest:
            matched_latest = True
        prepared.append(prepare_document(doc, run_id=run_id, source=source, is_latest=is_latest))

    if latest_only is not None and not matched_latest:
        prepared.append(
            prepare_document(latest_only, run_id="latest", source="reports/latest.json", is_latest=True)
        )

    prepared.sort(key=lambda item: (item.get("started_at") or "", item.get("id") or ""), reverse=True)
    if prepared and not any(item["is_latest"] for item in prepared):
        prepared[0]["is_latest"] = True
    return prepared


def load_run(root: Path, run_id: str) -> dict[str, Any] | None:
    """Return one annotated run, or None when the id is unknown."""
    if not _RUN_ID.match(run_id):
        return None
    runs = load_index(root)
    if run_id == "latest":
        for run in runs:
            if run.get("is_latest"):
                return run
        return runs[0] if runs else None
    for run in runs:
        if run.get("id") == run_id:
            return run
    return None


def list_runs(root: Path) -> list[dict[str, Any]]:
    return [_summary(run) for run in load_index(root)]
