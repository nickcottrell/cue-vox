#!/usr/bin/env python3
"""
cue-vox - Voice interface for Claude Code
Push-to-talk with interrupt support
"""

import sys
import pyaudio
import wave
import keyboard
import tempfile
from pathlib import Path
import whisper
import subprocess

# Audio config
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000  # 16kHz for Whisper

class PushToTalk:
    """Handle push-to-talk audio recording"""

    def __init__(self, hotkey='space'):
        self.hotkey = hotkey
        self.audio = pyaudio.PyAudio()
        self.is_recording = False
        self.frames = []

    def start_recording(self):
        """Start audio capture"""
        self.is_recording = True
        self.frames = []
        self.stream = self.audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK
        )
        print("🎤 Recording... (release to stop)")

    def stop_recording(self):
        """Stop audio capture and save to temp file"""
        if not self.is_recording:
            return None

        self.is_recording = False
        self.stream.stop_stream()
        self.stream.close()

        # Save to temp WAV file
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        wf = wave.open(temp_file.name, 'wb')
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self.audio.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(self.frames))
        wf.close()

        print(f"✅ Recorded {len(self.frames)} frames")
        return temp_file.name

    def record_chunk(self):
        """Record one chunk of audio"""
        if self.is_recording:
            data = self.stream.read(CHUNK, exception_on_overflow=False)
            self.frames.append(data)

    def cleanup(self):
        """Close audio resources"""
        self.audio.terminate()


def transcribe_audio(audio_file):
    """Transcribe audio file using Whisper"""
    print("🔄 Transcribing...")
    model = whisper.load_model("base")  # base model for speed
    result = model.transcribe(audio_file)
    text = result["text"].strip()
    print(f"💬 You said: {text}")
    return text


def send_to_claude(text):
    """Send text to Claude Code via stdin pipe"""
    print("🤖 Claude is thinking...")

    # Pipe text directly to claude command
    process = subprocess.Popen(
        ['claude'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    stdout, stderr = process.communicate(input=text)

    if stderr:
        print(f"⚠️  Error: {stderr}")

    return stdout.strip()


def main():
    """Main push-to-talk loop"""
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🎙️  CUE-VOX - Voice for Claude Code")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print("Loading Whisper model...")
    # Preload model to avoid delay on first use
    whisper.load_model("base")
    print("✅ Ready!")
    print()
    print("Hold SPACE to talk, release to process")
    print("Press Ctrl+C to exit")
    print()

    ptt = PushToTalk(hotkey='space')

    try:
        while True:
            # Check for key press
            if keyboard.is_pressed('space'):
                if not ptt.is_recording:
                    ptt.start_recording()
                else:
                    ptt.record_chunk()
            else:
                if ptt.is_recording:
                    audio_file = ptt.stop_recording()
                    if audio_file:
                        # Step 2: Transcribe with Whisper
                        text = transcribe_audio(audio_file)

                        # Step 3: Send to Claude Code
                        response = send_to_claude(text)
                        print(f"📝 Claude: {response}")

                        # TODO: TTS response (step 4)

                        # Clean up audio file
                        Path(audio_file).unlink()

    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
        ptt.cleanup()
        sys.exit(0)


if __name__ == '__main__':
    main()
