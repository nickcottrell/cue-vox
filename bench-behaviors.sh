#!/usr/bin/env bash
# Behavior policy bench: run each stance card through a standard scenario across agents,
# side-by-side with baseline, so you can see the policy move the output. See bench-behaviors.py.
# Model-agnostic and server-independent -- it tests the policy, not the plumbing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${CUE_VOX_PYTHON:-$HERE/.venv/bin/python}"
exec "$PY" "$HERE/bench-behaviors.py" "$@"
