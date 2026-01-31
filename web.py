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
import re

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

# Session variables - key-value pairs from text inputs
session_variables = {}

# Input history - tracks all structured inputs with metadata
# Also tracks VRGB tokens (immutable snapshot objects with key:hex pairs)
input_history = {}
# Format: {
#     'INPUT_timestamp_id': {
#         'type': 'text|hsl_slider|yes_no',
#         'key': 'variable_name',  # optional
#         'question': 'What is...?',
#         'semantic_mapping': 'dimension1/dimension2/dimension3',  # for HSL: what the colorspace encodes
#         'requested_at': '2026-01-25T12:00:00',
#         'value': 'user response',
#         'hsl': {'h': 190, 's': 75, 'l': 60},  # for HSL inputs
#         'hex': '#4ccce6',  # for HSL inputs
#         'interpretation': 'semantic description',  # human-readable interpretation
#         'responded_at': '2026-01-25T12:01:00',
#         'status': 'pending|completed'
#     },
#     'VRGB_timestamp_id': {
#         'type': 'vrgb_token',
#         'hex': '#e64c4c',
#         'hsl': {'h': 0, 's': 75.5, 'l': 60.0},
#         'interpretation': 'urgent/time-sensitive, high priority, moderately clear',
#         'created_at': '2026-01-25T14:07:00',
#         'expires_at': '2026-01-26T14:07:00',  # fixed 24hr expiry (for now)
#         'status': 'active|expired'
#     }
# }

# Default VRGB token expiry duration (in hours)
VRGB_TOKEN_EXPIRY_HOURS = 24

# Scalar parameter token expiry (half of active context window ~24h = 12h)
SCALAR_PARAM_TOKEN_EXPIRY_HOURS = 12

# Token directories
TOKENS_DIR = Path(__file__).parent.parent / '.claude' / 'tokens'
CONTEXT_DIR = Path(__file__).parent.parent / '.claude'

def sanitize_for_tts(text):
    """
    Sanitize text for TTS by extracting question text from structured input tags.
    Prevents TTS from trying to speak raw tags like [YES_NO: ...] or [INPUT: {...}]
    """
    # Check if entire message is a YES_NO question - extract the question text
    yes_no_match = re.match(r'^\[YES_NO:\s*(.+?)\]$', text, re.IGNORECASE)
    if yes_no_match:
        return yes_no_match.group(1).strip()

    # Check if entire message is an INPUT question - extract the question from JSON
    input_match = re.match(r'^\[INPUT:\s*(\{[\s\S]+?\})\]$', text, re.IGNORECASE)
    if input_match:
        try:
            import json
            input_data = json.loads(input_match.group(1))
            if 'question' in input_data:
                return input_data['question']
        except:
            pass
        return "Please provide input"

    # Check if message contains structured tags anywhere
    if re.search(r'\[YES_NO:', text, re.IGNORECASE) or re.search(r'\[INPUT:', text, re.IGNORECASE):
        # Extract questions and surrounding text
        result = text

        # Extract YES_NO questions
        yes_no_matches = re.finditer(r'\[YES_NO:\s*(.+?)\]', result, re.IGNORECASE)
        for match in yes_no_matches:
            question = match.group(1).strip()
            result = result.replace(match.group(0), question)

        # Extract INPUT questions
        input_matches = re.finditer(r'\[INPUT:\s*(\{[\s\S]+?\})\]', result, re.IGNORECASE)
        for match in input_matches:
            try:
                import json
                input_data = json.loads(match.group(1))
                if 'question' in input_data:
                    result = result.replace(match.group(0), input_data['question'])
                else:
                    result = result.replace(match.group(0), '')
            except:
                result = result.replace(match.group(0), '')

        return result.strip()

    # No structured tags found, return original text
    return text

def ensure_log_dir():
    LOG_DIR.mkdir(exist_ok=True)

def ensure_tokens_dir():
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)

def generate_input_id():
    """Generate unique input ID for tracking"""
    import time
    import random
    import string
    timestamp = int(time.time())
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"INPUT_{timestamp}_{random_suffix}"

def generate_vrgb_token_id():
    """Generate unique VRGB token ID"""
    import time
    import random
    import string
    timestamp = int(time.time())
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"VRGB_{timestamp}_{random_suffix}"

def hex_to_hsl(hex_color):
    """Parse hex coordinate string to HSL breakdown"""
    # Remove # if present
    hex_color = hex_color.lstrip('#')

    # Convert to RGB (0-1 range)
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0

    max_c = max(r, g, b)
    min_c = min(r, g, b)
    l = (max_c + min_c) / 2.0

    if max_c == min_c:
        h = s = 0.0
    else:
        d = max_c - min_c
        s = d / (2.0 - max_c - min_c) if l > 0.5 else d / (max_c + min_c)

        if max_c == r:
            h = (g - b) / d + (6.0 if g < b else 0.0)
        elif max_c == g:
            h = (b - r) / d + 2.0
        else:
            h = (r - g) / d + 4.0
        h /= 6.0

    return {
        'h': round(h * 360, 1),
        's': round(s * 100, 1),
        'l': round(l * 100, 1)
    }

def detect_and_create_vrgb_tokens(text):
    """
    Detect hex coordinate strings in text and create VRGB tokens.
    Returns list of created token IDs.

    Pattern: #RRGGBB (semantic interpretation)
    Example: "#e64c4c (urgent/time-sensitive, moderate, moderately clear)"

    Note: VRGB uses colorspace as encoding hack - hex strings are coordinates, not colors.
    """
    import re

    # Pattern: hex coordinate string followed by optional parenthetical interpretation
    pattern = r'#([0-9a-fA-F]{6})(?:\s*\(([^)]+)\))?'
    matches = re.finditer(pattern, text)

    created_tokens = []
    now = datetime.now()
    expiry = now + timedelta(hours=VRGB_TOKEN_EXPIRY_HOURS)

    for match in matches:
        hex_code = f"#{match.group(1).lower()}"
        interpretation = match.group(2) if match.group(2) else "no interpretation provided"

        # Parse to HSL breakdown
        hsl = hex_to_hsl(hex_code)

        # Generate token ID
        token_id = generate_vrgb_token_id()

        # Create immutable VRGB token
        input_history[token_id] = {
            'type': 'vrgb_token',
            'hex': hex_code,
            'hsl': hsl,
            'interpretation': interpretation.strip(),
            'created_at': now.isoformat(),
            'expires_at': expiry.isoformat(),
            'status': 'active'
        }

        created_tokens.append(token_id)
        print(f"✅ Created VRGB token: {token_id} = {hex_code} ({interpretation})")

    return created_tokens

def map_slider_to_semantic_value(slider_value, dimension_label):
    """Map slider value (0-100) to natural language based on dimension"""
    val = int(slider_value)

    # Generic 5-level mapping
    if val < 20:
        intensity = 'very low'
    elif val < 40:
        intensity = 'low'
    elif val < 60:
        intensity = 'moderate'
    elif val < 80:
        intensity = 'high'
    else:
        intensity = 'very high'

    return f"{intensity} {dimension_label}"

def generate_scalar_token_id(semantic_label):
    """Generate token ID for scalar parameter token"""
    import time
    timestamp = int(time.time())
    return f"ctx_{semantic_label}_{timestamp}"

def create_scalar_param_token(slider_value, semantic_label, hex_value, hsl_value, question=None):
    """
    Create a persistent scalar parameter token from slider input.

    This implements the object creation pipeline for VRGB-encoded tokens.
    Tokens are stored in:
    - input_history (in-memory)
    - .claude/tokens/{token_id}.json (persistent file)
    - logs/{date}.jsonl (appended to daily log)

    Args:
        slider_value: 0-100 slider position
        semantic_label: Dimension name (e.g., 'urgency', 'confidence')
        hex_value: Hex-encoded coordinate string
        hsl_value: HSL breakdown dict {h, s, l}
        question: Optional question text that prompted this input

    Returns:
        token_id: Generated token identifier
    """
    ensure_tokens_dir()

    now = datetime.now()
    expiry = now + timedelta(hours=SCALAR_PARAM_TOKEN_EXPIRY_HOURS)

    # Generate token ID
    token_id = generate_scalar_token_id(semantic_label)

    # Map slider value to natural language
    natural_value = map_slider_to_semantic_value(slider_value, semantic_label)

    # Create token object
    token = {
        'token_id': token_id,
        'type': 'scalar_param',
        'semantic_label': semantic_label,
        'value_hex': hex_value,
        'value_decoded': hsl_value,
        'slider_value': slider_value,
        'natural_value': natural_value,
        'question': question or '',
        'created_at': now.isoformat(),
        'expires_at': expiry.isoformat(),
        'status': 'active'
    }

    # Store in memory
    input_history[token_id] = token

    # Persist to file
    token_file = TOKENS_DIR / f"{token_id}.json"
    with open(token_file, 'w') as f:
        json.dump(token, f, indent=2)

    # Append to daily log
    ensure_log_dir()
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = LOG_DIR / f"{today}.jsonl"

    log_entry = {
        'timestamp': now.isoformat(),
        'event': 'token_created',
        'token': token
    }

    with open(log_file, 'a') as f:
        f.write(json.dumps(log_entry) + '\n')

    print(f"✅ Created scalar param token: {token_id} ({semantic_label}: {natural_value})")
    print(f"   Encoded as: {hex_value}")
    print(f"   Expires: {expiry.strftime('%Y-%m-%d %H:%M')}")

    return token_id

def check_and_expire_tokens():
    """
    Check all tokens and mark expired ones as expired.

    Scans:
    - input_history (in-memory)
    - .claude/tokens/*.json files

    Returns:
        dict: {"expired": [...], "active": [...]}
    """
    now = datetime.now()
    expired = []
    active = []

    # Check in-memory tokens
    for token_id, token_data in input_history.items():
        if token_data.get('type') not in ['scalar_param', 'vrgb_token']:
            continue

        expires_at_str = token_data.get('expires_at')
        if not expires_at_str:
            continue

        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if now > expires_at:
                token_data['status'] = 'expired'
                expired.append(token_id)
            else:
                active.append(token_id)
        except (ValueError, TypeError):
            continue

    # Check file-based tokens
    if TOKENS_DIR.exists():
        for token_file in TOKENS_DIR.glob('*.json'):
            try:
                with open(token_file, 'r') as f:
                    token_data = json.load(f)

                token_id = token_data.get('token_id')
                expires_at_str = token_data.get('expires_at')

                if expires_at_str:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if now > expires_at:
                        token_data['status'] = 'expired'
                        # Update file
                        with open(token_file, 'w') as f:
                            json.dump(token_data, f, indent=2)

                        if token_id not in expired:
                            expired.append(token_id)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

    return {"expired": expired, "active": active}

def cleanup_expired_tokens():
    """
    Remove expired tokens from active context.

    This function:
    - Marks tokens as expired (status field)
    - Keeps them in logs for provenance
    - Removes from active .claude/tokens/ directory by archiving
    - Keeps in-memory history for reference
    """
    ensure_tokens_dir()
    archive_dir = TOKENS_DIR / 'archive'
    archive_dir.mkdir(exist_ok=True)

    status = check_and_expire_tokens()
    expired_tokens = status['expired']

    if not expired_tokens:
        return {"archived": 0, "message": "No expired tokens to clean up"}

    archived_count = 0

    # Archive file-based tokens
    if TOKENS_DIR.exists():
        for token_file in TOKENS_DIR.glob('ctx_*.json'):
            try:
                with open(token_file, 'r') as f:
                    token_data = json.load(f)

                if token_data.get('status') == 'expired':
                    # Move to archive
                    archive_file = archive_dir / token_file.name
                    token_file.rename(archive_file)
                    archived_count += 1
                    print(f"📦 Archived expired token: {token_file.name}")
            except (json.JSONDecodeError, IOError):
                continue

    return {
        "archived": archived_count,
        "expired_count": len(expired_tokens),
        "message": f"Archived {archived_count} expired tokens"
    }

def get_active_scalar_tokens():
    """
    Get all active (non-expired) scalar parameter tokens.

    Returns:
        list: Active token objects with natural language values
    """
    check_and_expire_tokens()  # Ensure expiry status is current

    active_tokens = []

    for token_id, token_data in input_history.items():
        if token_data.get('type') == 'scalar_param' and token_data.get('status') == 'active':
            active_tokens.append({
                'label': token_data.get('semantic_label'),
                'value': token_data.get('natural_value'),
                'created_at': token_data.get('created_at'),
                'token_id': token_id
            })

    return active_tokens

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

def hsl_to_hex(h, s, l):
    """Convert HSL color values to hex code"""
    s = s / 100
    l = l / 100
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2

    if 0 <= h < 60:
        r, g, b = c, x, 0
    elif 60 <= h < 120:
        r, g, b = x, c, 0
    elif 120 <= h < 180:
        r, g, b = 0, c, x
    elif 180 <= h < 240:
        r, g, b = 0, x, c
    elif 240 <= h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    r = int((r + m) * 255)
    g = int((g + m) * 255)
    b = int((b + m) * 255)

    return f"#{r:02x}{g:02x}{b:02x}"

def interpret_confidence(h, s, l):
    """Interpret HSL values into semantic meaning"""
    # Domain interpretation (hue)
    if 0 <= h < 60:
        domain = "urgent/time-sensitive"
    elif 60 <= h < 120:
        domain = "creative/experimental"
    elif 120 <= h < 180:
        domain = "safe/approved-pattern"
    elif 180 <= h < 240:
        domain = "data-driven/analytical"
    elif 240 <= h < 300:
        domain = "strategic/long-term"
    else:
        domain = "edge-case/exception"

    # Conviction interpretation (saturation)
    if s > 75:
        conviction = "very strong"
    elif s > 50:
        conviction = "moderate"
    elif s > 25:
        conviction = "weak"
    else:
        conviction = "uncertain"

    # Clarity interpretation (lightness)
    if l > 70:
        clarity = "very clear"
    elif l > 50:
        clarity = "moderately clear"
    elif l > 30:
        clarity = "somewhat unclear"
    else:
        clarity = "very uncertain"

    return {
        "domain": domain,
        "conviction": conviction,
        "clarity": clarity
    }

def get_relative_time(timestamp):
    """Get human-readable relative time since session start"""
    delta = timestamp - SESSION_START
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    if hours == 0:
        return f"{minutes}m ago"
    else:
        return f"{hours}h {minutes}m ago"

def log_conversation(user_text, assistant_text, speech_metadata=None, input_length=None, confidence=None):
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

    if confidence:
        h, s, l = confidence['h'], confidence['s'], confidence['l']
        entry['confidence'] = {
            'hsl': {'h': h, 's': s, 'l': l},
            'hex': hsl_to_hex(h, s, l),
            'interpretation': interpret_confidence(h, s, l)
        }

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


def get_temporal_context():
    """Get temporal context including speech consumption"""
    return get_speech_consumption_context()


def get_variables_context():
    """Get session variables context"""
    if not session_variables:
        return ""

    variables_list = "\n".join([f"{key}={value}" for key, value in session_variables.items()])
    return f"""[SESSION VARIABLES]
{variables_list}

"""


def get_input_history_context():
    """Get input history context with metadata and semantic meaning"""
    if not input_history:
        return ""

    history_lines = []
    for input_id, data in input_history.items():
        status = data.get('status', 'unknown')
        input_type = data.get('type', 'unknown')

        # VRGB tokens (immutable snapshot objects with key:hex pairs)
        if input_type == 'vrgb_token' and status == 'active':
            hex_val = data.get('hex', 'N/A')
            interpretation = data.get('interpretation', '')
            expires_at = data.get('expires_at', '')

            # Compact format: key:hex (interpretation, expires ISO, active)
            history_lines.append(f"{input_id}:{hex_val} ({interpretation}, expires {expires_at}, active)")

        elif status == 'completed':
            key_str = f" [{data['key']}]" if 'key' in data else ""

            # For HSL inputs, show semantic context
            if input_type == 'hsl_slider' and 'hex' in data:
                hex_val = data.get('hex', 'N/A')
                interpretation = data.get('interpretation', '')
                semantic_mapping = data.get('semantic_mapping', '')

                if semantic_mapping:
                    # Show hex as encoding abstract dimensions, not color
                    history_lines.append(f"{input_id}{key_str}: {hex_val} encodes {interpretation} ({semantic_mapping}, completed)")
                else:
                    # Fallback without semantic mapping
                    history_lines.append(f"{input_id}{key_str}: {hex_val} ({interpretation}, hsl_slider, completed)")
            else:
                # Text or other input types
                value_str = data.get('value', 'N/A')
                history_lines.append(f"{input_id}{key_str}: {value_str} ({input_type}, completed)")
        elif status == 'pending':
            history_lines.append(f"{input_id}: pending {input_type} input")

    if not history_lines:
        return ""

    history_text = "\n".join(history_lines)

    # Add active scalar parameter tokens
    active_tokens = get_active_scalar_tokens()
    scalar_context = ""
    if active_tokens:
        token_lines = []
        for token in active_tokens:
            label = token['label']
            value = token['value']
            created = token.get('created_at', '')
            token_lines.append(f"- {label}: {value} (set {created})")

        scalar_context = f"""[ACTIVE CONTEXT TOKENS]
{chr(10).join(token_lines)}

"""

    # Add VRGB policy reminder before token context
    vrgb_policy_note = ""
    if any(data.get('type') in ['vrgb_token', 'scalar_param'] for data in input_history.values()):
        vrgb_policy_note = """[VRGB POLICY]
VRGB uses colorspace as semantic encoding hack - hex strings are coordinates, not colors.
Hex values encode abstract parameters via RGB/HSL structure. Never frame as color selection.
See VRGB_POLICY.md for complete policy.

"""

    return f"""{vrgb_policy_note}{scalar_context}[INPUT HISTORY]
{history_text}

"""


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

        # Detect and create VRGB tokens from hex codes in user input
        detect_and_create_vrgb_tokens(text)

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

        # Inject speech consumption, variables, and input history context
        speech_context = get_speech_consumption_context()
        variables_context = get_variables_context()
        input_history_context = get_input_history_context()

        # Add yes/no button and approval instructions to ALL prompts
        enhanced_text = f"""{speech_context}{variables_context}{input_history_context}{length_constraint}[VOICE INTERFACE INSTRUCTIONS]
When you need confirmation, format your response like this:
[YES_NO: your question here]

Example: "[YES_NO: Should I proceed with this operation?]"

When you need approval for an action (Read/Write/Edit/Bash/etc), format your response like this:
[APPROVAL: {{"action": "Write", "target": "/path/to/file", "description": "Creating new config file", "preview": "# Config\\nkey=value"}}]

When you need user input, use one of these formats:

1. Text input:
[INPUT: {{"type": "text", "question": "What should I name this file?"}}]

2. VRGB slider input (semantic metaphorical slider with labeled poles):
[INPUT: {{"type": "slider", "question": "How urgent is this?", "scale": {{"low": "casual", "high": "critical"}}, "semantic_label": "urgency"}}]

IMPORTANT: Always include "scale" with semantic pole labels (NOT technical HSL terms).

3. Multiple choice:
[INPUT: {{"type": "choice", "question": "Which approach should I use?", "options": [{{"label": "Option A", "hsl": {{"h": 120, "s": 75, "l": 60}}}}, {{"label": "Option B", "hsl": {{"h": 0, "s": 75, "l": 60}}}}]}}]

The UI will automatically render interactive input cards with appropriate controls.

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

        # Speak response (sanitize for TTS)
        tts_text = sanitize_for_tts(response)
        subprocess.run(['say', tts_text], check=True)

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

        # Inject speech consumption, variables, and input history context
        speech_context = get_speech_consumption_context()
        variables_context = get_variables_context()
        input_history_context = get_input_history_context()

        # Build prompt with context
        enhanced_text = f"""{speech_context}{variables_context}{input_history_context}{length_constraint}{context}[USER'S RESPONSE TO YOUR LAST QUESTION]
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

        # Speak response (sanitize for TTS)
        tts_text = sanitize_for_tts(response)
        subprocess.run(['say', tts_text], check=True)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('approval_response')
def handle_approval_response(data):
    """Handle approval gate response - similar to button_response"""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        subprocess.run(['killall', 'say'], stderr=subprocess.DEVNULL)

        decision = data['decision']  # "Approve" or "Deny"
        approval_data = data.get('approval_data', {})
        confidence = data.get('confidence')  # HSL confidence values

        emit('state_change', {'state': 'thinking'})

        # Calculate input length for response matching
        input_word_count = get_input_word_count(decision)
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

        # Inject speech consumption, variables, and input history context
        speech_context = get_speech_consumption_context()
        variables_context = get_variables_context()
        input_history_context = get_input_history_context()

        # Build prompt with approval context
        action_summary = f"{approval_data.get('action', 'Action')} on {approval_data.get('target', 'target')}"
        enhanced_text = f"""{speech_context}{variables_context}{input_history_context}{length_constraint}{context}[USER'S APPROVAL DECISION]
Action requested: {action_summary}
User decision: {decision}

[VOICE INTERFACE INSTRUCTIONS]
The user has responded to your approval request with "{decision}".
- If Approve: Proceed with the action and confirm completion
- If Deny: Acknowledge and ask what they'd like to do instead

When you need confirmation, format your response like this:
[YES_NO: your question here]

When you need approval for an action, format your response like this:
[APPROVAL: {{"action": "Write", "target": "/path/to/file", "description": "What you're doing", "preview": "content preview"}}]

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

        # Log conversation (approval decision as user input) with input length and confidence
        log_conversation(f"{decision} ({action_summary})", response, input_length=input_word_count, confidence=confidence)

        emit('response', {'text': response})
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(response)

        # Speak response (sanitize for TTS)
        tts_text = sanitize_for_tts(response)
        subprocess.run(['say', tts_text], check=True)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('input_response')
def handle_input_response(data):
    """Handle input response from INPUT cards (text, slider, choice)"""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        subprocess.run(['killall', 'say'], stderr=subprocess.DEVNULL)

        # Extract input data
        input_data = data.get('input') or data.get('choice')

        # Generate or extract input ID for tracking
        input_id = input_data.get('input_id') if isinstance(input_data, dict) else generate_input_id()
        if not input_id or not isinstance(input_data, dict) or 'input_id' not in input_data:
            input_id = generate_input_id()

        # Format input as string for user message and track in input_history
        if isinstance(input_data, dict):
            # Text input (key-value pair)
            if 'key' in input_data and 'value' in input_data:
                key = input_data['key']
                value = input_data['value']
                # Store as session variable
                session_variables[key] = value
                user_message = f"{key}={value}"

                # Track in input history
                input_history[input_id] = {
                    'type': 'text',
                    'key': key,
                    'value': value,
                    'responded_at': datetime.now().isoformat(),
                    'status': 'completed'
                }

            # HSL slider input (Scalar Parameter Token)
            elif 'hsl' in input_data:
                hsl = input_data['hsl']
                hex_val = input_data['hex']
                interp = input_data['interpretation']

                # Build semantic interpretation string
                interpretation_str = f"{interp['domain']}, {interp['conviction']}, {interp['clarity']}"
                hsl_summary = f"{hex_val} ({interpretation_str})"

                # Extract semantic label (try camelCase first, then snake_case, then fallbacks)
                semantic_label = (
                    input_data.get('semanticlabel') or
                    input_data.get('semantic_label') or
                    input_data.get('key')
                )

                if not semantic_label:
                    # Extract first component from semantic_mapping as label
                    semantic_mapping = input_data.get('semantic_mapping', 'parameter')
                    semantic_label = semantic_mapping.split('/')[0] if '/' in semantic_mapping else semantic_mapping

                # Extract slider value (try camelCase first, then snake_case, then fallback to lightness)
                slider_value = (
                    input_data.get('slidervalue') or
                    input_data.get('slider_value') or
                    hsl.get('l', 50)
                )

                # Get question text
                question = input_data.get('question', '')

                # Map slider value to natural language
                natural_value = map_slider_to_semantic_value(slider_value, semantic_label)

                # Create persistent scalar parameter token
                token_id = create_scalar_param_token(
                    slider_value=slider_value,
                    semantic_label=semantic_label,
                    hex_value=hex_val,
                    hsl_value=hsl,
                    question=question
                )

                # Format user message to clearly indicate this is answering the question
                if question:
                    user_message = f"[Re: {question}] {semantic_label}: {natural_value}"
                else:
                    user_message = f"{semantic_label}: {natural_value}"

                # Store as session variable if key is present
                if 'key' in input_data:
                    key = input_data['key']
                    session_variables[key] = hsl_summary

                # Note: Token is already stored in input_history by create_scalar_param_token()

            # Choice input
            elif 'label' in input_data:
                user_message = input_data['label']

                # Track in input history
                input_history[input_id] = {
                    'type': 'choice',
                    'value': input_data['label'],
                    'responded_at': datetime.now().isoformat(),
                    'status': 'completed'
                }
            else:
                user_message = str(input_data)
        else:
            user_message = input_data

        emit('state_change', {'state': 'thinking'})

        # Count input words
        input_word_count = len(str(user_message).split())

        # Get temporal, variables, and input history context
        speech_context = get_temporal_context()
        variables_context = get_variables_context()
        history_context = get_input_history_context()

        # Prepare input for Claude with all context
        enhanced_text = f"{speech_context}{variables_context}{history_context}[USER INPUT]\n{user_message}"

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

        # Log conversation with input length
        log_conversation(user_message, response, input_length=input_word_count)

        emit('response', {'text': response})
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(response)

        # Speak response (sanitize for TTS)
        tts_text = sanitize_for_tts(response)
        subprocess.run(['say', tts_text], check=True)

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

        # Inject speech consumption, variables, and input history context
        speech_context = get_speech_consumption_context()
        variables_context = get_variables_context()
        input_history_context = get_input_history_context()

        # Add yes/no button and approval instructions to ALL prompts
        enhanced_text = f"""{speech_context}{variables_context}{input_history_context}{length_constraint}[VOICE INTERFACE INSTRUCTIONS]
When you need confirmation, format your response like this:
[YES_NO: your question here]

Example: "[YES_NO: Should I proceed with this operation?]"

When you need approval for an action (Read/Write/Edit/Bash/etc), format your response like this:
[APPROVAL: {{"action": "Write", "target": "/path/to/file", "description": "Creating new config file", "preview": "# Config\\nkey=value"}}]

When you need user input, use one of these formats:

1. Text input:
[INPUT: {{"type": "text", "question": "What should I name this file?"}}]

2. VRGB slider input (semantic metaphorical slider with labeled poles):
[INPUT: {{"type": "slider", "question": "How urgent is this?", "scale": {{"low": "casual", "high": "critical"}}, "semantic_label": "urgency"}}]

IMPORTANT: Always include "scale" with semantic pole labels (NOT technical HSL terms).

3. Multiple choice:
[INPUT: {{"type": "choice", "question": "Which approach should I use?", "options": [{{"label": "Option A", "hsl": {{"h": 120, "s": 75, "l": 60}}}}, {{"label": "Option B", "hsl": {{"h": 0, "s": 75, "l": 60}}}}]}}]

The UI will automatically render interactive input cards with appropriate controls.

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

        # Speak response (sanitize for TTS)
        tts_text = sanitize_for_tts(response)
        subprocess.run(['say', tts_text], check=True)

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


@socketio.on('connect')
def handle_connect():
    """Track client connection"""
    ensure_log_dir()
    timestamp = datetime.now()
    log_file = LOG_DIR / f"{timestamp.strftime('%Y-%m-%d')}.jsonl"

    entry = {
        'timestamp': timestamp.isoformat(),
        'event': 'client_connected',
        't_relative': get_relative_time(timestamp),
        't_period': get_time_period(timestamp)
    }

    with open(log_file, 'a') as f:
        f.write(json.dumps(entry) + '\n')

    print(f"🔌 Client connected at {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")


@socketio.on('disconnect')
def handle_disconnect():
    """Track client disconnection"""
    ensure_log_dir()
    timestamp = datetime.now()
    log_file = LOG_DIR / f"{timestamp.strftime('%Y-%m-%d')}.jsonl"

    entry = {
        'timestamp': timestamp.isoformat(),
        'event': 'client_disconnected',
        't_relative': get_relative_time(timestamp),
        't_period': get_time_period(timestamp)
    }

    with open(log_file, 'a') as f:
        f.write(json.dumps(entry) + '\n')

    print(f"🔌 Client disconnected at {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")


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
