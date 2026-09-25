#!/bin/bash
# Dev mode - runs on port 3001 for UI development.
#
# Launch with the venv python DIRECTLY. `source activate && python3` is NOT reliable here:
# on this box it resolved to system python3 (3.14), which lacks sherpa_onnx, so Kokoro
# silently failed to load and the non-expressive voice produced no audio (expressive still
# worked, because that path only talks to the sidecar over HTTP). The direct interpreter
# path is the one that actually has sherpa_onnx / whisper / flask.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${CUE_VOX_PYTHON:-$HERE/.venv/bin/python}"
[ -x "$PY" ] || { echo "missing venv python: $PY (run setup.sh)" >&2; exit 1; }

export CUE_VOX_PORT="${CUE_VOX_PORT:-3001}"
exec "$PY" "$HERE/web.py"
