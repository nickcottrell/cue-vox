#!/bin/bash
#
# run.sh -- test verb: isolated syntax validation (py_compile) of this node's
# top-level Python. No server started, no state touched. Degenerate tier --
# sharpens to behavioural tests later.
#
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
DIR=$(cd "$(dirname "$0")" && pwd)
NODE=$(dirname "$DIR")
ROOT=$(git -C "$NODE" rev-parse --show-toplevel 2>/dev/null || echo "$NODE/../..")
PY=python3
[ -x "$ROOT/.venv/bin/python3" ] && PY="$ROOT/.venv/bin/python3"
[ -x "$NODE/.venv/bin/python3" ] && PY="$NODE/.venv/bin/python3"
cd "$NODE"
n=0
for f in *.py; do
    [ -e "$f" ] || continue
    "$PY" -m py_compile "$f" || { echo "FAIL  $f does not compile"; exit 1; }
    n=$((n+1))
done
echo "ok  ${n} python file(s) compile"
echo ""
echo "1 passed"
