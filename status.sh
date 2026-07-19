#!/bin/bash
#
# status.sh -- report verb for cue-vox. One line; exit 0 = serving.
#
set -euo pipefail
HOST=127.0.0.1
PORT=3000
if curl -sf -o /dev/null -m 2 "http://$HOST:$PORT/"; then
    echo "cue-vox: up ($HOST:$PORT)"
    exit 0
fi
echo "cue-vox: down ($HOST:$PORT)"
exit 1
