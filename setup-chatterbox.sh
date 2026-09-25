#!/usr/bin/env bash
# Build the isolated Chatterbox sidecar venv for cue-vox's expressive voice mode.
#
# Chatterbox is heavy (torch) and needs deps that conflict with the whisper/sherpa
# venv, so it lives in its OWN venv (.venv-chatterbox) with the model loaded once by
# a long-lived process. This script creates that venv and installs the deps. Refs
# (models/ref-*.wav) are rendered separately and already on disk.
#
# Idempotent: re-running reuses the venv and upgrades the packages.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv-chatterbox"
PY="${CBX_SETUP_PYTHON:-python3.12}"

command -v "$PY" >/dev/null 2>&1 || { echo "need $PY on PATH (set CBX_SETUP_PYTHON)" >&2; exit 1; }

if [ ! -d "$VENV" ]; then
    echo "[cbx-setup] creating venv at $VENV ($($PY --version 2>&1))"
    "$PY" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip wheel setuptools

# chatterbox-tts pulls torch, torchaudio, transformers, perth, etc.
python -m pip install chatterbox-tts

echo "[cbx-setup] verifying imports ..."
python - <<'PY'
import torch
from chatterbox.tts import ChatterboxTTS  # noqa: F401
dev = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
print("[cbx-setup] ok  torch=%s  device=%s" % (torch.__version__, dev))
PY

echo "[cbx-setup] done. start the sidecar with: ./chatterbox.sh"
