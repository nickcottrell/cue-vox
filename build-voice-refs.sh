#!/usr/bin/env bash
# Render a voice's Chatterbox REFERENCE CLIPS from its own Kokoro register blends.
#
# WHY: Chatterbox (expressive mode) has no named voices -- it CLONES a reference wav.
# So each cue-vox voice needs its own ref set, and the clip IS the voice. We render those
# clips with Kokoro from the SAME register blends the fast path uses, so the expressive
# timbre matches the fast timbre. Output: models/<voice>-ref-<slot>.wav, one per register
# (breathy/chill -> dramatic/peak). chatterbox_server.py globs these by voice id.
#
# Prereq: the voice must have blend_slots:true + a baked blend bin -- run
# ../config/voice/build-refs.sh first. Then restart the sidecar so it re-globs.
#
# Usage:  ./build-voice-refs.sh [voice_id]      (default: the ACTIVE voice in voices.json)
#
# This is HALF of minting an expressive voice; the full recipe is in chatterbox_server.py's
# module docstring ("TO MINT A NEW EXPRESSIVE VOICE").
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${CUE_VOX_PYTHON:-$HERE/.venv/bin/python}"     # the MAIN cue-vox venv (has Kokoro/sherpa)
VOICES_JSON="${CUE_VOX_VOICES_JSON:-$HERE/voices.json}"

[ -x "$PY" ]          || { echo "missing python: $PY (main cue-vox venv)" >&2; exit 1; }
[ -f "$VOICES_JSON" ] || { echo "missing voices.json: $VOICES_JSON" >&2; exit 1; }

# Default to whatever voice is active in voices.json -- no personality id is hardcoded.
VOICE="${1:-$("$PY" -c "import json;print(json.load(open('$VOICES_JSON')).get('active',''))" 2>/dev/null)}"
[ -n "$VOICE" ] || { echo "no voice_id given and no 'active' in voices.json" >&2; exit 1; }

# A neutral line with a little range -- gives Chatterbox a clean timbre to clone.
REF_TEXT="${CBX_REF_TEXT:-Here's the thing. It actually works, every time. So let's get into it, and see how this sounds.}"

CUE_VOX_VOICES_JSON="$VOICES_JSON" REF_TEXT="$REF_TEXT" TARGET_VOICE="$VOICE" "$PY" - <<'PYEOF'
import json, os, shutil, sys
import kokoro_voice as kv

vid = os.environ["TARGET_VOICE"]
text = os.environ["REF_TEXT"]
cfg = json.load(open(os.environ["CUE_VOX_VOICES_JSON"]))
voice = (cfg.get("voices") or {}).get(vid)
if not voice:
    sys.exit("unknown voice: %s" % vid)
if not voice.get("blend_slots"):
    sys.exit("%s has blend_slots:false -- expressive refs need a neural blend bin. "
             "Give it a blend library + bake it first." % vid)

blend_bin = voice.get("blend")
envs = voice.get("envelopes") or {}
model_dir = kv.MODEL_DIR
if not os.path.exists(os.path.join(model_dir, blend_bin)):
    sys.exit("blend bin not baked: %s (run ../config/voice/build-refs.sh)" % blend_bin)

# Load the voice's blend bin; every register speaks its own slot (kokoro_slot) out of it.
kv.set_voice(sid=int(voice.get("sid", 8)), blend=blend_bin, use_blend=True)

wrote = 0
for rk in sorted(envs.keys()):
    reg = envs[rk] or {}
    slot = int(reg.get("kokoro_slot", 0))
    kv.set_register(slot)
    tmp = kv.synth_to_file(text)             # renders the current register/slot to a temp wav
    if not tmp or not os.path.exists(tmp):
        sys.exit("render failed for %s register %s (slot %d)" % (vid, rk, slot))
    out = os.path.join(model_dir, "..", "%s-ref-%d.wav" % (vid, slot))
    out = os.path.normpath(out)             # -> models/<vid>-ref-<slot>.wav
    shutil.move(tmp, out)
    wrote += 1
    print("  %s register %s (%s, slot %d) -> %s" % (vid, rk, reg.get("label", ""), slot, os.path.basename(out)))

print("wrote %d reference clip(s) for %s. restart the sidecar (./chatterbox.sh) to load them." % (wrote, vid))
PYEOF

echo "voice refs built for $VOICE"
