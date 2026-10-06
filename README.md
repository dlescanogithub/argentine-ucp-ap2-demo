# argentine-ucp-ap2-demo (phase 1)

Local **UCP** stub merchant + **AP2** mandate fixtures + orchestrator that calls the live **ArGENTine** HITL gate on Railway.

**Phase 1 only.** No Railway deploy of this stub. Does **not** modify [`dlescanogithub/argentine-a2a`](https://github.com/dlescanogithub/argentine-a2a).

Diego never auto-pays. Happy path is Human Present + gate `GO`. Automated smoke shows `NEED_HUMAN` (or `diego_off` if the kill is engaged) and documents that a live GO needs Diego.

A2A Registry listing for ArGENTine does **not** certify UCP/AP2.

## What you get

| Piece | Path | Role |
| --- | --- | --- |
| UCP discovery | `GET /.well-known/ucp` | Business profile (checkout + AP2 mandate capability) |
| Checkout REST | `POST/GET .../checkout-sessions`, `.../complete`, `.../cancel` | Mock cart; amount **USD 0** |
| Platform profile | `/platform/profile.json` | Advertised via `UCP-Agent` header |
| AP2 fixtures | `fixtures/ap2/*.placeholder.txt` | SD-JWT-**shaped** lab placeholders (not verifiable) |
| Orchestrator | `orchestrator/run_demo.py` | `smoke` \| `demo` \| `abort` |

## Requirements

- Python 3.12+ (stdlib only)
- Partner allowlist secrets (never commit / never print):
  - `/home/box/secrets/argentine-partner-caller.env` (`ARGENTINE_CALLER_ID=partner`, token, gate URL)
  - `/home/box/secrets/argentine-admin.env` (`ARGENTINE_ADMIN_TOKEN`) — **abort** mode only

## Run locally

```bash
cd /workspace/argentine-ucp-ap2-demo   # or clone path

# Merchant only
python3 merchant/server.py --host 127.0.0.1 --port 9871
# → http://127.0.0.1:9871/.well-known/ucp
```

### Smoke (automated)

Local UCP discover → create → get → gate brief **without HITL** → local complete with AP2 placeholders.

```bash
python3 orchestrator/run_demo.py smoke
```

Expected gate outcomes:

- Gate **open**: HTTP 200 `NEED_HUMAN` (missing hitl / approved_until / digest / kill_path)
- Gate **shut** (`diego_off`): HTTP 503 `NO_GO` / `fails:["diego_off"]` — open the kill before NEED_HUMAN/GO demos

Local complete still runs (USD 0) so UCP/AP2 shape is proven either way.

### Demo (happy path shape)

Includes HITL markers for a mechanical `GO`. **Diego must open the gate.** Prefer a real approve in a live show.

```bash
python3 orchestrator/run_demo.py demo
```

If `diego_off` is true, exits early (code 3) and tells you to open the kill.

### Abort

Creates checkout, optionally probes the gate while open, **engages** `diego_off` via `POST /admin/kill` (engage-only), then shows the next gated call is `503` / `diego_off`. Cancels the local checkout; **does not complete**.

```bash
python3 orchestrator/run_demo.py abort
```

This demo **never clears** the kill (API is engage-only; Diego is custodian).

```bash
# One-shot helper
./scripts/smoke.sh          # smoke
./scripts/smoke.sh abort    # abort
```

## Secrets

Orchestrator loads env files from `/home/box/secrets/…`. It prints `decision` / `fails` / health flags only — **never** bearer tokens.

## What phase 2 will add (not now)

- Live sandbox partner merchant TBD (real `/.well-known/ucp` host, not localhost)
- Optional lab-signed SD-JWT (disposable keys) instead of placeholders
- Deploy story for the stub **only if Diego asks** (still not the main ArGENTine gate)
- Evidence pack under `docs/evidence/…` after a recorded demo
- Agent-card / Registry wording updates **only after** stub proof (separate PR on `argentine-a2a`)

## Explicit non-goals (phase 1)

- No PSP / no real card / no charge
- No Human Not Present / open mandates
- No changes to ArGENTine gate code or `GATE_RETURNS.md`
- No merge without Diego

## Open questions (live partner)

1. Who hosts the phase-2 UCP merchant sandbox (partner brand vs Diego lab)?
2. REST-only forever, or add A2A DataPart binding next?
3. Verifiable lab SD-JWT required for the live show, or placeholders enough?
4. When should Diego open `diego_off` for the GO take?
5. After abort engages file kill: restore path (clear file / boot flag) — who runs it?

## License

MIT (same spirit as ArGENTine). Demo stubs only.
