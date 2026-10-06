#!/usr/bin/env bash
# Phase-1 smoke/abort helper. Never echoes secrets.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-smoke}"
cd "$ROOT"
exec python3 orchestrator/run_demo.py "$MODE"
