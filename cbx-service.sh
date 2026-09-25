#!/usr/bin/env bash
# Managed entrypoint for the Chatterbox sidecar (run by com.maestro.cbx via launchd).
#
# Resilience: launchd owns the lifecycle (KeepAlive restarts on death, RunAtLoad + relaunch
# on wake). This script just mints the per-boot identity and hands off to the sidecar.
# Security: mints a pairing TOKEN (rejects impostors on :8123) and a SIGNED ATTESTATION
# (proves the running build is the authorized, untampered one) before the sidecar starts.
#
# Manual/dev use is still ./chatterbox.sh (no token/attestation). This is the managed path.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${MAESTRO_ROOT:-$(cd "$HERE/../.." && pwd)}"
MAIN_PY="${CUE_VOX_PYTHON:-$HERE/.venv/bin/python}"          # has cryptography + signing
CBX_PY="$HERE/.venv-chatterbox/bin/python"                   # the heavy torch venv

[ -x "$MAIN_PY" ] || { echo "missing cue-vox venv python: $MAIN_PY" >&2; exit 1; }
[ -x "$CBX_PY" ]  || { echo "missing chatterbox venv: $CBX_PY (run setup-chatterbox.sh)" >&2; exit 1; }

export MAESTRO_ROOT="$ROOT"

# Mint token + signed attestation (stdout = the token; stderr = a log line).
CBX_TOKEN="$(MAESTRO_ROOT="$ROOT" "$MAIN_PY" "$HERE/cbx_attest.py")"
export CBX_TOKEN
export CBX_ATTEST="$ROOT/.claude/runtime/cbx-attest.json"
export CBX_PORT="${CBX_PORT:-8123}"
export CBX_REF="${CBX_REF:-$HERE/models/ref-0.wav}"

echo "[cbx-service] identity minted, starting sidecar on :$CBX_PORT" >&2
exec "$CBX_PY" "$HERE/chatterbox_server.py"
