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

import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get(
    "CUE_VOX_KOKORO_DIR", os.path.join(_DIR, "models", "kokoro-en-v0_19")
)
VOICE_SID = int(os.environ.get("CUE_VOX_VOICE_SID", "8"))  # bf_isabella
SPEED = float(os.environ.get("CUE_VOX_VOICE_SPEED", "1.0"))
# Synth threads. Measured sweet spot on a 12-core machine is 8 (RTF ~0.15 vs
# ~0.36 at 2); beyond ~8 thread contention makes it slower again.
THREADS = int(os.environ.get("CUE_VOX_VOICE_THREADS", "8"))

# --- Prosody (the low-hanging, cheap, local knobs) ---
# speed  = tempo (Kokoro-native)
# bright = high-shelf lift; >0 more present/vibrant, <0 duller/softer
# gain   = loudness multiplier
# Set per turn via set_prosody() (the web layer forwards window.VOICE). Defaults
# come from env so they can be pinned without code changes.
_speed = SPEED
_bright = float(os.environ.get("CUE_VOX_VOICE_BRIGHT", "0.0"))
_gain = float(os.environ.get("CUE_VOX_VOICE_GAIN", "1.0"))
_lift = float(os.environ.get("CUE_VOX_LIFT", "0.8"))   # question rise strength (0 = off).
# The rise is baked INTO the voice by editing its pitch track with the WORLD vocoder
# (see _question_intonation): F0 is scaled up on the tail, timbre + tempo untouched.
# This replaces the old resample bend, which coupled pitch+tempo and read as an artifact.
# Register: which blend slot to speak (0=breathy Nicole .. 4=dramatic Sarah),
# from register-voices.bin. Default breathy -- the "hey" whisper entry point.
_register = int(os.environ.get("CUE_VOX_REGISTER", "0"))

# Which voice speaks. A voice is a base SID plus how registers map to timbre:
#   _use_blend True  -> slots 0..4 are the neural register blend (Isabella,
#                       register-voices.bin); generate() speaks sid=_register.
#   _use_blend False -> every register speaks the fixed _base_sid (a stock Kokoro
#                       speaker); registers differ only by prosody + DSP.
# _voices_file overrides which .bin the engine loads (None = auto: register blend
# then stock). set_voice() switches all three and reloads the engine if the file
# changes. The web layer drives this from voices.json.
_base_sid = VOICE_SID
_use_blend = True
_voices_file = None


def set_register(idx):
    """Select the voice register 0..4 (breathy -> dramatic) for the next turn."""
    global _register
    try:
        _register = max(0, min(4, int(idx)))
    except (TypeError, ValueError):
        pass


def set_voice(sid=None, blend=None, use_blend=None):
    """Switch the base voice. `sid` is the stock Kokoro speaker (used when the voice
    has no neural blend). `blend` is a .bin filename in MODEL_DIR (or None to auto).
    `use_blend` True means register slots 0..4 are the neural blend; False means every
    register speaks `sid`. Reloads the engine only when the loaded .bin actually changes."""
    global _base_sid, _use_blend, _voices_file
    if sid is not None:
        try:
            _base_sid = int(sid)
        except (TypeError, ValueError):
            pass
    if use_blend is not None:
        _use_blend = bool(use_blend)
    if blend is not None:
        newf = blend or None
        if newf != _voices_file:
            _voices_file = newf
            reload()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def set_prosody(speed=None, bright=None, gain=None, lift=None):
    """Update the live prosody knobs. None leaves a knob unchanged. Clamped to sane ranges."""
    global _speed, _bright, _gain, _lift
    if speed is not None:
        try:
            _speed = _clamp(float(speed), 0.5, 2.0)
        except (TypeError, ValueError):
            pass
    if bright is not None:
        try:
            _bright = _clamp(float(bright), -1.0, 3.0)
        except (TypeError, ValueError):
            pass
    if gain is not None:
        try:
            _gain = _clamp(float(gain), 0.1, 3.0)
        except (TypeError, ValueError):
            pass
    if lift is not None:
        try:
            _lift = _clamp(float(lift), 0.0, 1.5)
        except (TypeError, ValueError):
            pass


def _brighten(x, k):
    """High-shelf lift: add back the high-frequency detail (x minus a short moving average)."""
    if not k:
        return x
    kernel = np.ones(7, dtype=np.float32) / 7.0
    low = np.convolve(x, kernel, mode="same").astype(np.float32)
    return x + k * (x - low)


def _question_intonation(x, sr, amount=0.8, tail=0.55):
    """Put an ascending question contour INTO the voice by editing its pitch track.

    WORLD decomposes speech into F0 (pitch) + spectral envelope (timbre) + aperiodicity.
    We scale the F0 up along a smooth ramp over the last `tail` fraction of the utterance
    and resynthesize. The pitch rises; timbre and tempo are untouched -- no resample
    chipmunk artifact, because we are editing the actual pitch track, not the samples.
    Returns the original `x` unchanged if WORLD is unavailable or the clip is too short.
    """
    if amount <= 0 or len(x) < int(0.12 * sr):
        return x
    try:
        import pyworld as pw
    except Exception:
        return x
    try:
        xf = np.ascontiguousarray(x.astype(np.float64))
        _f0, t = pw.dio(xf, sr, frame_period=5.0)
        f0 = pw.stonemask(xf, _f0, t, sr)
        sp = pw.cheaptrick(xf, f0, t, sr)
        ap = pw.d4c(xf, f0, t, sr)
        n = len(f0)
        vidx = np.where(f0 > 0)[0]
        if n < 4 or vidx.size < 4:
            return x
        # Anchor the rise to the last VOICED frame, not the last frame. Words like
        # "it?" end in an unvoiced consonant (/t/), so the pitch peak must land on the
        # final vowel or it gets discarded and the question sounds flat. Words like
        # "huh?" end voiced, so this also covers the easy case.
        end = int(vidx[-1])
        k = max(2, int(min(0.6, tail) * n))              # window length up to the last vowel
        start = max(0, end - k)
        span_len = max(1, end - start)
        # pivot = mid pitch of the body BEFORE the rising window (declination-free anchor)
        body = f0[:start]
        bv = body[body > 0]
        pivot = float(np.median(bv)) if bv.size else float(np.median(f0[vidx]))
        top = 2.0 ** (min(1.5, amount) * 7.0 / 12.0)     # semitone ratio at the last vowel
        idx = np.arange(start, end + 1)
        pos = (idx - start) / span_len                   # 0 at window start, 1 at last vowel
        ease = 0.5 - 0.5 * np.cos(np.pi * pos)           # smooth cosine 0 -> 1
        target = pivot * (1.0 + (top - 1.0) * ease)      # body pitch climbing to pivot*top
        f0m = f0.copy()
        blended = f0[idx] * (1.0 - ease) + target * ease  # seam-free: ease=0 keeps natural
        f0m[idx] = np.where(f0[idx] > 0, blended, 0.0)    # unvoiced frames in the window stay 0
        y = pw.synthesize(f0m, sp, ap, sr, frame_period=5.0)
        return y.astype(np.float32)
    except Exception as e:
        print("[TTS] question intonation failed: %s" % e)
        return x


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
            # A voice may pin which .bin to load (_voices_file). Default: register-voices.bin
            # bakes the 5 Isabella register blends into slots 0..4; fall back to stock
            # voices.bin (all speakers at their real SIDs) if it is pinned or the blend
            # has not been built yet.
            voices = os.path.join(MODEL_DIR, _voices_file) if _voices_file \
                else os.path.join(MODEL_DIR, "register-voices.bin")
            if not os.path.exists(voices):
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
                    num_threads=THREADS,
                ),
            )
            _tts = sherpa_onnx.OfflineTts(cfg)
            print("[TTS] Kokoro ready (voice sid=%d)" % VOICE_SID)
        except Exception as e:
            print("[TTS] Kokoro load failed: %s -- using fallback" % e)
            _load_failed = True
    return _tts


def reload():
    """Drop the cached engine so the next synth rebuilds it, picking up a freshly
    written register-voices.bin (e.g. after a blend upload + rebuild). Cheap: the
    heavy model file is memory-mapped by sherpa on next construction."""
    global _tts, _load_failed
    with _lock:
        _tts = None
        _load_failed = False
    print("[TTS] Kokoro engine flagged for reload (new blend will load on next synth)")


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


def synth_to_file(text, question=None):
    """Synthesize `text` to a temp WAV and return its path (the caller deletes it).

    `question`: force the ascending question contour on/off. None = auto-detect from a
    trailing '?'. Returns None if Kokoro is unavailable or synthesis fails, so callers
    can fall back to `say`. This is the synth half of the pipeline: it does NOT play,
    which lets a caller synthesize the next chunk while the current one is still playing.
    """
    text = (text or "").strip()
    if not text:
        return None
    tts = _get_tts()
    if tts is None:
        return None
    is_q = question if question is not None else text.rstrip().endswith("?")
    try:
        # Blend voices read timbre from the register slot (0..4); stock voices speak
        # their fixed SID and let prosody + DSP carry the register difference.
        sid = _register if _use_blend else _base_sid
        audio = tts.generate(text, sid=sid, speed=_speed)
        if not audio.samples:
            return None
        x = np.asarray(audio.samples, dtype=np.float32)
        if is_q and _lift > 0:
            # Edit the pitch track BEFORE brighten/gain so DSP rides the final voice.
            x = _question_intonation(x, audio.sample_rate, amount=_lift)
        x = _brighten(x, _bright)
        if _gain != 1.0:
            x = x * _gain
        x = np.clip(x, -1.0, 1.0)
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="cuevox-tts-")
        os.close(fd)
        _write_wav(path, x, audio.sample_rate)
        return path
    except Exception as e:
        print("[TTS] Kokoro synth failed: %s -- using fallback" % e)
        return None


def speak(text):
    """Synthesize `text` as Isabella and play it (blocking). Return True on success.

    Playback is a plain `afplay`, so `killall afplay` (see flush_speech_queue)
    interrupts it the same way `killall say` interrupts the system voice.
    """
    if not (text or "").strip():
        return True
    path = synth_to_file(text)
    if not path:
        return False
    try:
        subprocess.run(["afplay", path], check=False)
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    import sys

    line = " ".join(sys.argv[1:]) or "Hello. This is Isabella, your local voice."
    if not speak(line):
        print("Kokoro unavailable; would fall back to say.")
