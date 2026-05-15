"""Cue-vox publisher: pin a note onto the shared substrate via RSS feed.

Sovereignty contract: cue-vox writes to its own feed file. Pipeline subscribes
to that feed on its own clock, mints unseen entries as notes. Author is
named instance identity (cue-vox / ninja / c2d2) carried on the feed item
via the maestro namespace -- never generic "assistant".

Author resolution (highest precedence first):
  1. explicit `author=` argument to pin_note()
  2. `--author` CLI flag
  3. PIPELINE_PIN_AUTHOR environment variable
  4. default: "cue-vox"

Usage:
    from pipeline_pin import pin_note
    pin_note("project", "sol", "field check today, no anomalies")
    pin_note("project", "sol", "ratchet vault hardening shipped", author="ninja")

The feed is pruned to MAX_ITEMS to stay readable; pipeline tracks seen guids
independently so pruning never causes re-mints.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET


FEED_PATH = Path(__file__).parent / "feeds" / "pinned-notes.xml"
NS_MAESTRO = "https://maestro.local/ns/2026"
MAX_ITEMS = 200

VALID_NODE_TYPES = ("project", "item", "doc", "band", "routine", "date")
VALID_AUTHORS = ("cue-vox", "ninja", "c2d2")
DEFAULT_AUTHOR = "cue-vox"


def _now_rfc822() -> str:
    return format_datetime(datetime.now(timezone.utc))


def _new_guid() -> str:
    return uuid.uuid4().hex


def _resolve_author(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("PIPELINE_PIN_AUTHOR") or DEFAULT_AUTHOR
    candidate = candidate.strip()
    if candidate not in VALID_AUTHORS:
        raise ValueError(
            f"invalid author: {candidate!r} (must be one of {VALID_AUTHORS})"
        )
    return candidate


def _load_channel() -> tuple[ET.ElementTree, ET.Element]:
    ET.register_namespace("maestro", NS_MAESTRO)
    tree = ET.parse(FEED_PATH)
    channel = tree.getroot().find("channel")
    if channel is None:
        raise RuntimeError(f"feed missing <channel>: {FEED_PATH}")
    return tree, channel


def _prune(channel: ET.Element) -> None:
    items = channel.findall("item")
    excess = len(items) - MAX_ITEMS
    if excess > 0:
        # items are appended chronologically; oldest are first.
        for item in items[:excess]:
            channel.remove(item)


def pin_note(
    node_type: str,
    node_id: str,
    body: str,
    author: str | None = None,
) -> str:
    """Append a backstage note entry to the feed. Returns the guid.

    Pipeline will mint this as a note carrying the resolved instance
    identity. Idempotent on the pipeline side via guid dedup.
    """
    if node_type not in VALID_NODE_TYPES:
        raise ValueError(f"invalid node_type: {node_type!r}")
    node_id = node_id.strip()
    body = body.strip()
    if not node_id:
        raise ValueError("node_id required")
    if not body:
        raise ValueError("body required")

    resolved_author = _resolve_author(author)
    tree, channel = _load_channel()
    guid = _new_guid()

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "guid", isPermaLink="false").text = guid
    ET.SubElement(item, "pubDate").text = _now_rfc822()
    ET.SubElement(item, "title").text = f"backstage note: {node_type}/{node_id}"
    ET.SubElement(item, "description").text = body
    # Standard RSS author field gets the instance handle for any generic
    # RSS reader; pipeline relies on the maestro-namespaced field below.
    ET.SubElement(item, "author").text = f"{resolved_author}@maestro"
    ET.SubElement(item, "category").text = node_type
    ET.SubElement(item, f"{{{NS_MAESTRO}}}nodeType").text = node_type
    ET.SubElement(item, f"{{{NS_MAESTRO}}}nodeId").text = node_id
    ET.SubElement(item, f"{{{NS_MAESTRO}}}author").text = resolved_author

    _prune(channel)
    tree.write(FEED_PATH, encoding="utf-8", xml_declaration=True)
    return guid


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="pipeline_pin",
        description="Pin a backstage note onto a pipeline node.",
    )
    parser.add_argument("node_type", choices=VALID_NODE_TYPES)
    parser.add_argument("node_id")
    parser.add_argument("body")
    parser.add_argument(
        "--author",
        choices=VALID_AUTHORS,
        default=None,
        help=f"instance identity (default: env PIPELINE_PIN_AUTHOR or {DEFAULT_AUTHOR})",
    )
    args = parser.parse_args(argv)
    guid = pin_note(args.node_type, args.node_id, args.body, author=args.author)
    print(guid)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
