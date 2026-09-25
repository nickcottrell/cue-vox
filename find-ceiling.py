"""Find a voice's Chatterbox expressiveness CEILING and cap it.

WHY: Chatterbox destabilizes at high exaggeration / low cfg_weight (the peak of the dial).
A cloned voice tolerates a different amount of push before it breaks up -- Bell caps at 1.60,
Mack at 1.55, set by ear. Beyond the cap it turns rough / unnatural.

THE PROCESS (the ear is the gate):
    1. ./find-ceiling.sh <voice> --play        # render + AUDITION the labeled dial ramp
    2. (listen -- find the last clean dial before it breaks up)
    3. ./find-ceiling.sh <voice> --cap <dial> --apply   # write that verdict into voices.json

HONEST LIMITATION -- the auto-score does NOT work well. This tool also computes objective
instability proxies (duration runaway, clipping, HF buzz) and prints a "recommended ceiling",
BUT in practice those proxies are UNDER-SENSITIVE: both Bell and Mack measured "stable to top"
while the ear clearly heard breakup ~1.55-1.60. The roughness is a timbre-quality failure the
crude DSP proxies miss. So treat the auto-recommendation as a weak directional hint only, and
ALWAYS set the cap by ear via --cap. The sweep's real value is the labeled audition ramp, not
the number it prints.

HOW THE DIAL WORKS (see chatterbox_server.py): the client "exaggeration" is a dial ~1.0..2.0.
The server maps it: style = clip((dial-1)/1, 0,1); cbx_exag = 0.6 + 1.4*style; cfg = 0.4 - 0.18*style.
So dial 1.0 = calm/stable (cbx 0.6, cfg 0.40), dial 2.0 = wild (cbx 2.0, cfg 0.22). Values above
2.0 all clip to the same ceiling. We sweep/cap the DIAL, because the cap we write is a dial value:
the max of a voice's peak `chatterbox_profile.exaggeration` in voices.json.

Usage:
    ./find-ceiling.sh <voice_id> --play              # audition the ramp (the real signal)
    ./find-ceiling.sh <voice_id> --cap <dial> --apply  # EAR VERDICT -> voices.json (no sweep)
    ./find-ceiling.sh <voice_id> [--apply]           # objective sweep (hint only, under-sensitive)
"""
import argparse
import glob
import json
import os
import time
import urllib.request
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES_JSON = os.environ.get("CUE_VOX_VOICES_JSON", os.path.join(HERE, "voices.json"))
CBX = "http://127.0.0.1:%s" % os.environ.get("CBX_PORT", "8123")
OUT_DIR = os.path.join(HERE, "models", "ceiling-sweep")

# A line with a hard stop and a couple of clauses -- runaway shows up as trailing garbage
# past the final word.
SWEEP_TEXT = ("Here is the thing. It actually works, every time. So let us get into it, "
              "and see exactly how far this voice can push before it breaks.")

# Thresholds, relative to the baseline (dial-lo) render.
DUR_RATIO_MAX = 1.30      # >30% longer than baseline = likely repeat/ramble
CLIP_FRAC_MAX = 0.010     # >1% samples pinned near full scale
HF_ABS_BUMP = 0.020       # hf_ratio may rise this much over baseline before it counts
HF_REL = 1.5              # ...or 1.5x baseline, whichever is larger


def _synth(voice, dial, timeout=180):
    body = json.dumps({"text": SWEEP_TEXT, "exaggeration": dial, "cfg_weight": 0.4,
                       "voice": voice}).encode()
    req = urllib.request.Request(CBX + "/synth", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["path"]


def _metrics(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if x.size == 0:
        return {"dur": 0.0, "clip_frac": 1.0, "hf_ratio": 1.0}
    dur = x.size / float(sr)
    clip_frac = float(np.mean(np.abs(x) > 0.985))
    mag = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    total = float(mag.sum()) or 1.0
    hf_ratio = float(mag[freqs > 6000].sum()) / total
    return {"dur": dur, "clip_frac": clip_frac, "hf_ratio": hf_ratio}


def _unstable(m, base):
    dur_ratio = m["dur"] / (base["dur"] or 1e-9)
    hf_thresh = max(base["hf_ratio"] * HF_REL, base["hf_ratio"] + HF_ABS_BUMP)
    flags = []
    if dur_ratio > DUR_RATIO_MAX:
        flags.append("dur x%.2f" % dur_ratio)
    if m["clip_frac"] > CLIP_FRAC_MAX and m["clip_frac"] > base["clip_frac"] * 3:
        flags.append("clip %.1f%%" % (m["clip_frac"] * 100))
    if m["hf_ratio"] > hf_thresh:
        flags.append("hf %.2f" % m["hf_ratio"])
    return flags, dur_ratio


def sweep(voice, lo, hi, step, repeats, play):
    os.makedirs(OUT_DIR, exist_ok=True)
    dials = [round(lo + i * step, 3) for i in range(int(round((hi - lo) / step)) + 1)]
    rows = []
    base = None
    for dial in dials:
        worst = None
        keep_path = None
        for k in range(repeats):
            p = _synth(voice, dial)
            m = _metrics(p)
            # keep the worse (longer/nosier) of the repeats -- instability is worst-case
            if worst is None or m["dur"] >= worst["dur"]:
                worst = m
                keep_path = p
        dst = os.path.join(OUT_DIR, "%s-dial-%.2f.wav" % (voice, dial))
        os.replace(keep_path, dst)
        if base is None:
            base = worst
        flags, dur_ratio = _unstable(worst, base)
        rows.append({"dial": dial, "m": worst, "flags": flags, "dur_ratio": dur_ratio, "wav": dst})
        print("  dial %.2f  dur %5.2fs (x%.2f)  clip %.2f%%  hf %.2f  %s"
              % (dial, worst["dur"], dur_ratio, worst["clip_frac"] * 100, worst["hf_ratio"],
                 ("UNSTABLE: " + ", ".join(flags)) if flags else "ok"), flush=True)
        if play:
            os.system("afplay '%s'" % dst)
    # ceiling = highest dial reached before the first unstable step
    ceiling = dials[-1]
    for r in rows:
        if r["flags"]:
            ceiling = round(r["dial"] - step, 3)
            break
    return rows, ceiling, base


PEAK_SPREAD = 0.4    # how far the peak exaggeration floor sits below the cap (keeps peak punchy)


def apply_cap(voice, cap):
    """Cap the voice's PEAK register chatterbox_profile.exaggeration at `cap` (the dial),
    with the floor a fixed PEAK_SPREAD below it -- so peak stays punchy and consistent
    across voices (cap 1.55 -> [1.15, 1.55], matching Mack)."""
    doc = json.load(open(VOICES_JSON))
    v = (doc.get("voices") or {}).get(voice)
    if not v:
        raise SystemExit("unknown voice: %s" % voice)
    envs = v.get("envelopes") or {}
    peak_key = max(envs.keys(), key=lambda k: int(envs[k].get("kokoro_slot", 0)))
    prof = envs[peak_key].setdefault("chatterbox_profile", {})
    cur = prof.get("exaggeration", [1.0, 2.0])
    lo, hi = (cur if isinstance(cur, list) else [cur, cur])
    new_hi = round(cap, 2)
    new_lo = round(max(0.6, new_hi - PEAK_SPREAD), 2)
    prof["exaggeration"] = [new_lo, new_hi]
    json.dump(doc, open(VOICES_JSON, "w"), indent=2)
    print("applied: %s peak (register %s) exaggeration -> [%.2f, %.2f]  (was [%.2f, %.2f])"
          % (voice, peak_key, new_lo, new_hi, lo, hi))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("voice")
    ap.add_argument("--lo", type=float, default=1.0)
    ap.add_argument("--hi", type=float, default=2.0)
    ap.add_argument("--step", type=float, default=0.1)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--play", action="store_true", help="afplay each step as it renders")
    ap.add_argument("--apply", action="store_true", help="write the recommended cap into voices.json")
    ap.add_argument("--cap", type=float, default=None,
                    help="EAR OVERRIDE: skip the sweep and cap the peak dial here directly. The "
                         "objective proxies are under-sensitive to timbre roughness, so a human "
                         "audition (the labeled ramp) is authoritative -- this writes that verdict.")
    args = ap.parse_args()

    if args.cap is not None:                 # ear override: no sweep, just apply the human's verdict
        print("ear-override cap for %s: dial %.2f (no sweep)" % (args.voice, args.cap))
        apply_cap(args.voice, args.cap)
        return

    print("sweeping %s dial %.2f..%.2f step %.2f (x%d) via %s"
          % (args.voice, args.lo, args.hi, args.step, args.repeats, CBX), flush=True)
    t = time.time()
    rows, ceiling, base = sweep(args.voice, args.lo, args.hi, args.step, args.repeats, args.play)
    print("\nbaseline (dial %.2f): dur %.2fs  clip %.2f%%  hf %.2f"
          % (args.lo, base["dur"], base["clip_frac"] * 100, base["hf_ratio"]))
    stable_top = ceiling >= args.hi
    print("\nAUTO-HINT ONLY (under-sensitive -- confirm by ear): dial %.2f%s" %
          (ceiling, "  (proxies saw nothing -- this does NOT mean it sounds clean)" if stable_top else ""))
    print("sweep wavs in %s  (%.0fs elapsed)" % (OUT_DIR, time.time() - t))
    print(">>> AUDITION the ramp, then set the real cap by ear:  "
          "./find-ceiling.sh %s --cap <dial> --apply" % args.voice)
    if args.apply and not stable_top:
        print("(--apply on the auto-hint; but the ear verdict via --cap is authoritative)")
        apply_cap(args.voice, ceiling)
    elif args.apply:
        print("not applying the auto-hint (it saw nothing). Use --cap <dial> --apply from your audition.")


if __name__ == "__main__":
    main()
