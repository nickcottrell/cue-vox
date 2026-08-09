#!/usr/bin/env bash
# Minimal node verb dispatcher (see maestro docs/design/node-standard.md).
# Only `security` is wired in this enrollment; other verbs are stubs so the
# uniform interface is visible. Named node.sh (not `node`) to avoid shadowing
# the Node.js binary.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
    security)
        exec "$HERE/.node-checks/run.sh" "$HERE"
        ;;
    test|status|deploy)
        echo "[$1] not yet wired for this node"
        exit 0
        ;;
    *)
        echo "usage: ./node.sh {security|test|status|deploy}"
        exit 2
        ;;
esac
