"""Crystallize the voice register system into an inert, CaaS-format SVG.

Same discipline as the CaaS export: pure declarative SVG, data in data-* attributes
and one <metadata> JSON block, animation via SMIL. NO <script>, NO external URLs,
nothing sensitive. Safe to generate, share, and version.

Each register becomes a VRGB swatch whose fill is a signature colour derived from
the reference clip's pitch (F0 -> hue) and brightness (spectral centroid -> sat).
The earn-curve is drawn from the tone params; a cursor animates the dial.
"""
import colorsys
import json
import os
import wave

import numpy as np

REGISTERS = [
    ("0 breathy · Nicole", "ref-0.wav"),
    ("1 · Nic/Sky", "ref-1.wav"),
    ("2 mid · Sky", "ref-2.wav"),
    ("3 · Sky/Sarah", "ref-3.wav"),
    ("4 dramatic · Sarah", "ref-4.wav"),
]


def _load(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0, w.getframerate()


def _f0(x, sr, fmin=120, fmax=320):
    x = x - x.mean()
    win = int(0.4 * sr)
    if len(x) > win:
        step = int(0.1 * sr)
        best, bi = -1, 0
        for i in range(0, len(x) - win, step):
            e = float(np.sum(x[i:i + win] ** 2))
            if e > best:
                best, bi = e, i
        seg = x[bi:bi + win]
    else:
        seg = x
    seg = seg * np.hanning(len(seg))
    ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
    lo, hi = int(sr / fmax), int(sr / fmin)
    return sr / (lo + int(np.argmax(ac[lo:hi])))


def _centroid(x, sr):
    X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / sr)
    return float(np.sum(f * X) / (np.sum(X) + 1e-9))


def _signature(path):
    x, sr = _load(path)
    F, C = _f0(x, sr), _centroid(x, sr)
    h = (250 - (F - 150) / (290 - 150) * 220) % 360     # low/breathy -> cool, high -> warm
    s = 0.30 + min(1.0, max(0.0, (C - 2000) / 2000)) * 0.4
    r, g, b = colorsys.hls_to_rgb(h / 360, 0.62, s)
    return dict(f0=round(F, 1), centroid=round(C), hex="#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))


def crystallize(model_dir, tone=None):
    """Return an SVG string crystallizing the register ladder + tone earn-curve.

    tone: dict with decay/cap/gamma (defaults applied if missing).
    """
    tone = tone or {}
    decay = float(tone.get("decay", 0.6))
    cap = float(tone.get("cap", 9.0))
    gamma = float(tone.get("gamma", 2.2))

    sig = []
    for label, fn in REGISTERS:
        p = os.path.join(model_dir, fn)
        s = _signature(p) if os.path.exists(p) else dict(f0=0, centroid=0, hex="#7c7f88")
        s["label"] = label
        sig.append(s)

    W, H, cx = 900, 520, 170
    ys = [440, 360, 280, 200, 120]
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="Zilla Slab, Georgia, serif">' % (W, H, W, H)]
    out.append("<title>cue-vox voice register ladder, VRGB export</title>")
    out.append("<desc>VRGB voice signature. Each swatch is a register (breathy to dramatic); fill is its signature colour from pitch and brightness. The cursor is the tone dial climbing the earn-curve.</desc>")
    out.append('<rect width="%d" height="%d" fill="#0a0c16"/>' % (W, H))
    out.append('<polyline points="%s" fill="none" stroke="#3a4260" stroke-width="1"/>' % (" ".join("%d,%d" % (cx, y) for y in ys)))
    for i, (s, y) in enumerate(zip(sig, ys)):
        out.append('<circle data-register="%d" data-coord="%s" cx="%d" cy="%d" r="11" fill="%s" stroke="#0a0c16" stroke-width="1"/>' % (i, s["hex"], cx, y, s["hex"]))
        out.append('<text x="%d" y="%d" fill="#aeb6c9" font-size="13">%s</text>' % (cx + 22, y + 4, s["label"]))
    ex0, ex1, ey0, ey1 = 560, 860, 440, 120
    cpath = []
    for k in range(41):
        t = k / 40.0
        style = t ** gamma
        cpath.append("%.1f,%.1f" % (ex0 + t * (ex1 - ex0), ey0 - style * (ey0 - ey1)))
    out.append('<polyline points="%s" fill="none" stroke="#9fb0f0" stroke-width="1.4" opacity="0.85"/>' % (" ".join(cpath)))
    out.append('<text x="%d" y="%d" fill="#8b93a8" font-size="12">earn-curve (gamma=%.1f): energy -&gt; register</text>' % (ex0 - 4, ey1 - 14, gamma))
    out.append('<circle cx="%d" cy="440" r="6" fill="#f2efe6"><animate attributeName="cy" dur="12s" repeatCount="indefinite" keyTimes="0;0.15;0.45;0.65;0.9;1.0" values="440;440;280;200;120;360"/></circle>' % cx)
    meta = {"surface": "cue-vox-voice", "registers": sig, "tone": {"decay": decay, "cap": cap, "gamma": gamma},
            "note": "fill=signature colour from pitch+brightness; cursor=tone dial on the earn-curve"}
    out.append('<metadata id="vrgb-voice">%s</metadata>' % json.dumps(meta).replace("&", "&amp;").replace("<", "&lt;"))
    out.append("</svg>")
    return "\n".join(out)
