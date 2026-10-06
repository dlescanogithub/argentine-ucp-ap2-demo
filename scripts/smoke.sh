#!/usr/bin/env bash
# Smoke helper. Never echoes secrets.
# The orchestrator also writes reports/latest.md (redacted interaction report).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-smoke}"
cd "$ROOT"
exec python3 orchestrator/run_demo.py "$MODE"
