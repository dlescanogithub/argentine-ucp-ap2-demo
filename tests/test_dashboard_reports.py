"""Dashboard reads redacted report files and does not echo secrets."""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.reports import list_runs, load_run  # noqa: E402
from dashboard.server import make_server  # noqa: E402
from orchestrator.interaction_log import InteractionLog  # noqa: E402

SECRET = "SUPERSECRETVALUE"


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _entry(**fields) -> dict:
    base = {
        "timestamp": "2026-10-06T22:40:01Z",
        "step": "discovery",
        "direction": "request",
        "summary": "discovery",
        "status": None,
        "payload_snippet": {},
    }
    base.update(fields)
    return base


class ReportLoaderTests(unittest.TestCase):
    def test_pairs_urls_and_splits_ucp_ap2_gate(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = {
                "mode": "live-ucp",
                "started_at": "2026-10-06T22:00:00Z",
                "finished_at": "2026-10-06T22:00:05Z",
                "exit_code": 0,
                "money": False,
                "result": "Live catalog only. No AP2.",
                "ap2_mandate": "not run — this merchant does not advertise AP2",
                "protocol_steps": {"discovery": True, "catalog": True, "ap2_mandate": False},
                "entries": [
                    _entry(
                        timestamp="2026-10-06T22:00:01Z",
                        step="discovery",
                        summary="GET live discovery",
                        payload_snippet={
                            "method": "GET",
                            "url": "https://ucp-demo.web.app/.well-known/ucp",
                        },
                    ),
                    _entry(
                        timestamp="2026-10-06T22:00:02Z",
                        step="discovery",
                        direction="response",
                        summary="Discovered live UCP",
                        status=200,
                        payload_snippet={
                            "ucp": {
                                "services": {
                                    "dev.ucp.shopping": {
                                        "rest": {"endpoint": "https://ucp-demo-api.hemanthhm.workers.dev"}
                                    }
                                }
                            }
                        },
                    ),
                ],
            }
            newer = {
                "mode": "smoke",
                "started_at": "2026-10-06T22:40:00Z",
                "finished_at": "2026-10-06T22:40:08Z",
                "exit_code": 0,
                "money": False,
                "result": "Smoke finished. No charge.",
                "ap2_mandate": "yes — lab placeholders only, not verifiable",
                "entries": [
                    _entry(
                        step="discovery",
                        summary="GET /.well-known/ucp",
                        payload_snippet={"method": "GET", "url": "http://127.0.0.1:9871/.well-known/ucp"},
                    ),
                    _entry(
                        timestamp="2026-10-06T22:40:02Z",
                        step="discovery",
                        direction="response",
                        summary="Discovered local UCP",
                        status=200,
                        payload_snippet={"ucp": {"version": "2026-08-25"}},
                    ),
                    _entry(
                        timestamp="2026-10-06T22:40:03Z",
                        step="ap2_mandate",
                        summary="Attach lab AP2 placeholders",
                        payload_snippet={
                            "method": "POST",
                            "url": "http://127.0.0.1:9871/ucp/v1/checkout-sessions/chk_demo/complete",
                        },
                    ),
                    _entry(
                        timestamp="2026-10-06T22:40:04Z",
                        step="ap2_mandate",
                        direction="response",
                        summary="Merchant accepted lab placeholders",
                        status=200,
                        payload_snippet={"ap2": {"checkout_mandate_received": True}},
                    ),
                    _entry(
                        timestamp="2026-10-06T22:40:05Z",
                        step="gate",
                        summary="Ask the gate",
                        payload_snippet={
                            "method": "POST",
                            "url": "https://argentine-a2a-production.up.railway.app/v1/gate",
                        },
                    ),
                    _entry(
                        timestamp="2026-10-06T22:40:06Z",
                        step="gate",
                        direction="response",
                        summary="Gate refused",
                        status=503,
                        payload_snippet={"decision": "NO_GO", "fails": ["diego_off"]},
                    ),
                ],
            }
            _write(root / "reports" / "demo-report-live-ucp-20261006T220000Z.json", older)
            _write(root / "reports" / "demo-report-smoke-20261006T224000Z.json", newer)
            _write(root / "reports" / "latest.json", newer)

            runs = list_runs(root)
            self.assertEqual([run["id"] for run in runs], [
                "demo-report-smoke-20261006T224000Z",
                "demo-report-live-ucp-20261006T220000Z",
            ])
            self.assertTrue(runs[0]["is_latest"])
            self.assertFalse(runs[1]["is_latest"])

            smoke = load_run(root, "latest")
            assert smoke is not None
            self.assertEqual(smoke["id"], "demo-report-smoke-20261006T224000Z")
            by_step = {}
            for entry in smoke["entries"]:
                by_step.setdefault(entry["step"], []).append(entry)
            discovery_response = by_step["discovery"][1]
            self.assertEqual(discovery_response["url"], "http://127.0.0.1:9871/.well-known/ucp")
            self.assertEqual(discovery_response["protocol"], "ucp")
            self.assertEqual(discovery_response["status"], 200)
            ap2_response = by_step["ap2_mandate"][1]
            self.assertEqual(ap2_response["protocol"], "ap2")
            self.assertIn("/complete", ap2_response["url"])
            self.assertEqual(by_step["gate"][1]["protocol"], "gate")
            self.assertEqual(by_step["gate"][1]["status"], 503)

            origins = {item["origin"]: item for item in smoke["connections"]}
            self.assertIn("http://127.0.0.1:9871", origins)
            self.assertEqual(origins["http://127.0.0.1:9871"]["label"], "Local UCP stub")
            self.assertEqual(
                origins["https://argentine-a2a-production.up.railway.app"]["label"],
                "ArGENTine gate",
            )

            live = load_run(root, "demo-report-live-ucp-20261006T220000Z")
            assert live is not None
            advertised = [item["url"] for item in live["advertised_endpoints"]]
            self.assertIn("https://ucp-demo-api.hemanthhm.workers.dev", advertised)
            self.assertEqual(live["connections"][0]["label"], "Live UCP discovery")

    def test_display_redacts_secrets_and_strips_url_credentials(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = {
                "mode": "abort",
                "started_at": "2026-10-06T23:00:00Z",
                "finished_at": "2026-10-06T23:00:04Z",
                "exit_code": 0,
                "money": False,
                "result": f"done Bearer {SECRET}",
                "entries": [
                    _entry(
                        step="gate",
                        summary=f"Authorization: Bearer {SECRET}",
                        payload_snippet={
                            "method": "POST",
                            "url": f"https://user:{SECRET}@argentine.example/v1/gate?token={SECRET}",
                            "checkout_mandate": SECRET,
                            "nested": {"email": "diego@example.com"},
                        },
                    )
                ],
            }
            _write(root / "reports" / "latest.json", doc)
            run = load_run(root, "latest")
            assert run is not None
            blob = json.dumps(run)
            self.assertNotIn(SECRET, blob)
            self.assertNotIn("diego@example.com", blob)
            self.assertEqual(run["entries"][0]["url"], "https://argentine.example/v1/gate")
            self.assertEqual(run["entries"][0]["host"], "argentine.example")
            self.assertNotIn("user:", run["entries"][0]["url"])

    def test_rejects_path_escape_and_reads_jsonl_fallback(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(load_run(root, "../secrets"))
            self.assertIsNone(load_run(root, "latest"))
            line = json.dumps(
                _entry(
                    step="catalog",
                    summary="catalog",
                    payload_snippet={"method": "GET", "url": "http://127.0.0.1:9871/ucp/v1/products"},
                )
            )
            path = root / "logs" / "interactions-smoke-20261006T210000Z.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(line + "\n", encoding="utf-8")
            runs = list_runs(root)
            self.assertEqual(len(runs), 1)
            self.assertTrue(runs[0]["is_latest"])
            self.assertEqual(runs[0]["mode"], "smoke")
            loaded = load_run(root, runs[0]["id"])
            assert loaded is not None
            self.assertEqual(loaded["entries"][0]["protocol"], "ucp")
            self.assertEqual(loaded["connections"][0]["host"], "127.0.0.1:9871")

    def test_interaction_log_files_round_trip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = InteractionLog("demo", root)
            log.record(
                step="catalog",
                direction="request",
                summary="GET the merchant catalog",
                status=None,
                payload={"method": "GET", "url": "http://127.0.0.1:9871/ucp/v1/products?token=ignore"},
            )
            log.record(
                step="catalog",
                direction="response",
                summary="Catalog lists one product",
                status=200,
                payload={"products": [{"sku": "demo-sticker", "price": {"currency": "USD", "value": 0}}]},
            )
            log.write(0)
            run = load_run(root, "latest")
            assert run is not None
            self.assertEqual(run["mode"], "demo")
            self.assertEqual(run["entries"][1]["status"], 200)
            self.assertEqual(run["entries"][1]["protocol"], "ucp")
            self.assertTrue(run["entries"][1]["url"].startswith("http://127.0.0.1:9871/"))
            self.assertNotIn("token", run["entries"][0]["url"])


class DashboardServerTests(unittest.TestCase):
    def test_http_is_read_only_and_serves_latest(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = {
                "mode": "smoke",
                "started_at": "2026-10-06T22:40:00Z",
                "finished_at": "2026-10-06T22:40:02Z",
                "exit_code": 0,
                "money": False,
                "result": "Smoke finished. No charge.",
                "entries": [
                    _entry(
                        payload_snippet={"method": "GET", "url": "http://127.0.0.1:9871/health"},
                        summary="health",
                        step="merchant",
                    )
                ],
            }
            _write(root / "reports" / "latest.json", doc)
            server = make_server(root, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                    html = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn("connect-src 'self'", resp.headers.get("Content-Security-Policy", ""))
                self.assertIn("UCP / AP2 interaction report", html)
                self.assertIn("Where it connected", html)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs") as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(payload["runs"][0]["mode"], "smoke")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs/latest") as resp:
                    detail = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(detail["entries"][0]["host"], "127.0.0.1:9871")
                request = urllib.request.Request(f"http://127.0.0.1:{port}/api/runs", method="POST", data=b"{}")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 405)
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs/..%2F..%2Fsecrets")
                self.assertEqual(missing.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
