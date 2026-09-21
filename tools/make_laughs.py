#!/usr/bin/env python3
"""Generate the in-voice laugh / giggle / chuckle cue clips (models/sfx/*.wav).

Design: an in-voice, lightly synthetic cue, not a faked human sample. But it must
actually READ as a laugh: articulated vowel bursts (ha-ha / hee-hee / huh-huh),
pitched into the voice's range, with a breathy onset and a natural envelope. The
old clips were a single 0.5s blob; these are syllabic and shaped.

Deterministic (fixed seeds) and dumb-by-design: run once, store flat wavs. Each cue
writes a base <name>.wav (the mid variant) plus <name>-0..4.wav, where the index is
intensity: 0 = small/soft, 4 = big/fast. The render keys the variant off the
register slot (tuner) or the Chatterbox exaggeration dial (live).

Run:  .venv/bin/python tools/make_laughs.py
"""
import os
import wave
import numpy as np

SR = 24000
SFX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "sfx")


def _syllable(f0, formants, dur, amp, noise=0.06, glide=-0.10, seed=0):
    """One voiced laugh syllable: an additive glottal tone shaped by vowel formants,
    a downward pitch glide, a breathy onset, and a fast-attack / exp-decay envelope."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    f0t = f0 * (1.0 + glide * (t / dur))           # slight downward glide within the syllable
    phase = 2 * np.pi * np.cumsum(f0t) / SR
    sig = np.zeros(n)
    kmax = max(1, int((SR / 2) / f0))
    for k in range(1, kmax + 1):
        fk = k * f0
        g = 0.0
        for (fc, bw, a) in formants:                # vowel colour = sum of formant resonances
            g += a * np.exp(-((fk - fc) / bw) ** 2)
        if g < 1e-3:
            continue
        sig += (1.0 / (k ** 1.15)) * g * np.sin(k * phase)
    sig /= (np.abs(sig).max() + 1e-9)
    rng = np.random.RandomState(seed)              # aspiration: the breathy "h" onset
    asp = rng.randn(n) * np.exp(-t / 0.02)
    sig = sig * (1.0 - noise) + asp * noise
    env = np.minimum(t / 0.006, 1.0) * np.exp(-t / (dur * 0.5))
    return sig * env * amp


def _seq(syllables, gaps):
    """Concatenate syllables with silent gaps between them."""
    out = []
    for i, s in enumerate(syllables):
        out.append(s)
        if i < len(gaps):
            out.append(np.zeros(int(SR * gaps[i])))
    return np.concatenate(out) if out else np.zeros(1)


VOWEL_A = [(850, 120, 1.0), (1300, 150, 0.7), (2800, 250, 0.30)]   # "ha"  (open, hearty)
VOWEL_EE = [(400, 120, 0.8), (2600, 300, 0.9), (3300, 300, 0.40)]  # "hee" (bright, light)
VOWEL_UH = [(600, 120, 1.0), (1000, 150, 0.6), (2500, 250, 0.20)]  # "huh" (soft, low)


def laugh(v):
    """A hearty laugh: several "ha" bursts, pitch and amplitude descending, easing slower."""
    nsyl = [3, 4, 4, 5, 5][v]
    f0 = [300, 310, 320, 330, 340][v]
    amp0 = [0.55, 0.65, 0.72, 0.80, 0.88][v]
    gap = [0.16, 0.15, 0.14, 0.13, 0.12][v]
    syl, gaps = [], []
    for i in range(nsyl):
        syl.append(_syllable(f0 * (1 - 0.06 * i), VOWEL_A, 0.12 * (1 + 0.03 * i),
                             amp0 * (1 - 0.12 * i), noise=0.06, glide=-0.11, seed=i + v * 10))
        gaps.append(gap * (1 + 0.05 * i))
    return _seq(syl, gaps)


def giggle(v):
    """A light giggle: quick high "hee" bursts on a gentle up-then-down pitch arc."""
    nsyl = [4, 5, 6, 6, 7][v]
    f0 = [420, 440, 460, 470, 480][v]
    amp0 = [0.34, 0.42, 0.50, 0.56, 0.62][v]
    gap = [0.100, 0.095, 0.090, 0.085, 0.080][v]
    syl, gaps = [], []
    for i in range(nsyl):
        arc = np.sin(np.pi * (i + 0.5) / nsyl)     # 0 -> 1 -> 0 across the run
        syl.append(_syllable(f0 * (0.95 + 0.12 * arc), VOWEL_EE, 0.075,
                             amp0 * (0.70 + 0.50 * arc), noise=0.05, glide=-0.06, seed=100 + i + v * 10))
        gaps.append(gap)
    return _seq(syl, gaps)


def chuckle(v):
    """A soft under-the-breath chuckle: two or three low, breathy "huh" bursts."""
    nsyl = [2, 2, 3, 3, 3][v]
    f0 = [210, 220, 230, 235, 240][v]
    amp0 = [0.34, 0.40, 0.45, 0.50, 0.55][v]
    gap = [0.14, 0.13, 0.12, 0.12, 0.11][v]
    syl, gaps = [], []
    for i in range(nsyl):
        syl.append(_syllable(f0 * (1 - 0.05 * i), VOWEL_UH, 0.13,
                             amp0 * (1 - 0.10 * i), noise=0.11, glide=-0.08, seed=200 + i + v * 10))
        gaps.append(gap)
    return _seq(syl, gaps)


def _write(path, sig):
    pad = np.zeros(int(SR * 0.01))                 # tiny lead/tail silence
    sig = np.concatenate([pad, sig, pad])
    fade = int(SR * 0.004)                          # click-free edges
    sig[:fade] *= np.linspace(0, 1, fade)
    sig[-fade:] *= np.linspace(1, 0, fade)
    sig = sig / (np.abs(sig).max() + 1e-9) * 0.70   # headroom
    pcm = (sig * 32767.0).astype(np.int16)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main():
    os.makedirs(SFX, exist_ok=True)
    for name, fn in (("laugh", laugh), ("giggle", giggle), ("chuckle", chuckle)):
        for v in range(5):
            _write(os.path.join(SFX, "%s-%d.wav" % (name, v)), fn(v))
        _write(os.path.join(SFX, "%s.wav" % name), fn(2))   # base = mid variant
        print("wrote %s.wav + %s-0..4.wav" % (name, name))


if __name__ == "__main__":
    main()
