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
import os
import json
from datetime import datetime, timedelta
import threading
import time
import math

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

# Speech consumption tracking
current_speech = None

# Conversation logging with 24-hour retention
LOG_DIR = Path(__file__).parent / 'logs'
LOG_RETENTION_HOURS = 24
SESSION_START = datetime.now()  # Track when server started

def ensure_log_dir():
    LOG_DIR.mkdir(exist_ok=True)

def calculate_sunrise_sunset(dt):
    """
    Calculate sunrise/sunset times using simple solar calculation
    Assumes approximate location (can be refined with actual coordinates)
    Returns (sunrise_hour, sunset_hour) as decimal hours
    """
    # Day of year
    day_of_year = dt.timetuple().tm_yday

    # Approximate latitude (40° N for rough US average - adjust for your location)
    latitude = 40.0

    # Solar declination (simplified)
    declination = 23.45 * math.sin(math.radians((360/365) * (day_of_year - 81)))

    # Hour angle at sunrise/sunset
    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)

    cos_hour_angle = -math.tan(lat_rad) * math.tan(dec_rad)

    # Clamp to valid range
    cos_hour_angle = max(-1, min(1, cos_hour_angle))

    hour_angle = math.degrees(math.acos(cos_hour_angle))

    # Sunrise and sunset in decimal hours (solar noon is 12:00)
    sunrise = 12 - (hour_angle / 15)
    sunset = 12 + (hour_angle / 15)

    return sunrise, sunset

def get_time_period(dt):
    """Return time-of-day period based on actual sunrise/sunset"""
    sunrise, sunset = calculate_sunrise_sunset(dt)

    hour = dt.hour + dt.minute / 60  # Decimal hour

    # Dawn: 1 hour before sunrise
    dawn = sunrise - 1
    # Dusk: 1 hour after sunset
    dusk = sunset + 1

    if hour < dawn:
        return 'night'
    elif hour < sunrise:
        return 'dawn'
    elif hour < 12:
        return 'morning'
    elif hour < sunset:
        return 'afternoon'
    elif hour < dusk:
        return 'dusk'
    else:
        return 'night'

def get_relative_time(timestamp):
    """Get human-readable relative time since session start"""
    delta = timestamp - SESSION_START
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    if hours == 0:
        return f"{minutes}m ago"
    else:
        return f"{hours}h {minutes}m ago"

def log_conversation(user_text, assistant_text, speech_metadata=None, input_length=None):
    ensure_log_dir()
    timestamp = datetime.now()
    log_file = LOG_DIR / f"{timestamp.strftime('%Y-%m-%d')}.jsonl"

    entry = {
        'timestamp': timestamp.strftime('%Y-%m-%dT%H:%M'),  # No seconds
        't_relative': get_relative_time(timestamp),
        't_period': get_time_period(timestamp),
        'user': user_text,
        'assistant': assistant_text
    }

    if speech_metadata:
        entry['speech'] = speech_metadata

    if input_length is not None:
        entry['input_length'] = input_length

    with open(log_file, 'a') as f:
        f.write(json.dumps(entry) + '\n')

    return log_file

def cleanup_old_logs():
    ensure_log_dir()
    cutoff = datetime.now() - timedelta(hours=LOG_RETENTION_HOURS)

    for log_file in LOG_DIR.glob('*.jsonl'):
        try:
            file_date = datetime.strptime(log_file.stem, '%Y-%m-%d')
            if file_date < cutoff:
                log_file.unlink()
                print(f"🗑️  Deleted old log: {log_file.name}")
        except (ValueError, OSError):
            pass

def start_log_cleanup_thread():
    def cleanup_loop():
        while True:
            cleanup_old_logs()
            time.sleep(3600)  # Check every hour

    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()


# Speech consumption tracking
def start_speech_tracking(response_text):
    """Start tracking speech playback"""
    global current_speech
    # Estimate duration based on character count
    # Average speaking rate: ~150 words/min, ~5 chars/word = 750 chars/min = 12.5 chars/sec
    estimated_duration = len(response_text) / 12.5

    current_speech = {
        'started_at': time.time(),
        'estimated_duration': estimated_duration,
        'text': response_text
    }

def handle_speech_interruption():
    """Handle interruption of current speech"""
    global current_speech
    if current_speech:
        actual_duration = time.time() - current_speech['started_at']
        consumption_ratio = min(1.0, actual_duration / current_speech['estimated_duration']) if current_speech['estimated_duration'] > 0 else 0

        # Update the last log entry with interruption data
        update_last_log_with_speech({
            'actual_duration': round(actual_duration, 1),
            'estimated_duration': round(current_speech['estimated_duration'], 1),
            'consumption_ratio': round(consumption_ratio, 2),
            'interrupted': True
        })

        current_speech = None

def finish_speech():
    """Mark speech as completed"""
    global current_speech
    if current_speech:
        actual_duration = time.time() - current_speech['started_at']

        # Update the last log entry with completion data
        update_last_log_with_speech({
            'actual_duration': round(actual_duration, 1),
            'estimated_duration': round(current_speech['estimated_duration'], 1),
            'consumption_ratio': 1.0,
            'interrupted': False
        })

        current_speech = None

def update_last_log_with_speech(speech_data):
    """Update the last log entry with speech metadata"""
    ensure_log_dir()
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = LOG_DIR / f"{today}.jsonl"

    if not log_file.exists():
        return

    # Read all entries
    with open(log_file, 'r') as f:
        lines = f.readlines()

    if not lines:
        return

    # Parse last entry
    try:
        last_entry = json.loads(lines[-1])
        last_entry['speech'] = speech_data
        lines[-1] = json.dumps(last_entry) + '\n'

        # Write back
        with open(log_file, 'w') as f:
            f.writelines(lines)
    except (json.JSONDecodeError, IndexError):
        pass


# Temporal context injection
def detect_temporal_query(text):
    """Check if query is time-related"""
    keywords = [
        'how long', 'when', 'last time', 'earlier', 'recently',
        'what time', 'how many', 'since when', 'how much time',
        'first time', 'before', 'after', 'ago'
    ]
    return any(kw in text.lower() for kw in keywords)


def load_recent_logs(limit=10):
    """Load recent log entries from today's log file"""
    ensure_log_dir()

    # Get today's log file
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = LOG_DIR / f"{today}.jsonl"

    if not log_file.exists():
        return []

    # Read last N entries
    entries = []
    with open(log_file, 'r') as f:
        lines = f.readlines()
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return entries


def format_logs_with_time(entries):
    """Format log entries with temporal tags"""
    if not entries:
        return "No recent activity"

    formatted = []
    for entry in entries:
        t_rel = entry.get('t_relative', '')
        t_per = entry.get('t_period', '')
        user = entry.get('user', '')
        assistant = entry.get('assistant', '')

        formatted.append(f"[{t_rel} - {t_per}]")
        formatted.append(f"User: {user}")
        formatted.append(f"Assistant: {assistant}")
        formatted.append("")

    return "\n".join(formatted)


def get_input_word_count(text):
    """Calculate word count of user input"""
    return len(text.split())


def get_response_length_constraint(word_count):
    """Generate length constraint instruction based on input word count"""
    if word_count < 10:
        # Very short input - keep response extremely brief
        return """[RESPONSE LENGTH CONSTRAINT]
CRITICAL: User input was very brief ({} words). Match their energy.
Maximum response: 1-2 short sentences. Be concise and direct.
""".format(word_count)
    elif word_count < 50:
        # Medium input - moderate response
        return """[RESPONSE LENGTH CONSTRAINT]
User input was moderate ({} words). Keep response proportional.
Maximum response: 2-4 sentences. Be clear but not verbose.
""".format(word_count)
    else:
        # Long input - can match their depth
        return """[RESPONSE LENGTH CONSTRAINT]
User input was detailed ({} words). You can match their depth.
Respond thoroughly but stay focused on their points.
""".format(word_count)


def get_speech_consumption_context():
    """Get context about whether user absorbed previous response"""
    recent_logs = load_recent_logs(limit=1)

    if not recent_logs:
        return ""

    entry = recent_logs[0]
    speech = entry.get('speech', {})

    if not speech:
        return ""

    if speech.get('interrupted'):
        ratio = speech.get('consumption_ratio', 0)
        actual = speech.get('actual_duration', 0)
        estimated = speech.get('estimated_duration', 0)

        return f"""[SPEECH CONTEXT]
Previous response was interrupted after {actual:.0f}s of estimated {estimated:.0f}s ({ratio*100:.0f}% heard).
User likely did NOT absorb the previous information - they interrupted to redirect or pivot to a new idea.
"""
    else:
        # Check time since completion
        timestamp_str = entry.get('timestamp', '')
        try:
            # Parse timestamp (format: YYYY-MM-DDTHH:MM)
            entry_time = datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M')
            time_elapsed = (datetime.now() - entry_time).total_seconds()

            if time_elapsed > 300:  # 5+ minutes
                minutes = int(time_elapsed // 60)
                hours = minutes // 60
                mins_remainder = minutes % 60

                if hours > 0:
                    time_str = f"{hours}h {mins_remainder}m"
                else:
                    time_str = f"{minutes}m"

                return f"""[SPEECH CONTEXT]
Previous response completed fully, then {time_str} elapsed.
User absorbed the information but has been away for a while.
"""
        except (ValueError, TypeError):
            pass

    return ""


def inject_temporal_context(text):
    """Inject recent log context for temporal queries"""
    if not detect_temporal_query(text):
        return text

    # Load recent logs
    log_context = load_recent_logs(limit=10)

    if not log_context:
        return text

    # Format with temporal tags
    formatted = format_logs_with_time(log_context)

    # Prepend context with brevity instructions
    instructions = """[Temporal Response Instructions]
CRITICAL: Keep responses BRIEF. One short sentence. NO calculations shown. NO timestamps with seconds.
This is a VOICE interface - responses will be spoken aloud. Be conversational, not computational.

Examples of GOOD responses:
- "About an hour"
- "Since 8 this morning"
- "It's 9 AM"

Examples of BAD responses (NEVER do this):
- "Based on the logs: Our first conversation was at 7:57:29 AM PST. It's currently 8:52:54 AM PST. That means..."
- "We started at 7:57 AM, so about 40 minutes since then"
- "According to the logs, approximately 55 minutes"

[Recent activity context]
{formatted}

[User question]
{text}"""

    return instructions.format(formatted=formatted, text=text)


@app.route('/')
def index():
    print("📄 Serving index.html")
    return render_template('index.html')


@socketio.on('audio_data')
def handle_audio(data):
    """Receive audio from browser, transcribe, send to Claude, speak response"""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        subprocess.run(['killall', 'say'], stderr=subprocess.DEVNULL)

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

        # Extract segment timing data for debugging
        segments = result.get("segments", [])
        segment_info = []
        for i, seg in enumerate(segments):
            segment_info.append({
                'block': i,
                'start': f"{seg['start']:.2f}s",
                'end': f"{seg['end']:.2f}s",
                'duration': f"{seg['end'] - seg['start']:.2f}s",
                'text': seg['text'].strip()
            })

        # Log segment data for analysis
        if segments:
            print(f"\n{'='*60}")
            print(f"TRANSCRIPTION SEGMENTS ({len(segments)} blocks)")
            print(f"{'='*60}")
            for info in segment_info:
                print(f"Block {info['block']}: {info['start']} → {info['end']} ({info['duration']})")
                print(f"  Text: {info['text']}")
            print(f"{'='*60}\n")

        emit('transcription', {'text': text, 'segments': segment_info})
        emit('state_change', {'state': 'thinking'})

        # Calculate input length for response matching
        input_word_count = get_input_word_count(text)
        length_constraint = get_response_length_constraint(input_word_count)

        # Inject temporal context if query is time-related
        enhanced_text = inject_temporal_context(text)

        # Inject speech consumption context
        speech_context = get_speech_consumption_context()

        # Add yes/no button instructions to ALL prompts
        enhanced_text = f"""{speech_context}{length_constraint}[VOICE INTERFACE INSTRUCTIONS]
When you need confirmation, format your response like this:
[YES_NO: your question here]

Example: "[YES_NO: Should I proceed with this operation?]"

The UI will automatically render Yes OR No buttons for the user to click.

IMPORTANT: When speaking, say "Yes OR No" not "yes-no" or "yes slash no".

[USER INPUT]
{enhanced_text}"""

        # Send to Claude Code (run from parent maestro directory if exists)
        cwd = Path(__file__).parent.parent if (Path(__file__).parent.parent / 'cuesheets').exists() else Path(__file__).parent

        process = subprocess.Popen(
            ['claude'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd
        )
        stdout, stderr = process.communicate(input=enhanced_text)
        response = stdout.strip()

        # Log conversation with input length
        log_conversation(text, response, input_length=input_word_count)

        emit('response', {'text': response})
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(response)

        # Speak response
        subprocess.run(['say', response], check=True)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

        # Cleanup
        Path(temp_file.name).unlink()

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('button_response')
def handle_button_response(data):
    """Handle yes/no button click - treat as voice input"""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        subprocess.run(['killall', 'say'], stderr=subprocess.DEVNULL)

        answer = data['answer']  # "Yes" or "No"

        emit('state_change', {'state': 'thinking'})

        # Calculate input length for response matching
        input_word_count = get_input_word_count(answer)
        length_constraint = get_response_length_constraint(input_word_count)

        # Load recent conversation for context
        recent_logs = load_recent_logs(limit=5)
        context = ""
        if recent_logs:
            context = "[RECENT CONVERSATION]\n"
            for entry in recent_logs:
                context += f"User: {entry.get('user', '')}\n"
                context += f"Assistant: {entry.get('assistant', '')}\n"
            context += "\n"

        # Inject speech consumption context
        speech_context = get_speech_consumption_context()

        # Build prompt with context
        enhanced_text = f"""{speech_context}{length_constraint}{context}[USER'S RESPONSE TO YOUR LAST QUESTION]
{answer}

[VOICE INTERFACE INSTRUCTIONS]
When you need confirmation, format your response like this:
[YES_NO: your question here]

The UI will automatically render Yes OR No buttons for the user to click.

CRITICAL: If the user responds "No" to a yes/no question, accept their answer as final. Do NOT ask another yes/no question or suggest alternatives unless the user explicitly asks for them. "No" means "No" - acknowledge it and move on.

IMPORTANT: When speaking, say "Yes OR No" not "yes-no" or "yes slash no"."""

        # Send to Claude Code
        cwd = Path(__file__).parent.parent if (Path(__file__).parent.parent / 'cuesheets').exists() else Path(__file__).parent

        process = subprocess.Popen(
            ['claude'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd
        )
        stdout, stderr = process.communicate(input=enhanced_text)
        response = stdout.strip()

        # Log conversation (button answer as user input) with input length
        log_conversation(answer, response, input_length=input_word_count)

        emit('response', {'text': response})
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(response)

        # Speak response
        subprocess.run(['say', response], check=True)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('text_message')
def handle_text_message(data):
    """Handle text message from input field - same flow as voice but without transcription"""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        subprocess.run(['killall', 'say'], stderr=subprocess.DEVNULL)

        text = data['text'].strip()

        if not text:
            return

        emit('state_change', {'state': 'thinking'})

        # Calculate input length for response matching
        input_word_count = get_input_word_count(text)
        length_constraint = get_response_length_constraint(input_word_count)

        # Inject temporal context if query is time-related
        enhanced_text = inject_temporal_context(text)

        # Inject speech consumption context
        speech_context = get_speech_consumption_context()

        # Add yes/no button instructions to ALL prompts
        enhanced_text = f"""{speech_context}{length_constraint}[VOICE INTERFACE INSTRUCTIONS]
When you need confirmation, format your response like this:
[YES_NO: your question here]

Example: "[YES_NO: Should I proceed with this operation?]"

The UI will automatically render Yes OR No buttons for the user to click.

IMPORTANT: When speaking, say "Yes OR No" not "yes-no" or "yes slash no".

[USER INPUT]
{enhanced_text}"""

        # Send to Claude Code (run from parent maestro directory if exists)
        cwd = Path(__file__).parent.parent if (Path(__file__).parent.parent / 'cuesheets').exists() else Path(__file__).parent

        process = subprocess.Popen(
            ['claude'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd
        )
        stdout, stderr = process.communicate(input=enhanced_text)
        response = stdout.strip()

        # Log conversation with input length
        log_conversation(text, response, input_length=input_word_count)

        emit('response', {'text': response})
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(response)

        # Speak response
        subprocess.run(['say', response], check=True)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

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
    # Allow port override via environment variable (default 3000)
    port = int(os.environ.get('CUE_VOX_PORT', 3000))

    # Start background log cleanup thread
    start_log_cleanup_thread()

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🎙️  CUE-VOX Web Interface")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print(f"Open: http://localhost:{port}")
    print(f"Logs: {LOG_DIR} (24hr retention)")
    print()
    socketio.run(app, host='127.0.0.1', port=port, debug=False, allow_unsafe_werkzeug=True)
