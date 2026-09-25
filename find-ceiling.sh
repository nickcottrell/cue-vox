#!/usr/bin/env bash
# Find a voice's Chatterbox expressiveness ceiling and (optionally) cap it.
# Sweeps the exaggeration dial against the running sidecar (:8123), scores instability
# proxies, recommends a cap, and writes labeled sweep wavs for audition. See find-ceiling.py
# for the method. The sidecar must be up (./chatterbox.sh) and the voice must have refs.
#
# Usage:  ./find-ceiling.sh <voice_id> [--apply] [--play] [--hi 2.0] [--step 0.1]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${CUE_VOX_PYTHON:-$HERE/.venv/bin/python}"     # main venv (has numpy)
[ -x "$PY" ] || { echo "missing python: $PY" >&2; exit 1; }
exec "$PY" "$HERE/find-ceiling.py" "$@"
