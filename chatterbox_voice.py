"""Client for the warm Chatterbox sidecar (see chatterbox_server.py).

Runs inside cue-vox's own venv (stdlib only), talks to the sidecar over localhost.
Used for the expressive voice mode; if the sidecar is not up, callers fall back to
Kokoro then `say`, so expressive mode degrades gracefully.
"""
import json
import os
import urllib.request

PORT = int(os.environ.get("CBX_PORT", "8123"))
_BASE = "http://127.0.0.1:%d" % PORT


def available(timeout=0.4):
    """True if the warm sidecar is up and its model is loaded."""
    try:
        with urllib.request.urlopen(_BASE + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def synth_to_file(text, exaggeration=0.6, cfg_weight=0.4, timeout=60):
    """Ask the sidecar to synthesize `text` and return the wav path (or None to fall back)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        req = urllib.request.Request(
            _BASE + "/synth",
            data=json.dumps({
                "text": text,
                "exaggeration": exaggeration,
                "cfg_weight": cfg_weight,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read()).get("path")
    except Exception as e:
        print("[TTS] chatterbox sidecar error: %s -- falling back" % e)
        return None
