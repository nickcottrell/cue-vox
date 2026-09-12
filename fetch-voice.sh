#!/usr/bin/env bash
# Fetch the local neural voice for cue-vox: Kokoro (used for the bf_isabella blend).
# The model is large and gitignored (see models/), so this re-obtains it on any
# machine. Safe to re-run; it skips work already done.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
MODELS="$HERE/models"
VOICE="kokoro-en-v0_19"
URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${VOICE}.tar.bz2"
PY="$HERE/.venv/bin/python"

echo "Installing sherpa-onnx into cue-vox venv..."
"$PY" -m pip install -q sherpa-onnx

mkdir -p "$MODELS"
if [ -f "$MODELS/$VOICE/model.onnx" ]; then
  echo "Voice model already present: $MODELS/$VOICE"
else
  echo "Downloading $VOICE (~330MB)..."
  curl -sL -o "$MODELS/$VOICE.tar.bz2" "$URL"
  tar xjf "$MODELS/$VOICE.tar.bz2" -C "$MODELS"
  rm -f "$MODELS/$VOICE.tar.bz2"
  echo "Installed to $MODELS/$VOICE"
fi

echo "Done. cue-vox will speak as Isabella (Kokoro), falling back to say if absent."
