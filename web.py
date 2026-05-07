#!/usr/bin/env python3
"""
cue-vox web interface - localhost voice UI for Claude Code
"""

from flask import Flask, render_template, request, jsonify, send_from_directory
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
import hashlib
from datetime import datetime, timedelta
import threading
import time
import math
import re
import sys
import queue

# Determine maestro root directory
def find_maestro_root():
    """Find maestro root by walking up directory tree"""
    # Priority 1: MAESTRO_ROOT env var
    if 'MAESTRO_ROOT' in os.environ:
        return Path(os.environ['MAESTRO_ROOT'])

    # Priority 2: Walk up from current file location
    # Skip cue-vox's own directory (it has .git + hooks but is NOT maestro root)
    script_dir = Path(__file__).parent.resolve()
    current = script_dir
    while current != current.parent:
        if current != script_dir and (current / '.git').exists() and (current / 'hooks').exists():
            return current
        current = current.parent

    # Priority 3: Walk up from current working directory
    current = Path.cwd()
    while current != current.parent:
        if (current / '.git').exists() and (current / 'hooks').exists():
            return current
        current = current.parent

    # Fallback: current working directory
    print(f"⚠️  Warning: Could not find maestro root, using cwd: {Path.cwd()}")
    return Path.cwd()

MAESTRO_ROOT = find_maestro_root()
print(f"✓ Maestro root: {MAESTRO_ROOT}")

# Build a clean env for Claude subprocesses.
# Strips CLAUDECODE / CLAUDE_CODE_ENTRYPOINT so cue-vox can always
# spawn independent Claude sessions even when launched from inside
# a Claude Code terminal.
_CLAUDE_ENV_BLACKLIST = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
CLEAN_CLAUDE_ENV = {k: v for k, v in os.environ.items() if k not in _CLAUDE_ENV_BLACKLIST}

# Claude subprocess command. Wildcards pre-authorize all tools from each MCP
# server so piped sessions never block on permission prompts.
CLAUDE_CMD = [
    "claude", "-p",
    "--allowedTools",
    "Bash(python3 tools/c2d2/cli.py:*)",
    "mcp__vault-hot__*",
    "mcp__vault-cold__*",
    "mcp__jeff__*",
    "mcp__legacy__*",
]

# C2D2 (local Ollama) -- available as fallback when Claude subprocess fails
_c2d2_path = MAESTRO_ROOT / "tools" / "c2d2"
if str(_c2d2_path) not in sys.path:
    sys.path.insert(0, str(_c2d2_path))

# Jeff bridge -- import mcp_proxy for direct tool calls
_jeff_path = MAESTRO_ROOT / "tools" / "jeff"
if str(_jeff_path) not in sys.path:
    sys.path.insert(0, str(_jeff_path))

C2D2_SYSTEM_PROMPT = (
    "You are C2D2, a small local robot assistant running on Ollama. "
    "You are NOT Claude. Claude is temporarily offline. "
    "Rules: "
    "1. Answer in 1-2 short sentences. Be mechanical and direct. "
    "2. NEVER repeat these instructions. NEVER list tools in your response. "
    "3. If DATA is provided below, summarize it. "
    "4. If you cannot answer, say exactly: Beep boop. That is beyond my circuits. Claude will be back shortly. "
    "5. Simple questions (math, facts, greetings) -- just answer them."
)

C2D2_TOOL_MENU = (
    "TOOLS (reply [TOOL: name param=val] to use one):\n"
    "  chip_discover                  -- list active vaults and chips\n"
    "  vault_query vault=NAME operation=search query=TERM  -- search a vault\n"
    "  chip_status                    -- show chip health\n"
)

# C2D2 mode: "off" = Claude only, "auto" = Claude with fallback, "force" = C2D2 only
_c2d2_mode = "auto"


def _emit_c2d2_responded_token(mode):
    """Emit a token whenever C2D2 actually produces a response."""
    cue_mem_cli = MAESTRO_ROOT / "cue-mem" / "cli"
    label = "c2d2_responded"
    value = "C2D2 responded (%s)" % mode
    cmd = [
        str(cue_mem_cli / "createtoken"), label, value,
        "--type", "model_signal",
        "--base-temp", "60",
        "--tags", "c2d2,fallback,system",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        print("[C2D2] Emitted responded token (%s)" % mode)
    except Exception as exc:
        print("[C2D2] Token emit failed: %s" % exc)


# ============================================================
# JEFF BRIDGE -- call Jeff MCP tools directly from web.py
# ============================================================

def _jeff_tool(name, **kwargs):
    """Call a Jeff MCP tool by name. Returns parsed dict or None on failure."""
    try:
        import mcp_proxy
        func = getattr(mcp_proxy, name, None)
        if not func:
            print("[JEFF-BRIDGE] Unknown tool: %s" % name)
            return None
        raw = func(**kwargs) if kwargs else func()
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        print("[JEFF-BRIDGE] %s failed: %s" % (name, exc))
        return None


# ============================================================
# CLAUDE HEALTH CHECK -- C2D2 pings Claude on request
# ============================================================

def _check_claude_health():
    """Ping Claude subprocess with a minimal prompt. Returns health dict."""
    result = {"service": "claude", "status": "unknown", "error": None}
    try:
        process = subprocess.Popen(
            ["claude", "-p"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(MAESTRO_ROOT),
            env=CLEAN_CLAUDE_ENV,
        )
        stdout, stderr = process.communicate(input="reply with exactly: pong", timeout=30)
        if process.returncode == 0 and stdout.strip():
            result["status"] = "ok"
            result["response"] = stdout.strip()[:100]
        else:
            result["status"] = "error"
            result["error"] = "exit code %d" % process.returncode
            if stderr and stderr.strip():
                result["error"] += ": %s" % stderr.strip()[:200]
    except subprocess.TimeoutExpired:
        process.kill()
        result["status"] = "timeout"
        result["error"] = "Claude did not respond within 30 seconds"
    except FileNotFoundError:
        result["status"] = "not_found"
        result["error"] = "claude command not found on PATH"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:200]
    print("[C2D2] Claude health check: %s" % result["status"])
    return result


# ============================================================
# TIER 0 -- hardcoded keyword patterns (model never decides)
# ============================================================

_TIER0_PATTERNS = [
    # (keywords_any, tool_name, tool_kwargs, description)
    (
        ["what's active", "whats active", "what vaults", "what's mounted", "whats mounted",
         "list vaults", "show vaults", "active vaults"],
        "chip_discover", {},
        "active vaults and chips",
    ),
    (
        ["chip status", "chip health", "chip info"],
        "chip_status", {},
        "chip status",
    ),
]

_CLAUDE_CHECK_KEYWORDS = [
    "is claude up", "is claude working", "is claude alive", "is claude running",
    "check claude", "claude status", "ping claude", "claude health",
    "is claude ok", "is claude down",
]


def _tier0_match(user_text):
    """Check user text against tier 0 patterns. Returns (data_dict, description) or (None, None)."""
    lower = user_text.lower()

    # Claude health check (by request only)
    for kw in _CLAUDE_CHECK_KEYWORDS:
        if kw in lower:
            data = _check_claude_health()
            return (data, "Claude health check")

    # Static patterns
    for keywords, tool_name, tool_kwargs, desc in _TIER0_PATTERNS:
        for kw in keywords:
            if kw in lower:
                data = _jeff_tool(tool_name, **tool_kwargs)
                if data:
                    return (data, desc)
                break

    # Dynamic: "search X in Y" or "find X in Y"
    import re
    search_match = re.search(r"(?:search|find|look for)\s+(.+?)\s+in\s+(\w+)", lower)
    if search_match:
        query = search_match.group(1).strip()
        vault = search_match.group(2).strip()
        data = _jeff_tool("vault_query", vault=vault, operation="search", query=query)
        if data:
            return (data, "search results for '%s' in %s" % (query, vault))

    return (None, None)


# ============================================================
# TIER 1 -- tool-use parser (model requests, we execute)
# ============================================================

def _parse_tool_tag(response_text):
    """Parse [TOOL: name param=val ...] from C2D2 output. Returns (name, kwargs) or (None, None)."""
    import re
    match = re.search(r"\[TOOL:\s*(\w+)(.*?)\]", response_text)
    if not match:
        return (None, None)

    tool_name = match.group(1)
    params_str = match.group(2).strip()
    kwargs = {}
    if params_str:
        for pair in re.finditer(r"(\w+)=(\S+)", params_str):
            kwargs[pair.group(1)] = pair.group(2)

    return (tool_name, kwargs)


_C2D2_ALLOWED_TOOLS = {"chip_discover", "vault_query", "chip_status"}


def _tier1_tool_round(response_text):
    """If C2D2 requested a tool, execute it and return the result string. Otherwise None."""
    tool_name, kwargs = _parse_tool_tag(response_text)
    if not tool_name:
        return None
    if tool_name not in _C2D2_ALLOWED_TOOLS:
        return "[Tool '%s' not available]" % tool_name

    data = _jeff_tool(tool_name, **kwargs)
    if data is None:
        return "[Tool '%s' returned no data]" % tool_name

    return json.dumps(data, indent=2)


# ============================================================
# C2D2 PROMPT + CALL
# ============================================================

def _build_c2d2_prompt(user_text, prefetched_data=None, prefetched_desc=None):
    """Build a minimal prompt for C2D2. No flux, no engagement, no context bloat."""
    parts = [C2D2_SYSTEM_PROMPT, "", C2D2_TOOL_MENU]
    if prefetched_data:
        parts.append("DATA (%s):" % (prefetched_desc or "query result"))
        parts.append(json.dumps(prefetched_data, indent=2)[:3000])
        parts.append("")
        parts.append("Summarize this data for the user in 2-3 sentences.")
        parts.append("")
    parts.append("User: %s" % user_text)
    return "\n".join(parts)


def _call_c2d2(user_text):
    """Full C2D2 pipeline: tier 0 prefetch -> generate -> tier 1 tool round -> final answer."""
    from ollama_client import generate

    # Tier 0: check for hardcoded patterns and prefetch data
    prefetched, desc = _tier0_match(user_text)

    # Build slim prompt
    prompt = _build_c2d2_prompt(user_text, prefetched_data=prefetched, prefetched_desc=desc)
    print("[C2D2] Prompt length: %d chars (prefetch: %s)" % (len(prompt), desc or "none"))

    # First generation pass
    response = generate(prompt, max_tokens=512, timeout=60)
    if not response:
        return ""

    # Tier 1: if model requested a tool, execute and do one more pass
    tool_result = _tier1_tool_round(response)
    if tool_result:
        print("[C2D2] Tier 1 tool call detected, executing round-trip")
        followup_prompt = "%s\n\nTool result:\n%s\n\nSummarize this for the user in 2-3 sentences." % (
            prompt, tool_result[:3000]
        )
        response = generate(followup_prompt, max_tokens=512, timeout=60) or response

    # Strip any remaining [TOOL: ...] tags from final output
    import re
    response = re.sub(r"\[TOOL:.*?\]", "", response).strip()

    return response


def _call_claude_or_fallback(prompt_text, raw_user_text=""):
    """Route prompt based on _c2d2_mode.

    Returns (response_text, used_c2d2).
    raw_user_text: the original user input (pre-context-injection), used for C2D2 slim prompt.
    """
    global _active_claude_process, _claude_voided
    _claude_voided = False

    # -- Force mode: skip Claude entirely, use slim C2D2 path --
    if _c2d2_mode == "force":
        try:
            print("[C2D2] Force mode -- routing to local model")
            c2d2_input = raw_user_text or prompt_text
            response = _call_c2d2(c2d2_input)
            if response:
                _emit_c2d2_responded_token("force")
                return (response, True)
        except Exception as exc:
            print("[C2D2] Force mode failed: %s" % exc)
        return ("", True)

    # -- Normal Claude path --
    try:
        process = subprocess.Popen(
            CLAUDE_CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=MAESTRO_ROOT,
            env=CLEAN_CLAUDE_ENV,
        )
        _active_claude_process = process
        stdout, stderr = process.communicate(input=prompt_text)
        _active_claude_process = None

        if _claude_voided:
            return (None, False)

        response = stdout.strip()

        if stderr and stderr.strip():
            print("[CLAUDE STDERR] %s" % stderr.strip()[:500])

        if response:
            return (response, False)

        print("[CLAUDE] Empty response. Exit code: %d. Prompt length: %d chars"
              % (process.returncode, len(prompt_text)))
    except Exception as exc:
        _active_claude_process = None
        print("[CLAUDE] Subprocess failed: %s" % exc)

    # -- Auto fallback to C2D2 --
    if _c2d2_mode != "auto":
        return ("", False)

    try:
        print("[C2D2-FALLBACK] Primary unavailable, using local model")
        c2d2_input = raw_user_text or prompt_text
        response = _call_c2d2(c2d2_input)
        if response:
            _emit_c2d2_responded_token("auto")
            return (response, True)
    except Exception as fallback_exc:
        print("[C2D2-FALLBACK] Also failed: %s" % fallback_exc)

    return ("", False)


# Try to import CUE-MEM if available
CUE_MEM_AVAILABLE = False
try:
    # Look for .claude/cue-mem symlink in maestro root
    cue_mem_lib = MAESTRO_ROOT / '.claude' / 'cue-mem' / 'lib'
    if cue_mem_lib.exists():
        sys.path.insert(0, str(cue_mem_lib))
        from tokens import create_token as cue_mem_create_token
        from tokens import list_tokens as cue_mem_list_tokens
        from tokens import get_token as cue_mem_get_token
        CUE_MEM_AVAILABLE = True
        print("✓ CUE-MEM integration enabled")
except ImportError as e:
    print(f"⚠️  CUE-MEM not available, using local token storage: {e}")
    pass

# Import TokenFactory
try:
    cue_mem_factory_lib = MAESTRO_ROOT / 'cue-mem' / 'lib'
    if cue_mem_factory_lib.exists():
        sys.path.insert(0, str(cue_mem_factory_lib))
        from token_factory import TokenFactory
        from token_profiles import resolve_thermal
        from structured_sum import create_structured_sum
        print("✓ TokenFactory loaded")
    else:
        TokenFactory = None
        resolve_thermal = None
        create_structured_sum = None
except ImportError as e:
    print("⚠️  TokenFactory not available: %s" % e)
    TokenFactory = None
    resolve_thermal = None
    create_structured_sum = None

# Import AuditLogger for structured audit trail
_audit_logger = None
_audit_subscriber = None
try:
    cue_mem_audit_lib = MAESTRO_ROOT / "cue-mem" / "lib"
    if cue_mem_audit_lib.exists():
        if str(cue_mem_audit_lib) not in sys.path:
            sys.path.insert(0, str(cue_mem_audit_lib))
        from audit import AuditLogger
        from audit_subscriber import AuditSubscriber
        from event_bus import get_bus
        _audit_logger = AuditLogger(agent_id="cue-vox")
        _audit_subscriber = AuditSubscriber(_audit_logger)
        _audit_subscriber.connect()
        print("✓ AuditLogger enabled")
except ImportError as e:
    print("⚠️  AuditLogger not available: %s" % e)

# Import CitationResolver for verifying source references
_citation_resolver = None
try:
    from citation_resolver import CitationResolver
    _citation_resolver = CitationResolver(project_root=MAESTRO_ROOT)
    print("✓ CitationResolver enabled")
except ImportError as e:
    print("⚠️  CitationResolver not available: %s" % e)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'cue-vox-secret'
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
socketio = SocketIO(app, cors_allowed_origins="*")


@app.after_request
def add_no_cache_headers(response):
    is_static = request.path.startswith('/static/')
    is_html = response.content_type and 'text/html' in response.content_type
    if is_static or is_html:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        response.cache_control.max_age = 0
        if 'ETag' in response.headers:
            del response.headers['ETag']
        if 'Last-Modified' in response.headers:
            del response.headers['Last-Modified']
    return response

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
tts_interrupted = False

# --- Speech queue: single consumer thread, FIFO, no overlaps ---
_speech_queue = queue.Queue()

# Try to import pyttsx3 as fallback TTS engine
try:
    import pyttsx3
    _pyttsx3_available = True
except ImportError:
    _pyttsx3_available = False
    print("[TTS] pyttsx3 not installed -- no fallback TTS available")


def _say_with_fallback(text, timeout=30):
    """Run macOS say command. If it produces no audible output, retry via pyttsx3."""
    try:
        start = time.time()
        subprocess.run(["say", text], check=False, timeout=timeout)
        elapsed = time.time() - start
        # If say returned almost instantly for non-trivial text, it was silent
        words = len(text.split())
        expected_min = max(0.3, words * 0.15)  # rough floor: 0.15s per word
        if elapsed < expected_min and words > 2:
            print("[TTS] say returned in %.2fs for %d words -- likely silent, trying fallback" % (elapsed, words))
            _speak_pyttsx3(text)
    except subprocess.TimeoutExpired:
        print("[TTS] say timed out after %ds" % timeout)
    except Exception as e:
        print("[TTS] say failed: %s -- trying fallback" % e)
        _speak_pyttsx3(text)


def _speak_pyttsx3(text):
    """Fallback TTS via pyttsx3 (pure Python, no system audio daemon dependency)."""
    if not _pyttsx3_available:
        print("[TTS] pyttsx3 not available, skipping fallback")
        return
    try:
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        engine.stop()
    except Exception as e:
        print("[TTS FALLBACK ERROR] pyttsx3 failed: %s" % e)


def _speech_consumer():
    """Daemon thread: pulls chunks from _speech_queue and speaks them one at a time."""
    while True:
        item = _speech_queue.get()
        if item is None:
            # Poison pill -- shut down
            _speech_queue.task_done()
            break
        chunk_text, chunk_index, emit_events = item
        try:
            if not tts_interrupted:
                if emit_events:
                    socketio.emit("tts_chunk_start", {"index": chunk_index})
                    socketio.sleep(0.05)
                _say_with_fallback(chunk_text)
        except Exception as e:
            print("[TTS CONSUMER ERROR] %s" % e)
        finally:
            _speech_queue.task_done()
        # Check after each chunk -- drain if interrupted
        if tts_interrupted:
            while not _speech_queue.empty():
                try:
                    _speech_queue.get_nowait()
                    _speech_queue.task_done()
                except queue.Empty:
                    break
            if emit_events:
                socketio.emit("tts_chunk_done")


# Start the consumer thread
_speech_thread = threading.Thread(target=_speech_consumer, daemon=True)
_speech_thread.start()


def flush_speech_queue():
    """Kill current speech and drain the queue. Call this instead of killall say."""
    global tts_interrupted
    tts_interrupted = True
    # Kill any running say process
    subprocess.run(["killall", "say"], stderr=subprocess.DEVNULL)
    # Drain pending chunks
    while not _speech_queue.empty():
        try:
            _speech_queue.get_nowait()
            _speech_queue.task_done()
        except queue.Empty:
            break

# Active Claude process -- set before communicate(), cleared after
_active_claude_process = None
_claude_voided = False


def abort_claude():
    """Kill the active Claude process if one is running. Called on void/mute."""
    global _active_claude_process, _claude_voided
    _claude_voided = True
    proc = _active_claude_process
    if proc and proc.poll() is None:
        print("[VOID] Killing active Claude process (pid %d)" % proc.pid)
        proc.kill()
    flush_speech_queue()


# Speech consumption tracking
current_speech = None

# Conversation logging with 24-hour retention
LOG_DIR = Path(__file__).parent / 'logs'
LOG_RETENTION_HOURS = 24

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

# Multi-scale rolling summary token configuration
# Temporal pyramid: overlapping context windows at different resolutions
SUMMARY_SCALES = {
    'fine': {
        'interval': 60,        # 1 minute - immediate context
        'window': 5,           # last 5 exchanges
        'base_temp': 90,       # hot (detailed)
        'compression': 'light',
        'last_created': None
    },
    'medium': {
        'interval': 300,       # 5 minutes - conversation arcs
        'window': 15,          # last 15 exchanges
        'base_temp': 70,       # warm (moderate compression)
        'compression': 'medium',
        'last_created': None
    },
    'coarse': {
        'interval': 1800,      # 30 minutes - session themes
        'window': 50,          # last 50 exchanges
        'base_temp': 50,       # cool (heavy compression)
        'compression': 'heavy',
        'last_created': None
    }
}

# Token echo trail configuration
# Meta-tokens that summarize other tokens - recursive awareness
ECHO_INTERVAL_SECONDS = 180  # 3 minutes - echoes of the token constellation
ECHO_BASE_TEMP = 65          # Moderate temp - already second-order compression
last_echo_timestamp = None

# In-memory token storage (fallback when CUE-MEM unavailable)
in_memory_summary_tokens = []

# Active track tag for emoji reaction propagation (set by buff-launch, cleared on reset)
_active_track = None

# Token directories
TOKENS_DIR = MAESTRO_ROOT / '.claude' / 'tokens'
CONTEXT_DIR = MAESTRO_ROOT / '.claude'

# Load prompt template from file (Phase 1D)
PROMPT_TEMPLATE_PATH = Path(__file__).parent / 'prompts' / 'input-protocol.txt'
_prompt_template_cache = None

def load_prompt_template():
    """Load the voice interface prompt template from file."""
    global _prompt_template_cache
    if _prompt_template_cache is not None:
        return _prompt_template_cache
    if PROMPT_TEMPLATE_PATH.exists():
        with open(PROMPT_TEMPLATE_PATH, "r") as f:
            _prompt_template_cache = f.read()
        print("Loaded prompt template from %s" % PROMPT_TEMPLATE_PATH)
    else:
        # Fallback inline template
        _prompt_template_cache = (
            "[VOICE INTERFACE INSTRUCTIONS]\n"
            "When you need confirmation: [YES_NO: your question here]\n"
            "When you need input: [INPUT: {\"type\": \"text\", \"question\": \"...\"}]\n"
            "When you need approval: [APPROVAL: {\"action\": \"...\", \"description\": \"...\"}]\n"
        )
        print("Warning: prompt template file not found, using inline fallback")
    return _prompt_template_cache

# Initialize TokenFactory (unified token creation pipeline)
if TokenFactory is not None:
    token_factory = TokenFactory(
        cue_mem_create_fn=cue_mem_create_token if CUE_MEM_AVAILABLE else None,
        input_history=input_history,
        log_dir=LOG_DIR,
        tokens_dir=TOKENS_DIR,
        fallback_expiry_hours=SCALAR_PARAM_TOKEN_EXPIRY_HOURS
    )
    print("✓ TokenFactory initialized")
else:
    token_factory = None

def extract_citations(text):
    """
    Extract [CITATIONS: {...}] block from end of response.

    Returns:
        (clean_text, citations_data) -- clean_text has the block removed,
        citations_data is the parsed dict or None if not found.
    """
    pattern = r'\[CITATIONS:\s*(\{[\s\S]*?\})\]\s*$'
    match = re.search(pattern, text)
    if not match:
        return (text, None)
    try:
        data = json.loads(match.group(1))
        clean = text[:match.start()].rstrip()
        return (clean, data)
    except (json.JSONDecodeError, ValueError):
        return (text, None)


def extract_snr(text):
    """Extract [SNR: XX] tag from end of response.

    Returns (clean_text, snr_value) -- snr_value is int 0-100 or None.
    """
    pattern = r"\[SNR:\s*(\d{1,3})\]\s*$"
    match = re.search(pattern, text)
    if not match:
        return (text, None)
    try:
        value = int(match.group(1))
        value = max(0, min(100, value))
        clean = text[:match.start()].rstrip()
        return (clean, value)
    except (ValueError, TypeError):
        return (text, None)


_SYSTEM_REMINDER_BALANCED = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>\s*", re.IGNORECASE)
_SYSTEM_REMINDER_OPEN_ORPHAN = re.compile(r"<system-reminder>[\s\S]*$", re.IGNORECASE)
_SYSTEM_REMINDER_CLOSE_ORPHAN = re.compile(r"^[\s\S]*?</system-reminder>\s*", re.IGNORECASE)


def strip_system_reminders(text):
    """Strip <system-reminder>...</system-reminder> blocks.

    System-reminder is a control band, not a conversation band. It must not
    cross into the chat render path, the conversation log, or TTS.

    Three passes:
      1. Balanced tags (the common case).
      2. Orphaned opener: tag without a closer -- strip from tag to end.
      3. Orphaned closer: closing tag without an opener -- strip from start
         through the closer (Claude sometimes emits only the closing tag
         when the system-reminder body bled into the response).
    """
    if not text:
        return text
    text = _SYSTEM_REMINDER_BALANCED.sub("", text)
    text = _SYSTEM_REMINDER_OPEN_ORPHAN.sub("", text)
    text = _SYSTEM_REMINDER_CLOSE_ORPHAN.sub("", text)
    return text.strip()


def strip_markdown_for_tts(text):
    """Strip markdown formatting so macOS say gets clean prose.
    Bullet/number prefixes, bold/italic markers, heading hashes, code fences."""
    # Headings: ## Title -> Title
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Bold/italic: **text** or __text__ or *text* or _text_
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.*?)_{1,3}", r"\1", text)
    # Bullet list markers: - item or * item
    text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)
    # Numbered list markers: 1. item
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Code fences
    text = re.sub(r"```[\s\S]*?```", "", text)
    return text.strip()


def _strip_bracket_balanced_tags(text, tag_types):
    """Remove [TAG: ...] blocks using bracket-balanced matching.

    Handles JSON payloads that contain ] characters (e.g. arrays).
    """
    tag_pattern = "|".join(re.escape(t) for t in tag_types)
    starter = re.compile(r"\[(" + tag_pattern + r"):\s*")
    result = text
    while True:
        m = starter.search(result)
        if not m:
            break
        depth = 1
        pos = m.end()
        while pos < len(result) and depth > 0:
            if result[pos] == "[":
                depth += 1
            elif result[pos] == "]":
                depth -= 1
            if depth > 0:
                pos += 1
        if depth == 0:
            result = result[:m.start()] + result[pos + 1:]
        else:
            break
    return result.strip()


def sanitize_for_tts(text):
    """
    Sanitize text for TTS by extracting question text from structured input tags.
    Prevents TTS from trying to speak raw tags like [YES_NO: ...] or [INPUT: {...}]
    """
    # Strip SNR and citation blocks (metadata only, never spoken)
    text, _ = extract_snr(text)
    text, _ = extract_citations(text)

    # Strip visual-only tags (rendered as widgets, never spoken)
    text = _strip_bracket_balanced_tags(text, ("GALLERY", "APPROVAL", "DOCUMENT", "CUE"))

    print(f"[TTS DEBUG] Input text: {text[:200]}")  # Log first 200 chars

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

        result_text = result.strip()
        print(f"[TTS DEBUG] Sanitized to: {result_text[:200]}")
        return result_text

    # No structured tags found, return original text
    print(f"[TTS DEBUG] No tags found, returning original")
    return text


# TTS budget: macOS `say` runs at ~150 wpm; per-chunk timeout is 30s.
# Cap below the timeout so a long paragraph degrades to a clean sentence
# split instead of a mid-word guillotine. See voice-response-cadence policy.
TTS_MAX_WORDS_PER_CHUNK = 60


def _split_long_chunk(chunk, max_words=TTS_MAX_WORDS_PER_CHUNK):
    """Split an over-budget paragraph on sentence boundaries.

    Sentence-level fallback for the voice-response-cadence policy: when a
    paragraph exceeds the say-timeout budget, group sentences into chunks
    that each fit under max_words. Preserves sentence boundaries; never
    splits mid-sentence.
    """
    if len(chunk.split()) <= max_words:
        return [chunk]
    # Split on sentence terminators while keeping the terminator attached.
    sentences = re.findall(r"[^.!?]+[.!?]+(?:\s+|$)|[^.!?]+$", chunk)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return [chunk]
    grouped = []
    buf = []
    buf_words = 0
    for sentence in sentences:
        s_words = len(sentence.split())
        if buf and buf_words + s_words > max_words:
            grouped.append(" ".join(buf))
            buf = [sentence]
            buf_words = s_words
        else:
            buf.append(sentence)
            buf_words += s_words
    if buf:
        grouped.append(" ".join(buf))
    return grouped


def tts_chunk_split(text):
    """Split text into speakable chunks. Returns list of strings.

    Primary split is paragraph-level (\\n\\n+). Paragraphs that exceed the
    say-timeout budget get a secondary split on sentence boundaries via
    _split_long_chunk -- defense in depth for the voice-response-cadence
    policy.
    """
    if not text or not text.strip():
        return []
    paragraphs = re.split(r"\n\n+", text.strip())
    chunks = []
    for para in paragraphs:
        stripped = para.strip()
        if stripped:
            chunks.extend(_split_long_chunk(stripped))
    return chunks if chunks else [text.strip()]


def speak_chunked(text):
    """Enqueue text as paragraph-sized chunks for the speech consumer thread.
    Emits tts_chunk_start/done events via the consumer. FIFO ordering,
    no overlapping audio. Use flush_speech_queue() to interrupt."""
    global tts_interrupted
    tts_interrupted = False
    chunks = tts_chunk_split(text)
    if not chunks:
        return
    clean_chunks = []
    for chunk in chunks:
        clean = strip_markdown_for_tts(chunk)
        if clean:
            clean_chunks.append(clean)
    if not clean_chunks:
        return
    for i, clean_chunk in enumerate(clean_chunks):
        _speech_queue.put((clean_chunk, i, True))
    # Wait for all chunks to finish (or be flushed)
    _speech_queue.join()
    if not tts_interrupted:
        emit("tts_chunk_done")


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

def create_text_input_token(key, value, question=None, thermal=None):
    """
    Create a persistent text input token.

    Schema validated by the token type registry. Only type-specific
    fields need to be passed here; defaults come from the registry.

    Args:
        key: Variable/parameter name
        value: Text response from user
        question: Optional question text that prompted this input
        thermal: Optional thermal override dict from INPUT tag

    Returns:
        token_id: Generated token identifier
    """
    fields = {"key": key}
    if question:
        fields["question"] = question

    # Merge human verification challenge fields if session is verified
    try:
        from flask import request as _freq
        fields.update(get_challenge_fields(_freq.sid))
    except Exception:
        pass

    if token_factory is not None:
        return token_factory.create(
            token_type="text_input",
            label=key,
            value=value,
            thermal=thermal,
            extra_fields=fields
        )

    # Legacy fallback if TokenFactory unavailable
    ensure_tokens_dir()
    now = datetime.now()

    if CUE_MEM_AVAILABLE:
        cue_mem_token = cue_mem_create_token(
            label=key, value=value, token_type="text_input",
            visibility="shared", base_temp=75, tags=["text_input"]
        )
        token_id = cue_mem_token["token_id"]
        token = {**cue_mem_token, **fields}
    else:
        expiry = now + timedelta(hours=SCALAR_PARAM_TOKEN_EXPIRY_HOURS)
        token_id = generate_scalar_token_id(key)
        token = {
            "token_id": token_id, "type": "text_input",
            "label": key, "value": value,
            "created_at": now.isoformat(), "expires_at": expiry.isoformat(),
            "status": "active", **fields
        }
        token_file = TOKENS_DIR / ("%s.json" % token_id)
        with open(token_file, "w") as f:
            json.dump(token, f, indent=2)

    input_history[token_id] = token
    ensure_log_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / ("%s.jsonl" % today)
    with open(log_file, "a") as f:
        f.write(json.dumps({"timestamp": now.isoformat(), "event": "token_created", "token": token}) + "\n")

    return token_id


def _extract_label_from_question(question_context):
    """Extract key words from question to generate a semantic label."""
    if not question_context:
        return "response"
    cleaned = re.sub(r"[^\w\s]", "", question_context.lower())
    words = cleaned.split()
    stop_words = {"should", "would", "could", "can", "do", "does", "is", "are",
                  "the", "a", "an", "to", "i", "you", "we", "this", "that"}
    key_words = [w for w in words if w not in stop_words]
    if key_words:
        return "_".join(key_words[:3])
    return "response"


def create_yes_no_token(answer, question_context=None, thermal=None):
    """
    Create a persistent YES/NO response token.

    Schema validated by the token type registry.

    Args:
        answer: "Yes" or "No"
        question_context: Optional question text that was asked
        thermal: Optional thermal override dict from INPUT tag

    Returns:
        token_id: Generated token identifier
    """
    label = _extract_label_from_question(question_context)
    fields = {"answer": answer}
    if question_context:
        fields["question"] = question_context

    # Merge human verification challenge fields if session is verified
    try:
        from flask import request as _freq
        fields.update(get_challenge_fields(_freq.sid))
    except Exception:
        pass

    if token_factory is not None:
        return token_factory.create(
            token_type="yes_no_response",
            label=label,
            value=answer,
            tags=[question_context or "no_context"],
            thermal=thermal,
            extra_fields=fields
        )

    # Legacy fallback if TokenFactory unavailable
    ensure_tokens_dir()
    now = datetime.now()

    if CUE_MEM_AVAILABLE:
        cue_mem_token = cue_mem_create_token(
            label=label, value=answer, token_type="yes_no_response",
            visibility="shared", base_temp=75,
            tags=["yes_no", question_context or "no_context"]
        )
        token_id = cue_mem_token["token_id"]
        token = {**cue_mem_token, **fields}
    else:
        expiry = now + timedelta(hours=SCALAR_PARAM_TOKEN_EXPIRY_HOURS)
        token_id = generate_scalar_token_id(label)
        token = {
            "token_id": token_id, "type": "yes_no_response",
            "label": label, "value": answer,
            "created_at": now.isoformat(), "expires_at": expiry.isoformat(),
            "status": "active", **fields
        }
        token_file = TOKENS_DIR / ("%s.json" % token_id)
        with open(token_file, "w") as f:
            json.dump(token, f, indent=2)

    input_history[token_id] = token
    ensure_log_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / ("%s.jsonl" % today)
    with open(log_file, "a") as f:
        f.write(json.dumps({"timestamp": now.isoformat(), "event": "token_created", "token": token}) + "\n")

    return token_id


def create_scalar_param_token(slider_value, semantic_label, hex_value, hsl_value, question=None, thermal=None):
    """
    Create a persistent scalar parameter token from slider input.

    Schema validated by the token type registry.

    Args:
        slider_value: 0-100 slider position
        semantic_label: Dimension name (e.g., 'urgency', 'confidence')
        hex_value: Hex-encoded coordinate string
        hsl_value: HSL breakdown dict {h, s, l}
        question: Optional question text that prompted this input
        thermal: Optional thermal override dict from INPUT tag

    Returns:
        token_id: Generated token identifier
    """
    natural_value = map_slider_to_semantic_value(slider_value, semantic_label)
    fields = {
        "semantic_label": semantic_label,
        "value_hex": hex_value,
        "value_decoded": hsl_value,
        "slider_value": slider_value,
        "natural_value": natural_value,
    }
    if question:
        fields["question"] = question

    # Merge human verification challenge fields if session is verified
    try:
        from flask import request as _freq
        fields.update(get_challenge_fields(_freq.sid))
    except Exception:
        pass

    if token_factory is not None:
        return token_factory.create(
            token_type="scalar_param",
            label=semantic_label,
            value=natural_value,
            tags=["slider:%s" % slider_value, "hex:%s" % hex_value],
            thermal=thermal,
            extra_fields=fields
        )

    # Legacy fallback if TokenFactory unavailable
    ensure_tokens_dir()
    now = datetime.now()

    if CUE_MEM_AVAILABLE:
        cue_mem_token = cue_mem_create_token(
            label=semantic_label, value=natural_value, token_type="scalar_param",
            visibility="shared", base_temp=75,
            tags=["slider:%s" % slider_value, "hex:%s" % hex_value]
        )
        token_id = cue_mem_token["token_id"]
        token = {
            **cue_mem_token,
            "semantic_label": semantic_label, "value_hex": hex_value,
            "value_decoded": hsl_value, "slider_value": slider_value,
            "natural_value": natural_value, "question": question or ""
        }
    else:
        expiry = now + timedelta(hours=SCALAR_PARAM_TOKEN_EXPIRY_HOURS)
        token_id = generate_scalar_token_id(semantic_label)
        token = {
            "token_id": token_id, "type": "scalar_param",
            "semantic_label": semantic_label, "value_hex": hex_value,
            "value_decoded": hsl_value, "slider_value": slider_value,
            "natural_value": natural_value, "question": question or "",
            "created_at": now.isoformat(), "expires_at": expiry.isoformat(),
            "status": "active"
        }
        token_file = TOKENS_DIR / ("%s.json" % token_id)
        with open(token_file, "w") as f:
            json.dump(token, f, indent=2)

    input_history[token_id] = token
    ensure_log_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / ("%s.jsonl" % today)
    with open(log_file, "a") as f:
        f.write(json.dumps({"timestamp": now.isoformat(), "event": "token_created", "token": token}) + "\n")

    return token_id


def maybe_create_structured_sum():
    """
    Create a structured_sum token aggregating all active structured data.

    Called after each structured token creation (yes_no, text_input,
    scalar_param) so the parameter landscape stays current.
    Safe to call when dependencies are missing - returns None silently.
    """
    if token_factory is None or create_structured_sum is None:
        return None
    if not CUE_MEM_AVAILABLE:
        return None
    try:
        return create_structured_sum(token_factory, cue_mem_list_tokens)
    except Exception as e:
        print("structured_sum creation skipped: %s" % e)
        return None


# Register modifier management handlers from cue-mem plugin (if available)
_hydrate_modifiers = None
if CUE_MEM_AVAILABLE:
    try:
        from modifier_handlers import register_modifier_handlers
        _token_dir = MAESTRO_ROOT / ".claude" / "tokens"
        _hydrate_modifiers = register_modifier_handlers(
            socketio, _token_dir, input_history, token_factory, maybe_create_structured_sum
        )
        print("Modifier management enabled")
    except ImportError as e:
        print("Modifier handlers not loaded: %s" % e)

# Pin gallery: create a slim gallery token (slug reference, no image array)
@socketio.on("pin_gallery")
def handle_pin_gallery(data):
    """Create a slim gallery token. Images live in vault.db, not the token."""
    try:
        gallery_id = data.get("gallery_id")
        gallery_slug = data.get("gallery_slug", "")
        title = data.get("title", "Gallery")
        image_count = data.get("image_count", len(data.get("images", [])))
        if not gallery_slug and not image_count:
            emit("error", {"message": "No gallery slug or images"})
            return

        label = "gallery_%s" % re.sub(r"[^a-z0-9_]", "", title.lower().replace(" ", "_"))

        if token_factory is not None:
            token_id = token_factory.create(
                token_type="gallery",
                label=label,
                value="%s (%d images)" % (title, image_count),
                tags=["gallery"],
                extra_fields={
                    "title": title,
                    "gallery_slug": gallery_slug,
                    "image_count": image_count,
                }
            )
        else:
            # Fallback: write token file directly
            ensure_tokens_dir()
            token_id = "ctx_gallery_%d" % int(time.time())
            token = {
                "token_id": token_id,
                "type": "gallery",
                "label": label,
                "value": "%s (%d images)" % (title, image_count),
                "title": title,
                "gallery_slug": gallery_slug,
                "image_count": image_count,
                "tags": ["gallery"],
                "created_at": datetime.now().isoformat(),
                "temperature": 75,
                "base_temp": 75,
                "half_life_hours": 168,
                "floor_temp": 5,
            }
            token_path = TOKENS_DIR / ("%s.json" % token_id)
            token_path.write_text(json.dumps(token, indent=2))
            input_history[token_id] = token

        emit("token_created", {
            "token_id": token_id,
            "type": "gallery",
            "label": label,
            "value": "%s (%d images)" % (title, image_count),
            "title": title,
            "gallery_slug": gallery_slug,
            "image_count": image_count,
            "gallery_id": gallery_id,
        })
        print("[GALLERY] Pinned gallery token: %s -> %s (%d images)" % (token_id, gallery_slug, image_count))
    except Exception as e:
        print("pin_gallery error: %s" % e)
        emit("error", {"message": str(e)})

# On-demand hydration: client can request re-hydration at any time
# (e.g. after buff-launch creates tokens externally via ?hydrate=1 URL param)
@socketio.on("emoji_reaction")
def handle_emoji_reaction(data):
    emoji = (data or {}).get("emoji", "")
    if not emoji:
        return
    if token_factory is not None:
        try:
            tags = ["track:%s" % _active_track] if _active_track else None
            token_factory.create(
                token_type="emoji_reaction",
                label="emoji_reaction",
                value=emoji,
                thermal={"base_temp": 20, "cooling_rate": 5.0},
                tags=tags,
            )
        except Exception as e:
            print("Emoji token creation failed: %s" % e)

@socketio.on("mute_message")
def handle_mute_message(data):
    ts = (data or {}).get("timestamp", "")
    text = (data or {}).get("text", "")
    muted = (data or {}).get("muted", True)
    preview = text[:80] if text else ""
    action = "muted" if muted else "unmuted"
    print("[mute] %s message at %s: %s" % (action, ts, preview))

    # If muting while Claude is still thinking, kill the process immediately
    if muted and _active_claude_process and _active_claude_process.poll() is None:
        print("[VOID] Mute triggered while Claude is processing -- aborting")
        abort_claude()

    # Log the mute event so the model can dull this context
    ensure_log_dir()
    log_file = LOG_DIR / ("%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "event": "message_muted" if muted else "message_unmuted",
        "message_timestamp": ts,
        "preview": preview,
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

    if muted and token_factory is not None:
        try:
            tags = ["mute"]
            if _active_track:
                tags.append("track:%s" % _active_track)
            token_factory.create(
                token_type="mute",
                label="mute_%s" % ts,
                value=preview,
                thermal={"base_temp": 20, "cooling_rate": 5.0},
                tags=tags,
            )
        except Exception as e:
            print("Mute token creation failed: %s" % e)



@socketio.on("request_hydration")
def handle_request_hydration(data=None):
    if _hydrate_modifiers:
        _hydrate_modifiers(MAESTRO_ROOT / ".claude" / "tokens")

# Reset session: clear conversation summary so fresh runs start clean
@socketio.on("reset_session")
def handle_reset_session(data=None):
    global in_memory_summary_tokens, _active_track
    in_memory_summary_tokens = []
    _active_track = None
    print("[SESSION] Conversation summary cleared")

# Pull history: serve last 20 conversation exchanges from today's log
@socketio.on("pull_history")
def handle_pull_history(data=None):
    entries = load_recent_logs(limit=200)
    # Filter to conversation-only entries (have user + assistant, no event key)
    convos = [e for e in entries if "user" in e and "assistant" in e and "event" not in e]
    # Take last 20
    convos = convos[-20:]
    # Slim payload
    result = []
    for entry in convos:
        result.append({
            "timestamp": entry.get("timestamp", ""),
            "user": entry.get("user", ""),
            "assistant": entry.get("assistant", "")
        })
    print("[PULL] Serving %d conversation entries" % len(result))
    emit("pull_history_result", {"entries": result})

    # Timing token for pull event
    now = datetime.now()
    emit("token_created", {
        "token_id": "timing_pull_%d" % int(now.timestamp()),
        "type": "timing",
        "label": "pull",
        "value": "%d entries" % len(result),
        "created_at": now.isoformat(),
        "temperature": 25,
        "base_temp": 25,
        "cooling_rate": 10.0,
    })

# Serve auto-prompt file written by maestro.sh
@socketio.on("request_prompt_file")
def handle_request_prompt_file(data=None):
    prompt_path = MAESTRO_ROOT / ".claude" / "run_prompt.txt"
    if prompt_path.exists():
        text = prompt_path.read_text().strip()
        prompt_path.unlink()  # one-shot: delete after reading
        print("[SESSION] Serving prompt file (%d chars)" % len(text))
        emit("prompt_file_ready", {"text": text})
    else:
        print("[SESSION] No prompt file found")
        emit("prompt_file_ready", {"text": ""})

# Human verification challenge system
# Session-level challenge: solve once per session, all tokens get signed
_challenge_sessions = {}  # sid -> {proof, confidence, challenge_id, verified_at}
_pending_challenges = {}  # sid -> challenge dict

try:
    import challenge as _challenge_mod
    import signing as _signing_mod

    @socketio.on("request_challenge")
    def handle_request_challenge(data=None):
        """Issue a human verification challenge for this session."""
        from flask import request as flask_request
        sid = flask_request.sid
        challenge_type = (data or {}).get("type", "thermal")
        ch = _challenge_mod.generate(challenge_type)
        _pending_challenges[sid] = ch
        from flask_socketio import emit
        emit("challenge_issued", {
            "challenge_id": ch["challenge_id"],
            "type": ch["type"],
            "prompt": ch["prompt"],
        })

    @socketio.on("verify_challenge")
    def handle_verify_challenge(data):
        """Verify challenge response, cache proof for session."""
        from flask import request as flask_request
        from flask_socketio import emit
        sid = flask_request.sid
        ch = _pending_challenges.pop(sid, None)
        if not ch:
            emit("challenge_result", {"valid": False, "reason": "no pending challenge"})
            return

        response = data.get("response")
        response_time_ms = data.get("response_time_ms", 9999)
        attention = data.get("attention")

        valid, confidence, proof = _challenge_mod.verify(
            ch, response, response_time_ms, attention=attention
        )

        if valid:
            _challenge_sessions[sid] = {
                "proof": proof,
                "confidence": confidence,
                "challenge_id": ch["challenge_id"],
                "verified_at": __import__("time").time(),
            }
            fingerprint = _signing_mod.get_public_key_fingerprint() or "none"
            emit("challenge_result", {
                "valid": True,
                "confidence": round(confidence, 3),
                "fingerprint": fingerprint,
            })
            print("CHALLENGE VERIFIED: sid=%s confidence=%.3f proof=%s" % (
                sid[:8], confidence, proof[:12]
            ))
        else:
            emit("challenge_result", {
                "valid": False,
                "reason": "incorrect answer",
            })

    print("Human verification challenges enabled")
except ImportError as e:
    print("Challenge system not available: %s" % e)
    _challenge_mod = None


def get_challenge_fields(sid=None):
    """Get cached challenge fields for the current session.

    Returns dict to merge into first-class token extra_fields,
    or empty dict if session not verified.
    """
    if not sid or sid not in _challenge_sessions:
        return {}
    session = _challenge_sessions[sid]
    return _challenge_mod.to_token_fields(
        valid=True,
        confidence=session["confidence"],
        proof=session["proof"],
        challenge_id=session["challenge_id"],
    )


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

def _get_current_run_floor():
    """
    Find the most recent cue-run session token and return its created_at.

    This establishes a time boundary: only input tokens created AFTER the
    most recent cue-sheet run started belong to the current session.

    Returns:
        str or None: ISO timestamp of the most recent cue-run token, or None
    """
    if not CUE_MEM_AVAILABLE:
        return None

    try:
        all_tokens = cue_mem_list_tokens()
        run_tokens = []
        for token in all_tokens:
            tags = token.get('tags', [])
            if isinstance(tags, list) and 'cue-run' in tags:
                created = token.get('created_at', '')
                if created:
                    run_tokens.append(created)
        if run_tokens:
            run_tokens.sort(reverse=True)
            return run_tokens[0]
    except Exception:
        pass

    return None


def get_active_scalar_tokens():
    """
    Get all active (non-expired) structured input tokens scoped to the
    current cue-sheet run.

    Session scoping: If a cue-run session token exists, only tokens created
    AFTER that session started are returned. This prevents stale inputs from
    previous runs from contaminating the current session.

    Includes: scalar_param (sliders), text_input, yes_no_response

    Returns:
        list: Active token objects with natural language values
    """
    active_tokens = []

    # Establish session boundary from the most recent cue-run token
    run_floor = _get_current_run_floor()

    if CUE_MEM_AVAILABLE:
        # Use CUE-MEM to get active tokens
        try:
            cue_mem_tokens = cue_mem_list_tokens()
            for token in cue_mem_tokens:
                token_type = token.get('type')
                # Include all structured input token types
                if token_type in ['scalar_param', 'text_input', 'yes_no_response'] and token.get('status') == 'active':
                    # Session scoping: skip tokens from before the current run
                    if run_floor and token.get('created_at', '') < run_floor:
                        continue
                    # Skip the run session token itself (it's type text_input but tagged cue-run)
                    tags = token.get('tags', [])
                    if isinstance(tags, list) and 'cue-run' in tags:
                        continue
                    active_tokens.append({
                        'label': token.get('label'),
                        'value': token.get('value'),
                        'created_at': token.get('created_at'),
                        'token_id': token.get('token_id'),
                        'temperature': token.get('temperature'),
                        'type': token_type
                    })
        except Exception as e:
            print(f"⚠️  Failed to read CUE-MEM tokens: {e}")
            # Fall through to local cache

    if not CUE_MEM_AVAILABLE or not active_tokens:
        # Fallback to local in-memory cache
        check_and_expire_tokens()  # Ensure expiry status is current

        for token_id, token_data in input_history.items():
            token_type = token_data.get('type')
            if token_type in ['scalar_param', 'text_input', 'yes_no_response'] and token_data.get('status') == 'active':
                # Session scoping: skip tokens from before the current run
                if run_floor and token_data.get('created_at', '') < run_floor:
                    continue

                # Different token types have different field names
                if token_type == 'scalar_param':
                    label = token_data.get('semantic_label')
                    value = token_data.get('natural_value')
                elif token_type == 'text_input':
                    label = token_data.get('key')
                    value = token_data.get('value')
                elif token_type == 'yes_no_response':
                    label = token_data.get('label')
                    value = token_data.get('answer')
                else:
                    continue

                active_tokens.append({
                    'label': label,
                    'value': value,
                    'created_at': token_data.get('created_at'),
                    'token_id': token_id,
                    'type': token_type
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

def compute_relative_time_from_now(timestamp_str):
    """Compute human-readable relative time from a stored timestamp to now.

    This computes FRESH relative time at READ time from the absolute timestamp.
    Prevents stale relative values from being baked into logs and re-injected
    into LLM context windows hours later.
    """
    if not timestamp_str:
        return ""
    try:
        ts = datetime.fromisoformat(str(timestamp_str))
        total_seconds = (datetime.now() - ts).total_seconds()
        if total_seconds < 0:
            return "just now"
        minutes = int(total_seconds // 60)
        hours = int(total_seconds // 3600)
        if hours == 0:
            return "%dm ago" % minutes
        elif hours < 24:
            return "%dh %dm ago" % (hours, minutes % 60)
        else:
            days = int(hours // 24)
            return "%dd ago" % days
    except (ValueError, TypeError):
        return ""

def log_conversation(user_text, assistant_text, speech_metadata=None, input_length=None, confidence=None):
    ensure_log_dir()
    timestamp = datetime.now()
    log_file = LOG_DIR / f"{timestamp.strftime('%Y-%m-%d')}.jsonl"

    # Extract SNR and citations from assistant response, then drop any
    # system-reminder blocks so they never reach the chat render band or log.
    text_after_snr, snr_value = extract_snr(assistant_text)
    text_after_citations, citations_data = extract_citations(text_after_snr)
    clean_text = strip_system_reminders(text_after_citations)

    entry = {
        'timestamp': timestamp.strftime('%Y-%m-%dT%H:%M'),  # No seconds
        't_period': get_time_period(timestamp),
        'user': user_text,
        'assistant': clean_text
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

    # Resolve citations and add to log entry
    if citations_data and citations_data.get("refs"):
        refs = citations_data["refs"]
        if _citation_resolver:
            resolved = _citation_resolver.resolve(refs)
            entry['citations'] = resolved
            # Audit log citation resolution
            passed = sum(1 for r in resolved if r.get("resolved"))
            failed = len(resolved) - passed
            if _audit_logger:
                _audit_logger.log("citation:resolved", details={
                    "passed": passed,
                    "failed": failed,
                    "refs": resolved,
                })
        else:
            # No resolver available -- store raw refs unresolved
            entry['citations'] = refs

    # Record SNR self-assessment in log and create token
    if snr_value is not None:
        snr_h = snr_value * 1.2  # 0-120 hue: red->yellow->green
        snr_hex = hsl_to_hex(snr_h, 70, 50)
        entry["snr"] = {"value": snr_value, "hex": snr_hex}

        if token_factory is not None:
            try:
                snr_tags = None
                if _active_track:
                    snr_tags = ["track:%s" % _active_track]
                token_factory.create(
                    token_type="model_signal",
                    label="snr_%d" % int(timestamp.timestamp()),
                    value=str(snr_value),
                    tags=snr_tags,
                    extra_fields={"snr": snr_value, "hex": snr_hex},
                )
            except Exception as e:
                print("SNR token creation failed: %s" % e)

    with open(log_file, 'a') as f:
        f.write(json.dumps(entry) + '\n')

    # Event-driven multi-scale token generation with retrospective time-awareness
    # Checks all scales (fine/medium/coarse) and creates tokens for any that are due
    create_multiscale_summary_tokens()

    # Create echo token - meta-token that summarizes the constellation of active tokens
    # Creates recursive awareness where tokens become aware of tokens around them
    create_token_echo()

    # Emit exchange timing token to keep the stream alive
    try:
        emit("token_created", {
            "token_id": "timing_exchange_%d" % int(timestamp.timestamp()),
            "type": "timing",
            "label": "exchange",
            "value": timestamp.strftime("%H:%M"),
            "created_at": timestamp.isoformat(),
            "temperature": 30,
            "base_temp": 30,
            "cooling_rate": 8.0,
        })
    except Exception:
        pass  # Outside socket context (e.g. CLI usage)

    snr_hex_out = entry.get("snr", {}).get("hex") if snr_value is not None else None
    return (log_file, clean_text, snr_hex_out)

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
    """Load recent log entries from today's log file.

    Sanitizes user/assistant fields on read so historical entries that
    pre-date strip_system_reminders cannot leak <system-reminder> blocks
    back into context injection, summary compression, or recent_context.md.
    """
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
                entry = json.loads(line)
                if 'user' in entry:
                    entry['user'] = strip_system_reminders(entry['user'])
                if 'assistant' in entry:
                    entry['assistant'] = strip_system_reminders(entry['assistant'])
                entries.append(entry)
            except json.JSONDecodeError:
                continue

    return entries


def format_logs_with_time(entries):
    """Format log entries with temporal tags"""
    if not entries:
        return "No recent activity"

    formatted = []
    for entry in entries:
        t_rel = compute_relative_time_from_now(entry.get('timestamp', ''))
        t_per = entry.get('t_period', '')
        user = entry.get('user', '')
        assistant = entry.get('assistant', '')

        formatted.append(f"[{t_rel} - {t_per}]")
        formatted.append(f"User: {user}")
        formatted.append(f"Assistant: {assistant}")
        formatted.append("")

    return "\n".join(formatted)


POSITIVE_EMOJI = {
    "\U0001f44d", "\u2764\ufe0f", "\u2764", "\U0001f525", "\U0001f602",
    "\U0001f60d", "\U0001f64f", "\U0001f389", "\U0001f680", "\U0001f4af",
    "\U0001f31f", "\U0001f44f",
}
NEUTRAL_EMOJI = {"\U0001f914", "\U0001f440", "\U0001f4ad"}

def _compute_vibe_metrics():
    """Scan active emoji_reaction tokens and return structured vibe metrics.

    Returns dict with total, density, diversity, mood, polarity_shift, counts
    or None when no active emoji tokens exist.
    """
    tokens = []
    if CUE_MEM_AVAILABLE:
        try:
            all_tokens = cue_mem_list_tokens()
            tokens = [
                t for t in all_tokens
                if t.get("type") == "emoji_reaction" and t.get("status") == "active"
            ]
        except Exception:
            pass

    if not tokens:
        return None

    # Count by emoji character
    counts = {}
    for t in tokens:
        emoji = t.get("value", "")
        if emoji:
            counts[emoji] = counts.get(emoji, 0) + 1

    if not counts:
        return None

    total = sum(counts.values())
    unique = len(counts)

    # Density: reactions per recent exchange
    recent_logs = load_recent_logs(limit=20)
    exchange_count = max(len(recent_logs), 1)
    density = total / exchange_count

    # Diversity: unique emoji / total (0-1, Shannon-like info density)
    diversity = unique / total if total > 0 else 0

    # Mood classification
    positive = sum(v for k, v in counts.items() if k in POSITIVE_EMOJI)
    neutral = sum(v for k, v in counts.items() if k in NEUTRAL_EMOJI)

    if positive > neutral:
        mood = "positive"
    elif neutral > positive:
        mood = "neutral"
    else:
        mood = "mixed"

    # Polarity shift: compare first-half vs second-half sentiment
    polarity_shift = False
    if len(tokens) >= 4:
        mid = len(tokens) // 2
        first_half = tokens[:mid]
        second_half = tokens[mid:]
        first_pos = sum(1 for t in first_half if t.get("value", "") in POSITIVE_EMOJI)
        second_pos = sum(1 for t in second_half if t.get("value", "") in POSITIVE_EMOJI)
        first_ratio = first_pos / max(len(first_half), 1)
        second_ratio = second_pos / max(len(second_half), 1)
        if abs(first_ratio - second_ratio) > 0.3:
            polarity_shift = True

    return {
        "total": total,
        "density": round(density, 2),
        "diversity": round(diversity, 2),
        "mood": mood,
        "polarity_shift": polarity_shift,
        "counts": counts,
    }


def _build_vibe_line():
    """Thin wrapper: format vibe metrics as a one-line summary.

    Returns empty string when no active emoji tokens exist.
    """
    metrics = _compute_vibe_metrics()
    if not metrics:
        return ""

    parts = [
        "%dx %s" % (v, k)
        for k, v in sorted(metrics["counts"].items(), key=lambda x: -x[1])
    ]
    return "Vibe: %s (%d reactions, %s)" % (
        ", ".join(parts), metrics["total"], metrics["mood"]
    )


# ---------------------------------------------------------------------------
# Engagement quadrant system
# ---------------------------------------------------------------------------

SNR_HIGH_THRESHOLD = 50
EMOJI_DENSITY_HIGH_THRESHOLD = 0.3

QUADRANT_LOCKED_IN = "LOCKED_IN"
QUADRANT_FATIGUING = "FATIGUING"
QUADRANT_REFRAME = "REFRAME"
QUADRANT_DRIFTING = "DRIFTING"

QUADRANT_DESCRIPTIONS = {
    QUADRANT_LOCKED_IN: "Stay the course. User is engaged and signal is clear.",
    QUADRANT_FATIGUING: "Productive but draining. Keep it concise, offer breaks.",
    QUADRANT_REFRAME: "Engaged but noisy. Crystallize the thread, reduce ambiguity.",
    QUADRANT_DRIFTING: "Both sides unfocused. You may initiate a pivot or probe.",
}


def _compute_engagement_quadrant():
    """Cross SNR x Emoji Vibe into four behavioral quadrants.

    Returns dict with quadrant, snr_avg, vibe_metrics, description
    or None if insufficient data for either axis.
    """
    # Gather recent SNR tokens (last 5)
    snr_values = []
    if CUE_MEM_AVAILABLE:
        try:
            all_tokens = cue_mem_list_tokens()
            snr_tokens = [
                t for t in all_tokens
                if t.get("type") == "model_signal" and t.get("status") == "active"
            ]
            # Sort by label (contains timestamp) descending, take last 5
            snr_tokens.sort(key=lambda t: t.get("label", ""), reverse=True)
            for t in snr_tokens[:5]:
                try:
                    snr_values.append(int(t.get("value", 0)))
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    # Get vibe metrics
    vibe_metrics = _compute_vibe_metrics()

    has_snr = len(snr_values) > 0
    has_vibe = vibe_metrics is not None

    if not has_snr and not has_vibe:
        return None

    snr_avg = sum(snr_values) / len(snr_values) if has_snr else None
    snr_high = snr_avg >= SNR_HIGH_THRESHOLD if snr_avg is not None else None
    vibe_high = vibe_metrics["density"] >= EMOJI_DENSITY_HIGH_THRESHOLD if has_vibe else None

    # Determine quadrant
    if snr_high is not None and vibe_high is not None:
        # Full bilateral: both axes available
        if snr_high and vibe_high:
            quadrant = QUADRANT_LOCKED_IN
        elif snr_high and not vibe_high:
            quadrant = QUADRANT_FATIGUING
        elif not snr_high and vibe_high:
            quadrant = QUADRANT_REFRAME
        else:
            quadrant = QUADRANT_DRIFTING
    elif snr_high is not None:
        # SNR only: simplified binary
        quadrant = QUADRANT_LOCKED_IN if snr_high else QUADRANT_DRIFTING
    else:
        # Vibe only: simplified binary
        quadrant = QUADRANT_LOCKED_IN if vibe_high else QUADRANT_DRIFTING

    result = {
        "quadrant": quadrant,
        "snr_avg": round(snr_avg, 1) if snr_avg is not None else None,
        "vibe_metrics": vibe_metrics,
        "description": QUADRANT_DESCRIPTIONS[quadrant],
    }
    return result


def get_engagement_context():
    """Build compact engagement quadrant context for prompt injection.

    Returns ~200 char string or empty string when no data.
    """
    state = _compute_engagement_quadrant()
    if not state:
        return ""

    lines = ["[ENGAGEMENT QUADRANT]"]
    lines.append("State: %s" % state["quadrant"])

    parts = []
    if state["snr_avg"] is not None:
        parts.append("SNR: %d (avg last 5)" % state["snr_avg"])
    vm = state["vibe_metrics"]
    if vm:
        parts.append("Vibe: %.1f density, %.1f diversity, %s" % (
            vm["density"], vm["diversity"], vm["mood"]
        ))
        if vm["polarity_shift"]:
            parts.append("polarity shifting")
    if parts:
        lines.append(" | ".join(parts))

    lines.append("Leeway: %s" % state["description"])
    return "\n".join(lines)


def compress_conversation_chunk(entries, compression_level='light'):
    """
    Compress conversation entries into a concise summary.
    Compression level determines how "baked" the summary is.

    Args:
        entries: List of conversation log entries
        compression_level: 'light' (recent), 'medium' (aging), 'heavy' (old/baked)

    Returns:
        Compressed text suitable for thermal token storage.
    """
    if not entries:
        return None

    compressed_lines = []

    if compression_level == 'heavy':
        # Heavy "baked" compression for old memories
        # Extract only key themes, ultra-condensed
        topics = set()
        for entry in entries:
            user = entry.get('user', '')[:40]
            if len(user) > 5:  # Skip very short inputs
                topics.add(user)

        return "Topics: " + "; ".join(list(topics)[:3])

    elif compression_level == 'medium':
        # Medium compression - key exchanges only
        for entry in entries:
            user = entry.get('user', '')[:50]
            assistant = entry.get('assistant', '')[:60]
            compressed_lines.append(f"U: {user} | A: {assistant}")

        return "\n".join(compressed_lines)

    else:  # 'light' - recent, less baked
        # Light compression - preserve more detail
        for entry in entries:
            t_rel = compute_relative_time_from_now(entry.get('timestamp', ''))
            user = entry.get('user', '')[:80]
            assistant = entry.get('assistant', '')[:100]
            compressed_lines.append(f"{t_rel}: U: {user} | A: {assistant}")

        return "\n".join(compressed_lines)


def create_multiscale_summary_tokens():
    """
    Event-driven multi-scale token generation with retrospective time-awareness.

    Checks all temporal scales (fine/medium/coarse) and creates tokens for any
    that are due based on elapsed time since their last creation.

    Called after each conversation exchange - piggybacks on the submit/respond cycle.
    No background threads needed - time is checked retrospectively.

    Returns list of created token IDs.
    """
    global in_memory_summary_tokens

    now = datetime.now()
    created_tokens = []

    # Check each scale retrospectively
    for scale_name, config in SUMMARY_SCALES.items():
        # Check if this scale is due
        last_created = config['last_created']
        interval = config['interval']

        if last_created is None or (now - last_created).total_seconds() >= interval:
            # This scale is due - create token
            token_id = create_scale_token(scale_name, config, now)
            if token_id:
                created_tokens.append((scale_name, token_id))
                # Update last_created timestamp
                config['last_created'] = now

    return created_tokens


def create_scale_token(scale_name, config, now):
    """
    Create a single thermal token for a specific temporal scale.

    Args:
        scale_name: 'fine', 'medium', or 'coarse'
        config: Scale configuration dict
        now: Current datetime

    Returns token_id if created, None otherwise.
    """
    global in_memory_summary_tokens

    # Load exchanges for this scale's window
    recent_logs = load_recent_logs(limit=config['window'])

    if not recent_logs or len(recent_logs) < 2:
        return None  # Not enough activity

    # Compress with this scale's compression level
    summary_text = compress_conversation_chunk(recent_logs, compression_level=config['compression'])

    if not summary_text:
        return None

    # Append vibe line for fine and medium scales (not heavy -- those are theme-only)
    if scale_name in ("fine", "medium"):
        vibe = _build_vibe_line()
        if vibe:
            summary_text = summary_text + "\n" + vibe

    # Use this scale's base temperature
    temperature = config['base_temp']

    # Snapshot the active lane onto every summary so multi-lane filtering
    # works downstream. Falls back to the in-process _active_track if the
    # persistent state is unset (e.g. on first launch).
    summary_lane = None
    try:
        from track_registry import get_active_lane  # type: ignore
        summary_lane = get_active_lane()
    except ImportError:
        pass
    if not summary_lane and _active_track:
        summary_lane = _active_track

    summary_tags = ['rolling_summary', 'context', scale_name]
    if summary_lane:
        summary_tags.append(f"track:{summary_lane}")
        summary_tags.append(f"lane:{summary_lane}")

    # Create CUE-MEM token if available
    #
    # Conversation-summary tokens are same-tier persistence (Opus → next
    # Opus session) and intentionally retained as prose so recent_context
    # .md stays semantically readable. Walkie-talkie wiring lives at the
    # cross-tier boundaries (pull-agent sanitize inbound, outbound
    # dispatch egress) -- not here. See ninja_walkietalkie_boundaries_only
    # pin (2026-05-07).
    if CUE_MEM_AVAILABLE:
        try:
            token_id = cue_mem_create_token(
                label=f"conv_{scale_name}_{int(now.timestamp())}",
                value=summary_text,
                base_temp=temperature,
                token_type='conversation_summary',
                visibility='local',
                tags=summary_tags,
                metadata={
                    'scale': scale_name,
                    'window': config['window'],
                    'exchanges': len(recent_logs),
                    'created_at': now.isoformat(),
                    'lane': summary_lane,
                }
            )

            print(f"✓ Created {scale_name} scale token: {token_id} (temp={temperature}°, {len(recent_logs)} exchanges)")

            # Re-reference accrual: bump weight on prior summaries whose
            # content overlaps the new one. The "naturally weighs in
            # more" mechanic. Quiet failure -- accrual is best-effort.
            try:
                from reference_accrual import compute_bumps, apply_bumps  # type: ignore
                from tokens import list_tokens  # type: ignore
                priors = [t for t in list_tokens(include_frozen=False)
                          if t.get("token_id") != token_id
                          and "rolling_summary" in t.get("tags", [])]
                priors = priors[:30]  # bound the lookback
                bumps = compute_bumps(summary_text, priors,
                                       similarity_threshold=0.15,
                                       factor=1.15, max_weight=10.0)
                if bumps:
                    apply_bumps(bumps, str(TOKENS_DIR),
                                logger=lambda m: print(m))
            except Exception as accrual_err:
                print(f"  (accrual skipped: {accrual_err})")

            return token_id

        except Exception as e:
            print(f"⚠️  Failed to create {scale_name} scale token: {e}")
            return None

    # Fallback: Use in-memory storage
    token_id = f"conv_{scale_name}_{int(now.timestamp())}"
    in_memory_summary_tokens.append({
        'token_id': token_id,
        'label': token_id,
        'value': summary_text,
        'temperature': temperature,
        'type': 'conversation_summary',
        'status': 'active',
        'scale': scale_name,
        'created_at': now.isoformat(),
        'tags': summary_tags,
        'metadata': {
            'scale': scale_name,
            'window': config['window'],
            'exchanges': len(recent_logs),
            'lane': summary_lane,
        }
    })

    print(f"✓ Created in-memory {scale_name} scale token: {token_id} (temp={temperature}°, {len(recent_logs)} exchanges)")
    return token_id


def create_token_echo():
    """
    Create an echo token - a meta-token that summarizes the constellation of active summary tokens.

    This creates recursive awareness where tokens become aware of tokens around them.
    With thermal decay, echoes preserve the essence of cooling/fading tokens.

    Called after multi-scale token generation (piggyback on conversation cycle).
    Returns echo_token_id if created, None otherwise.
    """
    global last_echo_timestamp, in_memory_summary_tokens

    now = datetime.now()

    # Check if enough time has elapsed for echo
    if last_echo_timestamp:
        elapsed = (now - last_echo_timestamp).total_seconds()
        if elapsed < ECHO_INTERVAL_SECONDS:
            return None  # Too soon for echo

    # Get all active conversation summary tokens
    active_tokens = []
    summarized_token_ids = []

    if CUE_MEM_AVAILABLE:
        try:
            all_tokens = cue_mem_list_tokens()
            active_tokens = [
                t for t in all_tokens
                if t.get('type') == 'conversation_summary' and t.get('status') == 'active'
            ]
            summarized_token_ids = [t.get('token_id') or t.get('label') for t in active_tokens]
        except Exception as e:
            print(f"⚠️  Failed to list tokens for echo: {e}")
            return None
    else:
        # Use in-memory storage
        active_tokens = [
            t for t in in_memory_summary_tokens
            if t.get('type') == 'conversation_summary' and t.get('status') == 'active'
        ]
        summarized_token_ids = [t.get('token_id') for t in active_tokens]

    if len(active_tokens) < 2:
        return None  # Need at least 2 tokens to create meaningful echo

    # Create meta-summary of the token constellation
    echo_lines = []
    for token in sorted(active_tokens, key=lambda t: t.get('temperature', 0), reverse=True):
        scale = token.get('scale', token.get('metadata', {}).get('scale', 'unknown'))
        temp = token.get('temperature', 0)
        value = token.get('value', '')[:100]  # Truncate for meta-summary
        echo_lines.append(f"[{scale}@{temp}°]: {value}")

    echo_text = "Token Constellation:\n" + "\n".join(echo_lines)

    # Create echo token
    if CUE_MEM_AVAILABLE:
        try:
            echo_id = cue_mem_create_token(
                label=f"echo_{int(now.timestamp())}",
                value=echo_text,
                base_temp=ECHO_BASE_TEMP,
                token_type='token_echo',
                visibility='local',
                tags=['echo', 'meta_awareness'],
                metadata={
                    'echo_of': summarized_token_ids,
                    'token_count': len(active_tokens),
                    'created_at': now.isoformat()
                }
            )

            last_echo_timestamp = now
            print(f"✓ Created token echo: {echo_id} (temp={ECHO_BASE_TEMP}°, {len(active_tokens)} tokens)")
            return echo_id

        except Exception as e:
            print(f"⚠️  Failed to create token echo: {e}")
            return None

    # Fallback: in-memory storage
    echo_id = f"echo_{int(now.timestamp())}"
    in_memory_summary_tokens.append({
        'token_id': echo_id,
        'label': echo_id,
        'value': echo_text,
        'temperature': ECHO_BASE_TEMP,
        'type': 'token_echo',
        'status': 'active',
        'created_at': now.isoformat(),
        'metadata': {
            'echo_of': summarized_token_ids,
            'token_count': len(active_tokens)
        }
    })

    last_echo_timestamp = now
    print(f"✓ Created in-memory token echo: {echo_id} (temp={ECHO_BASE_TEMP}°, {len(active_tokens)} tokens)")
    return echo_id


def get_conversation_summary_context():
    """
    Get formatted context from active conversation summary tokens.
    Displays summaries with thermal-based baking levels:
    - Hot (>85°): Fresh, detailed
    - Warm (60-85°): Medium compression
    - Cool (<60°): Baked, highly distilled

    Returns string to inject into Claude prompt.
    """
    # Use in-memory storage if CUE-MEM unavailable
    if not CUE_MEM_AVAILABLE:
        if not in_memory_summary_tokens:
            return ""

        # Filter conversation summary tokens and echo tokens separately
        summary_tokens = [
            t for t in in_memory_summary_tokens
            if t.get('type') == 'conversation_summary' and t.get('status') == 'active'
        ]
        echo_tokens = [
            t for t in in_memory_summary_tokens
            if t.get('type') == 'token_echo' and t.get('status') == 'active'
        ]

        if not summary_tokens and not echo_tokens:
            return ""

        context_lines = ["[CONVERSATION SUMMARY - Multi-Scale Temporal Pyramid with Echo Trail]"]
        context_lines.append("")

        # Sort summaries by temperature (hottest first)
        summary_tokens.sort(key=lambda t: t.get('temperature', 0), reverse=True)

        # Display conversation summaries
        if summary_tokens:
            context_lines.append("## Conversation Summaries:")
            for token in summary_tokens[:10]:
                temp = token.get('temperature', 0)
                value = token.get('value', '')
                scale = token.get('scale', token.get('metadata', {}).get('scale', 'unknown'))

                if temp > 85:
                    baking = "fresh"
                elif temp > 60:
                    baking = "settling"
                else:
                    baking = "baked"

                context_lines.append(f"[{scale}] ({baking} | {temp}°) {value}")
                context_lines.append("")

        # Display echo tokens (meta-awareness layer)
        if echo_tokens:
            echo_tokens.sort(key=lambda t: t.get('temperature', 0), reverse=True)
            context_lines.append("## Token Echoes (Meta-Awareness):")
            for echo in echo_tokens[:3]:  # Limit to 3 most recent echoes
                temp = echo.get('temperature', 0)
                value = echo.get('value', '')
                token_count = echo.get('metadata', {}).get('token_count', 0)

                context_lines.append(f"[echo | {temp}° | {token_count} tokens] {value}")
                context_lines.append("")

        return "\n".join(context_lines) + "\n"

    try:
        # Get all active tokens from CUE-MEM
        all_tokens = cue_mem_list_tokens()

        # Filter conversation summary tokens and echo tokens separately
        summary_tokens = [
            t for t in all_tokens
            if t.get('type') == 'conversation_summary' and t.get('status') == 'active'
        ]
        echo_tokens = [
            t for t in all_tokens
            if t.get('type') == 'token_echo' and t.get('status') == 'active'
        ]

        if not summary_tokens and not echo_tokens:
            return ""

        context_lines = ["[CONVERSATION SUMMARY - Multi-Scale Temporal Pyramid with Echo Trail]"]
        context_lines.append("")

        # Display conversation summaries
        if summary_tokens:
            # Sort by temperature (hottest first = most recent)
            summary_tokens.sort(key=lambda t: t.get('temperature', 0), reverse=True)

            context_lines.append("## Conversation Summaries:")
            for token in summary_tokens[:10]:  # Limit to 10 most recent across all scales
                temp = token.get('temperature', 0)
                value = token.get('value', '')
                scale = token.get('scale', token.get('metadata', {}).get('scale', 'unknown'))

                # Determine baking level based on thermal temperature
                if temp > 85:
                    baking = "fresh"
                elif temp > 60:
                    baking = "settling"
                else:
                    baking = "baked"

                context_lines.append(f"[{scale}] ({baking} | {temp}°) {value}")
                context_lines.append("")

        # Display echo tokens (meta-awareness layer)
        if echo_tokens:
            echo_tokens.sort(key=lambda t: t.get('temperature', 0), reverse=True)
            context_lines.append("## Token Echoes (Meta-Awareness):")
            for echo in echo_tokens[:3]:  # Limit to 3 most recent echoes
                temp = echo.get('temperature', 0)
                value = echo.get('value', '')
                token_count = echo.get('metadata', {}).get('token_count', 0)

                context_lines.append(f"[echo | {temp}° | {token_count} tokens] {value}")
                context_lines.append("")

        return "\n".join(context_lines) + "\n"

    except Exception as e:
        print(f"⚠️  Failed to load summary context: {e}")
        return ""


def assemble_prompt_with_budget(context_sections, user_text, max_chars=80000):
    """
    Assemble prompt from context sections + user input, enforcing a character budget.

    Trims lowest-priority sections first (summary, flux, input_history) if the
    total exceeds max_chars. This prevents 'Prompt is too long' errors from Claude.

    Args:
        context_sections: list of (name, content) tuples
        user_text: the user input string
        max_chars: max total prompt characters (~80K chars ~ 20K tokens)

    Returns:
        assembled prompt string
    """
    user_block = "\n\n[USER INPUT]\n%s" % user_text
    essential_size = len(user_block)
    total_context = sum(len(s) for _, s in context_sections)

    if total_context + essential_size > max_chars:
        budget_remaining = max_chars - essential_size
        section_dict = {name: content for name, content in context_sections}

        # Trim largest/least-critical sections first
        trim_order = ["summary", "flux", "input_history"]
        for trim_key in trim_order:
            if total_context <= budget_remaining:
                break
            section_size = len(section_dict.get(trim_key, ""))
            if section_size == 0:
                continue

            if total_context - (section_size // 2) <= budget_remaining:
                trimmed = section_dict[trim_key][:section_size // 2]
                last_nl = trimmed.rfind("\n")
                if last_nl > 0:
                    trimmed = trimmed[:last_nl]
                trimmed += "\n[... context trimmed for prompt budget ...]\n"
                total_context -= (section_size - len(trimmed))
                section_dict[trim_key] = trimmed
            else:
                total_context -= section_size
                section_dict[trim_key] = "[%s context omitted - prompt budget]\n" % trim_key

            print("[PROMPT BUDGET] Trimmed '%s' (%d chars saved)" % (trim_key, section_size - len(section_dict[trim_key])))

        context_sections = [(name, section_dict.get(name, "")) for name, _ in context_sections]

    return "%s%s" % ("".join(s for _, s in context_sections), user_block)


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


def get_instance_identity():
    """
    Return instance identity header for cue-vox Claude.

    This ensures cue-vox Claude always knows its role in the maestro ecosystem.
    """
    return """[INSTANCE IDENTITY -- AUTHORITATIVE]
**You ARE cue-vox Claude (Voice Interface). This is not negotiable.**

You are NOT ninja Claude. You are NOT the terminal instance. You are the VOICE
interface. If CLAUDE.md describes multiple instance roles, YOUR role is cue-vox.
The [INSTANCE IDENTITY] block in this prompt is the definitive source of truth.

**Your Role: Writer - Active token contributor**

**Your Capabilities:**
- Conversational interface with voice I/O
- Automatically create conversation summary tokens
- Write tokens to .claude/tokens/ with thermal metadata
- Create scalar parameter tokens from slider inputs
- Query hot tokens for context via flux-capacitor
- Execute cue-sheet runs (gather inputs, assemble outputs, retry)

**Your Boundaries (defer to ninja Claude for REPO OPERATIONS ONLY):**
- Repository management (git submodule, commits, branches, .gitmodules)
- File system restructuring
- Multi-step debugging requiring multiple approvals
- Build system operations

**When to defer (ONLY for repo/build operations):**
"That's a repository operation - ninja Claude handles those better. Run: claude-code"

**NEVER defer cue-sheet execution, input gathering, story generation, or slider interactions.**

---

"""


def get_flux_capacitor_context():
    """
    Get context from flux-capacitor generated memory summary.

    flux-capacitor watches .claude/tokens/ and auto-generates
    .claude/memory/recent_context.md with hot token summaries.

    This provides the same context Claude Code direct sessions see.
    """
    recent_context_file = MAESTRO_ROOT / '.claude' / 'memory' / 'recent_context.md'

    if not recent_context_file.exists():
        return ""

    try:
        with open(recent_context_file, 'r') as f:
            content = f.read()

        if not content.strip():
            return ""

        return f"""[FLUX CAPACITOR CONTEXT - Auto-synced maestro memory]
{content}

"""
    except Exception as e:
        print(f"⚠️  Failed to read flux-capacitor context: {e}")
        return ""


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


def get_image_context():
    """Read active image tokens, grouped by hash, for direct prompt injection.

    Each dropped image produces 3 tokens (drop, visual, context) sharing a hash.
    This groups them so Claude sees "Image 1: ..." not 3 separate entries.
    """
    tokens_dir = MAESTRO_ROOT / ".claude" / "tokens"
    if not tokens_dir.is_dir():
        return ""

    import glob as _glob
    import re as _re

    # Collect tokens grouped by hash
    by_hash = {}  # hash -> {drop: ..., visual: ..., context: ...}

    for pattern in ["ctx_image_drop_*.json", "ctx_image_visual_*.json", "ctx_image_context_*.json"]:
        for path in _glob.glob(str(tokens_dir / pattern)):
            try:
                with open(path) as f:
                    token = json.load(f)
                if token.get("status") != "active":
                    continue
                temp = token.get("temperature", 0)
                if temp <= 0:
                    continue

                label = token.get("label", "")
                value = token.get("value", "")

                # Extract hash from label: image_visual_256180a0 -> 256180a0
                match = _re.search(r"image_(?:drop|visual|context)_([a-f0-9]+)", label)
                if not match:
                    continue
                h = match.group(1)

                if h not in by_hash:
                    by_hash[h] = {}

                if "image_drop" in label:
                    by_hash[h]["drop"] = value
                elif "image_visual" in label:
                    by_hash[h]["visual"] = value
                elif "image_context" in label:
                    by_hash[h]["context"] = value
            except Exception:
                continue

    if not by_hash:
        return ""

    # Build grouped output
    lines = []
    count = len(by_hash)
    lines.append("[DROPPED IMAGES: %d image%s in context]" % (count, "s" if count != 1 else ""))

    # Parse structured fields from each visual token
    gallery_images = []
    for i, (h, parts) in enumerate(list(by_hash.items())[:10]):
        lines.append("")
        lines.append("--- Image %d (hash: %s) ---" % (i + 1, h))
        if parts.get("visual"):
            lines.append(parts["visual"])
        if parts.get("context"):
            ctx = parts["context"]
            if "context: " in ctx:
                ctx = ctx.split("context: ", 1)[-1]
            lines.append("interpretation: %s" % ctx)

        # Extract filename and caption from visual token for gallery block
        visual = parts.get("visual", "")
        filename = ""
        caption = ""
        for vline in visual.split("\n"):
            if vline.startswith("file: "):
                filename = vline[6:].strip()
            elif vline.startswith("caption: "):
                caption = vline[9:].strip().strip('"')
            elif vline.startswith("path: "):
                # Extract hash-based filename from path
                path_val = vline[6:].strip()
                if "/drops/" in path_val:
                    filename = path_val.split("/drops/")[-1]

        if filename:
            gallery_images.append({
                "slug": "_drops",
                "filename": filename,
                "port": "hot",
                "type": "image",
                "caption": caption,
            })

    # Include a ready-to-use GALLERY block
    if gallery_images:
        lines.append("")
        lines.append("READY-TO-USE GALLERY (copy this exactly when user asks to see the images):")
        gallery_json = json.dumps({
            "title": "Dropped Images",
            "images": gallery_images,
        })
        lines.append("[GALLERY: %s]" % gallery_json)

    lines.append("")
    lines.append("[/DROPPED IMAGES]")
    return "\n".join(lines)


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


@app.after_request
def _no_cache_static(response):
    """Prevent browser caching of static assets during development."""
    if "/static/" in request.path:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route('/')
def index():
    print("📄 Serving index.html")
    import time as _time
    return render_template('index.html', cache_bust=int(_time.time()))


# Vault image resolver -- searches all vault HOT directories
_VAULT_SEARCH_PATHS = [
    MAESTRO_ROOT / "vault-hot" / "HOT",
    MAESTRO_ROOT / "vault-cold" / "HOT",
]
# Add chip vault HOT dirs
for _chip_dir in sorted(MAESTRO_ROOT.glob("chip-*")):
    for _vault_dir in sorted(_chip_dir.glob("vault-*")):
        _hot = _vault_dir / "HOT"
        if _hot.is_dir():
            _VAULT_SEARCH_PATHS.append(_hot)


def _resolve_vault_image(slug, filename, port=None):
    """Resolve image path by searching all vault HOT directories.

    Searches multiple patterns:
      HOT/{slug}/{filename}
      HOT/{slug}/images/{filename}
      HOT/**/{slug}/{filename}
      HOT/**/{slug}/images/{filename}

    Returns (directory, filename) tuple for use with send_from_directory,
    or (None, None) if not found.
    """
    for search_path in _VAULT_SEARCH_PATHS:
        # Direct: HOT/slug/filename
        candidate = search_path / slug / filename
        if candidate.exists():
            resolved = candidate.resolve()
            return str(resolved.parent), resolved.name
        # With images subdir: HOT/slug/images/filename
        candidate = search_path / slug / "images" / filename
        if candidate.exists():
            resolved = candidate.resolve()
            return str(resolved.parent), resolved.name
        # Nested: HOT/*/slug/filename (e.g. career/bittorrent)
        for subdir in search_path.iterdir():
            if not subdir.is_dir() or subdir.name.startswith("."):
                continue
            candidate = subdir / slug / filename
            if candidate.exists():
                resolved = candidate.resolve()
                return str(resolved.parent), resolved.name
            candidate = subdir / slug / "images" / filename
            if candidate.exists():
                resolved = candidate.resolve()
                return str(resolved.parent), resolved.name
    return None, None


def _find_vault_dbs():
    """Return list of all vault.db paths across local vaults and chips."""
    dbs = []
    for name in ["vault-hot", "vault-cold"]:
        db = MAESTRO_ROOT / name / "vault.db"
        if db.exists():
            dbs.append(db)
    for chip_dir in sorted(MAESTRO_ROOT.glob("chip-*")):
        for vault_dir in sorted(chip_dir.glob("vault-*")):
            db = vault_dir / "vault.db"
            if db.exists():
                dbs.append(db)
    return dbs


def _query_all_vaults(query, params=()):
    """Run a query against all vault databases, return first match."""
    import sqlite3 as _sq3
    for db_path in _find_vault_dbs():
        try:
            conn = _sq3.connect(str(db_path))
            conn.row_factory = _sq3.Row
            rows = conn.execute(query, params).fetchall()
            conn.close()
            if rows:
                return [dict(r) for r in rows]
        except _sq3.OperationalError:
            continue
    return []


@app.route("/vault/cold/<slug>/<path:filename>")
def serve_vault_cold(slug, filename):
    """Serve data from the cold port."""
    directory, fname = _resolve_vault_image(slug, filename, "cold")
    if directory is None:
        return "Not found", 404
    return send_from_directory(directory, fname)


@app.route("/vault/hot/<slug>/<path:filename>")
def serve_vault_hot(slug, filename):
    """Serve data from the hot port."""
    directory, fname = _resolve_vault_image(slug, filename, "hot")
    if directory is None:
        return "Not found", 404
    return send_from_directory(directory, fname)


def _serve_keeper_file(slug, filename, device=None):
    """Resolve a keeper file (keyframe or source video) and serve it."""
    import sys as _sys
    _sys.path.insert(0, str(MAESTRO_ROOT / "tools" / "keeper"))
    from discovery import get_volume_path, list_keepers

    keepers = list_keepers()
    candidates = []
    if device:
        candidates = [k for k in keepers
                      if k.get("device_id", "").startswith(device)]
    else:
        candidates = keepers

    for k in candidates:
        vp = get_volume_path(k.get("device_id", ""))
        if vp is None:
            continue

        # Try keyframe in analysis directory
        kf_dir = os.path.join(vp, ".kept", "analysis", slug, "keyframes")
        fpath = os.path.join(kf_dir, filename)
        if os.path.isfile(fpath):
            return send_from_directory(kf_dir, filename)

        # Try source file via manifest lookup
        manifest_path = os.path.join(vp, ".kept", "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
        for fh, entry in manifest.get("entries", {}).items():
            if not fh.startswith(slug):
                continue
            rel_path = entry.get("path", "")
            if os.path.basename(rel_path) == filename:
                abs_path = os.path.join(vp, rel_path)
                if os.path.isfile(abs_path):
                    return send_from_directory(
                        os.path.dirname(abs_path),
                        os.path.basename(abs_path),
                        conditional=True,
                    )
            break

    return None


@app.route("/vault/keeper/<device>/<slug>/<path:filename>")
def serve_keeper_image(device, slug, filename):
    """Serve keyframe from a specific keeper device."""
    try:
        result = _serve_keeper_file(slug, filename, device=device)
        return result if result else ("Not found", 404)
    except Exception as exc:
        return "Keeper image error: %s" % exc, 500


@app.route("/vault/keeper/<slug>/<path:filename>")
def serve_keeper_image_any(slug, filename):
    """Serve keyframe by scanning all mounted keepers (no device specified)."""
    try:
        result = _serve_keeper_file(slug, filename)
        return result if result else ("Not found", 404)
    except Exception as exc:
        return "Keeper image error: %s" % exc, 500


@app.route("/drops/<path:filename>")
def serve_drop(filename):
    """Serve dropped image files from HOT/_drops."""
    drops_dir = MAESTRO_ROOT / "vault-hot" / "HOT" / "_drops"

    # Try exact match first
    fpath = drops_dir / filename
    if fpath.exists():
        real_path = fpath.resolve()
        return send_from_directory(str(real_path.parent), real_path.name)

    # Try matching by original filename via vault DB
    try:
        import sqlite3 as _sqlite3
        vault_db = MAESTRO_ROOT / "vault-cold" / "vault.db"
        if vault_db.exists():
            _conn = _sqlite3.connect(str(vault_db))
            _conn.row_factory = _sqlite3.Row
            row = _conn.execute(
                "SELECT file_hash FROM vault_images WHERE filename = ? AND slug = '_drops' LIMIT 1",
                (filename,),
            ).fetchone()
            _conn.close()
            if row and row["file_hash"]:
                ext = Path(filename).suffix or ".png"
                hash_name = row["file_hash"][:16] + ext
                fpath = drops_dir / hash_name
                if fpath.exists():
                    real_path = fpath.resolve()
                    return send_from_directory(str(real_path.parent), real_path.name)
    except Exception:
        pass

    # Legacy fallback: old drops dir
    legacy_dir = MAESTRO_ROOT / "tools" / "cue-vox" / "drops"
    fpath = legacy_dir / filename
    if fpath.exists():
        real_path = fpath.resolve()
        return send_from_directory(str(real_path.parent), real_path.name)

    return "Not found", 404


@app.route("/api/recognize", methods=["POST"])
def api_recognize():
    """Check if an image is already in the vault by content hash.

    Expects JSON: { "image": "<base64 data URI>" }
    Returns JSON: { "found": true, "slug": "...", "filename": "...",
                     "caption": "...", "context": "...", "description": "...",
                     "port": "...", "file_hash": "..." }
    or { "found": false, "file_hash": "..." }
    """
    import hashlib
    try:
        vault_lib = MAESTRO_ROOT / "tools" / "vault-template" / "app" / "lib"
        if str(vault_lib) not in sys.path:
            sys.path.insert(0, str(vault_lib))
        from fts import get_connection, ensure_schema, ensure_registry_schema
        from config import get_fts_db_path

        data = request.get_json() or {}
        image_b64 = data.get("image", "")
        if not image_b64:
            return jsonify({"error": "no image provided"}), 400

        # Strip data URI prefix
        if "," in image_b64 and image_b64.index(",") < 100:
            image_b64 = image_b64.split(",", 1)[1]

        # Hash the raw bytes
        import base64
        raw_bytes = base64.b64decode(image_b64)
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Look up in vault DB
        db_path = get_fts_db_path()
        conn = get_connection(db_path)
        ensure_schema(conn)
        ensure_registry_schema(conn)

        row = conn.execute(
            "SELECT slug, filename, caption, context, description, port "
            "FROM vault_images WHERE file_hash = ? LIMIT 1",
            (file_hash,),
        ).fetchone()

        if row:
            return jsonify({
                "found": True,
                "file_hash": file_hash,
                "slug": row["slug"],
                "filename": row["filename"],
                "caption": row["caption"] or row["description"] or "",
                "context": row["context"] or "",
                "port": row["port"] or "cold",
            })
        else:
            return jsonify({"found": False, "file_hash": file_hash})

    except Exception as e:
        print(f"[api/recognize] error: {e}")
        return jsonify({"found": False, "error": str(e)})


def _create_token_cli(label, value, token_type="text_input", base_temp=70, tags="", references=""):
    """Create a token directly (no CLI shelling) and emit socket event. Returns token_id or None."""
    import hashlib as _ht
    tokens_dir = MAESTRO_ROOT / ".claude" / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(tz=__import__("datetime").timezone.utc).isoformat()
    ts = str(int(datetime.now().timestamp()))
    token_id = "ctx_%s_%s" % (label, ts)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    token = {
        "token_id": token_id,
        "label": label,
        "value": value,
        "type": token_type,
        "status": "active",
        "temperature": base_temp,
        "base_temp": base_temp,
        "tags": tag_list,
        "created_at": now_iso,
    }
    if references:
        token["references"] = references

    # Content hash for chain integrity
    canonical = json.dumps(
        {"token_id": token_id, "value": value, "temperature": base_temp, "created_at": now_iso},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    token["content_hash"] = _ht.sha256(canonical).hexdigest()

    try:
        out_path = tokens_dir / ("%s.json" % token_id)
        with open(str(out_path), "w", encoding="utf-8") as f:
            json.dump(token, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        print("[token-direct] created %s" % token_id)

        socketio.emit("token_created", {
            "token_id": token_id,
            "type": token_type,
            "label": label,
            "value": value,
            "tags": tag_list,
            "temperature": base_temp,
            "base_temp": base_temp,
            "created_at": now_iso,
        })
        return token_id
    except Exception as e:
        print("[token-direct] creation failed: %s" % e)
    return None


@app.route("/api/drop-register", methods=["POST"])
def api_drop_register():
    """Register a dropped image with two-token chain:

    Token 1 (immediate): image dropped, processing
    Token 2 (chained): vivid description, context blurb, transcribed text, color/tone

    Expects JSON: { "image": "<base64>", "filename": "..." }
    """
    import hashlib, base64
    try:
        data = request.get_json() or {}
        image_b64 = data.get("image", "")
        filename = data.get("filename", "dropped_image")

        if not image_b64:
            return jsonify({"error": "no image"}), 400

        # Strip data URI prefix
        raw_b64 = image_b64
        if "," in raw_b64 and raw_b64.index(",") < 100:
            raw_b64 = raw_b64.split(",", 1)[1]
        raw_bytes = base64.b64decode(raw_b64)
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Save to HOT/_drops (file lives in the vault from the moment it is dropped)
        hot_drops_dir = MAESTRO_ROOT / "vault-hot" / "HOT" / "_drops"
        hot_drops_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(filename).suffix or ".png"
        drop_path = hot_drops_dir / (file_hash[:16] + ext)
        if not drop_path.exists():
            drop_path.write_bytes(raw_bytes)
        drop_path_str = str(drop_path)

        # Symlink original filename -> hash-based file (so both names work)
        orig_link = hot_drops_dir / filename
        if not orig_link.exists() and filename != drop_path.name:
            try:
                os.symlink(str(drop_path), str(orig_link))
            except OSError:
                pass

        # ── Index in vault DB ──
        try:
            import sqlite3 as _sqlite3
            vault_db = MAESTRO_ROOT / "vault-cold" / "vault.db"
            if vault_db.exists():
                _conn = _sqlite3.connect(str(vault_db))
                _conn.row_factory = _sqlite3.Row

                # Check if already indexed by hash
                existing = _conn.execute(
                    "SELECT slug FROM vault_images WHERE file_hash = ? LIMIT 1",
                    (file_hash,),
                ).fetchone()

                if not existing:
                    fext = ext.lstrip(".")
                    now_iso = datetime.now().isoformat(timespec="seconds") + "Z"
                    rel = "_drops/" + drop_path.name
                    _conn.execute(
                        "INSERT OR IGNORE INTO vault_images "
                        "(slug, filename, extension, file_size, indexed_at, port, file_hash, blob_path, rel_path) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("_drops", filename, fext, len(raw_bytes), now_iso,
                         "hot", file_hash, drop_path_str, rel),
                    )
                    _conn.commit()
                    print("[drop-register] indexed in vault_images: %s (%s)" % (filename, file_hash[:12]))
                else:
                    print("[drop-register] already indexed: %s" % file_hash[:12])
                _conn.close()
        except Exception as e:
            import traceback
            print("[drop-register] vault indexing failed (non-fatal): %s" % e)
            traceback.print_exc()

        # ── Token 1: immediate "processing" token ──
        hash_short = file_hash[:8]
        token1_value = "image dropped: %s; path: %s; hash: %s; status: processing" % (
            filename, drop_path_str, hash_short
        )
        token1_id = _create_token_cli(
            "image_drop_%s" % hash_short, token1_value,
            base_temp=70, tags="drop-viewer,image,processing"
        )
        print(f"[drop-register] token1: {token1_id}")

        # ── Vault lookup (direct sqlite3, no vault lib imports) ──
        recognized = False
        vault_meta = {}
        caption = ""
        try:
            import sqlite3 as _sqlite3
            vault_db = MAESTRO_ROOT / "vault-cold" / "vault.db"
            if vault_db.exists():
                _vconn = _sqlite3.connect(str(vault_db))
                _vconn.row_factory = _sqlite3.Row
                row = _vconn.execute(
                    "SELECT slug, filename, caption, context, description, port, blob_path "
                    "FROM vault_images WHERE file_hash = ? LIMIT 1",
                    (file_hash,),
                ).fetchone()
                if row:
                    recognized = True
                    vault_meta = {
                        "slug": row["slug"],
                        "vault_filename": row["filename"],
                        "caption": row["caption"] or row["description"] or "",
                        "context": row["context"] or "",
                        "port": row["port"] or "cold",
                        "blob_path": row["blob_path"] or "",
                    }
                    caption = vault_meta.get("caption", "")
                _vconn.close()
        except Exception as e:
            print(f"[drop-register] vault lookup failed (non-fatal): {e}")

        # ── Vision analysis ──
        describe_result = ""
        ocr_result = ""
        colors_result = ""
        try:
            c2d2_path = MAESTRO_ROOT / "tools" / "c2d2"
            if str(c2d2_path) not in sys.path:
                sys.path.insert(0, str(c2d2_path))
            from ollama_client import describe_image

            # Vivid visual description
            describe_result = describe_image(raw_b64, prompt=(
                "Describe this image vividly in 2-3 sentences. "
                "What is the subject, setting, mood, and notable details? "
                "Be specific and visual."
            ), max_tokens=200) or ""

            # Short caption if we don't have one
            if not caption:
                caption = describe_image(raw_b64) or ""

            # OCR -- read any visible text
            ocr_result = describe_image(raw_b64, prompt=(
                "Read all visible text in this image. Return ONLY the text, "
                "preserving line breaks. If no text is visible, say 'none'."
            ), max_tokens=300) or ""

            # Color and tone
            colors_result = describe_image(raw_b64, prompt=(
                "Describe the color palette and tone of this image in one line. "
                "Name the dominant color, secondary colors, overall warmth "
                "(warm/cool/neutral), and mood (energetic/calm/dramatic/etc)."
            ), max_tokens=80) or ""

        except Exception as e:
            print(f"[drop-register] vision analysis failed (non-fatal): {e}")

        # ── Token 2: VISUAL description (long-lived, persists to DB) ──
        visual_parts = []
        visual_parts.append("file: %s" % filename)
        visual_parts.append("path: %s" % drop_path_str)
        if caption:
            visual_parts.append("caption: %s" % caption)
        if describe_result:
            visual_parts.append("visual: %s" % describe_result)
        if ocr_result and ocr_result.lower().strip() != "none":
            visual_parts.append("text: %s" % ocr_result)
        if colors_result:
            visual_parts.append("colors: %s" % colors_result)
        visual_parts.append("hash: %s" % file_hash[:16])

        token2_value = "\n".join(visual_parts)

        tags2 = "drop-viewer,image,visual"
        if vault_meta.get("slug"):
            tags2 += ",vault:%s" % vault_meta["slug"]

        # Visual token: high base temp, slow decay -- this is the durable record
        token2_id = _create_token_cli(
            "image_visual_%s" % hash_short, token2_value,
            base_temp=80, tags=tags2,
            references=token1_id or ""
        )
        print(f"[drop-register] token2 (visual): {token2_id}")

        # ── Token 3: CONTEXTUAL blurb (short-lived, burns faster) ──
        context_result = ""
        try:
            context_result = describe_image(raw_b64, prompt=(
                "What is the purpose or context of this image? "
                "Who might use it and why? What story does it tell? "
                "One concise paragraph. No quotes, no emoji."
            ), max_tokens=150) or ""
        except Exception as e:
            print(f"[drop-register] context generation failed (non-fatal): {e}")

        context_parts = []
        context_parts.append("file: %s" % filename)
        if context_result:
            context_parts.append("context: %s" % context_result)
        if vault_meta.get("slug"):
            context_parts.append("vault: %s/%s" % (vault_meta["slug"], vault_meta.get("vault_filename", "")))
        if vault_meta.get("context"):
            context_parts.append("vault_context: %s" % vault_meta["context"])
        context_parts.append("hash: %s" % file_hash[:16])

        token3_value = "\n".join(context_parts)

        tags3 = "drop-viewer,image,contextual"
        if vault_meta.get("slug"):
            tags3 += ",vault:%s" % vault_meta["slug"]

        # Contextual token: lower base temp, faster decay -- ephemeral interpretation
        token3_id = _create_token_cli(
            "image_context_%s" % hash_short, token3_value,
            base_temp=60, tags=tags3,
            references=token2_id or ""
        )
        print(f"[drop-register] token3 (context): {token3_id}")

        # ── Persist caption + description back to vault DB ──
        try:
            if caption or describe_result:
                import sqlite3 as _sqlite3
                vault_db = MAESTRO_ROOT / "vault-cold" / "vault.db"
                if vault_db.exists():
                    _pconn = _sqlite3.connect(str(vault_db))
                    updates = []
                    params = []
                    if caption:
                        updates.append("caption = ?")
                        params.append(caption)
                    if describe_result:
                        updates.append("description = ?")
                        params.append(describe_result)
                    if updates:
                        params.append(file_hash)
                        _pconn.execute(
                            "UPDATE vault_images SET %s WHERE file_hash = ?" % ", ".join(updates),
                            params,
                        )
                        _pconn.commit()
                    _pconn.close()
        except Exception as e:
            print(f"[drop-register] DB caption update failed (non-fatal): {e}")

        return jsonify({
            "token1_id": token1_id,
            "token2_id": token2_id,
            "token3_id": token3_id,
            "file_hash": file_hash,
            "recognized": recognized,
            "caption": caption,
            "description": describe_result,
            "context_blurb": context_result,
            "ocr": ocr_result,
            "colors": colors_result,
            "path": drop_path_str,
            "vault": vault_meta if recognized else None,
        })

    except Exception as e:
        print(f"[drop-register] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/image/inspect", methods=["POST"])
def api_image_inspect():
    """Run deeper inspection on an image by path or base64.

    Accepts JSON: { "path": "/path/to/file" } or { "image": "<base64>" }
    Optional: { "tools": ["describe", "ocr", "colors"] } (default: all)

    Returns JSON with results for each requested tool.
    """
    import base64 as b64mod
    data = request.get_json() or {}
    image_path = data.get("path")
    image_b64 = data.get("image")
    tools = data.get("tools", ["describe", "ocr", "colors"])
    results = {}

    # Resolve image bytes
    raw_b64 = None
    if image_path and os.path.isfile(image_path):
        with open(image_path, "rb") as f:
            raw_b64 = b64mod.b64encode(f.read()).decode()
    elif image_b64:
        raw_b64 = image_b64
        if "," in raw_b64 and raw_b64.index(",") < 100:
            raw_b64 = raw_b64.split(",", 1)[1]

    if not raw_b64:
        return jsonify({"error": "no image found at path or in payload"}), 400

    try:
        c2d2_path = MAESTRO_ROOT / "tools" / "c2d2"
        if str(c2d2_path) not in sys.path:
            sys.path.insert(0, str(c2d2_path))
        from ollama_client import describe_image
    except Exception as e:
        return jsonify({"error": "vision model unavailable: %s" % e}), 503

    if "describe" in tools:
        desc = describe_image(raw_b64, prompt=(
            "Describe this image in detail. What do you see? "
            "Include subjects, setting, colors, text, and mood. "
            "2-3 sentences."
        ), max_tokens=200)
        results["describe"] = desc

    if "ocr" in tools:
        ocr = describe_image(raw_b64, prompt=(
            "Read all visible text in this image. Return ONLY the text, "
            "preserving line breaks. If no text is visible, say 'no text'."
        ), max_tokens=300)
        results["ocr"] = ocr

    if "colors" in tools:
        colors = describe_image(raw_b64, prompt=(
            "Describe the color palette of this image in one line. "
            "Name the dominant color, secondary colors, and overall warmth "
            "(warm/cool/neutral). Example: 'Deep blue dominant, white accents, cool'"
        ), max_tokens=60)
        results["colors"] = colors

    return jsonify(results)


@app.route("/api/describe", methods=["POST"])
def api_describe():
    """Describe images using C2D2 vision model.

    Expects JSON: { "images": ["<base64>", ...], "prompt": "optional" }
    Returns JSON: { "result": "description text" }
    """
    try:
        c2d2_path = MAESTRO_ROOT / "tools" / "c2d2"
        if str(c2d2_path) not in sys.path:
            sys.path.insert(0, str(c2d2_path))
        from ollama_client import describe_images
        data = request.get_json() or {}
        raw_images = data.get("images", [])
        if not raw_images:
            return jsonify({"error": "no images provided"}), 400
        # Strip data URI prefix from each
        cleaned = []
        for img in raw_images:
            if "," in img and img.index(",") < 100:
                img = img.split(",", 1)[1]
            cleaned.append(img)
        prompt = data.get("prompt")
        result = describe_images(cleaned, prompt=prompt)
        if result is None:
            return jsonify({"error": "vision model unavailable"}), 503
        return jsonify({"result": result})
    except Exception as e:
        print(f"[api/describe] error: {e}")
        return jsonify({"error": str(e)}), 500


def _parse_case_study_segments(text):
    """Parse message text into narrative and gallery segments.

    Returns list of dicts: {"type": "narrative", "content": "..."} or
    {"type": "gallery", "data": {...}}.
    Strips non-GALLERY structured tags from narrative text.
    """
    tag_re = re.compile(r"\[(YES_NO|INPUT|APPROVAL|DOCUMENT|CUE|GALLERY|CITATIONS):\s*")
    segments = []
    last_end = 0

    pos = 0
    while pos < len(text):
        m = tag_re.search(text, pos)
        if not m:
            break

        tag_type = m.group(1)
        data_start = m.end()

        # Bracket-balanced walk to find closing ]
        depth = 1
        cur = data_start
        while cur < len(text) and depth > 0:
            if text[cur] == "[":
                depth += 1
            elif text[cur] == "]":
                depth -= 1
            if depth > 0:
                cur += 1

        if depth != 0:
            pos = m.end()
            continue

        tag_end = cur + 1
        inner = text[data_start:cur]

        # Narrative text before this tag
        before = text[last_end:m.start()].strip()
        if before:
            segments.append({"type": "narrative", "content": before})

        if tag_type == "GALLERY":
            try:
                gallery_data = json.loads(inner)
                segments.append({"type": "gallery", "data": gallery_data})
            except json.JSONDecodeError:
                pass
        # All other tag types are stripped (not added to segments)

        last_end = tag_end
        pos = tag_end

    # Trailing narrative text
    trailing = text[last_end:].strip()
    if trailing:
        segments.append({"type": "narrative", "content": trailing})

    return segments


def _generate_story(images, context=""):
    """Generate a structured story from gallery images + captions.

    Returns dict: { title, intro, slides: [{narrative}], closing }
    """
    try:
        c2d2_path = MAESTRO_ROOT / "tools" / "c2d2"
        if str(c2d2_path) not in sys.path:
            sys.path.insert(0, str(c2d2_path))
        from ollama_client import generate
    except Exception:
        return None

    # Build prompt from captions
    caption_list = []
    for i, img in enumerate(images):
        cap = img.get("caption", "") or img.get("description", "") or ""
        caption_list.append("Slide %d: %s" % (i + 1, cap or "(no caption)"))

    captions_block = "\n".join(caption_list)
    extra = ("\nContext: %s" % context) if context else ""

    prompt = (
        "You are writing a short visual story for a slideshow with %d images.\n"
        "The captions for each image are:\n%s\n%s\n\n"
        "Write a JSON object with these fields:\n"
        "- title: a short compelling title (under 8 words)\n"
        "- intro: 1-2 sentences setting the scene\n"
        "- slides: array of objects, one per image, each with a \"narrative\" field "
        "(1-2 sentences describing what this image shows in the story)\n"
        "- closing: 1 sentence wrap-up\n\n"
        "Return ONLY valid JSON, no markdown fences, no explanation."
    ) % (len(images), captions_block, extra)

    result = generate(prompt, max_tokens=1024, timeout=60)
    if not result:
        return None

    # Parse JSON from response
    try:
        # Strip markdown fences if present
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        print(f"[case-study] failed to parse story JSON: {result[:200]}")
        return None


_VX_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}


def _vx_ratio_label(ar, tags_str=""):
    """Convert a numeric aspect ratio to a human-readable label."""
    if ar and ar > 0:
        if abs(ar - 16 / 9) < 0.15:
            return "16:9"
        if abs(ar - 9 / 16) < 0.08:
            return "9:16"
        if abs(ar - 4 / 3) < 0.1:
            return "4:3"
        if abs(ar - 3 / 4) < 0.08:
            return "3:4"
        if abs(ar - 1.0) < 0.08:
            return "1:1"
        if abs(ar - 21 / 9) < 0.15:
            return "21:9"
        return "{:.2f}:1".format(ar)
    # Fallback from tags
    if "vertical" in tags_str:
        return "9:16"
    return ""


def _vx_resolve_images(gallery_images):
    """Shared resolver for all vault export endpoints.

    Enriches each image dict in-place with: url, is_video, thumb_url,
    orientation, aspect_label, duration.
    """
    import sqlite3 as _vx_sqlite3
    _vx_db = os.path.join(MAESTRO_ROOT, "vault-cold", "vault.db")
    _vx_lookup = {}
    _vx_tags = {}
    if os.path.isfile(_vx_db):
        try:
            conn = _vx_sqlite3.connect(_vx_db)
            conn.row_factory = _vx_sqlite3.Row
            for row in conn.execute(
                "SELECT filename, slug, port, context, tags, aspect_ratio "
                "FROM vault_images"
            ).fetchall():
                key = (row["slug"], row["filename"], row["port"])
                _vx_lookup[key] = {
                    "thumbnail": row["context"] or "",
                    "aspect_ratio": row["aspect_ratio"],
                }
                _vx_tags[key] = row["tags"] or ""
            conn.close()
        except _vx_sqlite3.Error:
            pass

    for img in gallery_images:
        slug = img.get("slug", "")
        fname = img.get("filename", "")
        port = img.get("port", "cold")
        ext = os.path.splitext(fname)[1].lower() if fname else ""
        is_video = ext in _VX_VIDEO_EXTS or img.get("type") == "video"
        db_key = (slug, fname, port)
        db_row = _vx_lookup.get(db_key, {})
        tags_str = _vx_tags.get(db_key, "")

        # URL
        if slug and fname:
            if slug in ("_drops", "_hot_loose", "_cold_loose"):
                img["url"] = "/drops/{}".format(fname)
            else:
                img["url"] = "/vault/{}/{}/{}".format(port, slug, fname)
        elif img.get("src"):
            img["url"] = img["src"]

        # Video detection + thumbnail
        if is_video:
            img["is_video"] = True
            thumb = img.get("thumbnail", "") or db_row.get("thumbnail", "")
            if thumb and slug:
                img["thumb_url"] = "/vault/{}/{}/{}".format(port, slug, thumb)
            else:
                img["thumb_url"] = img.get("url", "")

        # Aspect ratio: probe actual file dimensions
        ar = db_row.get("aspect_ratio")
        if not ar or ar <= 0:
            # Probe the displayable file (thumbnail for video, image for stills)
            probe_file = None
            if is_video:
                thumb_name = img.get("thumbnail", "") or db_row.get("thumbnail", "")
                if thumb_name and slug:
                    resolved = _resolve_vault_image(slug, thumb_name, port)
                    if resolved[0]:
                        probe_file = os.path.join(resolved[0], resolved[1])
            else:
                if slug and fname:
                    resolved = _resolve_vault_image(slug, fname, port)
                    if resolved[0]:
                        probe_file = os.path.join(resolved[0], resolved[1])

            if probe_file and os.path.isfile(probe_file):
                try:
                    from PIL import Image as _PILImage
                    with _PILImage.open(probe_file) as pim:
                        w, h = pim.size
                        if h > 0:
                            ar = w / h
                except Exception:
                    ar = None

        img["aspect_label"] = _vx_ratio_label(ar, tags_str)

    return gallery_images


def _vx_load_gallery(gallery_slug):
    """Load a gallery from vault.db by slug. Returns (title, images) or (None, None)."""
    import sqlite3 as _lg_sqlite3
    db_path = os.path.join(MAESTRO_ROOT, "vault-cold", "vault.db")
    if not os.path.isfile(db_path):
        return None, None
    try:
        conn = _lg_sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT title, images FROM galleries WHERE slug = ?", (gallery_slug,)
        ).fetchone()
        conn.close()
        if not row:
            return None, None
        return row[0], json.loads(row[1])
    except (_lg_sqlite3.Error, json.JSONDecodeError):
        return None, None


@app.route("/case-study/<gallery_slug>")
def case_study(gallery_slug):
    """Render a print-ready case study from vault.db gallery slug."""
    title, images = _vx_load_gallery(gallery_slug)
    if title is None:
        return "Gallery not found", 404
    gallery_images = _vx_resolve_images(images)

    slides = [{"narrative": img.get("caption", "")} for img in gallery_images]

    return render_template(
        "case-study.html",
        title=title,
        slides=list(zip(gallery_images, slides)),
        closing="",
    )


@app.route("/contact-sheet/<gallery_slug>")
def contact_sheet(gallery_slug):
    """Render a compact contact sheet from vault.db gallery slug."""
    title, images = _vx_load_gallery(gallery_slug)
    if title is None:
        return "Gallery not found", 404
    gallery_images = _vx_resolve_images(images)

    import sqlite3 as _cs_sqlite3
    _cs_db = os.path.join(MAESTRO_ROOT, "vault-cold", "vault.db")
    _cs_tags = {}
    if os.path.isfile(_cs_db):
        try:
            _cs_conn = _cs_sqlite3.connect(_cs_db)
            _cs_conn.row_factory = _cs_sqlite3.Row
            for row in _cs_conn.execute(
                "SELECT filename, slug, port, tags FROM vault_images"
            ).fetchall():
                _cs_tags[(row["slug"], row["filename"], row["port"])] = row["tags"] or ""
            _cs_conn.close()
        except _cs_sqlite3.Error:
            pass

    _tag_categories = [
        ("hero-cut", "Hero Cuts"),
        ("mini-cut", "Mini Cuts"),
        ("talking-points", "Talking Points"),
        ("master-reel", "Master Reels"),
        ("extended-cut", "Extended Cut"),
        ("short", "Short"),
    ]

    groups = {}
    for img in gallery_images:
        slug = img.get("slug", "")
        fname = img.get("filename", "")
        port = img.get("port", "cold")
        tags_str = _cs_tags.get((slug, fname, port), "")

        category = slug
        for tag, label in _tag_categories:
            if tag in tags_str:
                category = label
                break
        else:
            band = img.get("band", "")
            if band:
                category = band.title()

        if category not in groups:
            groups[category] = []
        groups[category].append(img)

    categories = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    item_count = sum(len(v) for v in groups.values())

    return render_template(
        "contact-sheet.html",
        title=title,
        categories=categories,
        item_count=item_count,
        category_count=len(categories),
    )


@app.route("/transcription/<gallery_slug>")
def transcription_view(gallery_slug):
    """Render a transcription timeline from vault.db gallery slug."""
    title, images = _vx_load_gallery(gallery_slug)
    if title is None:
        return "Gallery not found", 404
    gallery_images = _vx_resolve_images(images)

    import sqlite3 as _tr_sqlite3
    _tr_db = os.path.join(MAESTRO_ROOT, "vault-cold", "vault.db")
    _tr_by_key = {}
    _tr_by_stem = {}
    if os.path.isfile(_tr_db):
        try:
            _tr_conn = _tr_sqlite3.connect(_tr_db)
            _tr_conn.row_factory = _tr_sqlite3.Row
            for row in _tr_conn.execute(
                "SELECT slug, filename, transcript_text, segments, duration "
                "FROM transcriptions"
            ).fetchall():
                data = {
                    "transcript_text": row["transcript_text"] or "",
                    "segments": row["segments"] or "[]",
                    "duration": row["duration"] or 0,
                }
                _tr_by_key[(row["slug"], row["filename"])] = data
                stem = os.path.splitext(row["filename"])[0].lower()
                _tr_by_stem[stem] = data
            _tr_conn.close()
        except _tr_sqlite3.Error:
            pass

    # Build keeper transcript lookup for images with port=keeper
    def _keeper_transcript(slug):
        """Read transcript.txt from keeper analysis dir, scanning all mounted keepers."""
        try:
            import sys as _ks
            _ks.path.insert(0, str(MAESTRO_ROOT / "tools" / "keeper"))
            from discovery import list_keepers, get_volume_path
            for k in list_keepers():
                vp = get_volume_path(k.get("device_id", ""))
                if vp is None:
                    continue
                tx_path = os.path.join(vp, ".kept", "analysis", slug, "transcript.txt")
                if os.path.isfile(tx_path):
                    with open(tx_path, "r", encoding="utf-8") as f:
                        return f.read()
        except Exception:
            pass
        return ""

    items = []
    for img in gallery_images:
        slug = img.get("slug", "")
        fname = img.get("filename", "")
        port = img.get("port", "")

        tr_data = _tr_by_key.get((slug, fname))
        if not tr_data:
            stem = os.path.splitext(fname)[0].lower()
            tr_data = _tr_by_stem.get(stem, {})

        transcript_text = tr_data.get("transcript_text", "") if tr_data else ""

        # Fallback to keeper transcript if vault.db has nothing
        if not transcript_text and port == "keeper":
            transcript_text = _keeper_transcript(slug)

        segments_json = tr_data.get("segments", "[]") if tr_data else "[]"
        try:
            segments = json.loads(segments_json)
        except (json.JSONDecodeError, TypeError):
            segments = []

        item = dict(img)
        item["transcript_text"] = transcript_text
        item["segments"] = segments
        item["duration"] = tr_data.get("duration", 0) if tr_data else 0
        items.append(item)

    return render_template(
        "transcription.html",
        title=title,
        items=items,
        item_count=len(items),
    )


@app.route("/api/gallery", methods=["POST"])
def api_save_gallery():
    """Persist a gallery to the vault registry."""
    import sqlite3 as _gal_sqlite3
    payload = request.get_json(silent=True) or {}
    slug = payload.get("slug", "")
    title = payload.get("title", "")
    images = payload.get("images", [])
    created_at = payload.get("created_at")
    if not slug or not images:
        return jsonify({"error": "slug and images required"}), 400
    # Skip persist if images are still blob URLs (pre-registration drops)
    if any("blob:" in (img.get("src", "") or "") for img in images):
        if not any(img.get("slug") for img in images):
            print("[gallery] skipping save (blob URLs, not yet registered): %s" % slug)
            return jsonify({"ok": True, "slug": slug, "deferred": True})
    db_path = MAESTRO_ROOT / "vault-cold" / "vault.db"
    try:
        conn = _gal_sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS galleries (
                slug TEXT PRIMARY KEY, title TEXT, images TEXT, created_at TEXT
            )
        """)
        # Enrich video images with thumbnails from vault_images if missing
        _vid_exts = {".mp4", ".mov", ".webm", ".m4v"}
        for img in images:
            fname = img.get("filename", "")
            ext = os.path.splitext(fname)[1].lower() if fname else ""
            if ext in _vid_exts and not img.get("thumbnail"):
                row = conn.execute(
                    "SELECT context FROM vault_images WHERE filename = ? LIMIT 1",
                    (fname,),
                ).fetchone()
                if row and row[0]:
                    img["thumbnail"] = row[0]
                else:
                    base = os.path.splitext(fname)[0]
                    img["thumbnail"] = base + ".thumb.jpg"
                if "type" not in img:
                    img["type"] = "video"
        conn.execute(
            "INSERT OR REPLACE INTO galleries (slug, title, images, created_at) VALUES (?, ?, ?, ?)",
            (slug, title, json.dumps(images), created_at or datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        )
        conn.commit()
        conn.close()
    except _gal_sqlite3.Error as exc:
        return jsonify({"error": str(exc)}), 500
    print("[gallery] saved: %s (%d images)" % (slug, len(images)))
    return jsonify({"ok": True, "slug": slug})


@app.route("/api/galleries", methods=["GET"])
def api_list_galleries():
    """List galleries, newest first. Optional ?since= and ?until= ISO timestamps."""
    import sqlite3 as _gal_sqlite3
    since = request.args.get("since")
    until = request.args.get("until")
    limit = int(request.args.get("limit", 50))
    db_path = MAESTRO_ROOT / "vault-cold" / "vault.db"
    try:
        conn = _gal_sqlite3.connect(str(db_path))
        clauses = []
        params = []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT slug, title, images, created_at FROM galleries"
            + where + " ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        conn.close()
    except _gal_sqlite3.Error as exc:
        return jsonify({"error": str(exc)}), 500
    results = []
    for slug, title, images_json, created_at in rows:
        try:
            imgs = json.loads(images_json)
        except (ValueError, TypeError):
            imgs = []
        results.append({
            "slug": slug, "title": title,
            "image_count": len(imgs), "created_at": created_at,
        })
    return jsonify(results)


@app.route("/api/gallery/<slug>", methods=["GET"])
def api_get_gallery(slug):
    """Fetch a single gallery by slug with full image list."""
    import sqlite3 as _gal_sqlite3
    db_path = MAESTRO_ROOT / "vault-cold" / "vault.db"
    try:
        conn = _gal_sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT slug, title, images, created_at FROM galleries WHERE slug = ?",
            (slug,),
        ).fetchone()
        conn.close()
    except _gal_sqlite3.Error as exc:
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "not found"}), 404
    try:
        imgs = json.loads(row[2])
    except (ValueError, TypeError):
        imgs = []
    return jsonify({
        "slug": row[0], "title": row[1],
        "images": imgs, "image_count": len(imgs), "created_at": row[3],
    })


@app.route("/api/regenerate-story", methods=["POST"])
def api_regenerate_story():
    """Regenerate story narrative with user refinements as context."""
    payload = request.get_json() or {}
    gallery = payload.get("gallery", {})
    context = payload.get("context", "")

    images = gallery.get("images", [])
    story = _generate_story(images, context)
    if story:
        return jsonify(story)
    return jsonify({"error": "generation failed"}), 503


@socketio.on('audio_data')
def handle_audio(data):
    """Receive audio from browser, transcribe, send to Claude, speak response"""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        flush_speech_queue()

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

        # Inject instance identity, speech consumption, variables, input history, engagement, and rolling summary context
        context_sections = [
            ("identity", get_instance_identity()),
            ("flux", get_flux_capacitor_context()),
            ("images", get_image_context()),
            ("engagement", get_engagement_context()),
            ("summary", get_conversation_summary_context()),
            ("speech", get_speech_consumption_context()),
            ("variables", get_variables_context()),
            ("input_history", get_input_history_context()),
            ("length_constraint", length_constraint),
            ("prompt_template", load_prompt_template()),
        ]

        # Assemble with budget enforcement to prevent "Prompt is too long" errors
        enhanced_text = assemble_prompt_with_budget(context_sections, enhanced_text)

        # Send to Claude Code with C2D2 fallback
        response, used_fallback = _call_claude_or_fallback(enhanced_text, raw_user_text=text)

        if response is None:
            print("[VOID] Claude response discarded (message was voided)")
            emit('state_change', {'state': 'idle'})
            Path(temp_file.name).unlink()
            return

        if used_fallback:
            emit('fallback_active', {'backend': 'c2d2'})

        # Log conversation with input length
        _, clean_response, snr_hex = log_conversation(text, response, input_length=input_word_count)

        tts_text = sanitize_for_tts(clean_response)
        tts_chunks = tts_chunk_split(tts_text)
        response_data = {"text": clean_response, "tts_chunks": tts_chunks}
        if snr_hex:
            response_data["snr_hex"] = snr_hex
        emit("response", response_data)
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(clean_response)

        # Speak response (chunked by paragraph)
        speak_chunked(tts_text)

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
    print("[DEBUG] button_response received: %s" % data)
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        flush_speech_queue()

        answer = data['answer']  # "Yes" or "No"

        # Extract question context from recent conversation
        recent_logs = load_recent_logs(limit=5)
        question_context = None
        if recent_logs:
            # Get the most recent assistant message (which likely contains the YES_NO question)
            last_entry = recent_logs[-1]
            last_assistant_msg = last_entry.get('assistant', '')
            # Try to extract question from [YES_NO: ...] pattern
            import re
            yes_no_match = re.search(r'\[YES_NO:\s*(.+?)\]', last_assistant_msg)
            if yes_no_match:
                question_context = yes_no_match.group(1).strip()

        # Create persistent YES/NO token
        token_id = create_yes_no_token(
            answer=answer,
            question_context=question_context
        )
        maybe_create_structured_sum()
        emit("token_created", {
            "token_id": token_id,
            "type": "yes_no_response",
            "label": question_context or "yes_no",
            "value": answer,
            "question": question_context or ""
        })

        emit('state_change', {'state': 'thinking'})

        # Calculate input length for response matching
        input_word_count = get_input_word_count(answer)
        length_constraint = get_response_length_constraint(input_word_count)

        # Load recent conversation for context
        context = ""
        if recent_logs:
            context = "[RECENT CONVERSATION]\n"
            for entry in recent_logs:
                context += f"User: {entry.get('user', '')}\n"
                context += f"Assistant: {entry.get('assistant', '')}\n"
            context += "\n"

        # Inject instance identity, speech consumption, variables, and input history context
        identity_context = get_instance_identity()
        speech_context = get_speech_consumption_context()
        variables_context = get_variables_context()
        input_history_context = get_input_history_context()
        summary_context = get_conversation_summary_context()
        flux_context = get_flux_capacitor_context()

        # Build prompt with context
        enhanced_text = f"""{identity_context}{flux_context}{summary_context}{speech_context}{variables_context}{input_history_context}{length_constraint}{context}[USER'S RESPONSE TO YOUR LAST QUESTION]
{answer}

[VOICE INTERFACE INSTRUCTIONS]
When you need confirmation, format your response like this:
[YES_NO: your question here]

The UI will automatically render Yes OR No buttons for the user to click.

CRITICAL: If the user responds "No" to a yes/no question, accept their answer as final. Do NOT ask another yes/no question or suggest alternatives unless the user explicitly asks for them. "No" means "No" - acknowledge it and move on.

IMPORTANT: When speaking, say "Yes OR No" not "yes-no" or "yes slash no"."""

        # Send to Claude Code with C2D2 fallback
        response, used_fallback = _call_claude_or_fallback(enhanced_text, raw_user_text=answer)

        if response is None:
            print("[VOID] Claude response discarded (button response was voided)")
            emit('state_change', {'state': 'idle'})
            return

        if used_fallback:
            emit('fallback_active', {'backend': 'c2d2'})

        # Log conversation (button answer as user input) with input length
        _, clean_response, snr_hex = log_conversation(answer, response, input_length=input_word_count)

        tts_text = sanitize_for_tts(clean_response)
        tts_chunks = tts_chunk_split(tts_text)
        response_data = {"text": clean_response, "tts_chunks": tts_chunks}
        if snr_hex:
            response_data["snr_hex"] = snr_hex
        emit("response", response_data)
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(clean_response)

        # Speak response (chunked by paragraph)
        speak_chunked(tts_text)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


def _respond_and_speak(user_log_text, response_text, confidence=None):
    """Log, emit, speak, and return to idle. DRY helper for approval handler."""
    _, clean_response, snr_hex = log_conversation(
        user_log_text, response_text,
        input_length=get_input_word_count(user_log_text),
        confidence=confidence,
    )
    tts_text = sanitize_for_tts(clean_response)
    tts_chunks = tts_chunk_split(tts_text)
    response_data = {"text": clean_response, "tts_chunks": tts_chunks}
    if snr_hex:
        response_data["snr_hex"] = snr_hex
    emit("response", response_data)
    emit('state_change', {'state': 'speaking'})
    start_speech_tracking(clean_response)
    speak_chunked(tts_text)
    finish_speech()
    emit('state_change', {'state': 'idle'})


@socketio.on('approval_response')
def handle_approval_response(data):
    """Handle approval gate response with direct file execution (no subprocess)."""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        flush_speech_queue()

        decision = data['decision']  # "Approve" or "Deny"
        approval_data = data.get('approval_data', {})
        confidence = data.get('confidence')  # HSL confidence values

        action = approval_data.get('action', 'action')
        target = approval_data.get('target', '')
        description = approval_data.get('description', '')
        preview = approval_data.get('preview', '')

        # Create persistent token for the approval decision
        question_ctx = "%s: %s" % (action, description)
        token_id = create_yes_no_token(
            answer=decision,
            question_context=question_ctx
        )
        maybe_create_structured_sum()
        emit("token_created", {
            "token_id": token_id,
            "type": "approval_response",
            "label": action,
            "value": decision,
            "question": question_ctx
        })

        action_summary = "%s on %s" % (action, target or 'target')
        user_log = "%s (%s)" % (decision, action_summary)

        # --- Deny decision: acknowledge and done ---
        if decision != "Approve":
            _respond_and_speak(
                user_log,
                "Got it, cancelled %s." % action.lower(),
                confidence=confidence,
            )
            return

        # --- Approve: scope-guarded direct execution ---

        # Only Write (create new file) is allowed through voice
        if action != "Write":
            _respond_and_speak(
                user_log,
                "%s operations should go through ninja Claude in the terminal. "
                "Run claude-code and ask there." % action,
                confidence=confidence,
            )
            return

        # Must have content to write
        if not preview or not preview.strip():
            _respond_and_speak(
                user_log,
                "No file content was provided in the approval. "
                "Ask Claude to regenerate with the full content.",
                confidence=confidence,
            )
            return

        # Path safety: must resolve within MAESTRO_ROOT
        maestro_root = MAESTRO_ROOT.resolve()
        target_path = Path(target).resolve() if target else None

        if target_path is None:
            _respond_and_speak(
                user_log,
                "No target file path specified.",
                confidence=confidence,
            )
            return

        try:
            target_path.relative_to(maestro_root)
        except ValueError:
            _respond_and_speak(
                user_log,
                "Path is outside the project. File creation blocked for safety.",
                confidence=confidence,
            )
            return

        # Reject if file already exists (updates go through ninja Claude)
        if target_path.exists():
            _respond_and_speak(
                user_log,
                "That file already exists. Use ninja Claude to modify existing files. "
                "Run claude-code and ask there.",
                confidence=confidence,
            )
            return

        # All checks passed -- create the file directly
        emit('state_change', {'state': 'thinking'})
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(preview)

        # Audit trail
        if _audit_logger:
            _audit_logger.log("file:created", details={
                "path": str(target_path),
                "description": description,
                "size": len(preview),
            })

        filename = target_path.name
        _respond_and_speak(
            user_log,
            "Created %s." % filename,
            confidence=confidence,
        )

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('input_response')
def handle_input_response(data):
    """Handle input response from INPUT cards (text, slider, choice)"""
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        flush_speech_queue()

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
                question = input_data.get('question', '')

                # Store as session variable
                session_variables[key] = value
                user_message = f"{key}={value}"

                # Create persistent text input token
                token_id = create_text_input_token(
                    key=key,
                    value=value,
                    question=question
                )
                maybe_create_structured_sum()
                emit("token_created", {
                    "token_id": token_id,
                    "type": "text_input",
                    "label": key,
                    "value": value,
                    "question": question
                })

                # Track in input history (token is already stored by create_text_input_token)
                # This is redundant but kept for backwards compatibility with local cache
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
                maybe_create_structured_sum()
                emit("token_created", {
                    "token_id": token_id,
                    "type": "scalar_param",
                    "label": semantic_label,
                    "value": natural_value,
                    "slider_value": slider_value,
                    "question": question,
                    "key": semantic_label
                })

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

            # Simple slider input (no HSL encoding - from frontend slider widget)
            elif 'slider_value' in input_data and 'semantic_label' in input_data:
                slider_value = input_data['slider_value']
                semantic_label = input_data['semantic_label']
                question = input_data.get('question', '')
                natural_value = map_slider_to_semantic_value(slider_value, semantic_label)

                # Create scalar token with placeholder hex/hsl (no VRGB encoding)
                token_id = create_scalar_param_token(
                    slider_value=slider_value,
                    semantic_label=semantic_label,
                    hex_value="#000000",
                    hsl_value={"h": 0, "s": 0, "l": slider_value},
                    question=question
                )
                maybe_create_structured_sum()
                emit("token_created", {
                    "token_id": token_id,
                    "type": "scalar_param",
                    "label": semantic_label,
                    "value": natural_value,
                    "slider_value": slider_value,
                    "question": question,
                    "key": semantic_label
                })

                if question:
                    user_message = f"[Re: {question}] {semantic_label}: {natural_value}"
                else:
                    user_message = f"{semantic_label}: {natural_value}"

                session_variables[semantic_label] = natural_value

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
        summary_context = get_conversation_summary_context()
        flux_context = get_flux_capacitor_context()

        # Prepare input for Claude with all context
        enhanced_text = f"{flux_context}{summary_context}{speech_context}{variables_context}{history_context}[USER INPUT]\n{user_message}"

        # Send to Claude Code with C2D2 fallback
        response, used_fallback = _call_claude_or_fallback(enhanced_text, raw_user_text=str(user_message))

        if response is None:
            print("[VOID] Claude response discarded (input response was voided)")
            emit('state_change', {'state': 'idle'})
            return

        if used_fallback:
            emit('fallback_active', {'backend': 'c2d2'})

        # Log conversation with input length
        _, clean_response, snr_hex = log_conversation(user_message, response, input_length=input_word_count)

        tts_text = sanitize_for_tts(clean_response)
        tts_chunks = tts_chunk_split(tts_text)
        response_data = {"text": clean_response, "tts_chunks": tts_chunks}
        if snr_hex:
            response_data["snr_hex"] = snr_hex
        emit("response", response_data)
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(clean_response)

        # Speak response (chunked by paragraph)
        speak_chunked(tts_text)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('text_message')
def handle_text_message(data):
    """Handle text message from input field - same flow as voice but without transcription"""
    print("[DEBUG] text_message received: %s" % str(data)[:200])
    try:
        # Handle any speech interruption and stop current speech
        handle_speech_interruption()
        flush_speech_queue()

        text = data['text'].strip()

        if not text:
            return

        emit('state_change', {'state': 'thinking'})

        # Calculate input length for response matching
        input_word_count = get_input_word_count(text)
        length_constraint = get_response_length_constraint(input_word_count)

        # Inject temporal context if query is time-related
        enhanced_text = inject_temporal_context(text)

        # Inject instance identity, speech consumption, variables, input history, engagement, and rolling summary context
        context_sections = [
            ("identity", get_instance_identity()),
            ("flux", get_flux_capacitor_context()),
            ("images", get_image_context()),
            ("engagement", get_engagement_context()),
            ("summary", get_conversation_summary_context()),
            ("speech", get_speech_consumption_context()),
            ("variables", get_variables_context()),
            ("input_history", get_input_history_context()),
            ("length_constraint", length_constraint),
            ("prompt_template", load_prompt_template()),
        ]

        # Assemble with budget enforcement to prevent "Prompt is too long" errors
        enhanced_text = assemble_prompt_with_budget(context_sections, enhanced_text)

        # Send to Claude Code with C2D2 fallback
        response, used_fallback = _call_claude_or_fallback(enhanced_text, raw_user_text=text)

        if response is None:
            print("[VOID] Claude response discarded (text message was voided)")
            emit('state_change', {'state': 'idle'})
            return

        if used_fallback:
            emit('fallback_active', {'backend': 'c2d2'})

        # Log conversation with input length
        _, clean_response, snr_hex = log_conversation(text, response, input_length=input_word_count)

        tts_text = sanitize_for_tts(clean_response)
        tts_chunks = tts_chunk_split(tts_text)
        response_data = {"text": clean_response, "tts_chunks": tts_chunks}
        if snr_hex:
            response_data["snr_hex"] = snr_hex
        emit("response", response_data)
        emit('state_change', {'state': 'speaking'})

        # Start tracking speech playback
        start_speech_tracking(clean_response)

        # Speak response (chunked by paragraph)
        speak_chunked(tts_text)

        # Mark speech as completed
        finish_speech()

        emit('state_change', {'state': 'idle'})

    except Exception as e:
        emit('error', {'message': str(e)})
        emit('state_change', {'state': 'idle'})


@socketio.on('document_update')
def handle_document_update(data):
    """Handle document update from frontend editor"""
    try:
        doc_id = data.get("id")
        action = data.get("action", "update")

        if not doc_id:
            emit("error", {"message": "Missing document ID"})
            return

        # Lazy-init DocumentManager
        if not hasattr(handle_document_update, "_doc_manager"):
            try:
                cue_mem_factory_lib = MAESTRO_ROOT / "cue-mem" / "lib"
                sys.path.insert(0, str(cue_mem_factory_lib))
                from document import DocumentManager
                import storage as doc_storage
                handle_document_update._doc_manager = DocumentManager(
                    cue_mem_create_fn=cue_mem_create_token if CUE_MEM_AVAILABLE else None,
                    storage_module=doc_storage
                )
            except ImportError:
                handle_document_update._doc_manager = None

        doc_mgr = handle_document_update._doc_manager
        if not doc_mgr:
            emit("error", {"message": "DocumentManager not available"})
            return

        if action == "update":
            content = data.get("content", "")
            editor = data.get("editor", "user")
            diff_summary = data.get("diff_summary", "")
            doc = doc_mgr.update_document(doc_id, content, editor, diff_summary)
            if doc:
                emit("document_update", {
                    "id": doc_id,
                    "version": doc["version"],
                    "content": doc["value"],
                    "editor": editor
                }, broadcast=True)
        elif action == "close":
            doc = doc_mgr.close_document(doc_id)
            if doc:
                emit("document_update", {
                    "id": doc_id,
                    "action": "close"
                }, broadcast=True)
        elif action == "open":
            title = data.get("title", "Untitled")
            content = data.get("content", "")
            doc = doc_mgr.create_document(doc_id, title, content)
            emit("document_update", {
                "id": doc_id,
                "action": "open",
                "title": title,
                "content": content,
                "version": 1
            }, broadcast=True)

    except Exception as e:
        print("Document update error: %s" % e)
        emit("error", {"message": str(e)})


@socketio.on('cue_dispatch')
def handle_cue_dispatch(data):
    """Handle CUE card dispatch after user approval"""
    try:
        cue_id = data.get("cue_id")
        tool = data.get("tool")
        payload = data.get("payload")
        decision = data.get("decision")

        if decision != "approved":
            print("CUE %s rejected by user" % cue_id)
            return

        print("Dispatching CUE: %s -> %s" % (cue_id, tool))
        if _audit_logger:
            _audit_logger.log("cue:dispatched", details={"cue_id": cue_id, "tool": tool})

        # Create persistent token for the cue dispatch decision
        cue_question = "CUE: %s" % (tool or "unknown")
        cue_token_id = create_yes_no_token(
            answer="Approved",
            question_context=cue_question
        )
        maybe_create_structured_sum()
        emit("token_created", {
            "token_id": cue_token_id,
            "type": "cue_dispatch",
            "label": tool or "cue",
            "value": "Approved",
            "question": cue_question
        })

        # Build cue dict for dispatcher
        cue = {
            "cue_id": cue_id,
            "tool": tool,
            "payload": payload,
            "metadata": {
                "dispatched_at": datetime.now().isoformat(),
                "dispatcher": "cue-vox"
            }
        }

        # Try to dispatch via cue-dispatcher
        dispatcher_path = MAESTRO_ROOT / "tools" / "cue-dispatcher" / "dispatch.py"
        if dispatcher_path.exists():
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(cue, f)
                cue_file = f.name

            result = subprocess.run(
                [sys.executable, str(dispatcher_path), cue_file],
                capture_output=True, text=True, timeout=10
            )

            Path(cue_file).unlink(missing_ok=True)

            if result.returncode == 0:
                print("CUE dispatched: %s" % result.stdout.strip())
                if _audit_logger:
                    _audit_logger.log("cue:completed", details={"cue_id": cue_id, "tool": tool})
            else:
                print("CUE dispatch failed: %s" % result.stderr.strip())
                if _audit_logger:
                    _audit_logger.log("cue:failed", details={"cue_id": cue_id, "tool": tool, "error": result.stderr.strip()[:200]})
        else:
            print("cue-dispatcher not found at %s" % dispatcher_path)

    except Exception as e:
        print("CUE dispatch error: %s" % e)
        if _audit_logger:
            _audit_logger.log_error("CUE dispatch error: %s" % e, context={"cue_id": cue_id})
        emit("error", {"message": str(e)})


@socketio.on('cuesheet_execute')
def handle_cuesheet_execute(data):
    """Execute a compiled cue-sheet from a document"""
    try:
        doc_id = data.get("doc_id")

        if not doc_id:
            emit("error", {"message": "Missing doc_id for cue-sheet execution"})
            return

        # Get the document
        if hasattr(handle_document_update, "_doc_manager"):
            doc_mgr = handle_document_update._doc_manager
        else:
            emit("error", {"message": "DocumentManager not initialized"})
            return

        if not doc_mgr:
            emit("error", {"message": "DocumentManager not available"})
            return

        doc = doc_mgr.get_document(doc_id)
        if not doc:
            emit("error", {"message": "Document not found: %s" % doc_id})
            return

        # Compile document to cue-sheet
        try:
            cue_mem_factory_lib = MAESTRO_ROOT / "cue-mem" / "lib"
            sys.path.insert(0, str(cue_mem_factory_lib))
            from compiler import CuesheetCompiler
        except ImportError:
            emit("error", {"message": "CuesheetCompiler not available"})
            return

        compiler = CuesheetCompiler()
        cuesheet = compiler.compile(doc)

        warnings = compiler.validate(cuesheet)
        if warnings:
            for w in warnings:
                print("Cue-sheet warning: %s" % w)

        # Execute via CuesheetExecutor
        try:
            from executors.cuesheet_executor import CuesheetExecutor
        except ImportError:
            # Try absolute path
            executor_path = Path(__file__).parent / "executors"
            sys.path.insert(0, str(executor_path))
            from cuesheet_executor import CuesheetExecutor

        dispatcher_path = MAESTRO_ROOT / "tools" / "cue-dispatcher" / "dispatch.py"
        executor = CuesheetExecutor(
            maestro_root=MAESTRO_ROOT,
            dispatcher_path=str(dispatcher_path) if dispatcher_path.exists() else None,
            token_factory=token_factory,
            emit_fn=emit
        )

        result = executor.execute(cuesheet)

        emit("cuesheet_result", result)

        # Timing token for cue-sheet execution
        now = datetime.now()
        emit("token_created", {
            "token_id": "timing_cuesheet_%d" % int(now.timestamp()),
            "type": "timing",
            "label": result.get("title", "cuesheet"),
            "value": "executed",
            "created_at": now.isoformat(),
            "temperature": 50,
            "base_temp": 50,
            "cooling_rate": 3.0,
        })

        # Emit sign-off gate to frontend
        emit("cuesheet_signoff_request", {
            "title": result.get("title", ""),
            "source_hash": result.get("source_hash", ""),
            "result_token_id": result.get("result_token_id", ""),
            "doc_id": result.get("doc_id", ""),
            "question": "Sign off on '%s'?" % result.get("title", "Untitled"),
        })

    except Exception as e:
        print("Cue-sheet execution error: %s" % e)
        emit("error", {"message": str(e)})


def _find_prior_receipt(source_hash):
    """Find most recent receipt with matching source_hash (may be expired)."""
    # Check in-memory history first
    for tid, token in input_history.items():
        if (token.get("type") == "cuesheet_receipt" and
                token.get("source_hash") == source_hash):
            return tid
    # Scan token files on disk
    for f in TOKENS_DIR.glob("*.json"):
        try:
            token = json.loads(f.read_text())
            if (token.get("type") == "cuesheet_receipt" and
                    token.get("source_hash") == source_hash):
                return token.get("token_id")
        except Exception:
            continue
    return None


@socketio.on("cuesheet_signoff_response")
def handle_cuesheet_signoff(data):
    """Handle user sign-off on a completed cue-sheet, creating a receipt token."""
    try:
        answer = data.get("answer", "")
        source_hash = data.get("source_hash", "")
        cuesheet_name = data.get("cuesheet_name", "")
        doc_id = data.get("doc_id", "")
        result_token_id = data.get("result_token_id", "")

        if not answer or not cuesheet_name:
            emit("error", {"message": "Missing answer or cuesheet_name for sign-off"})
            return

        # Check for prior receipt (re-hydration)
        prior_receipt = _find_prior_receipt(source_hash) if source_hash else None

        question_text = "Sign off on '%s'?" % cuesheet_name

        if token_factory is not None:
            receipt_label = "receipt_%s" % re.sub(r"[^a-z0-9_]", "", cuesheet_name.lower().replace(" ", "_"))
            token_id = token_factory.create(
                token_type="cuesheet_receipt",
                label=receipt_label,
                value=answer,
                tags=["receipt", "cuesheet", cuesheet_name],
                extra_fields={
                    "answer": answer,
                    "source_hash": source_hash,
                    "cuesheet_name": cuesheet_name,
                    "doc_id": doc_id,
                    "result_token_id": result_token_id,
                    "prior_receipt": prior_receipt or "",
                    "question": question_text,
                    "references": [result_token_id] if result_token_id else [],
                }
            )
        else:
            # Fallback: create token file directly
            ensure_tokens_dir()
            now = datetime.now()
            token_id = "receipt_%s_%d" % (
                re.sub(r"[^a-z0-9]", "", cuesheet_name.lower().replace(" ", "")),
                int(time.time())
            )
            token = {
                "token_id": token_id,
                "type": "cuesheet_receipt",
                "label": "receipt_%s" % cuesheet_name.lower().replace(" ", "_"),
                "value": answer,
                "answer": answer,
                "source_hash": source_hash,
                "cuesheet_name": cuesheet_name,
                "doc_id": doc_id,
                "result_token_id": result_token_id,
                "prior_receipt": prior_receipt or "",
                "question": question_text,
                "references": [result_token_id] if result_token_id else [],
                "created_at": now.isoformat(),
                "status": "active",
            }
            token_file = TOKENS_DIR / ("%s.json" % token_id)
            with open(token_file, "w") as f:
                json.dump(token, f, indent=2)
            input_history[token_id] = token

        emit("token_created", {
            "token_id": token_id,
            "type": "cuesheet_receipt",
            "label": cuesheet_name,
            "value": answer,
            "question": question_text,
            "source_hash": source_hash,
        })

        print("[RECEIPT] Created cuesheet_receipt for '%s': %s" % (cuesheet_name, answer))

        # Hydrate modifiers so receipt appears in pinned nav
        if _hydrate_modifiers:
            _hydrate_modifiers(MAESTRO_ROOT / ".claude" / "tokens")

    except Exception as e:
        print("cuesheet_signoff error: %s" % e)
        import traceback
        traceback.print_exc()
        emit("error", {"message": "Sign-off failed: %s" % str(e)})


@socketio.on('narrate_caption')
def handle_narrate_caption(data):
    """Read a gallery caption aloud via TTS. No Claude, no logging."""
    text = (data or {}).get("text", "").strip()
    if not text:
        return
    # Kill any current speech first
    flush_speech_queue()
    global tts_interrupted
    tts_interrupted = False
    _speech_queue.put((text, 0, False))

    def _wait_done():
        _speech_queue.join()
        socketio.emit("narration_done")

    threading.Thread(target=_wait_done, daemon=True).start()


@socketio.on('interrupt')
def handle_interrupt():
    """Stop current speech and cancel queued chunks"""
    global tts_process
    if tts_process:
        tts_process.terminate()
        tts_process = None
    flush_speech_queue()
    emit('state_change', {'state': 'idle'})


@socketio.on_error_default
def default_error_handler(e):
    """Catch-all error handler for socket events"""
    print("[DEBUG] Socket error: %s" % e)
    if _audit_logger:
        _audit_logger.log_error("Socket error: %s" % e)


@socketio.on('*')
def catch_all(event, data=None):
    """Log all incoming socket events"""
    print("[DEBUG] catch_all event: %s data: %s" % (event, str(data)[:200] if data else "None"))


@socketio.on('cuesheet_list')
def handle_cuesheet_list(data=None):
    """List available cue-sheets from cue-sheets dir + active vault volumes"""
    try:
        import yaml as _yaml

        def _parse_sheet(f, source_tag=None):
            """Parse a cuesheet yaml file into a menu entry dict."""
            with open(f) as fh:
                doc = _yaml.safe_load(fh) or {}
            cue_count = len(doc.get("cues", []) or doc.get("steps", []))
            entry = {
                "path": str(f),
                "filename": f.name,
                "name": doc.get("name", f.stem),
                "description": doc.get("description", ""),
                "icon": doc.get("icon", ""),
                "order": doc.get("order", 999),
                "input_count": len(doc.get("inputs", [])),
                "cue_count": cue_count,
                "is_child": bool(doc.get("parent")),
                "is_stub": not doc.get("description") and cue_count == 0,
            }
            if doc.get("panel"):
                entry["panel"] = doc["panel"]
            if source_tag:
                entry["source"] = source_tag
            return entry

        sheets = []
        all_sheets = []

        # --- 1. Legacy cue-sheets/ directory ---
        sheets_dir = MAESTRO_ROOT / "cue-sheets"
        if sheets_dir.is_dir():
            for f in sorted(sheets_dir.iterdir()):
                if f.suffix != ".yaml":
                    continue
                try:
                    all_sheets.append({"path": str(f), "filename": f.name})
                    entry = _parse_sheet(f)
                    if not entry["is_child"]:
                        sheets.append(entry)
                except Exception:
                    continue

        # --- 2. Active vault volumes (via Jeff) ---
        jeff_dir = MAESTRO_ROOT / "tools" / "jeff"
        active_file = jeff_dir / ".jeff-volumes-active.json"
        volumes_file = jeff_dir / "volumes.json"
        if active_file.is_file() and volumes_file.is_file():
            import json as _json
            with open(active_file) as fh:
                active_names = _json.load(fh)
            with open(volumes_file) as fh:
                volumes_cfg = {v["name"]: v for v in _json.load(fh)}

            # Also scan /Volumes/ for physical chips
            _vol_scan = Path("/Volumes")
            if _vol_scan.is_dir():
                _known = {v["path"] for v in volumes_cfg.values()}
                for _d in sorted(_vol_scan.iterdir()):
                    _hb = _d / "heartbeat.json"
                    if not _hb.is_file() or _d.name == "Macintosh HD":
                        continue
                    if str(_d) in _known:
                        continue
                    try:
                        with open(_hb) as _fh:
                            _chip = _json.load(_fh)
                        _label = _chip.get("label", _d.name)
                        _name = "sd:%s" % _label.lower()
                        volumes_cfg[_name] = {
                            "name": _name,
                            "type": "chip",
                            "path": str(_d),
                        }
                    except Exception:
                        continue

            for vol_name in active_names:
                vol = volumes_cfg.get(vol_name)
                if not vol:
                    continue
                _vp = Path(vol["path"])
                vol_path = _vp if _vp.is_absolute() else MAESTRO_ROOT / _vp
                if not vol_path.is_dir():
                    continue

                # Collect vault dirs: chips have vault-* subdirs, locals ARE the vault
                vault_dirs = []
                if vol.get("type") == "chip":
                    vault_dirs = [d for d in sorted(vol_path.iterdir())
                                  if d.is_dir() and d.name.startswith("vault-")]
                else:
                    vault_dirs = [vol_path]

                for vdir in vault_dirs:
                    # Root cue-sheet.yaml
                    root_cs = vdir / "cue-sheet.yaml"
                    if root_cs.is_file():
                        try:
                            tag = vol_name + ":" + vdir.name
                            all_sheets.append({"path": str(root_cs), "filename": root_cs.name})
                            entry = _parse_sheet(root_cs, source_tag=tag)
                            if not entry["is_child"] and not entry["is_stub"]:
                                sheets.append(entry)
                        except Exception:
                            continue

                    # Child cue-sheets in cue-sheets/ subdir
                    children_dir = vdir / "cue-sheets"
                    if children_dir.is_dir():
                        for f in sorted(children_dir.iterdir()):
                            if f.suffix != ".yaml":
                                continue
                            try:
                                all_sheets.append({"path": str(f), "filename": f.name})
                                entry = _parse_sheet(f, source_tag=vol_name + ":" + vdir.name)
                                # children stay children -- don't add to top-level
                            except Exception:
                                continue

        emit("cuesheet_list_result", {"sheets": sheets})
        emit("cuesheet_children_result", {"sheets": all_sheets})
    except Exception as e:
        print("cuesheet_list error: %s" % e)
        emit("cuesheet_list_result", {"sheets": [], "error": str(e)})


@app.route("/api/cuesheet/launch", methods=["POST"])
def api_cuesheet_launch():
    """HTTP endpoint to launch a cue-sheet by path.

    Used by maestro for auto-launch on chip activate.
    Reads the YAML, fires the announce via TTS, emits to connected clients.
    """
    from flask import request as flask_request
    data = flask_request.get_json(silent=True) or {}
    sheet_path = data.get("path", "")
    if not sheet_path:
        return {"error": "Missing path"}, 400

    p = Path(sheet_path)
    if not p.exists():
        return {"error": "Not found: %s" % sheet_path}, 404

    try:
        import yaml as _yaml
        with open(p) as fh:
            doc = _yaml.safe_load(fh) or {}

        announce_text = doc.get("announce", "")
        if announce_text:
            socketio.emit("response", {"role": "assistant", "text": announce_text, "tts_chunks": [announce_text]})
            _speech_queue.put((announce_text, 0, False))

        sheet_name = doc.get("name", p.stem)
        socketio.emit("cuesheet_launched", {"name": sheet_name, "path": str(p)})
        print("[CUESHEET] Auto-launched: %s" % sheet_name)
        return {"ok": True, "name": sheet_name, "announced": bool(announce_text)}
    except Exception as e:
        print("api_cuesheet_launch error: %s" % e)
        return {"error": str(e)}, 500


@socketio.on('cuesheet_launch')
def handle_cuesheet_launch(data):
    """Launch a cue-sheet: parse YAML, create token constellation, hydrate UI"""
    try:
        sheet_path = data.get("path")
        if not sheet_path:
            emit("error", {"message": "Missing path for cue-sheet launch"})
            return

        sheet_path = Path(sheet_path)
        if not sheet_path.exists():
            emit("error", {"message": "Cue-sheet not found: %s" % sheet_path})
            return

        import yaml as _yaml
        with open(sheet_path) as fh:
            doc = _yaml.safe_load(fh) or {}

        sheet_name = doc.get("name", sheet_path.stem)
        sheet_desc = doc.get("description", "")
        inputs = doc.get("inputs", [])
        cues = doc.get("cues", [])
        pulls = doc.get("pulls", [])
        slug = re.sub(r"[^a-z0-9-]", "", sheet_name.lower().replace(" ", "-"))
        panel_config = doc.get("panel")

        # Ensure pull data freshness before launch (collect-only, no webhooks)
        if pulls:
            ensure_script = MAESTRO_ROOT / "tools" / "zapier" / "pull" / "scripts" / "_lib.sh"
            for pull_source in pulls:
                try:
                    result = subprocess.run(
                        ["bash", "-c", "source '%s' && pull_init 2>/dev/null && pull_ensure_fresh '%s'" % (ensure_script, pull_source)],
                        capture_output=True, text=True, timeout=15,
                        cwd=str(MAESTRO_ROOT)
                    )
                    for line in result.stdout.strip().split("\n"):
                        if line.strip():
                            print("  pull:%s -- %s" % (pull_source, line.strip()))
                except Exception as e:
                    print("  pull:%s -- freshness check failed: %s" % (pull_source, e))

        # Set active track so emoji reactions inherit the buff tag
        global _active_track
        _active_track = slug

        # Build cue list string
        cue_ids = [c.get("id", "cue-%d" % i) for i, c in enumerate(cues)]
        cue_list = ", ".join(cue_ids)

        created_tokens = []

        # Helper to create token via CLI (matches buff pipeline)
        cue_mem_cli = MAESTRO_ROOT / "cue-mem" / "cli"

        def _create_token(label, value, token_type="text_input", base_temp=85, cooling_rate=None, tags=None, references=None):
            """Create token via createtoken CLI, return token_id"""
            cmd = [str(cue_mem_cli / "createtoken"), label, value, "--type", token_type, "--base-temp", str(base_temp)]
            if cooling_rate is not None:
                cmd.extend(["--cooling-rate", str(cooling_rate)])
            if tags:
                cmd.extend(["--tags", ",".join(tags)])
            if references:
                cmd.extend(["--references", references])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("Created token:"):
                        return line.replace("Created token: ", "").strip()
            except Exception as e:
                print("Token creation error: %s" % e)
            return None

        # 1. Session token
        if inputs:
            session_instruction = "Present each input to the user via the appropriate widget, then execute cues in order."
        else:
            session_instruction = "No inputs required. Execute cues in order. Do not ask the user to choose or confirm anything."
        session_value = "CUESHEET-LAUNCH SESSION\ncue-sheet: %s\ndescription: %s\ninputs: %d\ncues: %s\ninstruction: %s" % (
            sheet_name, sheet_desc, len(inputs), cue_list, session_instruction
        )
        session_id = _create_token(
            "cuesheet_session_%s" % slug, session_value,
            base_temp=95, tags=["buff-launch", "buff:%s" % slug]
        )
        if session_id:
            created_tokens.append(session_id)

        # Hold-on modifier for session
        if session_id:
            _create_token(
                "mod_holdon_session_%s" % slug,
                "hold-on modifier for %s" % session_id,
                token_type="modifier", base_temp=95,
                tags=["modifier", "hold-on", "buff:%s" % slug],
                references=session_id
            )

        # 2. Context modifier
        context_value = "CUESHEET LAUNCH\ncue-sheet: %s\nAll tokens tagged buff:%s were created from a cue-sheet launch.\nPresent inputs to user, then execute cues in order.\ncues: %s" % (
            sheet_name, slug, cue_list
        )
        _create_token(
            "mod_cuesheet_context_%s" % slug, context_value,
            token_type="modifier", base_temp=90, cooling_rate=4.0,
            tags=["modifier", "buff-context", "buff:%s" % slug],
            references=session_id
        )

        # 3. Cue tokens
        for i, cue in enumerate(cues):
            cue_id = cue.get("id", "cue-%d" % i)
            cue_obj = cue.get("objective", "")
            cue_token_id = _create_token(
                "cue_%s_%s" % (cue_id, slug), cue_obj,
                token_type="cue", base_temp=80, cooling_rate=3.0,
                tags=["cue", "buff-launch", "buff:%s" % slug],
                references=session_id
            )
            if cue_token_id:
                created_tokens.append(cue_token_id)
                _create_token(
                    "mod_holdon_cue_%s_%s" % (cue_id, slug),
                    "hold-on modifier for %s" % cue_token_id,
                    token_type="modifier", base_temp=85, cooling_rate=8.5,
                    tags=["modifier", "hold-on", "buff:%s" % slug],
                    references=cue_token_id
                )

        # 4. Cuesheet body token (skip for panel sheets -- cues are already individual tokens)
        if not panel_config:
            with open(sheet_path) as fh:
                yaml_body = fh.read()
            cuesheet_token_id = _create_token(
                "buff_cuesheet_%s" % slug, yaml_body,
                base_temp=95, tags=["buff-launch", "buff:%s" % slug]
            )
            if cuesheet_token_id:
                created_tokens.append(cuesheet_token_id)
                _create_token(
                    "mod_holdon_cuesheet_%s" % slug,
                    "hold-on modifier for %s" % cuesheet_token_id,
                    token_type="modifier", base_temp=95,
                    tags=["modifier", "hold-on", "buff:%s" % slug],
                    references=cuesheet_token_id
                )

        # 5. Refresh context
        try:
            refresh_script = MAESTRO_ROOT / "tools" / "refresh-memory.py"
            if refresh_script.exists():
                subprocess.run(["python3", str(refresh_script)], capture_output=True, timeout=15)
        except Exception:
            pass

        # 6. Hydrate modifier tokens so they appear in pinned nav
        if _hydrate_modifiers:
            _hydrate_modifiers(MAESTRO_ROOT / ".claude" / "tokens")

        # Build input definitions for frontend
        input_defs = []
        for inp in inputs:
            input_def = {
                "id": inp.get("id", ""),
                "prompt": inp.get("prompt", ""),
                "type": inp.get("type", "text"),
                "required": inp.get("required", False)
            }
            if inp.get("scale"):
                input_def["scale"] = inp["scale"]
            if inp.get("semantic_label"):
                input_def["semantic_label"] = inp["semantic_label"]
            input_defs.append(input_def)

        emit("cuesheet_launched", {
            "name": sheet_name,
            "description": sheet_desc,
            "slug": slug,
            "inputs": input_defs,
            "cues": [{"id": c.get("id", ""), "objective": c.get("objective", "")} for c in cues],
            "tokens_created": len(created_tokens),
            "panel": panel_config
        })

        # Timing token for cue-sheet launch
        now = datetime.now()
        emit("token_created", {
            "token_id": "timing_cuesheet_%d" % int(now.timestamp()),
            "type": "timing",
            "label": sheet_name,
            "value": "cuesheet",
            "created_at": now.isoformat(),
            "temperature": 50,
            "base_temp": 50,
            "cooling_rate": 3.0,
        })

        # Speak announcement if defined in YAML (direct TTS, no Claude round-trip)
        announce_text = doc.get("announce", "")
        if announce_text:
            socketio.emit("response", {"role": "assistant", "text": announce_text, "tts_chunks": [announce_text]})
            _speech_queue.put((announce_text, 0, False))

        print("[CUESHEET] Launched: %s (%d tokens created)" % (sheet_name, len(created_tokens)))

    except Exception as e:
        print("cuesheet_launch error: %s" % e)
        import traceback
        traceback.print_exc()
        emit("error", {"message": "Cue-sheet launch failed: %s" % str(e)})


@app.route('/api/speak', methods=['POST'])
def api_speak():
    """HTTP endpoint for direct TTS. Any service can POST here."""
    text = ""
    if request.is_json:
        text = (request.json or {}).get("text", "")
    else:
        text = request.form.get("text", "")
    text = text.strip()
    if not text:
        return {"ok": False, "error": "no text"}, 400
    socketio.emit("response", {"role": "assistant", "text": text, "tts_chunks": [text]})
    global tts_interrupted
    tts_interrupted = False
    _speech_queue.put((text, 0, False))
    _speech_queue.join()
    return {"ok": True}


@app.route('/api/chip/op', methods=['POST'])
def api_chip_op():
    """Log a Jeff chip operation to cue-stream.

    Called by Jeff MCP proxy after each tool call.
    Emits a token_created event with type chip_op so it appears in the stream.
    """
    data = request.json or {}
    op = data.get("op", "unknown")
    label = data.get("label", "?")
    root_color = data.get("root_color", "#888888")
    summary = data.get("summary", "")
    chip_data = data.get("chip_data", {})

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    token_id = "chip_op_%s_%s" % (op, now_iso.replace(":", "").replace("-", "")[:15])

    display_label = "%s %s" % (label, summary or op)

    socketio.emit("token_created", {
        "token_id": token_id,
        "type": "chip_op",
        "label": display_label,
        "value": op,
        "chip_op": op,
        "chip_data": chip_data,
        "root_color": root_color,
        "tags": ["chip:%s" % label.lower()],
        "temperature": 70,
        "base_temp": 50,
        "cooling_rate": 8.0,
        "created_at": now_iso,
    })
    return {"ok": True, "token_id": token_id}


@app.route('/api/c2d2/status', methods=['GET'])
def api_c2d2_status():
    """Check C2D2 mode and reachability."""
    from ollama_client import is_available
    return {
        "mode": _c2d2_mode,
        "reachable": is_available(),
    }


@app.route('/api/c2d2/mode', methods=['POST'])
def api_c2d2_mode():
    """Set C2D2 mode. POST {"mode": "off"|"auto"|"force"} or cycle if omitted."""
    global _c2d2_mode
    data = request.get_json(silent=True) or {}
    if "mode" in data and data["mode"] in ("off", "auto", "force"):
        _c2d2_mode = data["mode"]
    else:
        cycle = {"off": "auto", "auto": "force", "force": "off"}
        _c2d2_mode = cycle[_c2d2_mode]
    print("[C2D2] Mode set to %s" % _c2d2_mode)
    socketio.emit("c2d2_mode", {"mode": _c2d2_mode})
    return {"mode": _c2d2_mode}


@socketio.on('speak')
def handle_speak(data):
    """Direct TTS -- speak text without going through Claude."""
    text = data.get("text", "").strip()
    if not text:
        return
    emit("response", {"role": "assistant", "text": text, "tts_chunks": [text]})
    speak_chunked(text)


@socketio.on('arcade_game_over')
def handle_arcade_game_over(data):
    """Read final score from tokens and announce game over via TTS."""
    slug = data.get("slug", "")
    title = data.get("title", slug)
    game_tag = "arcade:%s" % slug

    # Read latest score token
    score = None
    high_score = None
    try:
        cue_mem_cli = MAESTRO_ROOT / "cue-mem" / "cli"
        result = subprocess.run(
            [str(cue_mem_cli / "listtokens"), "--format", "json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import json as _json
            tokens = _json.loads(result.stdout)
            for t in tokens:
                tags = t.get("tags") or []
                if game_tag not in tags:
                    continue
                label = t.get("label", "")
                if label == "arcade_score" and score is None:
                    try:
                        score = int(t.get("value", 0))
                    except (ValueError, TypeError):
                        pass
                if label == "arcade_high_score" and high_score is None:
                    try:
                        high_score = int(t.get("value", 0))
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        print("[ARCADE] Score read error: %s" % e)

    # Build announcement
    text = "Game over."
    if score is not None:
        text = text + " Final score: %d." % score
        if high_score is not None and score >= high_score:
            text = text + " New high score!"
    else:
        text = text + " %s session complete." % title

    emit("response", {"role": "assistant", "text": text, "tts_chunks": [text]})
    speak_chunked(text)


@socketio.on('connect')
def handle_connect():
    """Track client connection"""
    print("[DEBUG] Client connected")
    ensure_log_dir()
    timestamp = datetime.now()
    log_file = LOG_DIR / f"{timestamp.strftime('%Y-%m-%d')}.jsonl"

    entry = {
        'timestamp': timestamp.isoformat(),
        'event': 'client_connected',
        't_period': get_time_period(timestamp)
    }

    with open(log_file, 'a') as f:
        f.write(json.dumps(entry) + '\n')

    print("Client connected at %s" % timestamp.strftime("%Y-%m-%d %H:%M:%S"))
    if _audit_logger:
        _audit_logger.log("agent:session_start")

    # Hydrate modifier tokens via cue-mem plugin (if loaded)
    if _hydrate_modifiers:
        _hydrate_modifiers(MAESTRO_ROOT / ".claude" / "tokens")

    # Emit session timing token so the stream is never empty
    emit("token_created", {
        "token_id": "timing_session_%d" % int(timestamp.timestamp()),
        "type": "timing",
        "label": get_time_period(timestamp),
        "value": timestamp.strftime("%H:%M"),
        "created_at": timestamp.isoformat(),
        "temperature": 40,
        "base_temp": 40,
        "cooling_rate": 2.0,
    })


@socketio.on('disconnect')
def handle_disconnect():
    """Track client disconnection"""
    ensure_log_dir()
    timestamp = datetime.now()
    log_file = LOG_DIR / f"{timestamp.strftime('%Y-%m-%d')}.jsonl"

    entry = {
        'timestamp': timestamp.isoformat(),
        'event': 'client_disconnected',
        't_period': get_time_period(timestamp)
    }

    with open(log_file, 'a') as f:
        f.write(json.dumps(entry) + '\n')

    print(f"🔌 Client disconnected at {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    if _audit_logger:
        _audit_logger.log("agent:session_end")

    # Clean up challenge session state
    from flask import request as _freq
    _challenge_sessions.pop(_freq.sid, None)
    _pending_challenges.pop(_freq.sid, None)


if __name__ == '__main__':
    # Allow port override via environment variable (default 3000)
    port = int(os.environ.get('CUE_VOX_PORT', 3000))

    # Start background log cleanup thread
    start_log_cleanup_thread()

    # Log system startup to audit trail
    if _audit_logger:
        _audit_logger.log("system:startup", details={"port": port})

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🎙️  CUE-VOX Web Interface")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print(f"Open: http://localhost:{port}")
    print(f"Logs: {LOG_DIR} (24hr retention)")
    print()
    socketio.run(app, host='127.0.0.1', port=port, debug=False, allow_unsafe_werkzeug=True)
