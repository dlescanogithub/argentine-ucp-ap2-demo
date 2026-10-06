# argentine-ucp-ap2-demo

Local **UCP** stub + **live hemanth UCP** smoke + **AP2** lab fixtures + orchestrator against the live **ArGENTine** HITL gate on Railway.

Does **not** modify [`dlescanogithub/argentine-a2a`](https://github.com/dlescanogithub/argentine-a2a). No deploy of this stub to Railway.

Diego never auto-pays. A2A Registry listing for ArGENTine does **not** certify UCP/AP2.

## Modes

| Mode | Merchant | Gate expectation |
| --- | --- | --- |
| `smoke` | Local stub `:9871` | `NEED_HUMAN` if open; `503 diego_off` if shut |
| `demo` | Local stub | Mechanical `GO` (HITL markers); **requires gate open** |
| `abort` | Local stub | Engage kill → next call `diego_off`; no complete |
| **`live-ucp`** | **hemanth live** | **Gate stays CLOSED** → expect `503 diego_off`. GO = later open window |

## Live UCP (`live-ucp`)

| | |
| --- | --- |
| Discovery | https://ucp-demo.web.app/.well-known/ucp |
| API | https://ucp-demo-api.hemanthhm.workers.dev |
| Auth | None observed |
| Money | **Mock only** — `mock-payment-handler` + `success_token`. Hard-refuse `card-handler` |
| AP2 | **None** on this merchant — do not claim AP2 |
| Known flake | Worker stores checkouts in an **in-memory `Map`**. `create` often returns 201, then `get`/`cancel`/`complete` return **404** (different isolate). Smoke retries get; documents flake; still requires catalog+create+gate 503 |

```bash
python3 orchestrator/run_demo.py live-ucp
# or: ./scripts/smoke.sh live-ucp
```

Flow: discovery → catalog (pick in-stock SKU) → create checkout → get (retries) → complete(mock) if get OK → gate call with kill engaged → expect `fails:["diego_off"]`.

**Do not open Railway** for this mode. Live `GO` needs a separate Diego-approved open window (as in the phase-1 demo take).

## Local stub (phase 1)

```bash
python3 merchant/server.py --host 127.0.0.1 --port 9871
python3 orchestrator/run_demo.py smoke
python3 orchestrator/run_demo.py demo    # gate must be open
python3 orchestrator/run_demo.py abort
```

Stub serves `/.well-known/ucp`, checkout REST, USD 0, AP2 **lab placeholders** (not verifiable).

## Requirements

- Python 3.12+ (stdlib only)
- Secrets (never commit / never print):
  - `/home/box/secrets/argentine-partner-caller.env`
  - `/home/box/secrets/argentine-admin.env` — **abort** only

## Secrets

Orchestrator prints `decision` / `fails` / health flags only — **never** bearer tokens.

## Non-goals

- No PSP / no real card charge
- No Human Not Present / open AP2 mandates on live hemanth
- No changes to ArGENTine gate / `GATE_RETURNS.md`
- No merge without Diego after smoke OK

## License

MIT
