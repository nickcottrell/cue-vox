"""Op client. cue-vox's handle on any cue-sheet-app.

An Op is a shared UI for agents and humans. Each Op exposes the same write
API (notes today, more verbs as the contract grows) on a durable
launchd-managed port.

Usage:
    from ops import write_note
    write_note(
        op="pipeline",
        node_type="project",
        node_id="platform-work",
        body="cue-vox heard X happen",
    )

Contract: docs/services/ops.md
"""
from __future__ import annotations

import json
import os
import urllib.request

OPS = {
    "pipeline": os.environ.get("PIPELINE_URL", "http://localhost:5050"),
    "cube":     os.environ.get("CUBE_URL",     "http://localhost:5052"),
    "markets":  os.environ.get("MARKETS_URL",  "http://localhost:3003"),
}

VALID_AUTHORS = ("user", "cue-vox", "ninja", "c2d2")


def write_note(
    op: str,
    node_type: str,
    node_id: str,
    body: str,
    author: str = "cue-vox",
    timeout: float = 5.0,
) -> dict:
    """POST a note to an Op's /api/notes. Returns the created note dict.

    Raises ValueError for unknown Op or invalid author.
    Raises urllib.error.HTTPError for 4xx/5xx (e.g. Op not yet wired).
    """
    if op not in OPS:
        raise ValueError(
            "unknown Op: " + op + ". Known: " + ", ".join(OPS)
        )
    if author not in VALID_AUTHORS:
        raise ValueError(
            "invalid author: " + author
            + ". Valid: " + ", ".join(VALID_AUTHORS)
        )
    payload = json.dumps({
        "node_type": node_type,
        "node_id": node_id,
        "author": author,
        "body": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPS[op] + "/api/notes",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())
