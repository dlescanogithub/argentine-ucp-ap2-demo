#!/usr/bin/env bash
# Read-only presenter dashboard. Never calls the gate or a payment endpoint.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 dashboard/server.py "$@"
