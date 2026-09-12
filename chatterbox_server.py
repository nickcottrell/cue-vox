"""Warm Chatterbox sidecar for cue-vox's expressive voice mode.

Chatterbox is heavy (torch), so it runs as its own long-lived process with the
model loaded ONCE, in its own venv (isolated from cue-vox's whisper/sherpa venv).
cue-vox talks to it over localhost HTTP via chatterbox_voice.py.

Run (from the chatterbox venv):
    CBX_REF=models/isabella-ref.wav CBX_PORT=8123 <chatterbox-venv>/bin/python chatterbox_server.py

Endpoints:
    GET  /health          -> "ok" once the model is loaded
    POST /synth {text, exaggeration, cfg_weight} -> {"path": "/tmp/....wav"}

exaggeration is Chatterbox's expressiveness dial (the thing Kokoro cannot do):
0.3 calm, 0.6 lively, 1.0 excited, 1.6 over the top.
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

REF = os.environ.get("CBX_REF", "models/isabella-ref.wav")
# Breathy <-> present is the NEURAL voice blend (Isabella/Nicole), rendered to
# separate reference clips that Chatterbox clones. No DSP breath (that was static).
_REF_DIR = os.path.dirname(os.path.abspath(REF))
# Register blend path, all American female: Nicole (breathy) -> af_sky (mid) ->
# af_sarah (dramatic). The dial picks the nearest step, so it glides through the
# three registers as a blend. No British.
REF_STEPS = [os.path.join(_REF_DIR, "ref-%d.wav" % i) for i in range(5)]
PORT = int(os.environ.get("CBX_PORT", "8123"))
DEVICE = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")

print("[cbx] loading Chatterbox on %s ..." % DEVICE, flush=True)
MODEL = ChatterboxTTS.from_pretrained(device=DEVICE)
print("[cbx] model ready, sr=%d, ref=%s" % (MODEL.sr, REF), flush=True)


def _pick_ref(style):
    """Pick the nearest blend step for this dial position (breathy .. dramatic)."""
    idx = int(round(style * (len(REF_STEPS) - 1)))
    idx = max(0, min(len(REF_STEPS) - 1, idx))
    p = REF_STEPS[idx]
    return p if os.path.exists(p) else REF


def synth(text, dial, cfg_weight):
    # The client's "exaggeration" is a STYLE dial in ~1.0..2.0:
    # 1.0 = breathy/intimate aside, 2.0 = dramatic proclamation. It selects both the
    # neural voice (breathy Nicole-heavy .. present Isabella-heavy) AND the feeling.
    style = float(np.clip((dial - 1.0) / 1.0, 0.0, 1.0))
    ref = _pick_ref(style)
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/synth"):
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            path = synth(
                body.get("text", ""),
                float(body.get("exaggeration", 0.6)),
                float(body.get("cfg_weight", 0.4)),
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
