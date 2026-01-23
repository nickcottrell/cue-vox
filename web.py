#!/usr/bin/env python3
"""
cue-vox web interface - localhost voice UI for Claude Code
"""

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import whisper
import subprocess
import tempfile
import base64
from pathlib import Path
import io
import wave

app = Flask(__name__)
app.config['SECRET_KEY'] = 'cue-vox-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Load Whisper model lazily
whisper_model = None

def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        print("Loading Whisper model...")
        whisper_model = whisper.load_model("base")
        print("✅ Whisper ready!")
    return whisper_model

# TTS process
tts_process = None


@app.route('/')
def index():
    print("📄 Serving index.html")
    return render_template('index.html')


@socketio.on('audio_data')
def handle_audio(data):
    """Receive audio from browser, transcribe, send to Claude, speak response"""
    try:
        # Decode base64 audio
        audio_bytes = base64.b64decode(data['audio'].split(',')[1])

        # Save to temp WAV file
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_file.write(audio_bytes)
        temp_file.close()

        # Update UI state
        emit('state_change', {'state': 'transcribing'})

        # Transcribe with Whisper
        model = get_whisper_model()
        result = model.transcribe(temp_file.name)
        text = result["text"].strip()

        emit('transcription', {'text': text})
        emit('state_change', {'state': 'thinking'})

        # Send to Claude Code (run from parent maestro directory)
        process = subprocess.Popen(
            ['claude'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).parent.parent
        )
        stdout, stderr = process.communicate(input=text)
        response = stdout.strip()

        emit('response', {'text': response})
        emit('state_change', {'state': 'speaking'})

        # Speak response
        subprocess.run(['say', response], check=True)

        emit('state_change', {'state': 'idle'})

        # Cleanup
        Path(temp_file.name).unlink()

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('interrupt')
def handle_interrupt():
    """Stop current speech"""
    global tts_process
    if tts_process:
        tts_process.terminate()
        tts_process = None
    subprocess.run(['killall', 'say'], stderr=subprocess.DEVNULL)
    emit('state_change', {'state': 'idle'})


if __name__ == '__main__':
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🎙️  CUE-VOX Web Interface")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print("Open: http://localhost:3000")
    print()
    socketio.run(app, host='127.0.0.1', port=3000, debug=False, allow_unsafe_werkzeug=True)
