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


def _token():
    """The pairing token: env first, else the per-boot file the managed launcher wrote.
    Empty when unmanaged (dev) -- the sidecar is then ungated and this is a no-op."""
    t = (os.environ.get("CBX_TOKEN") or "").strip()
    if t:
        return t
    root = (os.environ.get("MAESTRO_ROOT") or "").strip()
    here = os.path.dirname(os.path.abspath(__file__))
    path = (os.path.join(root, ".claude", "runtime", "cbx.token") if root
            else os.path.join(here, "..", "..", ".claude", "runtime", "cbx.token"))
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def available(timeout=0.4):
    """True if the warm sidecar is up and its model is loaded."""
    try:
        with urllib.request.urlopen(_BASE + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def attestation(timeout=1.0):
    """The sidecar's signed identity ({fingerprint, signature, ...}) or None. cue-vox verifies
    it with signing.verify_bytes to confirm the running sidecar is the authorized build."""
    try:
        with urllib.request.urlopen(_BASE + "/attest", timeout=timeout) as r:
            return json.loads(r.read()) if r.status == 200 else None
    except Exception:
        return None


def synth_to_file(text, exaggeration=0.6, cfg_weight=0.4, voice=None, timeout=60):
    """Ask the sidecar to synthesize `text` and return the wav path (or None to fall back).

    `voice` is the active voice id from voices.json (no personality is hardcoded). The
    sidecar clones that voice's reference clips so expressive mode keeps the voice's own
    timbre. If the sidecar has no refs for that voice it falls back to the unbranded
    default set, so passing an unknown voice never errors. Refs are built by
    build-voice-refs.sh (models/<voice>-ref-*.wav)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        headers = {"Content-Type": "application/json"}
        tok = _token()
        if tok:
            headers["X-CBX-Token"] = tok        # pairing proof; the sidecar rejects without it
        req = urllib.request.Request(
            _BASE + "/synth",
            data=json.dumps({
                "text": text,
                "exaggeration": exaggeration,
                "cfg_weight": cfg_weight,
                "voice": voice or "",
            }).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read()).get("path")
    except Exception as e:
        print("[TTS] chatterbox sidecar error: %s -- falling back" % e)
        return None
