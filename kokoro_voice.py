"""Local neural TTS for cue-vox: Kokoro voice bf_isabella via sherpa-onnx.

This is the primary mouth. It runs fully local and offline, no API key, no cloud.
web.py's `_say_with_fallback` calls `speak()` first and falls back to the macOS
`say` command (then pyttsx3) if the model or sherpa-onnx is unavailable, so the
voice degrades gracefully and never hard-fails.

Model home: core/cue-vox/models/kokoro-en-v0_19 (gitignored, fetched by
fetch-voice.sh). Override with CUE_VOX_KOKORO_DIR.

Voice selection: sid 8 is bf_isabella (British female). Override with
CUE_VOX_VOICE_SID. Playback speed via CUE_VOX_VOICE_SPEED.
"""

import os
import struct
import subprocess
import tempfile
import threading
import wave

_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get(
    "CUE_VOX_KOKORO_DIR", os.path.join(_DIR, "models", "kokoro-en-v0_19")
)
VOICE_SID = int(os.environ.get("CUE_VOX_VOICE_SID", "8"))  # bf_isabella
SPEED = float(os.environ.get("CUE_VOX_VOICE_SPEED", "1.0"))

_tts = None
_load_failed = False
_lock = threading.Lock()


def _get_tts():
    """Lazily build the sherpa-onnx Kokoro engine once. Returns None if unavailable."""
    global _tts, _load_failed
    if _tts is not None or _load_failed:
        return _tts
    with _lock:
        if _tts is not None or _load_failed:
            return _tts
        try:
            import sherpa_onnx

            model = os.path.join(MODEL_DIR, "model.onnx")
            voices = os.path.join(MODEL_DIR, "voices.bin")
            tokens = os.path.join(MODEL_DIR, "tokens.txt")
            data_dir = os.path.join(MODEL_DIR, "espeak-ng-data")
            missing = [p for p in (model, voices, tokens, data_dir) if not os.path.exists(p)]
            if missing:
                print("[TTS] Kokoro model files missing: %s -- using fallback" % missing)
                _load_failed = True
                return None
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=model, voices=voices, tokens=tokens, data_dir=data_dir,
                    ),
                    num_threads=2,
                ),
            )
            _tts = sherpa_onnx.OfflineTts(cfg)
            print("[TTS] Kokoro ready (voice sid=%d)" % VOICE_SID)
        except Exception as e:
            print("[TTS] Kokoro load failed: %s -- using fallback" % e)
            _load_failed = True
    return _tts


def available():
    """True if the Kokoro engine can be used."""
    return _get_tts() is not None


def _write_wav(path, samples, sample_rate):
    """Write float samples to a 16-bit mono WAV. Uses numpy when present for speed."""
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        try:
            import numpy as np

            pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
            w.writeframes((pcm * 32767).astype("<i2").tobytes())
        except Exception:
            w.writeframes(
                b"".join(
                    struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples
                )
            )


def speak(text):
    """Synthesize `text` as Isabella and play it (blocking). Return True on success.

    Returns False on any problem so the caller can fall back to `say`. Playback is
    a plain `afplay`, so `killall afplay` (see flush_speech_queue) interrupts it the
    same way `killall say` interrupts the system voice.
    """
    text = (text or "").strip()
    if not text:
        return True
    tts = _get_tts()
    if tts is None:
        return False
    path = None
    try:
        audio = tts.generate(text, sid=VOICE_SID, speed=SPEED)
        if not audio.samples:
            return False
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="cuevox-tts-")
        os.close(fd)
        _write_wav(path, audio.samples, audio.sample_rate)
        subprocess.run(["afplay", path], check=False)
        return True
    except Exception as e:
        print("[TTS] Kokoro speak failed: %s -- using fallback" % e)
        return False
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    import sys

    line = " ".join(sys.argv[1:]) or "Hello. This is Isabella, your local voice."
    if not speak(line):
        print("Kokoro unavailable; would fall back to say.")
