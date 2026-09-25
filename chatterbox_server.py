"""Warm Chatterbox sidecar for cue-vox's expressive voice mode.

Chatterbox is heavy (torch), so it runs as its own long-lived process with the
model loaded ONCE, in its own venv (isolated from cue-vox's whisper/sherpa venv).
cue-vox talks to it over localhost HTTP via chatterbox_voice.py.

Setup + run (once):
    ./setup-chatterbox.sh     # build .venv-chatterbox (python3.12, torch, chatterbox-tts)
    ./chatterbox.sh           # start this sidecar on :8123

Endpoints:
    GET  /health          -> "ok" once the model is loaded
    POST /synth {text, exaggeration, cfg_weight, voice} -> {"path": "/tmp/....wav"}

exaggeration is Chatterbox's expressiveness dial (the thing Kokoro cannot do):
0.3 calm, 0.6 lively, 1.0 excited, 1.6 over the top.

--- HOW EXPRESSIVE MODE KEEPS EACH VOICE'S TIMBRE (mint a new expressive voice) ---
Chatterbox does not have named voices; it CLONES a reference clip. So each cue-vox
voice needs its own set of reference wavs, and the clip we clone IS the voice.

  * The `voice` field on /synth (the active voice id, whatever it is in voices.json)
    selects the ref set. _ref_steps(voice) looks for models/<voice>-ref-*.wav; if a voice
    has none, it falls back to the unbranded default set (ref-N.wav) -- so an unknown voice
    degrades, never errors. No personality id is hardcoded here.
  * The refs are Kokoro renders of that voice's own register blends, so the expressive
    (Chatterbox) timbre matches the fast (Kokoro) timbre. They are built by
    build-voice-refs.sh in this dir.

TO MINT A NEW EXPRESSIVE VOICE (end to end):
  1. Add the voice to voices.json with a blend library + blend_slots true + its own
     `blend` bin name (copy an existing voice's shape).
  2. ../config/voice/build-refs.sh          # bake its register blend bin (kokoro timbre)
  3. ./build-voice-refs.sh <voice_id>       # render models/<voice_id>-ref-*.wav (clone src)
  4. restart this sidecar (./chatterbox.sh) so it re-globs the new ref files.
A voice with no <voice>-ref-*.wav clones the unbranded default set (ref-N.wav).
"""
import json
import os
import tempfile
import wave

import numpy as np
import torch

# perth's watermarker class loads as None on Python 3.14; stub it (inaudible/optional).
import perth
if getattr(perth, "PerthImplicitWatermarker", None) is None:
    class _NoWatermark:
        def apply_watermark(self, wav, sample_rate=None, watermark=None, **kw):
            return wav
    perth.PerthImplicitWatermarker = _NoWatermark

from chatterbox.tts import ChatterboxTTS
from http.server import BaseHTTPRequestHandler, HTTPServer

import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
# Voice-agnostic: no personality is baked in. The ref DIR holds all clone clips; the DEFAULT
# fallback set is the unbranded ref-N.wav clips (breathy -> present -> dramatic), cloned only
# when the requested voice has no <voice>-ref-*.wav of its own. Each real voice always passes
# its id (web.py sends _ACTIVE_VOICE), so the default is a safety net, not a voice.
_REF_DIR = os.environ.get("CBX_REF_DIR", os.path.join(_HERE, "models"))
REF = os.environ.get("CBX_REF", os.path.join(_REF_DIR, "ref-0.wav"))   # last-resort single clip
_REF_DIR = os.path.dirname(os.path.abspath(REF))                       # honor an explicit CBX_REF dir
REF_STEPS = sorted(glob.glob(os.path.join(_REF_DIR, "ref-[0-9].wav"))) \
            or [os.path.join(_REF_DIR, "ref-%d.wav" % i) for i in range(5)]
PORT = int(os.environ.get("CBX_PORT", "8123"))
DEVICE = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")

print("[cbx] loading Chatterbox on %s ..." % DEVICE, flush=True)
MODEL = ChatterboxTTS.from_pretrained(device=DEVICE)
print("[cbx] model ready, sr=%d, ref=%s" % (MODEL.sr, REF), flush=True)


def _ref_steps(voice):
    """The reference clips to clone for this voice, breathy -> dramatic. A voice with its
    own models/<voice>-ref-*.wav uses them (Mack = male); otherwise the default female set.
    Globbed fresh each call so newly-minted voices appear without a restart of _this_ fn --
    but note the model is warm, so a brand-new ref file added mid-run is picked up here."""
    if voice:
        own = sorted(glob.glob(os.path.join(_REF_DIR, "%s-ref-*.wav" % voice)))
        if own:
            return own
    return REF_STEPS


def _pick_ref(style, steps):
    """Pick the nearest step for this dial position (breathy .. dramatic) within a ref set."""
    idx = int(round(style * (len(steps) - 1)))
    idx = max(0, min(len(steps) - 1, idx))
    p = steps[idx]
    return p if os.path.exists(p) else REF


def synth(text, dial, cfg_weight, voice=""):
    # The client's "exaggeration" is a STYLE dial in ~1.0..2.0:
    # 1.0 = breathy/intimate aside, 2.0 = dramatic proclamation. It selects both the
    # neural voice (breathy Nicole-heavy .. present Isabella-heavy) AND the feeling.
    # `voice` picks WHOSE reference clips we clone, so timbre tracks the active voice.
    style = float(np.clip((dial - 1.0) / 1.0, 0.0, 1.0))
    ref = _pick_ref(style, _ref_steps(voice))
    cbx_exag = 0.6 + 1.4 * style            # 0.6 calm/breathy .. 2.0 wild
    cfg = 0.4 - 0.18 * style                # 0.4 measured .. 0.22 dramatic pacing
    wav = MODEL.generate(text, audio_prompt_path=ref, exaggeration=cbx_exag, cfg_weight=cfg)
    x = np.clip(wav.squeeze(0).detach().cpu().numpy().astype(np.float32), -1.0, 1.0)
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="cbx-")
    os.close(fd)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MODEL.sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return path


# Pairing token (managed launch sets it; empty = dev/manual, ungated). The sidecar holds no
# secret -- this just proves cue-vox is talking to the sidecar that was launched with it, not
# an impostor squatting the port. CBX_ATTEST points at the signed attestation file we serve
# (we do NOT sign here; cbx_attest.py minted it in the crypto-capable venv, we just serve it).
CBX_TOKEN = os.environ.get("CBX_TOKEN", "").strip()
CBX_ATTEST = os.environ.get("CBX_ATTEST", "").strip()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path.startswith("/attest"):        # the signed identity of this build
            data = b"{}"
            if CBX_ATTEST and os.path.exists(CBX_ATTEST):
                with open(CBX_ATTEST, "rb") as f:
                    data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/synth"):
            self.send_response(404)
            self.end_headers()
            return
        if CBX_TOKEN and self.headers.get("X-CBX-Token", "") != CBX_TOKEN:
            self.send_response(401)                    # wrong/absent token -> not the paired client
            self.end_headers()
            self.wfile.write(b"unpaired")
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            path = synth(
                body.get("text", ""),
                float(body.get("exaggeration", 0.6)),
                float(body.get("cfg_weight", 0.4)),
                (body.get("voice") or "").strip(),
            )
            payload = json.dumps({"path": path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, *a):
        pass


print("[cbx] serving on 127.0.0.1:%d" % PORT, flush=True)
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
