#!/usr/bin/env bash
# Run the warm Chatterbox sidecar (expressive voice mode) from its own venv.
# The model loads once and stays resident; cue-vox talks to it on 127.0.0.1:$CBX_PORT.
# Run setup-chatterbox.sh first. Falls back gracefully: if this is down, cue-vox
# uses Kokoro then `say`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv-chatterbox"

[ -d "$VENV" ] || { echo "no venv -- run ./setup-chatterbox.sh first" >&2; exit 1; }

export CBX_PORT="${CBX_PORT:-8123}"
export CBX_REF="${CBX_REF:-$HERE/models/ref-0.wav}"

# shellcheck disable=SC1091
source "$VENV/bin/activate"
exec python "$HERE/chatterbox_server.py"
