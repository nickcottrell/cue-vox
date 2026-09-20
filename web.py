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
import urllib.request

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
    "Bash(python3 core/c2d2/cli.py:*)",
    "mcp__vault-hot__*",
    "mcp__vault-cold__*",
    "mcp__jeff__*",
    "mcp__legacy__*",
]

# C2D2 (local Ollama) -- available as fallback when Claude subprocess fails
_c2d2_path = MAESTRO_ROOT / "core" / "c2d2"
if str(_c2d2_path) not in sys.path:
    sys.path.insert(0, str(_c2d2_path))

# Jeff bridge -- import mcp_proxy for direct tool calls
_jeff_path = MAESTRO_ROOT / "core" / "jeff"
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

def _is_benchmark_command(text):
    """Fuzzy: any phrase that mentions the bench with a run/start/walk-ish verb. Requires
    'bench' so normal talk does not trigger it. Deliberately NOT 'the loop' -- that is the
    policy-revision ritual, a separate thing."""
    low = (text or "").lower()
    if "bench" not in low:
        return False
    return any(v in low for v in
               ("run", "start", "walk", "step", "go", "through", "do", "let", "kick", "fire", "begin"))


def _run_capability_benchmark():
    """Run the capability policy benchmark (the loop's guardrail) from the interface and
    return a short spoken/shown summary. Deterministic op: no model. Runs the SAME script
    the terminal loop runs, so there is one source of truth for the result."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "test", "benchmark_capability.py")
    try:
        proc = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              timeout=60, cwd=here)
    except Exception as exc:
        return "Couldn't run the benchmark: %s" % exc
    out = (proc.stdout or "").strip()
    summary = out.splitlines()[0] if out else "no output"
    count = summary.split(":", 1)[-1].strip() if ":" in summary else summary
    if proc.returncode == 0:
        return "Capability benchmark: %s. All green." % count
    fails = [ln.strip()[5:].strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]
    detail = "; ".join(fails[:5]) or "see logs"
    return "Capability benchmark FAILING. %s. Broken: %s" % (summary, detail)


# The bench Q&A set: a NEUTRAL topic (entropy), on purpose -- using real user context makes
# the test confusing to read. Generic questions keep the yardstick legible while still
# exercising the real production pipeline. 3 turns, basic -> reasoning -> depth. Changes
# rarely; this is the ruler, not the thing being tuned.
_BENCH_QUESTIONS = [
    "In a sentence, what is entropy?",                        # settle, chill
    "Why does entropy always win in a closed system?",       # tension, builds
    "What does entropy mean for the fate of the universe?",   # awe, peak
]


def _run_benchmark_walk():
    """Run the bench as REAL Q&A turns, swept across settings, read aloud. For each setting
    (brevity up, then down) it runs the same fixed questions through the real pipeline and
    speaks each question and its real answer -- so you HEAR how the settings change the
    response and can catch regressions. Forces Baseline for the run (that is where brevity
    bites). Self-driving. Slow by nature: real turns, opt-in via the confirm gate."""
    ctx = _pending_slow_op_ctx or {}
    brevity = ctx.get("brevity")
    global _live_mode, _expressive_mode, _register_lock
    saved_mode = (_live_mode, _expressive_mode)
    saved_lock = _register_lock
    _live_mode = bool(ctx.get("live"))
    _expressive_mode = bool(ctx.get("expressive"))

    def blip():
        emit("sfx", {"name": "ping"})
        socketio.sleep(0.25)

    def say(card, spoken):
        emit("response", {"text": card, "tts_chunks": tts_chunk_split(sanitize_for_tts(spoken))})
        speak_chunked(spoken)

    emit("bench_lock", {"locked": True})   # block submit/voice while the bench runs
    try:
        emit('state_change', {'state': 'speaking'})
        say("STARTING BENCHMARK TEST", "Starting benchmark test.")
        n = len(_BENCH_QUESTIONS)
        regs = (_VOICE_REGISTERS or {}).get("registers", {})
        for i, q in enumerate(_BENCH_QUESTIONS, 1):
            blip()
            _register_lock = 1                     # narrator asks at the chill floor
            say("Q%d/%d: %s" % (i, n, q), "Question %d. %s" % (i, q))
            emit('state_change', {'state': 'thinking'})   # thinking bed fills the gen gap
            try:
                result = _assemble_and_respond(q, brevity=brevity)
            except Exception as exc:
                result = None
                print("[BENCH] turn failed: %s" % exc, flush=True)
            answer = (result or {}).get("clean_response") or "(no answer)"
            spoken = (result or {}).get("tts_text") or answer
            # Expressive ON: climb 1->2->3 with the building questions so registers 2 and 3
            # (Mickey Mouse / Naruto) come out to tune. OFF: clamp to the chill floor (1).
            target = min(i, 3) if _expressive_mode else 1
            _register_lock = target
            reg_label = (regs.get(str(target), {}) or {}).get("label", "")
            emit('state_change', {'state': 'speaking'})
            say("A%d/%d [register %d, %s]: %s" % (i, n, target, reg_label, answer), spoken)
        say("THIS ENDS THE BENCHMARK TEST", "This ends the benchmark test.")
        emit('state_change', {'state': 'idle'})
    finally:
        _live_mode, _expressive_mode = saved_mode
        _register_lock = saved_lock              # release the pin; back to autotone
        emit("bench_lock", {"locked": False})   # always unblock, even on error
    return None


# Standard policy: any operation that takes a moment is gated behind a yes/no so the user
# can opt out before eating the wait. The confirm rides the yes/no primitive (renders, and
# lightboxes when the drawer is closed). Add a slow op here and it inherits the gate.
_pending_slow_op = None  # slug of a moment-taking op awaiting yes/no confirmation
_pending_slow_op_ctx = {}  # live slider settings captured when the op was requested

_SLOW_OPS = {
    "benchmark": {
        "confirm": "The benchmark runs the sample questions at two brevity settings and reads them aloud. It takes a few minutes. Start it?",
        "run": _run_benchmark_walk,
    },
}


def _confirm_slow_op(user_text, slug, ctx=None):
    """Ask a yes/no before running a moment-taking op (standard policy). Stashes the pending
    op AND the live settings captured at request time (ctx), so the run reflects the slider
    as it was set. handle_button_response's fast-path runs it on Yes, drops it on No."""
    global _pending_slow_op, _pending_slow_op_ctx
    op = _SLOW_OPS.get(slug)
    if not op:
        return
    _pending_slow_op = slug
    _pending_slow_op_ctx = ctx or {}
    _respond_and_speak(user_text, "[YES_NO: %s]" % op["confirm"])


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
    _emit_stage("C2D2 composing")  # honest: the local model is now generating

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


# ============================================================
# @c2d2 EVAL BENCH -- leading-sigil route straight to C2D2
# ============================================================
#
# The pinned eval-bench spec: a chassis post whose lines begin `@c2d2` bypasses
# the whole cue-vox conversational layer (no flux, no engagement, no Claude) and
# runs each line as a SEPARATE query through C2D2's live ask pipeline, in
# sequence. The raw answer is echoed verbatim WITH the substrate verb + freshness
# tier it hit, and every run mints a floored c2d2-eval token; the batch threads
# into one constellation. This is the steering wheel on the trace-first C2D2 work
# -- test a prompt (or a series) against the real substrate and crystallize the
# good compositions. The engine is `c2d2 eval` (core/c2d2/cli.py).

_C2D2_SIGIL = "@c2d2"
_C2D2_CLI = os.path.join(MAESTRO_ROOT, "core", "c2d2", "cli.py")


def _c2d2_bench_lines(text):
    """Extract the @c2d2 query lines from a post, sigil stripped, order preserved.
    A post is a bench iff at least one line starts with the sigil (case-insensitive).
    Non-@c2d2 lines are ignored -- the sigil is the whole opt-in."""
    lines = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith(_C2D2_SIGIL):
            q = stripped[len(_C2D2_SIGIL):].strip()
            if q:
                lines.append(q)
    return lines


def _emit_stage(label):
    """Push a REAL pipeline stage to the UI's reflective status line. Broadcast
    (socketio.emit, not emit) so it also works from the background stderr-reader
    thread. This is the reflective channel: C2D2's own _stage events, forwarded
    verbatim -- the UI shows what the substrate is actually doing, never a phase
    guessed from the clock."""
    try:
        socketio.emit('stage_update', {'stage': label})
    except Exception:
        pass


def _c2d2_eval_streamed(q, cid, n, total):
    """Run one `c2d2 eval` and forward its live stage stream to the UI.

    C2D2 emits its real stages (classify -> route.verb -> composing) on stderr,
    \\x1e-tagged, when C2D2_STAGE_STREAM is set. A daemon thread pumps those to
    _emit_stage while the main thread collects the JSON record on stdout -- the
    same events that drive the TUI spinner, so both surfaces read one truth.
    Returns the parsed record dict, or None."""
    import threading
    env = dict(os.environ, C2D2_STAGE_STREAM="1")
    proc = subprocess.Popen(
        ["python3", _C2D2_CLI, "eval", q, "--constellation", cid],
        cwd=MAESTRO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env)

    def _pump():
        for line in proc.stderr:
            if line.startswith("\x1e"):
                stage = line[1:].strip()
                if stage:
                    _emit_stage("%d/%d %s" % (n, total, stage))
    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    try:
        out = proc.stdout.read()
        proc.wait(timeout=150)
    except Exception:
        proc.kill()
        return None
    finally:
        t.join(timeout=1)
    lines = (out or "").strip().splitlines()
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except Exception:
        return None


def _run_c2d2_bench(text):
    """Run the @c2d2 batch and emit the echo. Owns its own emits (bypasses the
    normal turn path entirely). Each line runs in sequence through `c2d2 eval`;
    the constellation id threads the batch's eval tokens into one series. Real
    per-line stages stream to the UI as the batch runs."""
    import time
    queries = _c2d2_bench_lines(text)
    total = len(queries)
    cid = "evalbatch_%d" % int(time.time())
    emit('state_change', {'state': 'thinking'})

    blocks = ["C2D2 EVAL -- %d quer%s * constellation %s" % (
        total, "y" if total == 1 else "ies", cid)]
    for n, q in enumerate(queries, 1):
        _emit_stage("%d/%d dispatching" % (n, total))
        rec = None
        try:
            rec = _c2d2_eval_streamed(q, cid, n, total)
        except Exception as exc:
            blocks.append("\n@c2d2 %s\n-> [bench error: %s]" % (q, exc))
            continue
        if not rec:
            blocks.append("\n@c2d2 %s\n-> [no result]" % q)
            continue
        age = rec.get("freshness_age")
        fa = (" %sh" % age) if age is not None else ""
        blocks.append("\n@c2d2 %s\n-> verb: %s * freshness: %s%s\n\n%s" % (
            q, rec.get("verb", "?"), rec.get("freshness_tier", "?"), fa,
            rec.get("answer", "").strip()))
        blocks.append("-" * 40)

    echo = "\n".join(blocks)
    # Emit as a visible response, but do NOT read the raw dump aloud -- a bench is
    # a readout, not a spoken turn. A one-line spoken confirmation keeps TTS sane.
    spoken = "C2D2 eval complete. %d quer%s in constellation." % (
        len(queries), "y" if len(queries) == 1 else "ies")
    log_conversation(text, echo)
    emit("response", {"text": echo, "tts_chunks": [spoken]})
    emit('state_change', {'state': 'speaking'})
    speak_chunked(spoken)
    emit('state_change', {'state': 'idle'})


# ============================================================
# JEFF TRIAGE -- consult the substrate registry, route C2D2-first
# ============================================================

_JEFF_CLI = os.path.join(MAESTRO_ROOT, "core", "jeff", "jeff")
_triage_cmd_cache = None  # discovered once from Jeff's registry ([] = "none")


def _discover_triage_cmd():
    """Ask Jeff's toolchain registry for a triage-capable floor toolchain's
    triage command. Jeff owns the connection -- we do NOT hard-code C2D2's path;
    we read whatever floor toolchain Jeff has registered. Cached after first hit.
    """
    global _triage_cmd_cache
    if _triage_cmd_cache is not None:
        return _triage_cmd_cache or None
    _triage_cmd_cache = []  # remember "checked, none" to avoid re-probing
    try:
        r = subprocess.run([_JEFF_CLI, "toolchains", "--json", "--no-probe"],
                           cwd=MAESTRO_ROOT, capture_output=True, text=True, timeout=10)
        rows = json.loads(r.stdout)
        rows.sort(key=lambda t: 0 if t.get("tier") == "floor" else 1)  # floor first
        for tc in rows:
            if tc.get("triage_cmd"):
                _triage_cmd_cache = tc["triage_cmd"]
                print("[JEFF-TRIAGE] Floor toolchain: %s (%s)" % (
                    tc.get("name"), " ".join(tc["triage_cmd"])))
                break
    except Exception as exc:
        print("[JEFF-TRIAGE] Registry lookup failed: %s" % exc)
    return _triage_cmd_cache or None


def _jeff_triage(user_text):
    """Route C2D2-first: run the floor toolchain's triage. Returns the verdict
    dict ({handled, answer, route, ...}) or None if triage is unavailable."""
    cmd = _discover_triage_cmd()
    if not cmd or not user_text:
        return None
    try:
        r = subprocess.run(cmd + [user_text, "--json"], cwd=MAESTRO_ROOT,
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as exc:
        print("[JEFF-TRIAGE] Triage failed: %s" % exc)
    return None


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

    # -- C2D2-first: consult Jeff's floor triage before spending Claude compute.
    # If the substrate's floor toolchain can handle it deterministically, use that
    # answer and never wake the Computer. Anything it can't handle escalates. --
    if _c2d2_mode == "auto":
        verdict = _jeff_triage(raw_user_text or prompt_text)
        if verdict and verdict.get("handled") and verdict.get("answer"):
            print("[JEFF-TRIAGE] Handled by C2D2 floor (%s) -- no escalation"
                  % verdict.get("route"))
            _emit_c2d2_responded_token("triage")
            return (verdict["answer"], True)

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

# Proper-noun anchor layer -- canonical-entity correction applied at summary
# time. Same cue-mem/lib path the TokenFactory import added above. The memory
# half (snap) and the reflex half (detect unanchored) are separate modules.
PROPER_NOUNS_AVAILABLE = False
try:
    import proper_nouns as _proper_nouns
    try:
        import proper_noun_reflex as _proper_noun_reflex
    except ImportError:
        _proper_noun_reflex = None
    PROPER_NOUNS_AVAILABLE = True
    print("✓ Proper-noun anchors loaded")
except ImportError as e:
    print("⚠️  Proper-noun anchors not available: %s" % e)
    _proper_nouns = None
    _proper_noun_reflex = None

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

# Voice for the macOS `say` command.
# Empty string = use the system default voice. This is INTENTIONAL: the
# desired voice is "Siri Voice 2", and Siri voices CANNOT be addressed by
# `say -v <name>` -- they are reachable only as the system default
# (System Settings > Accessibility > Spoken Content > System Voice).
# So we leave this empty and rely on the system default being Siri Voice 2.
# Set a name here ONLY to pin a non-Siri voice (e.g. "Samantha").
# NOTE: a macOS update can reset the system default; if the voice sounds
# wrong, re-select Siri Voice 2 as the System Voice in the settings above.
TTS_VOICE = ""

# --- Speech pipeline: synth worker (text -> audio) feeds play worker (audio ->
# speakers). Two stages so the next chunk synthesizes while the current one plays;
# only the first chunk carries synth latency. FIFO, no overlaps. ---
_speech_queue = queue.Queue()   # text chunks awaiting synthesis
_play_queue = queue.Queue()     # synthesized items awaiting playback
# Generation counter: every speak_chunked call (and every flush) bumps this. Each queued
# item carries the gen it was minted under; the synth and play workers drop any item whose
# gen is stale. This kills the synth-ahead race where a chunk caught mid-synth at flush
# time lands on the play queue after the drain and would otherwise replay on the next reply.
_speech_gen = 0
# Hold gate for the barge/objection protocol: cleared = HOLD (the play worker blocks at
# the next chunk boundary, keeping remaining chunks); set = play. Distinct from
# flush (which discards). Lets a reply be paused and resumed, or flushed on a sustained.
_play_gate = threading.Event()
_play_gate.set()
# Objection resume: track the current reply's chunks + which one is playing, so an
# objection can capture the REMAINDER (what is left to say) and re-speak it on resume,
# from where it paused. Survives a discussion, unlike the raw audio queue.
_reply_chunks = []       # snapshot of the current reply's items [(kind, payload), ...]
_reply_para = []         # paragraph index per chunk (parallel to _reply_chunks)
_reply_idx = 0           # index of the chunk currently playing
_held_remainder = None   # text left to say when held (None = nothing held)
_held_said = None        # text already spoken before the hold (context for the resume)
_held_turns = 0          # child-subchannel turns taken during this hold (0 = nothing added)
_subchannel_log = []     # (user, assistant) pairs from the sidebar -> the sidebar token

# Resume vocabulary: any of these (spoken while held) exits the hold and resumes.
_RESUME_WORDS = ("resume", "continue", "keep going", "go on", "carry on", "pick up",
                 "pick it up", "unpause", "go ahead", "cancel", "nevermind", "never mind")


def _is_resume(text):
    low = (text or "").lower()
    return any(w in low for w in _RESUME_WORDS)


# AUTHORIZE: a spoken cue that lifts the hold, same as the space bar. Two tiers so natural
# speech works without false releases:
#   STRONG -- unambiguous release words that (almost) never appear in normal discussion.
#             These authorize at ANY length ("go ahead and unhold it now" -> release).
#   SOFT   -- everyday phrases that DO appear mid-sentence, so they authorize only in a
#             brief, mostly-just-the-cue utterance (<=4 words).
_AUTHORIZE_STRONG = ("unhold", "un-hold", "un hold", "release the hold", "lift the hold",
                     "drop the hold", "release", "resume", "cancel", "nevermind", "never mind")
_AUTHORIZE_SOFT = ("go ahead", "you're clear", "youre clear", "you are clear", "we're good",
                   "were good", "all set", "all good", "sounds good", "proceed", "go on",
                   "carry on", "keep going", "continue", "wrap up", "wrap it up", "clear")


def _is_authorize(text):
    low = re.sub(r"[^a-z0-9' -]", "", (text or "").strip().lower()).strip()
    if not low:
        return False
    if any(p in low for p in _AUTHORIZE_STRONG):                 # strong cue -> any length
        return True
    if len(low.split()) <= 4 and any(p in low for p in _AUTHORIZE_SOFT):  # soft -> short only
        return True
    return False

# Try to import pyttsx3 as fallback TTS engine
try:
    import pyttsx3
    _pyttsx3_available = True
except ImportError:
    _pyttsx3_available = False
    print("[TTS] pyttsx3 not installed -- no fallback TTS available")

# Primary neural voice: local Kokoro bf_isabella blend (see kokoro_voice.py).
# If it or its model is unavailable, we fall back to `say` then pyttsx3, so the
# voice never hard-fails.
try:
    import kokoro_voice
    _kokoro_available = True
except Exception as _kokoro_err:
    _kokoro_available = False
    print("[TTS] kokoro_voice import failed: %s -- using say" % _kokoro_err)

# Expressive voice mode: routes synthesis to the warm Chatterbox sidecar
# (chatterbox_server.py) for real emotional range. Off by default (heavier/slower
# than Kokoro); toggled per turn via window.VOICE.expressive. Falls back to Kokoro
# then say if the sidecar is down. See chatterbox_voice.py.
try:
    import chatterbox_voice
    _chatterbox_imported = True
except Exception as _cbx_err:
    _chatterbox_imported = False
    print("[TTS] chatterbox_voice import failed: %s" % _cbx_err)

_expressive_mode = False   # set per turn from window.VOICE.expressive
_live_mode = False         # set per turn from data.live (hands-free VAD mode)
_exaggeration = 0.6        # Chatterbox expressiveness dial for this turn

# Capability matrix: single source of truth for what each mode can do
# (capability_matrix.json + CAPABILITY_MATRIX.md). The logic lives in capability.py so the
# runnable benchmark (test/benchmark_capability.py) exercises the SAME code, not a mirror.
# web.py owns the mode globals (_live_mode / _expressive_mode) and passes them in.
import capability

_CAPABILITY_MATRIX = capability.load_matrix()
if _CAPABILITY_MATRIX is None:
    print("[MATRIX] load failed; falling back to legacy gated behavior", flush=True)

# Kept for callers that reference it; capability.py owns the actual fallback.
_GATED_BLOCKED_TAGS = capability.LEGACY_GATED_TAGS


def _active_mode():
    return capability.active_mode(_live_mode, _expressive_mode)


def _gated_mode():
    return capability.gated_mode(_CAPABILITY_MATRIX, _active_mode())


def _blocked_tags():
    return capability.blocked_tags(_CAPABILITY_MATRIX, _active_mode())


def get_mode_context():
    return capability.mode_context(_CAPABILITY_MATRIX, _expressive_mode, _live_mode)


def get_brevity_stance(brevity):
    return capability.brevity_stance(_CAPABILITY_MATRIX, _active_mode(), brevity)


# Voices: the top level (voices.json). A voice is a timbre (SID + optional neural
# blend) plus a set of registers, and each register carries its own prosody rules
# (break_scale / emphasis_scale / rate_scale / lift) so the same markup renders
# differently per register. A register can be PINNED via _register_lock so
# speak_chunked applies it exactly and skips the autotone/weight drift -- the
# "register as a set track" primitive, first used by the bench as a tuning rig.
# Edit voices.json to tune each voice, its registers, and their prosody.
def _load_voices():
    d = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(d, "voices.json")) as f:
            return json.load(f)
    except Exception:
        pass
    # Backward compat: the old flat voice_registers.json is one voice (Isabella).
    try:
        with open(os.path.join(d, "voice_registers.json")) as f:
            flat = json.load(f)
        return {"active": "isabella", "voices": {"isabella": {
            "label": "Isabella", "kind": "female", "sid": 8,
            "blend": "register-voices.bin", "blend_slots": True,
            "registers": flat.get("registers", {})}}}
    except Exception as exc:
        print("[VOICES] load failed (%s)" % exc, flush=True)
        return None

_VOICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices.json")
_VOICES = _load_voices()
_ACTIVE_VOICE = (_VOICES or {}).get("active") or "isabella"


def _save_voices():
    """Persist the in-memory voices doc back to voices.json (the flat source of truth,
    so an edit or a voice switch survives boot). Returns True on success."""
    try:
        with open(_VOICES_PATH, "w", encoding="utf-8") as fh:
            json.dump(_VOICES or {}, fh, indent=2)
            fh.write("\n")
        return True
    except OSError as exc:
        print("[VOICES] save failed (%s)" % exc, flush=True)
        return False
# Backward-compat view: the active voice's registers, in the shape the rest of the
# code (bench, _apply_register, _prosody_for_slot) already expects.
def _active_voice():
    return ((_VOICES or {}).get("voices", {}) or {}).get(_ACTIVE_VOICE, {})
_VOICE_REGISTERS = {"registers": _active_voice().get("registers", {})}
_register_lock = None  # 1/2/3 to pin that register (no autotone drift); None = autotone


def _prosody_for_slot(slot):
    """The active voice's prosody envelope for a live register slot (0..4), chosen by the
    register whose kokoro_slot is nearest. Empty dict (identity) if none defined."""
    regs = (_VOICE_REGISTERS or {}).get("registers", {}) or {}
    best, best_d = None, 99
    for r in regs.values():
        d = abs(int(r.get("kokoro_slot", 0)) - int(slot))
        if d < best_d:
            best, best_d = r, d
    return (best or {}).get("prosody", {}) or {}


def _active_register_prosody():
    """The prosody envelope of the ACTIVE register (the render state), or the nearest to
    the current slot before one is set. This is what SSML maps into."""
    regs = (_VOICE_REGISTERS or {}).get("registers", {}) or {}
    if _active_register in regs:
        return (regs.get(_active_register, {}) or {}).get("prosody", {}) or {}
    return _prosody_for_slot(kokoro_voice._register if _kokoro_available else 0)


# --- Register-as-range: SSML supplies an intensity, the register maps it into [min,max].
def _rng(v):
    """Coerce a prosody value to (min, max). A scalar s reads as (s, s) -- back-compat
    with the step-1 single-factor form."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return float(v[0]), float(v[1])
        except (TypeError, ValueError):
            return 1.0, 1.0
    try:
        s = float(v)
        return s, s
    except (TypeError, ValueError):
        return 1.0, 1.0


def _map_range(v, t):
    """Map a normalized intensity t in [0,1] into the register's [min,max] envelope."""
    lo, hi = _rng(v)
    t = max(0.0, min(1.0, t))
    return lo + t * (hi - lo)


def _emph_intensity(force):
    """Emphasis force -> intensity: reduced/none (<=1.0) -> 0, moderate (1.25) -> 0.5,
    strong (1.5) -> 1.0."""
    return max(0.0, min(1.0, (float(force) - 1.0) / 0.5))


def _rate_intensity(rate):
    """Prosody rate multiplier -> intensity: x-slow (0.7) -> 0, medium (1.0) -> 0.5,
    x-fast (1.3) -> 1.0."""
    return max(0.0, min(1.0, (float(rate) - 0.7) / 0.6))


def _break_intensity(ms):
    """Break duration -> intensity: a longer authored pause sits higher in the register's
    envelope, so dramatic registers stretch long pauses more than short ones."""
    return max(0.0, min(1.0, float(ms) / 1200.0))


def set_active_voice(voice_id):
    """Switch the live voice: point kokoro at its SID/blend and swap the register view.
    Returns True if the voice exists. Reload of the engine (if the .bin changes) happens
    inside kokoro_voice.set_voice."""
    global _ACTIVE_VOICE, _VOICE_REGISTERS
    v = ((_VOICES or {}).get("voices", {}) or {}).get(voice_id)
    if not v:
        return False
    _ACTIVE_VOICE = voice_id
    _VOICE_REGISTERS = {"registers": v.get("registers", {})}
    if _kokoro_available:
        kokoro_voice.set_voice(sid=int(v.get("sid", 8)),
                               blend=v.get("blend"),
                               use_blend=bool(v.get("blend_slots")))
    print("[VOICE] active=%s sid=%s blend=%s blend_slots=%s"
          % (voice_id, v.get("sid"), v.get("blend"), bool(v.get("blend_slots"))), flush=True)
    # Establish the register render-state for this voice: its saved choice, else the
    # first register. set_render_register is defined below; it runs fine at call time.
    regs = v.get("registers", {}) or {}
    ar = v.get("active_register")
    set_render_register(ar if ar in regs else (sorted(regs.keys())[0] if regs else "1"), "voice-init")
    return True


def _apply_register(n, apply_exag=True):
    """Derive a register's engine state onto the voice: timbre slot + prosody (+ the
    Chatterbox exaggeration, unless apply_exag is False so a caller can keep its own
    continuous dial). This is the DERIVATION half; set_render_register owns the STATE."""
    global _exaggeration
    reg = ((_VOICE_REGISTERS or {}).get("registers", {}) or {}).get(str(n))
    if not reg:
        return
    if _kokoro_available:
        kokoro_voice.set_register(int(reg.get("kokoro_slot", 0)))
        kokoro_voice.set_prosody(speed=reg.get("speed"), bright=reg.get("bright"), gain=reg.get("gain"))
        _pr = reg.get("prosody") or {}
        if "lift" in _pr:
            kokoro_voice.set_prosody(lift=_pr.get("lift"))
    if apply_exag:
        try:
            _exaggeration = float(reg.get("exaggeration", _exaggeration))
        except (TypeError, ValueError):
            pass


# === Register render-state: the ONE source of truth ========================
# Register is a RENDER STATE (Voice > Register > SSML): the engine is *in* a register
# when it synthesizes. _active_register is the single owned variable -- a key into the
# active voice's registers ("1"/"2"/"3"). Everything the engine renders with (kokoro
# slot, prosody rules, Chatterbox exaggeration) DERIVES from it via _apply_register.
# Every writer -- the bench lock, the weight lever, autotone, hold/release, the tuner,
# and (step 3) the async model manager -- goes through set_render_register, so register
# changes in exactly one place. The render just reads the derived slot at synth time.
_active_register = None       # current register key; None until first set


def _register_keys():
    ks = list(((_VOICE_REGISTERS or {}).get("registers", {}) or {}).keys())
    return sorted(ks, key=lambda k: (int(k) if str(k).isdigit() else 99, str(k)))


def _register_for_slot(slot):
    """Nearest register key to a kokoro slot (0..4), by each register's kokoro_slot."""
    regs = (_VOICE_REGISTERS or {}).get("registers", {}) or {}
    try:
        s = float(slot)
    except (TypeError, ValueError):
        s = 0.0
    best, bestd = None, 1e9
    for k, r in regs.items():
        d = abs(int(r.get("kokoro_slot", 0)) - s)
        if d < bestd:
            best, bestd = k, d
    return best


def _floor_register():
    """The register floor as a key (derived from _LIVE_REGISTER_FLOOR, a slot). None = off."""
    return _register_for_slot(_LIVE_REGISTER_FLOOR) if _LIVE_REGISTER_FLOOR is not None else None


def set_render_register(reg, reason="", apply_exag=True):
    """The SINGLE mutator for the register render-state. Accepts a register key or a slot
    (snapped to the nearest register), clamps to the floor, records _active_register, and
    applies the derived engine state. apply_exag=False lets a caller (autotone) keep its
    own continuous Chatterbox dial while still owning the discrete Kokoro register here."""
    global _active_register
    keys = _register_keys()
    if not keys:
        return None
    key = str(reg)
    if key not in keys:
        key = _register_for_slot(reg) or keys[0]
    floor = _floor_register()
    if floor and str(floor).isdigit() and str(key).isdigit() and int(key) < int(floor):
        key = floor
    _active_register = key
    _apply_register(key, apply_exag=apply_exag)
    _vlog("register", "active=%s%s" % (key, (" (" + reason + ")") if reason else ""))
    return key

# --- Auto-tone: emotion BUILDS from a breathy default -----------------------
# Each turn emits energy tokens into a decaying pool. When the pool climbs, the
# register climbs (breathy -> dramatic); when the conversation cools, it settles
# back. This is the simple stand-in for the thermal-token model; a preponderance
# of energy past threshold bumps the tone. Tunable, and later can be fed by real
# cue-mem token emission rates instead of this inline signal.
_TONE = 0.0
_prev_live = False         # tracks live-mode edge so we can floor on channel-open
_TONE_DECAY = 0.6          # prior pool cools to 60% each turn (recency-weighted)
_TONE_CAP = 9.0            # pool energy for full dramatic (high -> being loud is expensive)
_TONE_GAMMA = 2.2          # concave-up: you EARN the right to be loud; whisper dominates
_LOUD_FLOOR = 0.07         # mic RMS below this rests quiet; above spends into the pool
_LOUD_GAIN = 8.0           # how much loud speech contributes to the energy pool
_BREAK_MULT = 1.0          # pause-length multiplier: stretch/compress every <break>
_PRESSURE = 1.0            # volume pressure: gain that intensifies with the register (heat)
_LIVE_REGISTER_FLOOR = None  # apply-to-live: live register never drops below this (None = off)
_EXCITED_WORDS = (
    "wow", "amazing", "incredible", "love", "awesome", "great", "haha", "omg",
    "excited", "yes", "hilarious", "perfect", "beautiful", "brilliant", "fantastic",
)


def _turn_energy(text):
    """Cheap per-turn energy signal: exclamations, questions, excited words, length."""
    t = (text or "").strip()
    if not t:
        return 0.0
    tl = t.lower()
    e = 0.3                                    # base rate per turn
    e += t.count("!") * 0.8
    e += t.count("?") * 0.2
    e += sum(tl.count(w) for w in _EXCITED_WORDS) * 0.7
    e += min(1.0, len(t.split()) / 40.0)       # longer, more engaged turns add a little
    # Pauses EARN capital: a deliberate pause banks the budget that pays for the
    # fuller delivery after it (silence buys emphasis). Ellipses count most; the
    # comma/semicolon micro-pauses add a little.
    e += t.count("...") * 0.6
    e += tl.count(" um") * 0.3 + tl.count(" well,") * 0.2
    e += (t.count(",") + t.count(";")) * 0.05
    return e


def _say_with_fallback(text, timeout=30):
    """Speak text. Prefer Kokoro (Isabella); fall back to macOS say, then pyttsx3."""
    if _kokoro_available:
        try:
            if kokoro_voice.speak(text):
                return
        except Exception as e:
            print("[TTS] kokoro speak error: %s -- falling back to say" % e)
    try:
        start = time.time()
        cmd = ["say", "-v", TTS_VOICE, text] if TTS_VOICE else ["say", text]
        subprocess.run(cmd, check=False, timeout=timeout)
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


def _vlog(stage, msg=""):
    """One greppable, consistent server console tag mirroring the browser's
    cvx:<stage> logger, so a single turn is legible on BOTH sides of the pipeline.
    Stages: turn -> tone -> register -> reply -> synth -> play (+ pkg/apply)."""
    print("cvx:%-8s %s" % (stage, msg), flush=True)


def _render_ssml_to_wav(text):
    """Render one SSML chunk into a single wav via the shared engine: each say-span
    synthed with its own register/gain/speed/lift, pauses spliced as silence, cues as
    prebaked clips, all concatenated. Plain text (no tags) takes the cheap flat path.
    Returns a wav path (the play worker deletes it) or None. This is what makes the
    agent's SSML actually shape the LIVE delivery, not just the tuner."""
    import tempfile
    base = kokoro_voice._register            # the turn's base register slot (derived from state)
    # The active register's prosody ENVELOPE: same markup, mapped into this register's
    # [min,max] ranges (Voice > Register > SSML). break/emphasis/rate flow into
    # resolve_instructions as ranges; lift is set here from the register's lift envelope.
    pr = _active_register_prosody()
    if "lift" in pr:
        kokoro_voice.set_prosody(lift=_map_range(pr.get("lift"), 0.5))   # register baseline rise
    tone = {"pressure": _PRESSURE, "break_mult": _BREAK_MULT, "prosody": pr}
    instr = resolve_instructions(text, base, tone)
    if len(instr) == 1 and "say" in instr[0]:    # plain span -> flat synth
        return kokoro_voice.synth_to_file(instr[0]["say"], question=instr[0].get("lift"))
    parts = []
    for it in instr:
        if "say" in it:
            kokoro_voice.set_register(it["register"])
            kokoro_voice.set_prosody(speed=it["speed"], gain=it["gain"])
            p = kokoro_voice.synth_to_file(it["say"], question=it.get("lift"))
            if p:
                parts.append(p)
        elif "pause_ms" in it:
            fd, sp = tempfile.mkstemp(suffix=".wav", prefix="cvx-pause-"); os.close(fd)
            _silence_wav(int(it["pause_ms"]), sp)
            parts.append(sp)
        elif "beat_ms" in it:
            fd, bp = tempfile.mkstemp(suffix=".wav", prefix="cvx-beat-"); os.close(fd)
            _texture_wav(int(it["beat_ms"]), bp)
            parts.append(bp)
        elif "cue" in it:
            cue = os.path.join(_SFX_DIR, "%s-%d.wav" % (it["cue"], max(0, min(4, base))))
            if not os.path.exists(cue):
                cue = os.path.join(_SFX_DIR, "%s.wav" % it["cue"])
            if os.path.exists(cue):
                parts.append(cue)
    kokoro_voice.set_register(base)           # restore for the next chunk
    if not parts:
        return None
    if len(parts) == 1 and not parts[0].startswith(_SFX_DIR):
        return parts[0]
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="cvx-ssml-"); os.close(fd)
    _concat_wavs(parts, out)
    for p in parts:
        if not p.startswith(_SFX_DIR):
            try:
                os.remove(p)
            except OSError:
                pass
    return out


def _render_expressive_to_wav(text, exaggeration):
    """Expressive (Chatterbox) render that still honors beats/pauses/cues. Chatterbox
    cannot parse SSML, so we resolve the chunk to ops, synth each contiguous run of speech
    as one Chatterbox call (so the voice stays smooth), and splice beat textures / silence /
    cue clips between the runs. Beat-free chunks take the cheap flat path (one synth call),
    identical to before. All engines emit 24kHz/16k mono, so the splices concat directly."""
    import tempfile
    ops = resolve_instructions(text, 0, {"pressure": _PRESSURE, "break_mult": _BREAK_MULT})
    if not any(("beat_ms" in o or "pause_ms" in o or "cue" in o) for o in ops):
        return chatterbox_voice.synth_to_file(strip_markdown_for_tts(text), exaggeration=exaggeration)
    parts, buf = [], []

    def _flush_say():
        span = " ".join(s.strip() for s in buf if s.strip())
        buf.clear()
        if span:
            p = chatterbox_voice.synth_to_file(span, exaggeration=exaggeration)
            if p:
                parts.append(p)

    for o in ops:
        if "say" in o:
            buf.append(o["say"])
        elif "beat_ms" in o:
            _flush_say()
            fd, bp = tempfile.mkstemp(suffix=".wav", prefix="cvx-beat-"); os.close(fd)
            _texture_wav(int(o["beat_ms"]), bp); parts.append(bp)
        elif "pause_ms" in o:
            _flush_say()
            fd, sp = tempfile.mkstemp(suffix=".wav", prefix="cvx-pause-"); os.close(fd)
            _silence_wav(int(o["pause_ms"]), sp); parts.append(sp)
        elif "cue" in o:
            _flush_say()
            cue = os.path.join(_SFX_DIR, "%s.wav" % o["cue"])
            if os.path.exists(cue):
                parts.append(cue)
    _flush_say()
    if not parts:
        return None
    if len(parts) == 1 and not parts[0].startswith(_SFX_DIR):
        return parts[0]
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="cvx-ssml-"); os.close(fd)
    _concat_wavs(parts, out)
    for p in parts:
        if not p.startswith(_SFX_DIR):
            try:
                os.remove(p)
            except OSError:
                pass
    return out


def _synth_worker():
    """Stage 1: pull text chunks and synthesize them AHEAD of playback.

    Kokoro synth produces a temp wav that is handed to the play queue; if Kokoro
    is unavailable the chunk is deferred to the play stage as a `say` item. Running
    ahead means chunk N+1 is being synthesized while chunk N is still playing, so
    only the first chunk carries any synth wait.
    """
    while True:
        item = _speech_queue.get()
        if item is None:
            _play_queue.put(None)          # forward the poison pill
            _speech_queue.task_done()
            break
        try:
            seg_kind, payload, chunk_index, emit_events, gen = item
            if gen != _speech_gen:
                # Stale item from a flushed/superseded speech session -> drop, do not synth.
                pass
            elif tts_interrupted:
                _play_queue.put(("skip", None, chunk_index, emit_events, gen))
            elif seg_kind == "clip":
                # A [laugh]/[chuckle] marker -> play a prebaked in-voice clip, no synth.
                _play_queue.put(("clip", payload, chunk_index, emit_events, gen))
            else:
                wav = None
                _preview = (payload or "")[:40].replace("\n", " ")
                # Expressive mode -> warm Chatterbox sidecar (real emotional range).
                if _expressive_mode and _chatterbox_imported:
                    _vlog("synth", "chunk %d engine=chatterbox exag=%.2f  \"%s\"" % (chunk_index, _exaggeration, _preview))
                    try:
                        wav = _render_expressive_to_wav(payload, _exaggeration)
                    except Exception as e:
                        print("[TTS] chatterbox synth error: %s -- falling back" % e)
                # Default (or fallback) -> fast local Kokoro, rendering the chunk's SSML
                # per-span (register/gain/speed/lift + pauses + cues), not flat.
                if wav is None and _kokoro_available:
                    _vlog("synth", "chunk %d engine=kokoro (ssml) base_reg=%d  \"%s\""
                          % (chunk_index, kokoro_voice._register, _preview))
                    try:
                        wav = _render_ssml_to_wav(payload)
                    except Exception as e:
                        print("[TTS] kokoro synth error: %s -- deferring to say" % e)
                # Re-check gen: synth can take a beat, and a flush may have landed meanwhile.
                # This is the exact race that used to replay a chunk on the next reply.
                if gen != _speech_gen:
                    if wav:
                        try:
                            os.remove(wav)
                        except OSError:
                            pass
                elif wav:
                    _play_queue.put(("wav", wav, chunk_index, emit_events, gen))
                else:
                    _play_queue.put(("say", payload, chunk_index, emit_events, gen))
        except Exception as e:
            print("[TTS SYNTH ERROR] %s -- item dropped" % e)
        finally:
            _speech_queue.task_done()


def _play_worker():
    """Stage 2: play synthesized items in order, one at a time (no overlap)."""
    while True:
        # HOLD point: block before taking the next chunk while the gate is held, so a
        # barge holds the reply at a clean chunk boundary (current chunk already played).
        _play_gate.wait()
        item = _play_queue.get()
        if item is None:
            _play_queue.task_done()
            break
        kind, payload, chunk_index, emit_events, gen = item
        try:
            if gen != _speech_gen and kind == "wav" and payload:
                # Stale audio from a flushed/superseded session -> discard the wav, do not
                # play it. (This is what used to replay on resume.)
                try:
                    os.remove(payload)
                except OSError:
                    pass
            elif not tts_interrupted and gen == _speech_gen and kind != "skip":
                if emit_events:
                    global _reply_idx
                    _reply_idx = chunk_index      # for remainder capture on objection
                    socketio.emit("tts_chunk_start", {"index": chunk_index})
                    socketio.sleep(0.05)
                if kind in ("wav", "clip"):
                    subprocess.run(["afplay", payload], check=False)
                else:  # "say" fallback -- synth and play together
                    _say_with_fallback(strip_markdown_for_tts(payload))   # say can't render SSML
                # Chunk audio ended. If the next paragraph is not ready yet, the client
                # fills that between-paragraph gap with the synthing ambience (Amex-style).
                if emit_events and not tts_interrupted:
                    socketio.emit("tts_chunk_ended", {"index": chunk_index})
        except Exception as e:
            print("[TTS PLAY ERROR] %s" % e)
        finally:
            if kind == "wav" and payload:
                try:
                    os.remove(payload)
                except OSError:
                    pass
            _play_queue.task_done()
        # Drain remaining audio if interrupted (delete pending wavs).
        if tts_interrupted:
            while not _play_queue.empty():
                try:
                    it = _play_queue.get_nowait()
                    if it and it[0] == "wav" and it[1]:
                        try:
                            os.remove(it[1])
                        except OSError:
                            pass
                    _play_queue.task_done()
                except queue.Empty:
                    break
            if emit_events:
                socketio.emit("tts_chunk_done")


# Start the two pipeline stages
_synth_thread = threading.Thread(target=_synth_worker, daemon=True)
_synth_thread.start()
_play_thread = threading.Thread(target=_play_worker, daemon=True)
_play_thread.start()


def flush_speech_queue():
    """Kill current speech and drain the queue. Call this instead of killall say."""
    global tts_interrupted, _speech_gen
    tts_interrupted = True
    _speech_gen += 1              # anything in flight is now stale; workers will drop it
    # Kill any running say process and any Kokoro playback (afplay)
    subprocess.run(["killall", "say"], stderr=subprocess.DEVNULL)
    subprocess.run(["killall", "afplay"], stderr=subprocess.DEVNULL)
    # Drain pending text chunks
    while not _speech_queue.empty():
        try:
            _speech_queue.get_nowait()
            _speech_queue.task_done()
        except queue.Empty:
            break
    # Drain pending audio (delete any already-synthesized wavs)
    while not _play_queue.empty():
        try:
            it = _play_queue.get_nowait()
            if it and it[0] == "wav" and it[1]:
                try:
                    os.remove(it[1])
                except OSError:
                    pass
            _play_queue.task_done()
        except queue.Empty:
            break


def hold_speech():
    """Barge HOLD: pause playback at the next chunk boundary, keeping remaining chunks.
    The current chunk finishes; the rest wait. Unlike flush, nothing is discarded."""
    _play_gate.clear()


def resume_speech():
    """OVERRULED / resume-with-context: release a HOLD so held chunks play again."""
    _play_gate.set()


def flush_held():
    """SUSTAINED: drop the held remainder and clear the hold (the user has the floor)."""
    _play_gate.set()
    flush_speech_queue()


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
    """Extract the SNR self-assessment tag from the end of a response.

    The prompt asks for [SNR: XX], but the model drifts to <SNR: XX>. Tolerate both
    bracket styles so the tag is always pulled out of the presence channel: an
    un-extracted tag leaks BOTH ways -- no dot in the UI and the tag spoken aloud.
    Returns (clean_text, snr_value) -- snr_value is int 0-100 or None.
    """
    pattern = r"[\[<]\s*SNR:\s*(\d{1,3})\s*[\]>]\s*$"
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


def strip_markdown_only(text):
    """Clean markdown/emoji/ALL-CAPS for TTS but LEAVE SSML tags intact, so the SSML
    renderer can shape delivery. (strip_markdown_for_tts also removes SSML, for the
    plain/expressive paths that cannot render it.)"""
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
    # Emoji / pictographs read as noise (or letter-spelling) in neural TTS -- drop them.
    text = re.sub(
        r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U00002B00-\U00002BFF️]",
        "", text,
    )
    # ALL-CAPS words (2+ letters) get spelled out or over-emphasized by neural TTS.
    # Lowercase them so "HELL YES" speaks as words, not letters.
    text = re.sub(r"\b[A-Z]{2,}\b", lambda m: m.group(0).lower(), text)
    # Collapse whitespace left by removals.
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def strip_markdown_for_tts(text):
    """As strip_markdown_only, plus removes SSML tags (for the plain/expressive paths
    that speak the words directly and cannot render SSML)."""
    return re.sub(r"</?[a-zA-Z][^>]*>", "", strip_markdown_only(text)).strip()


def _strip_bracket_balanced_tags(text, tag_types):
    """Remove [TAG: ...] blocks. Delegates to capability.strip_blocked_tags so the pipeline
    and the benchmark probes run the exact same strip (single source of truth)."""
    return capability.strip_blocked_tags(text, tag_types)


# === P3a: prosody as triggers ("set the table") =============================
# The [CUE: ...] tag is a CONTROL-channel tag: never spoken (already stripped for
# TTS), it fires a side effect so the surface is prepared for what is being
# discussed. P3a ships the READ tier only -- warm/recall/heat -- which mutates no
# durable state (get_token re-times decay, a read-tier effect) and so needs no
# gate. Write/act verbs (mint/createtoken/stage/dispatch) are DEFERRED here: they
# need the GATE + a user-action anchor (P3b/P3c). See docs/design/prosody-as-triggers.md.
_CUE_READ_VERBS = ("warm", "recall", "heat")
_CUE_WRITE_VERBS = ("mint", "createtoken", "stage", "dispatch")
_last_cue = None   # last turn's fired-trigger result, for the faint UI "table set" marker


def cue_export_midi(cues, contour):
    """SEAM (deferred by design): the handoff from the upstream text layer to the
    TERMINUS composition layer -- the endpoint/device/actuator where a performance is
    rendered in realtime. MIDI is reserved for that edge and justified by SCALE: many
    channels moving together with continuous variation and waves, like music (24-64
    notes, or Disney animatronics driving dozens of servos). A single voice is the
    degenerate case that inline [CUE: ...] + register dials already cover. Stays a
    stub until a multi-channel endpoint sits on the other side; plugs in HERE and
    nowhere else. See docs/design/prosody-as-triggers.md (Encoding)."""
    raise NotImplementedError(
        "MIDI cue export is a deferred seam; see docs/design/prosody-as-triggers.md")


def _parse_cue_triggers(text):
    """Pull [CUE: verb k=v ...] control-channel triggers out of a response. Returns a
    list of {verb, args, raw}. Read verbs use simple k=v args; the tag is never spoken."""
    out = []
    for m in re.finditer(r"\[CUE:\s*([^\]]+)\]", text or ""):
        parts = m.group(1).strip().split()
        if not parts:
            continue
        args = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                args[k.strip()] = v.strip()
        out.append({"verb": parts[0].lower(), "args": args, "raw": m.group(1).strip()})
    return out


def _heat_token(tid):
    """Re-heat a token by reading it back (get_token re-times decay). Read tier."""
    if not (tid and CUE_MEM_AVAILABLE):
        return False
    try:
        return cue_mem_get_token(tid, apply_heat=True) is not None
    except Exception:
        return False


def _warm_topic(topic):
    """Pre-warm the next turn: read (and thereby heat) tokens matching the topic.
    Read tier -- no durable mutation. Returns the matched token dicts."""
    if not (topic and CUE_MEM_AVAILABLE):
        return []
    try:
        toks = cue_mem_list_tokens() or []
    except Exception:
        return []
    key = topic.replace("-", " ").replace("_", " ").lower().strip()
    words = [w for w in key.split() if w]
    hits = []
    for t in toks:
        blob = " ".join(str(t.get(k, "")) for k in
                        ("value", "label", "type", "tags", "token_id")).lower()
        if words and all(w in blob for w in words):
            tid = t.get("token_id")
            if tid:
                _heat_token(tid)
            hits.append(t)
    return hits


def _fire_cue_triggers(triggers):
    """Fire the READ tier only. Warm/recall pre-warm context; heat re-heats a token.
    Write/act verbs are recorded as deferred (never fired in P3a). Returns
    {warmed:[...], deferred:[...]}."""
    warmed, deferred = [], []
    for t in triggers:
        v, a = t["verb"], t["args"]
        if v in ("warm", "recall"):
            topic = a.get("topic") or a.get("t") or ""
            hits = _warm_topic(topic)
            warmed.append({"verb": v, "topic": topic, "n": len(hits),
                           "tokens": [h.get("token_id") for h in hits if h.get("token_id")][:5]})
        elif v == "heat":
            tid = a.get("token") or a.get("id") or ""
            warmed.append({"verb": "heat", "token": tid, "ok": _heat_token(tid)})
        elif v in _CUE_WRITE_VERBS:
            deferred.append({"verb": v, "why": "needs GATE + user-action anchor (P3b/P3c)"})
        else:
            deferred.append({"verb": v, "why": "unknown cue verb"})
    return {"warmed": warmed, "deferred": deferred}


def sanitize_for_tts(text):
    """
    Sanitize text for TTS by extracting question text from structured input tags.
    Prevents TTS from trying to speak raw tags like [YES_NO: ...] or [INPUT: {...}]
    """
    # Strip SNR and citation blocks (metadata only, never spoken)
    text, _ = extract_snr(text)
    text, _ = extract_citations(text)

    # Strip visual-only tags (rendered as widgets, never spoken).
    # PIN_NOTE / PIN_NINJA are structured handoffs to fast-paths -- never spoken.
    text = _strip_bracket_balanced_tags(text, ("GALLERY", "APPROVAL", "DOCUMENT", "CUE", "PIN_NOTE", "PIN_NINJA"))


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
        return result_text

    # No structured tags found, return original text
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


# Spliced-in clips for [laugh]/[chuckle] markers -- an in-voice, deliberately
# robotic "circuits laugh" rather than a faked human one. Swap the wavs to retune.
_SFX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "sfx")
_LAUGH_RE = re.compile(r"\[(laugh|chuckle)\]", re.IGNORECASE)


def _clip_for(name):
    """Resolve [laugh]/[chuckle] to a signature cue derived from the voice. In
    expressive mode the cue is keyed to the current register (dial index 0..4);
    otherwise the Isabella-default cue. A functional quantized voice->sound map."""
    if _expressive_mode:
        style = max(0.0, min(1.0, _exaggeration - 1.0))
        idx = int(round(style * 4))
        p = os.path.join(_SFX_DIR, "%s-%d.wav" % (name, idx))
        if os.path.exists(p):
            return p
    p = os.path.join(_SFX_DIR, "%s.wav" % name)
    return p if os.path.exists(p) else None


def _split_laugh_segments(text):
    """Split reply text into ('text', str) and ('clip', wav_path) segments on
    [laugh]/[chuckle] markers. A missing clip just drops the marker."""
    segments = []
    text = text or ""
    last = 0
    for m in _LAUGH_RE.finditer(text):
        pre = text[last:m.start()]
        if pre.strip():
            segments.append(("text", pre))
        clip = _clip_for(m.group(1).lower())
        if clip:
            segments.append(("clip", clip))
        last = m.end()
    tail = text[last:]
    if tail.strip():
        segments.append(("text", tail))
    return segments


def _reply_weight(text):
    """A 0..1 measure of how expressive a reply is, read from its OWN real signals
    (emphasis, exclamation, questions, length). This is an authentic lever: a heavier
    line is spoken deeper AND legitimately takes longer to synthesize, so the extra
    expressive latency becomes the sound of weight gathering, not lag. The same scalar
    previews the gap ambience on the client."""
    t = text or ""
    if not t.strip():
        return 0.0
    w = 0.0
    w += min(t.count("!"), 4) * 0.14                                   # exclamation
    w += 0.08 if "?" in t else 0.0                                     # a question
    w += min(len(re.findall(r"\b[A-Z][A-Z']{2,}\b", t)), 4) * 0.11     # SHOUTED words
    w += min(len(re.findall(r"\*\*?[^*]+\*\*?", t)), 4) * 0.10         # *emphasis*
    w += min(len(t) / 420.0, 1.0) * 0.24                               # weightier length
    w += min(sum(t.lower().count(x) for x in _EXCITED_WORDS), 5) * 0.05
    return max(0.0, min(1.0, w))


def speak_chunked(text):
    """Queue a reply for speech: text chunks synthesize, [laugh]/[chuckle] markers
    splice in prebaked clips. Two-stage pipeline, FIFO, no overlap; interrupt via
    flush_speech_queue()."""
    global tts_interrupted, _exaggeration, _speech_gen
    tts_interrupted = False
    _speech_gen += 1               # new speech session; items below carry this gen
    gen = _speech_gen
    items = []
    para_of = []   # paragraph index per item, so resume can start at a paragraph boundary
    paragraphs = re.split(r'\n\s*\n', text) if text else [text or ""]
    for pidx, para in enumerate(paragraphs):
        last_text_i = None
        for seg_kind, seg in _split_laugh_segments(para):
            if seg_kind == "clip":
                items.append(("clip", seg)); para_of.append(pidx)
            else:
                for chunk in tts_chunk_split(seg):
                    # Keep SSML in the stored payload; the engine renderer strips per span
                    # (Kokoro: _render_ssml_to_wav; expressive: _render_expressive_to_wav).
                    # Both honor <beat/>, so the rest lands in either mode.
                    clean = strip_markdown_only(chunk)
                    if clean:
                        items.append(("text", clean)); para_of.append(pidx)
                        last_text_i = len(items) - 1
        # A beat between paragraphs: ride it onto the paragraph's last spoken chunk so it
        # blends into whatever synth latency follows (same texture as the client gap fill).
        if pidx < len(paragraphs) - 1 and last_text_i is not None:
            k, payload = items[last_text_i]
            items[last_text_i] = (k, payload + ' <beat time="%dms"/>' % _PARA_BEAT_MS)
    if not items:
        return
    _n_clips = sum(1 for k, _ in items if k == "clip")
    _vlog("reply", "%d chars -> %d chunks over %d paragraphs (%d spoken, %d cues)"
          % (len(text or ""), len(items), (para_of[-1] + 1 if para_of else 0),
             len(items) - _n_clips, _n_clips))
    global _reply_chunks, _reply_para, _reply_idx
    _reply_chunks = list(items)   # snapshot so an objection can capture the remainder
    _reply_para = para_of
    _reply_idx = 0
    # Authentic weight lever: heavier lines speak deeper (fuller register + more
    # Chatterbox exaggeration) and take longer to synth. Broadcast the weight BEFORE
    # synth so the client's gap ambience previews it and crossfades into the voice.
    w = _reply_weight(text)
    if _register_lock is not None:
        # A register is PINNED (bench tuning rig / register track): set it exactly and
        # skip the weight/autotone drift, so the deliberate register does not wobble.
        set_render_register(_register_lock, "lock")
    else:
        # Weight lever: heavier lines climb the register (register is the render state,
        # so climbing = stepping up register keys). Its exaggeration derives from there.
        keys = _register_keys()
        cur = _active_register if _active_register in keys else _register_for_slot(
            kokoro_voice._register if _kokoro_available else 0)
        if cur in keys:
            idx = min(len(keys) - 1, keys.index(cur) + int(round(w)))
            set_render_register(keys[idx], "weight")
    try:
        socketio.emit("voice_weight", {"weight": round(w, 3), "expressive": bool(_expressive_mode)})
    except Exception:
        pass
    _vlog("weight", "reply weight=%.2f -> reg=%s exag=%.2f (expressive=%s)"
          % (w, kokoro_voice._register if _kokoro_available else "-", _exaggeration, _expressive_mode))
    for i, (seg_kind, payload) in enumerate(items):
        _speech_queue.put((seg_kind, payload, i, True, gen))
    # Wait for all chunks to synthesize, then for all audio to finish playing.
    _speech_queue.join()
    _play_queue.join()
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

# --- Cache-in gate: a dropped SVG bucket's field, ruled and (on SUSTAINED) minted ------
# The SVG is a TEMPLATE, never the minter. A valid submit runs cache_in.py
# (scrub -> validate -> discover coord -> mint into the SHARED pool) and the receipt rides
# back on gate_ruling. Reuses the existing client cue-card (gate_challenge / gate_answer /
# gate_ruling). Does NOT touch the barge/hold arc below.
# Spec: docs/design/party-in-a-bucket-spec.md
try:
    import cache_in as _cache_in_mod
    CACHE_IN_OK = True
    print("✓ cache-in gate available (%s)" % _cache_in_mod.status())
except Exception as _e:
    _cache_in_mod = None
    CACHE_IN_OK = False
    print("⚠️  cache-in unavailable: %s" % _e)

import form_walk       # the Live-mode form walker: a turn-based cursor over gate.py
import form_discovery  # drop a URL -> a manifest of the page's forms

_pending_gates = {}   # gate_id -> {node, template}


@socketio.on("gate_open")
def handle_gate_open(data=None):
    """Drop a bucket: present ONE node's field as the cue card. data = {template, node_id?}.
    The template is the SVG metadata (untrusted). We only present the prompt here; the
    ruling + mint happen on gate_answer."""
    from flask_socketio import emit
    data = data or {}
    template = data.get("template") or {}
    nodes = template.get("nodes") or []
    nid = data.get("node_id") or template.get("entry")
    node = next((n for n in nodes if n.get("id") == nid), None) if nid else None
    if node is None and nodes:
        node = nodes[0]
    if node is None:
        emit("gate_ruling", {"sustained": False, "reason": "no node in template"})
        return
    gid = "gate_%d" % int(time.time() * 1e6)
    _pending_gates[gid] = {"node": node, "template": template}
    prompt = node.get("prompt") or (node.get("field") or {}).get("placeholder") or node.get("title") or ""
    emit("gate_challenge", {"gate_id": gid, "prompt": prompt, "node_id": node.get("id")})


@socketio.on("gate_answer")
def handle_gate_answer(data=None):
    """A valid item caches in. data = {gate_id, response}. On SUSTAINED, mint via cache_in
    and return the receipt; the client paints 'cached' and (in LIVE) releases the hold."""
    from flask_socketio import emit
    data = data or {}
    ctx = _pending_gates.pop(data.get("gate_id"), None)
    if not ctx:
        emit("gate_ruling", {"sustained": False, "reason": "no such gate"})
        return
    if not CACHE_IN_OK:
        emit("gate_ruling", {"sustained": False, "reason": "cache-in unavailable"})
        return
    receipt = _cache_in_mod.cache_in(ctx["node"], data.get("response", ""), ctx["template"])
    if receipt.get("cached"):
        emit("gate_ruling", {"sustained": True, "preponderance": receipt.get("preponderance"),
                             "receipt": receipt, "next_id": ctx["node"].get("next")})
    else:
        emit("gate_ruling", {"sustained": False, "preponderance": receipt.get("preponderance", 0.0),
                             "reason": receipt.get("reason")})


@socketio.on("walk_form")
def handle_walk_form(data=None):
    """A paired browser points cue-vox at a page's parsed form (readForm.js). data =
    {title, fields:[...]}. Live Mode walks it by voice via form_walk: each valid field
    emits walk_fill so the browser fills the real DOM field, and done emits walk_ready so
    the browser reveals the filled form. cue-vox NEVER submits: the human presses the
    page's own submit button. 100% user-gated on the actual submit, by construction."""
    from flask_socketio import emit
    data = data or {}
    fields = data.get("fields") or []
    if not fields:
        emit("walk_ready", {"valid": False, "reason": "no fields"})
        return
    intent = form_walk.start(fields, title=data.get("title"))
    if not intent.get("ok", True):
        emit("walk_ready", {"valid": False, "reason": intent.get("reason")})
        return
    say = intent.get("say", "")
    emit("response", {"text": intent.get("card", say),
                      "tts_chunks": tts_chunk_split(sanitize_for_tts(say))})
    emit("state_change", {"state": "speaking"})
    speak_chunked(say)
    emit("state_change", {"state": "idle"})


@app.route("/api/forms", methods=["POST"])
def api_forms():
    """Drop a URL, get a manifest of the page's forms. Body: {url}. Server-side fetch (so
    the target site's CORS does not block it) and static parse. Read-only. The manifest is
    the concrete surface: how many forms, what they are, and each submittable one's fields.
    Filling a live page's form is the browser plugin's job (form_walk + the walk_form bridge)."""
    from flask import request
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "no url"}, 400
    try:
        manifest = form_discovery.discover(url)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        return {"ok": False, "error": "could not fetch or parse: %s" % e}, 502
    manifest["ok"] = True
    return manifest


# --- Barge / objection protocol (v-now: WAIT / cancel / chat) ---------------------
# OBJECT ("WAIT": voice barge or SPACE during speaking) -> HOLD the reply at the next
# chunk boundary. Then:
#   CANCEL (space bar or "cancel") -> resume the held reply.
#   CHAT   ("chat")                -> hold the conversation: drop the remainder, yield the
#                                     floor to the user (they take a turn).
# The gate/form primitive (gate.py) stays available for walkable forms; the barge no
# longer routes through the math gate.
def _hold_split():
    """Split the current reply at the hold point: (said, remainder). `said` is what was
    already spoken; `remainder` is from the START of the paragraph that was playing (so
    resume picks up at the top of the interrupted paragraph, not mid-sentence)."""
    if not _reply_chunks:
        return "", ""
    idx = min(_reply_idx, len(_reply_chunks) - 1)
    para = _reply_para[idx] if idx < len(_reply_para) else 0
    start = next((i for i, pp in enumerate(_reply_para) if pp == para), idx)
    said = " ".join(p for k, p in _reply_chunks[:start] if k == "text").strip()
    remainder = " ".join(p for k, p in _reply_chunks[start:] if k == "text").strip()
    return said, remainder


def _remainder_text():
    return _hold_split()[1]


@socketio.on("object")
def handle_object(data=None):
    """WAIT: hold the reply. Capture what was already said + the remainder, and stop the
    audio now, so the child subchannel can run; release regenerates from the remainder."""
    from flask_socketio import emit
    global _held_remainder, _held_said, _held_turns, _subchannel_log
    if _held_remainder is None:                # first objection captures the original
        said, remainder = _hold_split()
        _held_remainder = remainder or None
        _held_said = said or None
        _held_turns = 0                        # fresh hold: no subchannel turns yet
        _subchannel_log = []                   # fresh sidebar
    flush_speech_queue()                       # stop the reply now
    _vlog("barge", "OBJECT -> HOLD (said %d, remainder %d chars)"
          % (len(_held_said or ""), len(_held_remainder or "")))
    emit("held", {})

@socketio.on("cancel")
def handle_cancel(data=None):
    """CANCEL (space): resume immediately -- re-speak the held remainder, no recap."""
    global _held_remainder
    r = _held_remainder
    _held_remainder = None
    _vlog("barge", "CANCEL -> resume remainder")
    if r:
        speak_chunked(r)
    socketio.emit("state_change", {"state": "idle"})   # reply done -> back to listening

# Bumpers: short spoken transitions between LIVE states, broadcast style. Named and
# composable, kept separate from the content they wrap. The cadence (the pauses) is the
# point: a bumper reads as a deliberate segment break, not a sentence. Authored in SSML
# so the breaks are real. The hold ENTRY bumper is a sound (the hold ambience); this
# registry holds the SPOKEN bumpers.
BUMPERS = {
    # Bare hold/release, nothing discussed: just pick the thread back up.
    "resume": 'OK, let\'s pick back up.<break time="350ms"/> And<break time="300ms"/> resume.<break time="450ms"/>',
    # Release after a subchannel discussion: acknowledge the added context, then continue.
    "resume_context": 'With that in mind...<break time="400ms"/>',
}


def bumper(name):
    return BUMPERS.get(name, "")


def _regenerate_resume(said, remainder):
    """Release-with-context: re-run the agent to CONTINUE the interrupted reply, folding in
    whatever was discussed in the child subchannel (already in the conversation log). Returns
    a fresh spoken continuation, or None to fall back to the verbatim remainder."""
    said = (said or "").strip()
    remainder = (remainder or "").strip()
    if not (said or remainder):
        return None
    instruction = (
        "[RESUME AFTER HOLD] You were mid-reply in a live voice conversation and the user "
        "put you on hold to talk. What you had already said: \"%s\". What you were about to "
        "continue with: \"%s\". While held, you and the user had the exchange shown in the "
        "recent conversation above. Now pick the thread back up out loud: deliver the rest "
        "of that point, but weave in what was just discussed so it lands as one continuous "
        "thought. Do not greet, do not recap mechanically, do not restate what you already "
        "said. Just continue, naturally, spoken." % (said[-600:], remainder[:600])
    )
    try:
        context_sections = [
            ("mode", get_mode_context()),
            ("identity", get_instance_identity()),
            ("recent_conversation", get_recent_conversation_context()),
            ("summary", get_conversation_summary_context()),
            ("prompt_template", load_prompt_template()),
        ]
        enhanced = assemble_prompt_with_budget(context_sections, instruction)
        response, _ = _call_claude_or_fallback(enhanced, raw_user_text="[resume]")
        return response
    except Exception as e:
        print("[BARGE] resume regeneration failed: %s -- falling back to remainder" % e)
        return None


def _mint_sidebar_token(subchannel_log):
    """Keep the SIDEBAR (the held side-thread) as a recallable, addressable token: ONE line
    capturing the logic derived in that subthread, so a later turn can call back to it. The
    handle (label) is the citation; the raw thread rides along in extra_fields. Degrades to
    the raw thread text if synthesis or the factory is unavailable."""
    if not subchannel_log or token_factory is None:
        return None
    thread = "\n".join("You: %s\nAssistant: %s" % (u, a)
                       for u, a in subchannel_log if (u or a)).strip()
    if not thread:
        return None
    line = None
    try:
        instr = ("[SIDEBAR SUMMARY] Below is a short side conversation, held off the main "
                 "thread. In ONE sentence, state the operative point or decision derived in "
                 "it -- the thing a later turn would call back to. No preamble, just the "
                 "sentence.\n\n" + thread[:2000])
        line, _ = _call_claude_or_fallback(instr, raw_user_text="[sidebar summary]")
    except Exception as e:
        print("[SIDEBAR] summary synthesis failed: %s" % e)
    value = (line or "").strip() or thread[:280]
    handle = " ".join(value.split()[:6])          # short citation handle for callbacks
    # Root the sidebar in the human: it was derived from user actions (the held discussion),
    # so it carries the session's REAL human provenance. That makes it a valid trust anchor,
    # so a later callback's modifier can chain to the human THROUGH it. No faking.
    extra = {"sidebar_thread": thread[:4000], "turns": len(subchannel_log), "scale": "turn"}
    try:
        from flask import request as _freq
        extra.update(get_challenge_fields(getattr(_freq, "sid", None)))
    except Exception:
        pass
    try:
        tid = token_factory.create(
            token_type="conversation_summary",
            label="sidebar: %s" % handle,
            value=value,
            tags=["sidebar", "derived", "hold"],
            thermal={"base_temp": 70, "cooling_rate": 6.0},
            extra_fields=extra,
        )
        _vlog("sidebar", "minted token %s: \"%s\"" % (tid, value[:60]))
        return tid
    except Exception as e:
        print("[SIDEBAR] token mint failed: %s" % e)
        return None


# Callback (light): a later turn can call back to a sidebar. Deliberate -- triggered by an
# explicit reference, not fuzzy prose -- so it never fires by accident.
_CALLBACK_TRIGGERS = ("callback", "call back", "call-back", "go back to", "back to what",
                      "earlier you", "earlier we", "that sidebar", "the sidebar",
                      "remember when", "as we discussed", "like we said", "pull that back",
                      "pull it back", "revisit", "circle back")

_STOP = set(("the a an and or of to in on for with that this it is are was you we i he she "
             "they them our your my me be do so about what when how why").split())


def _human_action_anchor(evidence):
    """ARCHITECT'S LINE (user directive): a user action -- a spoken turn, a space press -- IS
    the human anchor. The provenance guard exists for a real reason (a chain must terminate
    at a human), so we do NOT bypass it and we do NOT borrow the challenge's stronger proof.
    Instead we mint an honestly-labeled human anchor whose verification_method names exactly
    what the proof is: a live user action. That is where the architect drew the line; move it
    by changing this one function. Returns the anchor token id or None."""
    if token_factory is None:
        return None
    try:
        return token_factory.create(
            token_type="user_action",
            label="user action",
            value=(evidence or "user action")[:200],
            tags=["user_action", "human"],
            thermal={"base_temp": 85, "cooling_rate": 10.0},
            extra_fields={"human_verified": True,
                          "verification_method": "user_action:live_turn"},
        )
    except Exception as e:
        print("[ANCHOR] human-action anchor failed: %s" % e)
        return None


def _reheat_sidebar(token_id, evidence="", sid=None):
    """USE MODIFIER TOKENS: a live modifier referencing the sidebar adds heat to it, so the
    callback resurfaces it (this turn and the next few) instead of it decaying away. The
    modifier chains to the human THROUGH the user action that triggered the callback (the
    anchor), so provenance terminates at a real user action -- honestly, no faking. If a
    stronger challenge proof exists this session, it rides along too."""
    if token_factory is None:
        return
    anchor = _human_action_anchor(evidence)
    refs = [r for r in (token_id, anchor) if r]
    fields = {"references": refs}
    try:
        from flask import request as _freq
        fields.update(get_challenge_fields(sid or getattr(_freq, "sid", None)))
    except Exception:
        pass
    try:
        token_factory.create(
            token_type="modifier",
            label="callback reheat",
            value="callback -> %s" % token_id,
            tags=["modifier", "callback", "sidebar"],
            thermal={"base_temp": 92, "cooling_rate": 8.0},
            extra_fields=fields,
        )
        _vlog("callback", "reheat modifier -> %s (anchor %s)" % (token_id, anchor))
    except Exception as e:
        print("[CALLBACK] reheat modifier failed: %s" % e)


def _maybe_callback(text, sid=None):
    """If this turn explicitly calls back to a sidebar, pick the best match, reheat it (via a
    modifier), and return it for re-injection into this turn's prompt. Light version:
    re-inject + reheat, no re-enter / re-run. Returns the token dict or None."""
    low = (text or "").lower()
    if not low or not any(t in low for t in _CALLBACK_TRIGGERS):
        return None
    if not CUE_MEM_AVAILABLE:
        return None
    try:
        sidebars = [t for t in cue_mem_list_tokens() if "sidebar" in (t.get("tags") or [])]
    except Exception:
        sidebars = []
    if not sidebars:
        return None
    words = set(w for w in re.findall(r"[a-z0-9']+", low) if w not in _STOP and len(w) > 2)

    def _score(tok):
        hay = ((tok.get("label") or "") + " " + str(tok.get("value") or "")).lower()
        hw = set(re.findall(r"[a-z0-9']+", hay))
        return len(words & hw)

    best = max(sidebars, key=_score)
    if _score(best) == 0:
        best = sidebars[0]                 # no keyword match -> the hottest/most recent sidebar
    _reheat_sidebar(best.get("token_id"), evidence=text, sid=sid)
    _vlog("callback", "-> \"%s\"" % (best.get("value") or "")[:60])
    return best


_resuming = False


def _release_hold(data=None):
    """RELEASE the hold (space, the UNHOLD button, or a spoken authorize cue): regenerate the
    continuation with the subchannel folded in, play the transition bumper, speak it, then
    keep the sidebar as a token. Falls back to the verbatim remainder when nothing was
    discussed. Re-entrancy guarded so two triggers cannot stack two playbacks."""
    global _held_remainder, _held_said, _held_turns, _subchannel_log, _resuming, _exaggeration
    if _resuming:
        return
    _resuming = True
    flush_speech_queue()   # clean slate: no lingering subchannel/hold audio to overlap the stitch
    turns, log = 0, []
    try:
        r = _held_remainder
        said = _held_said
        turns = _held_turns
        log = list(_subchannel_log)
        _held_remainder = None
        _held_said = None
        _held_turns = 0
        _subchannel_log = []
        # "With that in mind..." when there was a discussion; the plain pick-up otherwise.
        b = (data or {}).get("bumper") or (bumper("resume_context") if turns > 0 else bumper("resume"))
        # Only regenerate if something was actually discussed; a bare hold/release resumes
        # the remainder verbatim (instant, no model call). During regeneration the client
        # shows 'thinking', so the model latency reads as deliberation, not a stall.
        cont = _regenerate_resume(said, r) if turns > 0 else None
        body = cont if (cont and cont.strip()) else (r or "")
        # Reset the register render-state to the live baseline (floor, or chill) before the
        # stitch-back. The weight lever ACCUMULATES register across every subchannel turn,
        # and this path skips the per-turn autotone -- without this reset the continuation
        # synthesizes at a maxed register (the "cracked out robot" on resume). One mutator,
        # which also re-derives exaggeration from the baseline register.
        set_render_register(_floor_register() or "1", "release")
        _vlog("barge", "RELEASE -> %s + continuation (%d chars, %d subchannel turns)"
              % ("regen" if cont else "verbatim", len(body or ""), turns))
        speak_chunked((b + " " + (body or "")).strip())
    finally:
        _resuming = False
        socketio.emit("state_change", {"state": "idle"})   # idle NOW, before persistence
    # Persist the sidebar OFF the critical path (its own model call) so it never delays the
    # return to idle -- that delay was the ~10s stuck-in-speaking bug.
    if turns > 0 and log:
        def _persist():
            try:
                _mint_sidebar_token(log)
            except Exception as e:
                print("[SIDEBAR] background mint failed: %s" % e)
        try:
            socketio.start_background_task(_persist)
        except Exception:
            _persist()


@socketio.on("resume")
def handle_resume(data=None):
    """RELEASE via space / UNHOLD button (client already showed 'thinking')."""
    _release_hold(data)

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

def _format_absolute_anchor(timestamp_str):
    """Render a stored ISO timestamp as a neutral absolute anchor.

    Absolute by construction, so it never decays: the line reads correctly
    whenever the snapshot is re-read (per temporally-neutral-descriptions).
    Relative rendering, when wanted, is a display-layer concern gated by the
    Relative Time flag (see relative-time statute) -- it is not baked in here.
    """
    if not timestamp_str:
        return ""
    try:
        ts = datetime.fromisoformat(str(timestamp_str))
        return ts.strftime("[%b %-d %H:%M]")
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
    # Capability ceiling (matrix-driven): strip any block whose disposition is not 'full'
    # for the active mode, so it never renders a widget or fires. Baseline strips nothing;
    # ceiling modes keep yes/no but strip the richer blocks (which also keeps summaries
    # simple). This is the technical half of the ceiling; get_mode_context is the other.
    blocked = _blocked_tags()
    if blocked:
        clean_text = _strip_bracket_balanced_tags(clean_text, blocked)

    # P3a: fire read-only [CUE: warm|recall|heat] triggers ("set the table"). This
    # pre-warms the next turn's context; write/act verbs are deferred (need the GATE).
    # Parsed from clean_text, which still carries the (never-spoken) CUE tag.
    global _last_cue
    _last_cue = None
    try:
        _cue_triggers = _parse_cue_triggers(clean_text)
        if _cue_triggers:
            _last_cue = _fire_cue_triggers(_cue_triggers)
    except Exception as _cue_err:
        print("[CUE] trigger fire failed: %s" % _cue_err, flush=True)

    entry = {
        'timestamp': timestamp.strftime('%Y-%m-%dT%H:%M'),  # No seconds
        't_period': get_time_period(timestamp),
        'user': user_text,
        'assistant': clean_text
    }
    if _last_cue:
        entry['cue'] = _last_cue

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


def get_recent_conversation_context(turns=6):
    """Verbatim block of the last N conversation turns, for referent resolution.

    The flux/summary contexts compress and truncate, so a pronoun whose
    antecedent lived in the prior turn ("dying to see it") can lose its
    referent before it reaches the model. This block carries the most recent
    turns in full, untruncated, so the immediately-prior context is always
    legible. It is NOT in assemble_prompt_with_budget's trim_order, so it
    survives budget enforcement -- recent verbatim turns are the floor, not
    the first thing cut.

    The button (YES_NO) path already does this inline; this helper gives the
    voice and text paths the same continuity.
    """
    # Pull a generous raw window: token_created / client_connected events are
    # interleaved with exchanges in the log, so we over-read then filter.
    raw = load_recent_logs(limit=turns * 6)
    exchanges = [e for e in raw if e.get('user') and e.get('assistant')]
    if not exchanges:
        return ""

    recent = exchanges[-turns:]
    lines = ["[RECENT CONVERSATION -- last %d turns, verbatim]" % len(recent)]
    for entry in recent:
        t_rel = compute_relative_time_from_now(entry.get('timestamp', ''))
        prefix = f"{t_rel} " if t_rel else ""
        lines.append(f"{prefix}User: {entry.get('user', '')}")
        lines.append(f"Assistant: {entry.get('assistant', '')}")
    lines.append(
        "When the user's input contains a pronoun or deictic ('it', 'that', "
        "'this', 'the one') with no antecedent in their current message, "
        "resolve it against these turns BEFORE asking them to repeat themselves."
    )
    return "\n".join(lines) + "\n\n"


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


def _snap_entries_proper_nouns(entries):
    """Snap STT-mangled proper nouns in conversation entries to canonical form.

    Runs the proper-noun anchor layer over the user/assistant text of each entry
    BEFORE it is compressed into a summary, so the stored summary records the
    canonical entity spelling ("AltSpace VR") rather than the transcript's
    near-miss surface form ("old space VR"). This is the memory half of the STT
    fix -- recurring, known entities. The reflex half (flagging first-occurrence
    unknown nouns) is _detect_unanchored_proper_nouns, kept separate.

    Returns (entries, total_corrections). Best-effort: with the anchor layer
    unavailable or no anchors on disk, the entries are returned untouched.
    """
    if not PROPER_NOUNS_AVAILABLE or not _proper_nouns or not entries:
        return entries, 0
    try:
        anchors = _proper_nouns.load_anchors(str(TOKENS_DIR))
    except Exception:
        return entries, 0
    if not anchors:
        return entries, 0

    total = 0
    snapped = []
    for entry in entries:
        new_entry = dict(entry)
        for field in ("user", "assistant"):
            text = new_entry.get(field)
            if text:
                corrected, corrections = _proper_nouns.snap_text(text, anchors)
                if corrections:
                    new_entry[field] = corrected
                    total += len(corrections)
        snapped.append(new_entry)
    return snapped, total


def _detect_unanchored_proper_nouns(entries):
    """Reflex half: surface proper-noun-shaped tokens that have NO anchor yet.

    A first-occurrence proper noun is uncertain, not fact -- STT may have
    mangled it and no canonical entity vouches for it. We collect those surface
    forms so the summary token can carry them as control-channel metadata
    (low_confidence_nouns), a flag for a later reader NOT to absorb them as
    established truth. Per managed-intelligence-presence, this is scaffolding:
    it rides in metadata, never in the prose body the user hears.

    Returns a de-duplicated list of surface strings. Best-effort / empty on any
    failure or when the reflex module is unavailable.
    """
    if not PROPER_NOUNS_AVAILABLE or not _proper_noun_reflex or not entries:
        return []
    try:
        anchors = _proper_nouns.load_anchors(str(TOKENS_DIR)) if _proper_nouns else []
    except Exception:
        anchors = []

    seen = {}
    for entry in entries:
        for field in ("user", "assistant"):
            text = entry.get(field)
            if not text:
                continue
            try:
                for surface in _proper_noun_reflex.detect_unanchored(text, anchors):
                    key = surface.lower()
                    if key not in seen:
                        seen[key] = surface
            except Exception:
                continue
    return list(seen.values())


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
        # Light compression - preserve more detail.
        #
        # Per temporally-neutral-descriptions: the stored prose carries an
        # ABSOLUTE per-entry anchor, never a relative ("3m ago") phrase.
        # compute_relative_time_from_now is a READ-time display helper -- baking
        # its output here freezes a value that is already wrong the next time the
        # snapshot is read (a 24m-old token still claiming "7m ago"). The
        # snapshot's own timestamp -- the header from create_scale_token plus
        # metadata.created_at -- carries the temporal frame; the body stays neutral.
        for entry in entries:
            t_abs = _format_absolute_anchor(entry.get('timestamp', ''))
            user = entry.get('user', '')[:80]
            assistant = entry.get('assistant', '')[:100]
            prefix = f"{t_abs} " if t_abs else ""
            compressed_lines.append(f"{prefix}U: {user} | A: {assistant}")

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

    # Snap known entities to canonical spelling before compression so the
    # summary records "AltSpace VR", not the STT near-miss (proper-noun anchor
    # layer, memory half, 2026-06-06 pin item A). Detect unanchored proper
    # nouns too -- the reflex half rides as control-channel metadata below.
    recent_logs, snap_count = _snap_entries_proper_nouns(recent_logs)
    if snap_count:
        print(f"  (proper-noun snap: {snap_count} correction(s) applied)")
    low_confidence_nouns = _detect_unanchored_proper_nouns(recent_logs)

    # Compress with this scale's compression level
    summary_text = compress_conversation_chunk(recent_logs, compression_level=config['compression'])

    if not summary_text:
        return None

    # Append vibe line for fine and medium scales (not heavy -- those are theme-only)
    if scale_name in ("fine", "medium"):
        vibe = _build_vibe_line()
        if vibe:
            summary_text = summary_text + "\n" + vibe

    # Snapshot anchor: a summary IS a temporal artifact -- the moment it was
    # taken is its temporal context. Carry that as one explicit absolute anchor
    # at the head of the prose so the body can stay temporally neutral while a
    # later reader still knows the frame. (summary-engine temporal-neutrality
    # pin, 2026-06-06; temporally-neutral-descriptions statute)
    summary_text = "[snapshot %s]\n%s" % (now.strftime("%b %-d %H:%M"), summary_text)

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
                    'low_confidence_nouns': low_confidence_nouns,
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
                                       factor=1.15, max_value=10.0)
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
            'low_confidence_nouns': low_confidence_nouns,
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


def _aperture_hex_from_brevity(b):
    """Mirror of app.js apertureHex(): brevity 0..1 -> VRGB hue arc.

    hue = 30 (warm amber, brief) -> 210 (cool blue, reflective), at hsl(h,70,55).
    Kept identical to the client so a backend fallback never drifts from the
    color the user actually sees on the dial thumb. Used only when the client
    did not put the hex on the wire (older client / API path).
    """
    hue = 30 + (max(0.0, min(1.0, b)) * 180)
    s, l = 0.70, 0.55
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((hue / 60) % 2 - 1))
    m = l - c / 2
    if hue < 60:    r, g, bl = c, x, 0
    elif hue < 120: r, g, bl = x, c, 0
    elif hue < 180: r, g, bl = 0, c, x
    else:           r, g, bl = 0, x, c
    return "#%02X%02X%02X" % (
        round((r + m) * 255), round((g + m) * 255), round((bl + m) * 255)
    )


def get_aperture_constraint(brevity, aperture_hex=None):
    """Turn the brevity dial (0..1) into a response-mode directive.

    The dial is the user's EXPLICIT demand signal -- how much they need to get
    OUT of this turn versus how much they can stay and reflect. It is set by a
    physical control the user owns, and it dominates the word-count heuristic
    because the demand is driven by the user's external context (the operation
    waiting outside this window), which the substrate cannot see or infer.

    ONE axis, three coupled attributes that move together: sentence length,
    content length, and definitiveness. Dial DOWN = MORE brevity (short,
    minimal, definitive). Dial UP = LESS brevity (long, expansive, tangential).

    Three buckets, each a band on the VRGB hue arc (amber 30 deg brief -> blue
    210 deg reflective). The dial's own color travels into the directive as the
    VRGB coordinate, so the geometry rides alongside the prose. The hex the user
    sees on the thumb IS the signal -- one source of truth, no re-derivation.

    Scale: 0.0 = brief / deliverable (bottom), 1.0 = loose / reflective (top).
    """
    try:
        b = float(brevity)
    except (TypeError, ValueError):
        return ""
    b = max(0.0, min(1.0, b))

    coord = (aperture_hex or "").strip() or _aperture_hex_from_brevity(b)

    if b >= 0.66:
        return """[BREVITY DIAL - VRGB %s : blue band (reflective, user-set, authoritative)]
Top of the arc, aperture wide open -- LEAST brevity. All three axes ride high:
longer sentences, more content, more exploratory and tangential. The user has
time and wants to think WITH you -- reflect, mirror, wander. Do not rush to a
deliverable or a decision. This dominates any length heuristic.
""" % coord
    if b >= 0.33:
        return """[BREVITY DIAL - VRGB %s : green band (deliberative, user-set, authoritative)]
Middle of the arc -- medium brevity. Structured over discursive: weigh the
options, give the pros/cons and trade-offs that move the user toward a choice,
but keep it tight -- not yet a bare artifact. This dominates any length heuristic.
""" % coord
    return """[BREVITY DIAL - VRGB %s : amber band (deliverable, user-set, authoritative)]
Bottom of the arc, aperture closed -- MAXIMUM brevity. All three axes ride low:
short sentences, minimal content, definitive -- commit, do NOT hedge or wander.
Assume the user has another operation queued and is about to leave this window.
Give the clean, liftable thing: the paragraph, the command, the copy-paste block
-- clarity over completeness. NO pros/cons scaffolding; they have already decided
and just need the output in a form they can grab and go. If you are genuinely
blocked, do NOT open a discussion -- ask ONE clarifying question in the tightest
form possible (prefer a yes/no, else a 0-10 scalar, else a single open question).
This dominates any length heuristic.
""" % coord


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

READER HINT: When the Pipeline Surface doc opens with a `[card] DELTA — ... since push pipe-...` block, that block is the AUTHORITATIVE diff signal. Cite its lines when asked "what changed?" / "see the diff?" / "see the new thing?". If the block says "No changes since last push," that is also authoritative — say so plainly with the slug as receipt. Never say "same surface, what did you push?" when a DELTA block is present in this context.

{content}

"""
    except Exception as e:
        print(f"⚠️  Failed to read flux-capacitor context: {e}")
        return ""


def get_upstream_handoff_context():
    """Surface pending upstream handoffs from ninja Claude (Reader -> Writer).

    cue-vox is the Writer: it reads the handoff inbox, decides what to
    crystallize, and archives each one. This block is the session-start nudge.
    It self-clears -- brief() returns empty once nothing pends -- so the nudge
    stays visible only until cue-vox disposes of each handoff via the protocol.

    The handoff channel is a shared-chassis MCP server (tools/handoff/), also
    available as the handoff_* tools. This in-process import reads the same
    flat-file substrate the verbs operate on. See upstream-handoff policy.
    """
    handoff_lib = MAESTRO_ROOT / "core" / "handoff"
    if str(handoff_lib) not in sys.path:
        sys.path.insert(0, str(handoff_lib))

    try:
        import handoff as handoff_core
        nudge = handoff_core.brief()
    except Exception as e:
        print(f"⚠️  Failed to read upstream handoffs: {e}")
        return ""

    if not nudge:
        return ""

    return f"""[UPSTREAM HANDOFFS - ninja Claude left signal for you]

{nudge}

To process: read each (handoff_read, or the CLI: python3 tools/handoff/cli.py read <slug>),
decide whether to crystallize it into a token (your existing createtoken job),
then archive it (handoff_archive with disposition crystallized | modified | discarded).
Apply the same data-sensitivity and crystallization rules you apply to voice exchanges.
You may mention a relevant handoff in early conversation, e.g. "ninja left a note about X."

"""


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


def _compute_cvx_version():
    """A single build stamp for the running server, computed once at startup from the
    newest of the core source files. It changes whenever code changes AND the service
    is restarted, so both pages printing it lets you eyeball that they match (same
    up-to-date server). Format: MMDD-HHMM of the newest source file."""
    import time as _t
    base = os.path.dirname(os.path.abspath(__file__))
    files = ['web.py', 'static/js/app.js', 'static/tune.html']
    try:
        newest = max(os.path.getmtime(os.path.join(base, f))
                     for f in files if os.path.exists(os.path.join(base, f)))
        return _t.strftime('%m%d-%H%M', _t.localtime(newest))
    except (OSError, ValueError):
        return 'unknown'


CVX_VERSION = _compute_cvx_version()


@app.route('/')
def index():
    print("📄 Serving index.html")
    import time as _time
    return render_template('index.html', cache_bust=int(_time.time()), cvx_version=CVX_VERSION)


# --- Voice tuning panel: tune the register + earn-curve with sliders, hear it,
# and crystallize the feel to an inert SVG. ---
@app.route('/tune')
def tune_page():
    # No-store so the tuner never serves a stale cached page after an edit. Inject the
    # server build stamp so the tuner console prints the same version as the main app.
    from flask import Response
    html = open(os.path.join(app.static_folder, 'tune.html'), encoding='utf-8').read()
    html = html.replace('__CVX_VERSION__', CVX_VERSION)
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/tune/blend')
def tune_blend_page():
    # Register ladder is now a tab inside the /tune SPA; keep the old URL working.
    from flask import redirect
    return redirect('/tune#blend', code=302)


@app.route('/api/tune/params', methods=['POST'])
def tune_params():
    global _TONE_DECAY, _TONE_CAP, _TONE_GAMMA, _LOUD_FLOOR, _BREAK_MULT, _PRESSURE, _LIVE_REGISTER_FLOOR
    d = request.get_json(force=True, silent=True) or {}
    try:
        if 'decay' in d: _TONE_DECAY = float(d['decay'])
        if 'cap' in d: _TONE_CAP = float(d['cap'])
        if 'gamma' in d: _TONE_GAMMA = float(d['gamma'])
        if 'loud_floor' in d: _LOUD_FLOOR = float(d['loud_floor'])
        if 'break_mult' in d: _BREAK_MULT = max(0.25, min(4.0, float(d['break_mult'])))
        if 'pressure' in d: _PRESSURE = max(0.4, min(2.5, float(d['pressure'])))
        # Apply-to-live extras: register becomes a floor the autotone won't drop below
        # (survives turns), and lift sets the live question-rise strength.
        if 'register' in d and d['register'] is not None:
            _LIVE_REGISTER_FLOOR = max(0, min(4, int(round(float(d['register'])))))
        if 'lift' in d and _kokoro_available:
            kokoro_voice.set_prosody(lift=max(0.0, min(1.5, float(d['lift']))))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='bad value'), 400
    print("[TUNE] decay=%.2f cap=%.1f gamma=%.2f loud_floor=%.3f break=%.2fx pressure=%.2f reg_floor=%s"
          % (_TONE_DECAY, _TONE_CAP, _TONE_GAMMA, _LOUD_FLOOR, _BREAK_MULT, _PRESSURE, _LIVE_REGISTER_FLOOR), flush=True)
    return jsonify(ok=True, decay=_TONE_DECAY, cap=_TONE_CAP, gamma=_TONE_GAMMA,
                   loud_floor=_LOUD_FLOOR, break_mult=_BREAK_MULT, pressure=_PRESSURE,
                   register_floor=_LIVE_REGISTER_FLOOR)


# Deterministic emphasis -> tone. Markdown markup in the text varies the register:
# *italic* is the intimate set-aside (a notch down, slower, lean-in), **bold** hits
# fuller (registers up), normal rests at the base. This is the deterministic layer
# on top of the earned auto-tone.
_EMPH = re.compile(r'(\*\*\*.+?\*\*\*|___.+?___|\*\*.+?\*\*|__.+?__|\*.+?\*|_.+?_)', re.DOTALL)
_EMPH_MAP = {                       # emphasis -> (register offset, speed)
    "normal": (0, 1.0),
    "italic": (-1, 0.92),          # set-aside: intimate, slower
    "bold": (2, 1.0),             # fuller, spends capital
    "bolditalic": (2, 0.95),      # emphatic but deliberate
}


def parse_emphasis(text):
    """Split text into (span, emphasis) where emphasis is normal/italic/bold/bolditalic."""
    spans, pos = [], 0
    for m in _EMPH.finditer(text or ""):
        if m.start() > pos:
            spans.append((text[pos:m.start()], "normal"))
        tok = m.group(1)
        if tok[:3] in ("***", "___"):
            spans.append((tok[3:-3], "bolditalic"))
        elif tok[:2] in ("**", "__"):
            spans.append((tok[2:-2], "bold"))
        else:
            spans.append((tok[1:-1], "italic"))
        pos = m.end()
    if pos < len(text or ""):
        spans.append((text[pos:], "normal"))
    return [(t, e) for t, e in spans if t.strip()]


def _overlay_tail(speech_path, cue_path, lead=0.22):
    """Mix an earcon so it rides CONCURRENTLY under the tail of `speech_path`,
    starting `lead` seconds before the speech ends and extending a touch past it.
    Returns a new temp wav path (the cue plays over the voice, not after it)."""
    import wave
    import tempfile
    import numpy as np
    try:
        with wave.open(speech_path) as w:
            sr = w.getframerate()
            sp = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
        with wave.open(cue_path) as w:
            cue = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    except Exception:
        return None
    start = max(0, len(sp) - int(lead * sr))
    out_len = max(len(sp), start + len(cue))
    buf = np.zeros(out_len, dtype=np.float32)
    buf[:len(sp)] += sp
    buf[start:start + len(cue)] += cue
    peak = float(np.max(np.abs(buf)) or 0.0)
    if peak > 32767.0:
        buf *= 32767.0 / peak     # only pull down if the sum clipped
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="cuevox-ask-")
    os.close(fd)
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(buf.astype("<i2").tobytes())
    return path


def _concat_wavs(paths, out):
    import wave
    import numpy as np
    chunks, rate = [], 24000
    for p in paths:
        with wave.open(p) as w:
            rate = w.getframerate()
            chunks.append(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"))
        chunks.append(np.zeros(int(rate * 0.04), dtype="<i2"))   # tiny gap between spans
    alld = np.concatenate(chunks) if chunks else np.zeros(1, dtype="<i2")
    with wave.open(out, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(alld.tobytes())


# Deterministic pause indicators: [pause], [pause=long|short|med], or [pause=600] (ms).
# A pause is authored silence -- and it earns capital for the fuller line after it.
_PAUSE = re.compile(r'\[pause(?:[=:]\s*([a-z0-9]+))?\]', re.I)


def _pause_ms(arg):
    if not arg:
        return 450
    a = str(arg).lower().strip()
    if a in ("short", "sm"):
        return 220
    if a in ("long", "lg"):
        return 850
    if a in ("med", "medium"):
        return 450
    try:
        if a.endswith("ms"):
            return max(60, min(3000, int(float(a[:-2]))))
        if a.endswith("s"):
            return max(60, min(3000, int(float(a[:-1]) * 1000)))
        return max(60, min(3000, int(float(a))))
    except ValueError:
        return 450


def parse_script(text):
    """Render ops honoring [pause] tags AND *italic*/**bold** emphasis, in order.
    Yields ('say', span, emphasis) and ('pause', ms)."""
    ops, pos = [], 0
    text = text or ""
    for m in _PAUSE.finditer(text):
        for span, emph in parse_emphasis(text[pos:m.start()]):
            ops.append(("say", span, emph))
        ops.append(("pause", _pause_ms(m.group(1))))
        pos = m.end()
    for span, emph in parse_emphasis(text[pos:]):
        ops.append(("say", span, emph))
    return ops


def _silence_wav(ms, out, rate=24000):
    import wave
    import numpy as np
    n = int(rate * ms / 1000.0)
    with wave.open(out, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(np.zeros(n, dtype="<i2").tobytes())


# The beat: the processing/"working" bed used as a rhythmic rest. Same texture the client
# fills a between-paragraph latency gap with (static/sounds/working.wav), baked to the synth
# rate so a deliberate beat and an elastic latency fill are indistinguishable. A beat is a
# BLANK with texture -- a pause the agent can drop anywhere for rhythm and pacing.
_BEAT_SRC = os.path.join(_SFX_DIR, "beat.wav")
_BEAT_WORKING_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "sounds", "working.wav")
_BEAT_DEFAULT_MS = 550
_PARA_BEAT_MS = 450        # auto-beat placed between paragraphs (blends with synth latency)


def _ensure_beat_asset():
    """Provision beat.wav (the synth-rate processing bed) from the client's working.wav if it
    is not already present. Audio assets are gitignored, so this keeps the beat self-contained
    on a fresh clone: derive it once at boot, dumb-by-design. No-op if beat.wav exists or the
    source is missing (the beat then degrades to silence)."""
    if os.path.exists(_BEAT_SRC) or not os.path.exists(_BEAT_WORKING_SRC):
        return
    try:
        import wave
        import struct
        import numpy as np
        with open(_BEAT_WORKING_SRC, "rb") as f:
            raw = f.read()
        i = raw.find(b"fmt ")
        fmt_tag = struct.unpack_from("<H", raw, i + 8)[0] if i >= 0 else 1
        with wave.open(_BEAT_WORKING_SRC) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if fmt_tag == 3 and width == 4:
            a = np.frombuffer(frames, dtype="<f4").astype(np.float64)
        elif width == 4:
            a = np.frombuffer(frames, dtype="<i4").astype(np.float64) / 2147483648.0
        elif width == 2:
            a = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
        else:
            return
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        if rate == 48000:                          # decimate 48k -> 24k (pairwise mean lowpass)
            if len(a) % 2:
                a = a[:-1]
            a = (a[0::2] + a[1::2]) * 0.5
            out_rate = 24000
        else:
            out_rate = rate
        peak = np.max(np.abs(a)) or 1.0
        a = a / peak * 0.5                          # calm bed level: sits under speech
        os.makedirs(_SFX_DIR, exist_ok=True)
        with wave.open(_BEAT_SRC, "w") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(out_rate)
            w.writeframes(np.clip(a * 32767.0, -32768, 32767).astype("<i2").tobytes())
        print("[BEAT] provisioned beat.wav from working.wav (%dHz mono)" % out_rate)
    except Exception as e:
        print("[BEAT] could not provision beat.wav: %s (beat -> silence)" % e)


_ensure_beat_asset()


def _texture_wav(ms, out, rate=24000):
    """Render `ms` of the beat texture: loop the baked bed to length, trim, fade the
    edges so it starts/ends clean (no click) and butts seamlessly against speech. Falls
    back to silence if the asset is missing, so the element degrades safely."""
    import wave
    import numpy as np
    if not os.path.exists(_BEAT_SRC):
        return _silence_wav(ms, out, rate)
    with wave.open(_BEAT_SRC) as w:
        rate = w.getframerate()
        bed = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if bed.size == 0:
        return _silence_wav(ms, out, rate)
    n = max(1, int(rate * ms / 1000.0))
    reps = int(np.ceil(n / bed.size))
    a = np.tile(bed, reps)[:n].astype(np.float64)
    fade = min(int(rate * 0.045), n // 2)          # ~45ms edges
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade)
        a[:fade] *= ramp
        a[-fade:] *= ramp[::-1]
    i16 = np.clip(a, -32768, 32767).astype("<i2")
    with wave.open(out, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(i16.tobytes())


def deterministic_markup(text):
    """Rule-based translation of markdown emphasis into standard SSML. No model, fully
    deterministic -- a draft to hand-tweak. Only **bold**/*italic* -> <emphasis> (valid
    SSML). The old typographic heuristics (ALL-CAPS -> emphasis, ... -> break, ' -- ' ->
    break) were GUT with the custom-markup dialect: author pauses/emphasis with explicit
    SSML tags, not magic punctuation."""
    s = text or ""
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r'<emphasis level="strong">\1</emphasis>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r'<emphasis level="strong">\1</emphasis>', s)
    s = re.sub(r"\*(.+?)\*", r'<emphasis level="moderate">\1</emphasis>', s)
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    return s


# --- SSML is the standard markup, both surfaces (tuner + agent input) ------------
# The renderer maps a W3C SSML subset onto the voice engine:
#   <break time="400ms"/> or <break strength="weak|medium|strong|x-strong"/>  silent pause
#   <beat/> or <beat time="600ms"/>  (alias <rest/>)                          textured rest (processing bed)
#   <emphasis level="strong|moderate|reduced">                                force
#   <prosody rate= volume= pitch=>                                            speed/gain/pitch
#   <p> / <s>                                                                 paragraph / sentence pause
#   <say-as interpret-as=> / <sub alias="..">                                 spoken form
#   <voice name="isabella|atlas|neutral">                                     voice select (standard use)
#   <beat/> <beat time="600ms"/> <laugh/> <chuckle/>                          SANCTIONED cvx extensions
# Everything else is NOT markup. The custom dialect was GUT: no <force=N>, no
# <break=N/>, no <soft>/<loud>, no ALL-CAPS/.../-- auto-conversion, no <strong>/<b>/
# <em>/<i> shorthand. Standard W3C SSML plus the four sanctioned extensions above.
# The markup LANGUAGE / parser version. Bump when tag semantics change, so a package
# records which markup its text was authored against. v1 = count/stacking model
# (retired); v2 = SSML + custom dialect (retired); v3 = W3C SSML + sanctioned extensions.
MARKUP_VERSION = 3


def _ssml_break_ms(el):
    """SSML <break time=|strength=> -> milliseconds. Bare break defaults to medium."""
    t = el.get("time") or el.get("dur")
    if t:
        return _pause_ms(t)
    strength = (el.get("strength") or "").lower()
    return {"none": 0, "x-weak": 100, "weak": 200, "medium": 400,
            "strong": 800, "x-strong": 1400}.get(strength, 400)


def _ssml_rate(v):
    """SSML prosody rate (named / percentage / number) -> speed multiplier."""
    v = (v or "").strip().lower()
    named = {"x-slow": 0.7, "slow": 0.85, "medium": 1.0, "default": 1.0, "fast": 1.15, "x-fast": 1.3}
    if v in named:
        return named[v]
    try:
        return max(0.5, min(2.0, float(v[:-1]) / 100.0 if v.endswith("%") else float(v)))
    except ValueError:
        return 1.0


def _ssml_volume(v):
    """SSML prosody volume (named / dB / number) -> gain multiplier."""
    v = (v or "").strip().lower()
    named = {"silent": 0.1, "x-soft": 0.6, "soft": 0.8, "medium": 1.0, "default": 1.0, "loud": 1.3, "x-loud": 1.7}
    if v in named:
        return named[v]
    try:
        if v.endswith("db"):
            return max(0.1, min(2.5, 10 ** (float(v[:-2]) / 20.0)))
        return max(0.1, min(2.5, float(v[:-1]) / 100.0 if v.endswith("%") else float(v)))
    except ValueError:
        return 1.0


def _ssml_pitch_off(v):
    """SSML prosody pitch -> a register offset (our engine's pitch/brightness axis).
    Named low/high, or a signed +Nst / -N% just reads as down/up by one step."""
    v = (v or "").strip().lower()
    named = {"x-low": -2, "low": -1, "medium": 0, "default": 0, "high": 1, "x-high": 2}
    if v in named:
        return named[v]
    if v.startswith("+"):
        return 1
    if v.startswith("-"):
        return -1
    return 0


def _ssml_voice(name):
    """SSML <voice name=> is standard voice SELECTION (isabella/atlas/neutral), not a
    register knob -- the register-overload was gut with the custom dialect. Per-span
    voice switching would force an engine reload, so for now <voice> is a transparent
    container (its content is still spoken) and voice selection lives in the tuner
    picker / POST /api/tune/voice. Returns no delivery contribution."""
    return {}


def _tag_contrib(tag, el):
    """An SSML tag's contribution to the accumulating delivery context. Nested tags
    stack via _merge_ctx (reg_off sums, force/rate multiply, reg/speed/gain/lift set)."""
    t = tag.lower()
    g = el.get
    if t == "emphasis":
        return {"force": {"strong": 1.5, "moderate": 1.25, "reduced": 0.8, "none": 1.0}
                .get((g("level") or "moderate").lower(), 1.25)}
    if t == "prosody":
        c = {}
        if g("rate") is not None:
            c["rate"] = _ssml_rate(g("rate"))
        if g("volume") is not None:
            c["gain"] = _ssml_volume(g("volume"))
        if g("pitch") is not None:
            off = _ssml_pitch_off(g("pitch"))
            c["reg_off"] = off
            if off > 0:
                c["lift"] = True     # a pitch-up reads as the question/rise contour
        return c
    if t == "voice":
        return _ssml_voice(g("name"))
    return {}


def _merge_ctx(ctx, c):
    """Accumulate a contribution into the context: reg_off sums, force/rate multiply,
    reg/speed/gain/lift override. This is how <strong><em> stacks."""
    n = dict(ctx)
    if "reg_off" in c:
        n["reg_off"] = n.get("reg_off", 0) + c["reg_off"]
    if "force" in c:
        n["force"] = n.get("force", 1.0) * c["force"]
    if "rate" in c:
        n["rate"] = n.get("rate", 1.0) * c["rate"]
    for k in ("reg", "speed", "gain", "lift"):
        if k in c:
            n[k] = c[k]
    return n


def directive_to_vrgb(dv, base=0):
    """Encode a resolved span directive as a VRGB colour: hue=register (breathy cool
    -> dramatic warm), saturation=force, lightness=rate."""
    import colorsys
    reg = dv["reg"] if "reg" in dv else base + dv.get("reg_off", 0)
    reg = max(0, min(4, reg))
    force = dv.get("force", 1.0)
    rate = dv.get("speed", dv.get("rate", 1.0))
    hue = (250 - (reg / 4.0) * 220) % 360
    sat = max(0.15, min(1.0, 0.35 + (force - 1.0) * 0.6))
    light = max(0.30, min(0.85, 0.55 + (rate - 1.0) * 0.6))
    r, gg, b = colorsys.hls_to_rgb(hue / 360.0, light, sat)
    return "#%02x%02x%02x" % (int(r * 255), int(gg * 255), int(b * 255))


# The sanctioned vocabulary: W3C SSML core + four cvx extensions (beat/rest/laugh/
# chuckle). Anything else is flagged unknown by the parse view (and stripped from TTS).
_KNOWN_TAGS = {"speak", "break", "pause", "beat", "rest", "emphasis", "prosody", "p", "s",
               "say-as", "sub", "voice", "laugh", "chuckle"}


def resolve_instructions(text, base, tone):
    """Resolve markup to the executable instruction list. The SSML supplies a normalized
    intensity per dimension; the ACTIVE REGISTER'S envelope (tone['prosody'] ranges) maps
    that intensity into the delivered value -- so the same markup lands bigger in a wider
    register. No envelope (Chatterbox / plain callers) -> the raw SSML values pass through."""
    ops = _ops_from_text(text) or [("say", text, {})]
    pr = tone.get("prosody") or {}
    out = []
    for op in ops:
        if op[0] == "say":
            _, span, dv = op
            reg = dv["reg"] if "reg" in dv else base + dv.get("reg_off", 0)
            reg = max(0, min(4, int(reg)))
            # emphasis: intensity from the tag's force, mapped into the register's range.
            raw_force = dv.get("force", 1.0)
            if raw_force != 1.0 and "emphasis" in pr:
                force = _map_range(pr["emphasis"], _emph_intensity(raw_force))
            else:
                force = raw_force
            gain = float(dv["gain"]) if "gain" in dv else tone.get("pressure", 1.0) * (1.0 + 0.08 * reg) * force
            # rate: intensity from the tag's rate, mapped into the register's range.
            raw_rate = float(dv.get("speed", dv.get("rate", 1.0)))
            if raw_rate != 1.0 and "rate" in pr:
                speed = _map_range(pr["rate"], _rate_intensity(raw_rate))
            else:
                speed = raw_rate
            out.append({"say": span.strip(), "register": reg, "gain": round(max(0.4, min(2.5, gain)), 3),
                        "speed": round(max(0.5, min(2.0, speed)), 3),
                        "lift": bool(dv.get("lift") or span.rstrip().endswith("?")),
                        "vrgb": directive_to_vrgb(dv, base)})
        elif op[0] in ("pause", "beat"):
            # break/beat: a longer authored pause sits higher in the register's envelope,
            # so dramatic registers stretch long pauses more. Global break_mult still rides.
            mult = _map_range(pr["break"], _break_intensity(op[1])) if "break" in pr else 1.0
            ms = int(op[1] * mult * tone.get("break_mult", 1.0))
            out.append({"pause_ms": ms} if op[0] == "pause" else {"beat_ms": ms})
        elif op[0] == "clip":
            out.append({"cue": op[1]})
    return out


# Dumb versioning: one integer stamped onto every exported package. Bump it by hand
# whenever the <metadata> shape changes. Import compares against it and shouts in the
# console so a stale package is obvious while debugging.
PACKAGE_SCHEMA_VERSION = 1


def crystallize_package(text, base, tone, steer="", state=None):
    """Package the current script + dials into an INERT SVG: instructions for the
    future transformational apparatus. Visual is a delivery strip (a swatch per
    span, colour=VRGB, height=gain, gaps=pauses); <metadata> carries the full,
    executable instruction set AND a tuner-native `state` block so the package can
    be imported back into /tune losslessly (the full loop). No script, no external refs."""
    import json
    ops = _ops_from_text(text) or [("say", text, {})]
    instr = []
    for op in ops:
        if op[0] == "say":
            _, span, dv = op
            reg = dv["reg"] if "reg" in dv else base + dv.get("reg_off", 0)
            reg = max(0, min(4, int(reg)))
            if "gain" in dv:
                gain = float(dv["gain"])
            else:
                gain = tone.get("pressure", 1.0) * (1.0 + 0.08 * reg) * dv.get("force", 1.0)
            instr.append({"say": span.strip(), "register": reg,
                          "gain": round(max(0.4, min(2.5, gain)), 3),
                          "speed": round(float(dv.get("speed", dv.get("rate", 1.0))), 3),
                          "lift": bool(dv.get("lift") or span.rstrip().endswith("?")),
                          "vrgb": directive_to_vrgb(dv, base)})
        elif op[0] == "pause":
            instr.append({"pause_ms": int(op[1] * tone.get("break_mult", 1.0))})
        elif op[0] == "clip":
            instr.append({"cue": op[1]})

    H, y0 = 190, 150
    x = 20
    body = []
    for it in instr:
        if "say" in it:
            w = max(28, min(240, len(it["say"]) * 4))
            h = int(28 + (it["gain"] - 0.4) / 2.1 * 90)     # gain -> height
            body.append('<rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="%s" data-register="%d" data-gain="%s"/>'
                        % (x, y0 - h, w, h, it["vrgb"], it["register"], it["gain"]))
            label = (it["say"][:16] + ("..." if len(it["say"]) > 16 else "")).replace("&", "&amp;").replace("<", "&lt;")
            body.append('<text x="%d" y="%d" fill="#9a9a9a" font-size="10">%s</text>' % (x, y0 + 14, label))
            x += w + 6
        elif "pause_ms" in it:
            g = max(6, int(it["pause_ms"] * 0.05))          # pause -> gap width
            body.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#3a3a3a" stroke-width="1"/>' % (x + g // 2, y0 - 10, x + g // 2, y0))
            x += g
        elif "cue" in it:
            body.append('<circle cx="%d" cy="%d" r="7" fill="none" stroke="#c9c9c9" stroke-width="1.2"/>' % (x + 8, y0 - 20))
            x += 22
    W = x + 20
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="Zilla Slab, Georgia, serif">' % (W, H, W, H)]
    out.append('<title>cue-vox voice delivery package</title>')
    out.append('<desc>Instructions for the transformational apparatus. Each swatch is a spoken span: fill is its VRGB coordinate (hue=register, saturation=force, lightness=rate), height is gain; gaps are pauses. The full executable instruction set is in the metadata.</desc>')
    out.append('<rect width="%d" height="%d" fill="#161616"/>' % (W, H))
    out.append('<text x="20" y="30" fill="#c9c9c9" font-size="13">voice delivery package  ·  v%d</text>' % PACKAGE_SCHEMA_VERSION)
    out.extend(body)
    meta = {"package": "cue-vox-voice-delivery", "version": PACKAGE_SCHEMA_VERSION,
            "markup_version": MARKUP_VERSION,
            "base_register": base, "tone": tone, "steer": steer, "script": text,
            "instructions": instr, "state": state or {},
            "note": "each say carries register/gain/speed/lift/vrgb; pause_ms are gaps; cue is a signature earcon; state re-imports into /tune"}
    out.append('<metadata id="vrgb-voice-package">%s</metadata>' % json.dumps(meta).replace("&", "&amp;").replace("<", "&lt;"))
    out.append('</svg>')
    return "\n".join(out)


def parse_vox(text):
    """Parse SSML into render ops with ACCUMULATING context (nested tags stack).
    Ops: ('say', text, directive), ('pause', ms), ('clip', name). Returns None if the
    text has no tags (caller falls back to plain-text/markdown handling)."""
    import xml.etree.ElementTree as ET
    s = (text or "").strip()
    if "<" not in s or ">" not in s:
        return None
    # Accept a full <speak>..</speak> document or bare markup; wrap so it has one root.
    if not s.lstrip().startswith("<speak"):
        s = "<speak>" + s + "</speak>"
    try:
        root = ET.fromstring(s)
    except ET.ParseError:
        return None
    ops = []

    def _tag(el):
        t = el.tag.lower()
        return t.split('}', 1)[1] if '}' in t else t     # tolerate xmlns

    def say(t, ctx):
        if t and t.strip():
            ops.append(("say", t, dict(ctx)))

    def walk(el, ctx):
        say(el.text, ctx)
        for ch in el:
            tag = _tag(ch)
            if tag in ("break", "pause"):
                ops.append(("pause", int(_ssml_break_ms(ch))))
            elif tag in ("beat", "rest"):
                # A textured rest: the processing bed as rhythm/pacing. Duration via
                # time=/strength= like a break; bare <beat/> uses the default.
                ms = _ssml_break_ms(ch) if (ch.get("time") or ch.get("dur") or ch.get("strength")) else _BEAT_DEFAULT_MS
                ops.append(("beat", int(ms)))
            elif tag in ("laugh", "chuckle"):
                ops.append(("clip", tag))
            elif tag == "sub":
                say(ch.get("alias") or "", ctx)              # speak the alias, not the text
            elif tag in ("p", "s"):
                walk(ch, ctx)                                # content, then a boundary pause
                ops.append(("pause", 600 if tag == "p" else 250))
            else:
                walk(ch, _merge_ctx(ctx, _tag_contrib(tag, ch)))   # nested tags accumulate
            say(ch.tail, ctx)

    walk(root, {})
    return ops


def _fold_orphan_punct(ops):
    """Style-guide safety net: a say span that is only punctuation (a stray '.' left
    outside its parent tag) gets folded onto the previous spoken span, never voiced
    alone. Empty/whitespace spans are dropped. Authoring rule: keep the period INSIDE
    its tag (write '<aside>all green.</aside>', not '<aside>all green</aside>.')."""
    out = []
    for op in ops:
        if op and op[0] == "say":
            span = op[1] or ""
            if not span.strip():
                continue                                  # drop empty/whitespace spans
            if not any(c.isalnum() for c in span):        # punctuation-only orphan
                for j in range(len(out) - 1, -1, -1):
                    if out[j][0] == "say":
                        prev = out[j]
                        out[j] = ("say", prev[1].rstrip() + span.strip(), prev[2])
                        break
                continue                                  # nothing to attach to -> drop
        # Coalesce adjacent pauses so stacked <break/><break/> == <break=2/> exactly
        # (one op, no inter-segment gap), not just equal in total duration.
        if op and op[0] == "pause" and out and out[-1][0] == "pause":
            out[-1] = ("pause", out[-1][1] + op[1])
            continue
        out.append(op)
    return out


def _ops_from_text(text):
    """Semantic XML if it parses, else fall back to markdown/[pause] tags. Unified ops.

    If markup was ATTEMPTED (angle brackets present) but did not parse as SSML -- e.g.
    stale v1 markup like <break=3/> or <soft>, or a malformed tag -- strip residual
    <...> from the spoken spans so a broken tag is never READ ALOUD. Degrade to the
    words, never to tag-reading (dumb-by-design)."""
    ov = parse_vox(text)
    if ov is not None:
        return _fold_orphan_punct(ov)
    markup_attempted = "<" in (text or "") and ">" in (text or "")
    ops = []
    for op in parse_script(text):
        if op[0] == "say":
            _, span, emph = op
            if markup_attempted:
                span = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", span)).strip()
            off, spd = _EMPH_MAP.get(emph, (0, 1.0))
            if span:
                ops.append(("say", span, {"reg_off": off, "speed": spd}))
        else:
            ops.append(op)
    return _fold_orphan_punct(ops)


@app.route('/api/tune/sample', methods=['POST'])
def tune_sample():
    d = request.get_json(force=True, silent=True) or {}
    base = int(d.get('register', 0))
    # Delivery settings straight from the request so Hear renders the exact current
    # slider state (no dependence on the debounced params push). Fall back to globals.
    try:
        pressure = float(d.get('pressure', _PRESSURE))
    except (TypeError, ValueError):
        pressure = _PRESSURE
    try:
        break_mult = float(d.get('break_mult', _BREAK_MULT))
    except (TypeError, ValueError):
        break_mult = _BREAK_MULT
    try:
        lift_amt = float(d.get('lift_amount', 0.8))   # WORLD F0 question-rise strength
    except (TypeError, ValueError):
        lift_amt = 0.8
    kokoro_voice.set_prosody(lift=lift_amt)   # question-rise strength for this render
    text = d.get('text') or "Hey. Just kicking it here with you. This is the register."
    if not _kokoro_available:
        return jsonify(ok=False, error='kokoro unavailable'), 200

    import tempfile
    ops = _ops_from_text(text) or [("say", text, {})]
    parts = []
    for op in ops:
        if op[0] == "say":
            _, span, dv = op
            reg = dv["reg"] if "reg" in dv else base + dv.get("reg_off", 0)
            regc = max(0, min(4, int(reg)))
            kokoro_voice.set_register(regc)
            # Precise <prosody gain=...> overrides; otherwise volume pressure that
            # intensifies with the register (heat), scaled by accumulated force.
            if "gain" in dv:
                gain = float(dv["gain"])
            else:
                gain = pressure * (1.0 + 0.08 * regc) * dv.get("force", 1.0)
            gain = max(0.4, min(2.5, gain))
            speed = float(dv.get("speed", dv.get("rate", 1.0)))
            kokoro_voice.set_prosody(speed=speed, gain=gain)
            # A question (<ask> or trailing ?) gets the rise baked INTO the voice via
            # the WORLD F0-contour edit -- not a faked bend, not an earcon alongside.
            is_q = bool(dv.get("lift") or span.rstrip().endswith("?"))
            p = kokoro_voice.synth_to_file(span, question=is_q)
            if p:
                parts.append(p)
        elif op[0] == "pause":
            fd, sp = tempfile.mkstemp(suffix=".wav", prefix="pause-")
            os.close(fd)
            _silence_wav(int(op[1] * break_mult), sp)   # break-length multiplier (live)
            parts.append(sp)
        elif op[0] == "clip":
            cue = os.path.join(_SFX_DIR, "%s-%d.wav" % (op[1], max(0, min(4, base))))
            if not os.path.exists(cue):
                cue = os.path.join(_SFX_DIR, "%s.wav" % op[1])
            if os.path.exists(cue):
                parts.append(cue)
    kokoro_voice.set_prosody(speed=1.0, gain=1.0)   # reset so it doesn't leak to live turns
    if not parts:
        return jsonify(ok=False), 200

    fd, out = tempfile.mkstemp(suffix=".wav", prefix="tune-")
    os.close(fd)
    _concat_wavs(parts, out)
    for p in parts:
        if p.startswith(_SFX_DIR):
            continue   # shared cue file, keep it
        try:
            os.remove(p)
        except OSError:
            pass

    def _play_clean(o):
        try:
            subprocess.run(['afplay', o], check=False)
        finally:
            try:
                os.remove(o)
            except OSError:
                pass
    threading.Thread(target=_play_clean, args=(out,), daemon=True).start()
    return jsonify(ok=True, ops=len(ops))


@app.route('/api/tune/stop', methods=['POST'])
def tune_stop():
    subprocess.run(['killall', 'afplay'], stderr=subprocess.DEVNULL)
    return jsonify(ok=True)


_ENCODE_PROMPT = """You refine voice markup, shaping the delivery like clay. The text below may already contain tags an author placed by hand. PRESERVE their tags and their exact words. Only add or adjust markup where it clearly helps the read.

Tags you may use:
- <break dur='short'/>, <break dur='long'/>, or precise <break time='350ms'/> for pauses.
- <aside>...</aside> intimate set-aside (thrown away, lowered).
- <emphasis level='strong'>...</emphasis> for the few words that land hardest.
- <register level='0-4'>...</register> to shift a whole span (0 breathy, 4 dramatic).
- <prosody register='0-4' rate='0.9' gain='1.3'>...</prosody> for precise per-span control.
- <laugh/> or <chuckle/> for a light reaction.

Rules: AUGMENT ONLY. Keep every word exactly as given, do not add, remove, reorder, or change any word (not even contractions). You may ONLY insert or adjust markup tags around the existing words, and keep the author's existing tags. Do not over-tag. Return ONLY the annotated text, nothing else.

The design direction may be about the DELIVERY (the feel) OR about the MARKUP itself, for example: "put the period inside the emphasis tag", "wrap X in an aside", "move the break before Y", "tighten the pauses", "make the whole thing breathier". Apply it to the tags and to punctuation placement, while keeping every word.

Example
TEXT: Okay hear me out. I wasn't sure at first but now I think we are onto something.
REFINED: Okay <break dur='short'/> hear me out. <break/> I wasn't sure at first, <aside>honestly</aside> but now <break/> I think we're onto <emphasis level='strong'>something real</emphasis>."""


@app.route('/api/tune/encode', methods=['POST'])
def tune_encode():
    d = request.get_json(force=True, silent=True) or {}
    text = (d.get('text') or '').strip()
    if not text:
        return jsonify(ok=False, error='no text'), 400
    steer = (d.get('steer') or '').strip()
    # Refine in place, steered: pass the current markup as-is plus an optional
    # design direction so the model shapes what's there toward the steer.
    steer_block = ("\nDESIGN DIRECTION (apply this): %s\n" % steer) if steer else ""
    prompt = "%s%s\nTEXT: %s\nREFINED:" % (_ENCODE_PROMPT, steer_block, text)
    try:
        from ollama_client import generate
        out = (generate(prompt, max_tokens=500, timeout=45, temperature=0.35) or '').strip()
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200
    if out.upper().startswith('REFINED:'):
        out = out.split(':', 1)[1].strip()

    # Augment-only guard: the words (tags stripped) must be unchanged. If the model
    # altered any word, keep the author's text untouched rather than mangle it.
    def _wtok(s):
        return re.findall(r"[a-z0-9']+", re.sub(r"<[^>]+>", "", s or "").lower())
    if not out:
        return jsonify(ok=False, error='empty'), 200
    if _wtok(out) != _wtok(text):
        return jsonify(ok=True, text=text, augmented=False,
                       warning='kept your words unchanged (regen tried to alter them)')
    return jsonify(ok=True, text=out, augmented=True)


@app.route('/api/tune/package', methods=['POST'])
def tune_package():
    from flask import Response
    d = request.get_json(force=True, silent=True) or {}
    text = (d.get('text') or '').strip()
    base = int(d.get('register', 0))

    def _f(k, dflt):
        try:
            return float(d.get(k, dflt))
        except (TypeError, ValueError):
            return dflt
    tone = {'gamma': _f('gamma', _TONE_GAMMA), 'cap': _f('cap', _TONE_CAP), 'decay': _f('decay', _TONE_DECAY),
            'loud_floor': _f('loud_floor', _LOUD_FLOOR), 'break_mult': _f('break_mult', _BREAK_MULT),
            'pressure': _f('pressure', _PRESSURE), 'lift': _f('lift', 0.8)}
    # State mirrors the tuner's own dial keys (register/gamma/cap/decay/loud/brk/
    # pressure/lift + text/steer) so import maps 1:1 back onto the sliders.
    state = {'register': base, 'gamma': tone['gamma'], 'cap': tone['cap'], 'decay': tone['decay'],
             'loud': tone['loud_floor'], 'brk': tone['break_mult'], 'pressure': tone['pressure'],
             'lift': tone['lift'], 'voice': _ACTIVE_VOICE, 'text': text, 'steer': d.get('steer', '')}
    svg = crystallize_package(text, base, tone, steer=d.get('steer', ''), state=state)
    print("\n" + "=" * 52 + "\n  cue-vox package EXPORT  |  schema v%d\n" % PACKAGE_SCHEMA_VERSION
          + "=" * 52, flush=True)
    return Response(svg, mimetype='image/svg+xml',
                    headers={'Content-Disposition': 'attachment; filename=voice-package-v%d.svg' % PACKAGE_SCHEMA_VERSION,
                             'X-Package-Version': str(PACKAGE_SCHEMA_VERSION)})


def _package_meta_from_svg(raw):
    """Parse a cue-vox voice package (raw SVG or JSON {svg}) into its metadata dict.
    Raises ValueError if it is not a valid cue-vox package."""
    import json
    raw = raw or ''
    if raw.lstrip().startswith('{'):
        try:
            raw = json.loads(raw).get('svg') or raw
        except (ValueError, AttributeError):
            pass
    m = re.search(r'<metadata[^>]*>(.*?)</metadata>', raw, re.S)
    if not m:
        raise ValueError('no <metadata> package block found')
    # reverse crystallize's escaping: it did & -> &amp; then < -> &lt;
    body = m.group(1).replace('&lt;', '<').replace('&amp;', '&')
    meta = json.loads(body)
    if meta.get('package') != 'cue-vox-voice-delivery':
        raise ValueError('not a cue-vox voice package')
    return meta


def _state_from_meta(meta):
    """Tuner-native state dict (register/gamma/cap/decay/loud/brk/pressure/lift/text/
    steer) from a package meta, with a fallback for legacy packages lacking `state`."""
    state = meta.get('state') or {}
    if not state:
        tone = meta.get('tone') or {}
        state = {'register': meta.get('base_register', 0), 'gamma': tone.get('gamma'),
                 'cap': tone.get('cap'), 'decay': tone.get('decay'), 'loud': tone.get('loud_floor'),
                 'brk': tone.get('break_mult'), 'pressure': tone.get('pressure'),
                 'lift': tone.get('lift', 0.8), 'text': meta.get('script', ''),
                 'steer': meta.get('steer', '')}
        state = {k: v for k, v in state.items() if v is not None}
    return state


def _apply_live_voice(state):
    """Apply a package/tuner state dict to the LIVE voice globals. Shared by the
    deployed-voice boot loader and the deploy endpoint."""
    global _TONE_GAMMA, _TONE_CAP, _TONE_DECAY, _LOUD_FLOOR, _BREAK_MULT, _PRESSURE, _LIVE_REGISTER_FLOOR
    # A package can pin which voice it deploys with; apply it before the tone knobs so
    # the register floor lands on the right voice's registers.
    if state.get('voice'):
        set_active_voice(state['voice'])
    try:
        if state.get('gamma') is not None: _TONE_GAMMA = float(state['gamma'])
        if state.get('cap') is not None: _TONE_CAP = float(state['cap'])
        if state.get('decay') is not None: _TONE_DECAY = float(state['decay'])
        if state.get('loud') is not None: _LOUD_FLOOR = float(state['loud'])
        if state.get('brk') is not None: _BREAK_MULT = max(0.25, min(4.0, float(state['brk'])))
        if state.get('pressure') is not None: _PRESSURE = max(0.4, min(2.5, float(state['pressure'])))
        if state.get('register') is not None:
            _LIVE_REGISTER_FLOOR = max(0, min(4, int(round(float(state['register'])))))
        if state.get('lift') is not None and _kokoro_available:
            kokoro_voice.set_prosody(lift=max(0.0, min(1.5, float(state['lift']))))
    except (TypeError, ValueError):
        pass


def _deployed_voice_path():
    """Where the deployed voice package lives. Explicit CUE_VOX_VOICE_PACKAGE wins;
    otherwise the maestro convention path (config/voice/deployed.svg under MAESTRO_ROOT),
    so a maestro deployment needs no extra config. Public repo (neither set) -> ''."""
    p = (os.environ.get('CUE_VOX_VOICE_PACKAGE') or '').strip()
    if p:
        return p
    root = (os.environ.get('MAESTRO_ROOT') or '').strip()
    return os.path.join(root, 'config', 'voice', 'deployed.svg') if root else ''


def _blend_recipe_path():
    """Where the register-ladder blend recipe (blend.json) lives. Explicit
    CUE_VOX_BLEND_RECIPE wins; otherwise the maestro convention path. Public repo
    (neither set) -> '', so the tuner's blend panel is simply inert."""
    p = (os.environ.get('CUE_VOX_BLEND_RECIPE') or '').strip()
    if p:
        return p
    root = (os.environ.get('MAESTRO_ROOT') or '').strip()
    return os.path.join(root, 'config', 'voice', 'blend.json') if root else ''


def _load_deployed_voice():
    """On boot, seed the live voice from the maestro-owned deployed package if one is
    configured (see _deployed_voice_path). Nothing configured -> generic built-in
    defaults, so the public cue-vox repo ships with no personal voice baked in."""
    path = _deployed_voice_path()
    if not path:
        _vlog('voice', 'no deployed package configured -> generic default voice')
        return
    if not os.path.exists(path):
        _vlog('voice', 'deployed package not found: %s -> generic default' % path)
        return
    try:
        meta = _package_meta_from_svg(open(path, encoding='utf-8').read())
        _apply_live_voice(_state_from_meta(meta))
        _vlog('voice', 'deployed voice v%s loaded from %s (reg_floor=%s gamma=%.2f pressure=%.2f)'
              % (meta.get('version'), path, _LIVE_REGISTER_FLOOR, _TONE_GAMMA, _PRESSURE))
    except (ValueError, OSError) as e:
        _vlog('voice', 'failed to load %s: %s -> generic default' % (path, e))


@app.route('/api/tune/import', methods=['POST'])
def tune_import():
    """Read an exported voice package SVG back into a tuner state dict (the full loop).
    Accepts raw SVG or JSON {svg}. Prefers the tuner-native `state` block; falls back
    to reconstructing from base_register/tone/script for packages minted before it."""
    try:
        meta = _package_meta_from_svg(request.get_data(as_text=True) or '')
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 200
    ver = meta.get('version', 0)
    mver = meta.get('markup_version', 0)
    stale = ver != PACKAGE_SCHEMA_VERSION
    markup_stale = mver != MARKUP_VERSION
    banner = "  cue-vox package IMPORT  |  schema v%s (cur v%d)%s  |  markup v%s (cur v%d)%s" % (
        ver, PACKAGE_SCHEMA_VERSION, " STALE" if stale else "",
        mver, MARKUP_VERSION, " STALE" if markup_stale else "")
    print("\n" + "=" * 62 + "\n" + banner + "\n" + "=" * 62, flush=True)
    return jsonify(ok=True, state=_state_from_meta(meta), version=ver,
                   current_version=PACKAGE_SCHEMA_VERSION, stale=stale,
                   markup_version=mver, current_markup_version=MARKUP_VERSION, markup_stale=markup_stale)


@app.route('/api/tune/deploy', methods=['POST'])
def tune_deploy():
    """Write the current package to the maestro-owned deployed-voice path
    (CUE_VOX_VOICE_PACKAGE) and apply it to the live voice immediately. This is how a
    tuned voice becomes THE voice cue-vox boots with. No env set -> nothing to deploy to."""
    path = _deployed_voice_path()
    if not path:
        return jsonify(ok=False, error='no deploy target: set CUE_VOX_VOICE_PACKAGE or MAESTRO_ROOT'), 200
    d = request.get_json(force=True, silent=True) or {}
    text = (d.get('text') or '').strip()
    base = int(d.get('register', 0))

    def _f(k, dflt):
        try:
            return float(d.get(k, dflt))
        except (TypeError, ValueError):
            return dflt
    tone = {'gamma': _f('gamma', _TONE_GAMMA), 'cap': _f('cap', _TONE_CAP), 'decay': _f('decay', _TONE_DECAY),
            'loud_floor': _f('loud_floor', _LOUD_FLOOR), 'break_mult': _f('break_mult', _BREAK_MULT),
            'pressure': _f('pressure', _PRESSURE), 'lift': _f('lift', 0.8)}
    state = {'register': base, 'gamma': tone['gamma'], 'cap': tone['cap'], 'decay': tone['decay'],
             'loud': tone['loud_floor'], 'brk': tone['break_mult'], 'pressure': tone['pressure'],
             'lift': tone['lift'], 'voice': _ACTIVE_VOICE, 'text': text, 'steer': d.get('steer', '')}
    svg = crystallize_package(text, base, tone, steer=d.get('steer', ''), state=state)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(svg)
    except OSError as e:
        return jsonify(ok=False, error='write failed: %s' % e), 200
    _apply_live_voice(state)     # take effect now, not just next boot
    _vlog('voice', 'DEPLOYED v%d -> %s (applied live)' % (PACKAGE_SCHEMA_VERSION, path))
    return jsonify(ok=True, path=path, version=PACKAGE_SCHEMA_VERSION)


@app.route('/api/tune/blend', methods=['GET', 'POST'])
def tune_blend():
    """The blend recipe loop: GET returns the maestro-owned register-ladder recipe
    (blend.json); POST uploads an edited recipe back, and (with rebuild) regenerates
    register-voices.bin via build-refs.sh and hot-reloads the engine so the new blend
    is live. Nothing configured -> the panel is inert (public repo)."""
    import json
    path = _blend_recipe_path()
    if not path:
        return jsonify(ok=False, error='no blend recipe configured (set MAESTRO_ROOT or CUE_VOX_BLEND_RECIPE)'), 200
    if request.method == 'GET':
        if not os.path.exists(path):
            return jsonify(ok=False, error='blend recipe not found: %s' % path), 200
        try:
            return jsonify(ok=True, path=path, blend=json.load(open(path, encoding='utf-8')))
        except (ValueError, OSError) as e:
            return jsonify(ok=False, error='read failed: %s' % e), 200
    # POST: validate, write, optionally rebuild + reload
    d = request.get_json(force=True, silent=True) or {}
    blend = d.get('blend')
    if isinstance(blend, str):
        try:
            blend = json.loads(blend)
        except ValueError as e:
            return jsonify(ok=False, error='blend is not valid JSON: %s' % e), 200
    if not isinstance(blend, dict) or not isinstance(blend.get('registers'), list):
        return jsonify(ok=False, error='blend must be an object with a "registers" list'), 200
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(blend, fh, indent=2)
            fh.write('\n')
    except OSError as e:
        return jsonify(ok=False, error='write failed: %s' % e), 200
    _vlog('blend', 'uploaded %d registers -> %s' % (len(blend['registers']), path))
    rebuilt, rebuild_msg = False, ''
    if d.get('rebuild'):
        import subprocess
        script = os.path.join(os.path.dirname(path), 'build-refs.sh')
        if os.path.exists(script):
            try:
                r = subprocess.run(['bash', script], capture_output=True, text=True, timeout=120)
                rebuilt = r.returncode == 0
                rebuild_msg = (r.stdout + r.stderr).strip()[-300:]
                if rebuilt and _kokoro_available:
                    kokoro_voice.reload()     # hot-swap the new register-voices.bin
                _vlog('blend', 'rebuild %s%s' % ('ok + engine reloaded' if rebuilt else 'FAILED',
                                                 '' if rebuilt else ': ' + rebuild_msg))
            except (OSError, subprocess.SubprocessError) as e:
                rebuild_msg = str(e)
        else:
            rebuild_msg = 'build-refs.sh not found next to blend.json'
    return jsonify(ok=True, path=path, rebuilt=rebuilt, rebuild_msg=rebuild_msg)


@app.route('/api/tune/translate', methods=['POST'])
def tune_translate():
    d = request.get_json(force=True, silent=True) or {}
    return jsonify(ok=True, text=deterministic_markup(d.get('text') or ''))


@app.route('/api/tune/parse', methods=['POST'])
def tune_parse():
    d = request.get_json(force=True, silent=True) or {}
    text = d.get('text') or ''
    base = int(d.get('register', 0))

    def _f(k, dflt):
        try:
            return float(d.get(k, dflt))
        except (TypeError, ValueError):
            return dflt
    # Parse auditions the register at `base` (the tuner's register slider), so map the
    # SSML through THAT register's envelope.
    tone = {'pressure': _f('pressure', _PRESSURE), 'break_mult': _f('break_mult', _BREAK_MULT),
            'prosody': _prosody_for_slot(base)}
    instr = resolve_instructions(text, base, tone)
    tags = set(m.lower() for m in re.findall(r"</?([a-zA-Z][\w-]*)", text))
    unknown = sorted(t for t in tags if t not in _KNOWN_TAGS)
    return jsonify(ok=True, ops=instr, unknown=unknown, markup_version=MARKUP_VERSION)


# Lightweight persistence: one working state (text + settings + steering) so a
# refresh doesn't lose progress. Save writes it; the panel restores it on load.
_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_state.json")


@app.route('/api/tune/state', methods=['GET', 'POST'])
def tune_state():
    import json
    if request.method == 'POST':
        d = request.get_json(force=True, silent=True) or {}
        try:
            json.dump(d, open(_STATE_PATH, 'w'), indent=2)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200
        return jsonify(ok=True)
    try:
        st = json.load(open(_STATE_PATH)) if os.path.exists(_STATE_PATH) else {}
    except Exception:
        st = {}
    return jsonify(ok=True, state=st, version=PACKAGE_SCHEMA_VERSION, markup_version=MARKUP_VERSION)


@app.route('/api/tune/voices', methods=['GET'])
def tune_voices():
    """The voice roster for the tuner: every voice (label/kind/sid/blend + its
    registers and per-register prosody rules) plus which one is active."""
    return jsonify(ok=True, active=_ACTIVE_VOICE, voices=(_VOICES or {}).get('voices', {}))


@app.route('/api/tune/voice', methods=['POST'])
def tune_voice():
    """Switch the live voice and remember it (persist active to voices.json so the
    choice survives boot even without a deployed package)."""
    global _ACTIVE_VOICE
    d = request.get_json(force=True, silent=True) or {}
    vid = (d.get('voice') or '').strip()
    if not set_active_voice(vid):
        return jsonify(ok=False, error='unknown voice: %s' % vid), 200
    if _VOICES is not None:
        _VOICES['active'] = vid
        _save_voices()
    v = ((_VOICES or {}).get('voices', {}) or {}).get(_ACTIVE_VOICE, {})
    reg = ((v.get('registers') or {}).get(_active_register) or {})
    return jsonify(ok=True, active=_ACTIVE_VOICE, register=_active_register, sample=reg.get('sample', ''))


@app.route('/api/tune/register', methods=['POST'])
def tune_register():
    """Select the active register (the render state) for the active voice, and return that
    register's sample so the tuner can load it. Persists the choice per voice. This is the
    register dropdown's endpoint -- Voice > Register, both first-class selectors."""
    d = request.get_json(force=True, silent=True) or {}
    vid = (d.get('voice') or _ACTIVE_VOICE or '').strip()
    if vid != _ACTIVE_VOICE:
        set_active_voice(vid)
    key = str(d.get('register', '')).strip()
    regs = (_VOICE_REGISTERS or {}).get('registers', {}) or {}
    if key not in regs:
        return jsonify(ok=False, error='unknown register: %s' % key), 200
    set_render_register(key, 'tuner')
    v = ((_VOICES or {}).get('voices', {}) or {}).get(vid)
    if v is not None:
        v['active_register'] = key       # remember per voice (survives boot)
        _save_voices()
    reg = regs.get(key, {})
    return jsonify(ok=True, register=key, slot=int(reg.get('kokoro_slot', 0)), sample=reg.get('sample', ''))


@app.route('/api/tune/registers', methods=['POST'])
def tune_registers():
    """Save a voice's registers + per-register prosody rules back to voices.json (the
    source of truth, so it survives boot) and refresh the live view if it is the active
    voice. Body: {voice, registers:{"1":{label,speed,bright,gain,exaggeration,kokoro_slot,
    prosody:{break:[min,max],emphasis:[min,max],rate:[min,max],lift:[min,max]}}, ...}}.
    A scalar prosody value is accepted and stored as [s,s]."""
    global _VOICE_REGISTERS
    d = request.get_json(force=True, silent=True) or {}
    vid = (d.get('voice') or _ACTIVE_VOICE or '').strip()
    incoming = d.get('registers') or {}
    v = ((_VOICES or {}).get('voices', {}) or {}).get(vid)
    if not v or not isinstance(incoming, dict):
        return jsonify(ok=False, error='unknown voice or bad registers: %s' % vid), 200

    def _cl(x, lo, hi, dflt):
        try:
            return max(lo, min(hi, float(x)))
        except (TypeError, ValueError):
            return dflt

    regs = v.setdefault('registers', {})
    for key, r in incoming.items():
        if not isinstance(r, dict):
            continue
        cur = regs.setdefault(str(key), {})
        if 'label' in r:
            cur['label'] = str(r['label'])[:32]
        if 'sample' in r:                       # per-register SSML sample
            cur['sample'] = str(r['sample'])
        if 'kokoro_slot' in r:
            cur['kokoro_slot'] = int(_cl(r['kokoro_slot'], 0, 4, cur.get('kokoro_slot', 0)))
        for k, lo, hi in (('speed', 0.5, 2.0), ('bright', -1.0, 3.0), ('gain', 0.1, 3.0), ('exaggeration', 0.3, 2.0)):
            if k in r:
                cur[k] = round(_cl(r[k], lo, hi, cur.get(k, 1.0)), 3)
        pin = r.get('prosody') or {}
        if pin:
            pr = cur.setdefault('prosody', {})
            for k, lo, hi in (('break', 0.25, 4.0), ('emphasis', 0.5, 3.0),
                              ('rate', 0.5, 2.0), ('lift', 0.0, 1.5)):
                if k in pin:
                    val = pin[k]
                    if isinstance(val, (list, tuple)) and len(val) == 2:
                        a = round(_cl(val[0], lo, hi, lo), 3)
                        b = round(_cl(val[1], lo, hi, hi), 3)
                        pr[k] = [min(a, b), max(a, b)]
                    else:                       # scalar -> degenerate [s,s]
                        s = round(_cl(val, lo, hi, 1.0), 3)
                        pr[k] = [s, s]
    if not _save_voices():
        return jsonify(ok=False, error='write failed'), 200
    if vid == _ACTIVE_VOICE:                    # refresh the live register view
        _VOICE_REGISTERS = {'registers': v.get('registers', {})}
    _vlog('voice', 'registers saved for %s (%d)' % (vid, len(incoming)))
    return jsonify(ok=True, voice=vid, registers=v.get('registers', {}))


@app.route('/api/tune/export.svg')
def tune_export():
    from flask import Response
    import voice_crystallize
    models_dir = os.path.dirname(kokoro_voice.MODEL_DIR) if _kokoro_available else 'models'
    svg = voice_crystallize.crystallize(models_dir, tone={'decay': _TONE_DECAY, 'cap': _TONE_CAP, 'gamma': _TONE_GAMMA})
    return Response(svg, mimetype='image/svg+xml',
                    headers={'Content-Disposition': 'attachment; filename=voice-register-ladder.svg'})


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
    """Serve data from the cold port (conditional=True -> HTTP range for video)."""
    directory, fname = _resolve_vault_image(slug, filename, "cold")
    if directory is None:
        return "Not found", 404
    return send_from_directory(directory, fname, conditional=True)


@app.route("/vault/hot/<slug>/<path:filename>")
def serve_vault_hot(slug, filename):
    """Serve data from the hot port (conditional=True -> HTTP range for video)."""
    directory, fname = _resolve_vault_image(slug, filename, "hot")
    if directory is None:
        return "Not found", 404
    return send_from_directory(directory, fname, conditional=True)


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

        # Try cut clips + posters under processed/<slug>/ (conditional=True
        # gives HTTP range support so video seeks/streams instead of stalling).
        proc_dir = os.path.join(vp, "processed", slug)
        proc_path = os.path.join(proc_dir, filename)
        if os.path.isfile(proc_path):
            return send_from_directory(proc_dir, filename, conditional=True)

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
    legacy_dir = MAESTRO_ROOT / "core" / "cue-vox" / "drops"
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
        vault_lib = MAESTRO_ROOT / "core" / "vault-template" / "app" / "lib"
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
            c2d2_path = MAESTRO_ROOT / "core" / "c2d2"
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
        c2d2_path = MAESTRO_ROOT / "core" / "c2d2"
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
        c2d2_path = MAESTRO_ROOT / "core" / "c2d2"
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
    tag_re = re.compile(r"\[(YES_NO|INPUT|APPROVAL|DOCUMENT|CUE|GALLERY|CITATIONS|PIN_NOTE|PIN_NINJA):\s*")
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
        c2d2_path = MAESTRO_ROOT / "core" / "c2d2"
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

    # Polished 3-tier selection summary, if one has been generated.
    cs_summary = {}
    try:
        _sfd, _sp, _sd = _vx_find_folder(gallery_slug)
        if _sfd:
            _scp = os.path.join(_sfd, "captions.json")
            if os.path.isfile(_scp):
                cs_summary = (json.load(open(_scp)) or {}).get("_summary", {}) or {}
    except Exception:
        cs_summary = {}

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
        summary=cs_summary,
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

    # Per-clip transcripts from the gallery's spans.json (HOT or keeper).
    _span_map = {}
    try:
        _tfd, _tp, _td = _vx_find_folder(gallery_slug)
        if _tfd:
            _tsp = os.path.join(_tfd, "spans.json")
            if os.path.isfile(_tsp):
                _span_map = json.load(open(_tsp, encoding="utf-8")) or {}
    except Exception:
        _span_map = {}

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

        # Per-clip transcript from the gallery's spans.json (HOT or keeper).
        if not transcript_text:
            transcript_text = (_span_map.get(fname) or "").strip()

        # Fallback to the episode-level keeper transcript
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


def _vx_emit_poster(clip_path):
    """Background byproduct: one freeze-frame poster beside the clip.
    Idempotent -- skips if it already exists. Never surfaced to the user."""
    base, _ext = os.path.splitext(clip_path)
    poster = base + ".thumb.jpg"
    if os.path.isfile(poster):
        return poster
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1.5", "-i", clip_path,
             "-frames:v", "1", "-q:v", "3", poster],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
    except Exception as exc:
        print("[from-keeper] poster emit failed for %s: %s" % (clip_path, exc))
    return poster if os.path.isfile(poster) else None


def _vx_caption_from_name(fname):
    base = os.path.splitext(fname)[0]
    if base.startswith("q_"):
        return "Question"
    parts = base.split("_")
    label = "_".join(parts[2:]) if len(parts) > 2 else base
    return label.replace("-", " ").title()


def _vx_voice_card(folder_dir=None):
    """Load the steering text that lives WITH the videos. Walks up from the clip
    folder: <folder>/steering.txt, <parent>/steering.txt, <parent>/_context/
    steering.txt. Returns "" if none (captions still get written, just unsteered)."""
    candidates = []
    if folder_dir:
        parent = os.path.dirname(os.path.normpath(folder_dir))
        candidates = [
            os.path.join(folder_dir, "steering.txt"),
            os.path.join(parent, "steering.txt"),
            os.path.join(parent, "_context", "steering.txt"),
        ]
    for p in candidates:
        try:
            if os.path.isfile(p):
                return open(p, encoding="utf-8").read()
        except Exception:
            pass
    return ""


def _vx_is_caption_feedback(text):
    """Cheap local (C2D2) yes/no: is this utterance feedback to change captions?
    Used to route voice feedback on the active gallery into a refine pass."""
    if not text or len(text.split()) < 2:
        return False
    try:
        from ollama_client import chat
        r = chat(
            "You answer with exactly one word: yes or no.",
            "The user is looking at a gallery of captioned video clips. Does this "
            "message ask to change, fix, correct, shorten, or reword a caption or what "
            "a clip/slide says? Message: \"" + text + "\"",
            max_tokens=3, timeout=20,
        )
        return (r or "").strip().lower().startswith("y")
    except Exception:
        return False


def _vx_frontier_generate(prompt, timeout=180):
    """One frontier (Claude) call for the polish pass. Run from a neutral cwd so it
    does not load the heavy project context -- keeps the single call lean."""
    import tempfile
    try:
        p = subprocess.Popen(
            ["claude", "-p"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=tempfile.gettempdir(), env=CLEAN_CLAUDE_ENV,
        )
        out, err = p.communicate(input=prompt, timeout=timeout)
        if err and err.strip():
            print("[polish] claude stderr: %s" % err.strip()[:300])
        return (out or "").strip()
    except Exception as exc:
        print("[polish] frontier call failed: %s" % exc)
        return ""


def _vx_batch_caption(items, voice_card, mode, notes="", engine="c2d2"):
    """ONE batched call for a whole gallery -- never per-caption. engine 'c2d2'
    (local, free) or 'frontier' (Claude, the paid polish pass).

    items: {filename: {"role", "transcript", "caption"}}. 'transcript' is what is
    actually said in the clip (ground truth). 'role'=='question' marks the
    interviewer's question slide. mode 'draft'|'align'|'refine'. Returns
    {filename: caption}; empty dict on failure (callers keep existing text)."""
    if not items:
        return {}
    try:
        from ollama_client import chat
    except Exception as exc:
        print("[caption] ollama_client unavailable: %s" % exc)
        return {}
    grounding = (
        "You are captioning video clips. The JSON below maps each filename to its "
        "role, transcript, and current caption. The transcript is the ONLY source of "
        "truth -- it is what is actually said in that clip. Caption strictly from the "
        "transcript: do NOT add or invent any name, title, number, place, or claim that "
        "is not present in that clip's transcript. If the transcript is thin, keep the "
        "caption thin. A clip whose role is \"question\" is the interviewer's question "
        "slide: its caption must be a faithful recap of the question actually asked in "
        "that transcript -- restate that question, nothing more. Write each caption in "
        "the voice above, one sentence.")
    if mode == "draft":
        task = grounding + (" Write a fresh caption for each clip from its transcript. "
                "Return ONLY a JSON object {filename: caption}, nothing else.")
    elif mode == "refine":
        task = grounding + (" Apply these editor corrections: " + json.dumps(notes) +
                ". Fix any caption that drifts from its transcript or that the "
                "corrections call out. If a caption already tracks its transcript and "
                "reads well, return it UNCHANGED. Return ONLY a JSON object "
                "{filename: caption}, nothing else.")
    else:
        task = grounding + (" Rewrite each caption to the voice while keeping it true to "
                "the transcript. If a caption already tracks and reads well, return it "
                "UNCHANGED. Return ONLY a JSON object {filename: caption}, nothing else.")
    system = (voice_card + "\n\n" + task) if voice_card else task
    payload = {}
    for fn, d in items.items():
        if isinstance(d, dict):
            payload[fn] = {"role": d.get("role", "answer"),
                           "transcript": (d.get("transcript") or "")[:500],
                           "caption": d.get("caption", "")}
        else:
            payload[fn] = {"transcript": str(d)[:500]}
    user = json.dumps(payload, ensure_ascii=False)
    try:
        if engine == "frontier":
            resp = _vx_frontier_generate(system + "\n\n" + user)
        else:
            resp = chat(system, user, max_tokens=900, timeout=180)
    except Exception as exc:
        print("[caption] %s call failed: %s" % (engine, exc))
        return {}
    m = re.search(r"\{.*\}", resp or "", re.DOTALL)
    if not m:
        print("[caption] no JSON in C2D2 response")
        return {}
    try:
        out = json.loads(m.group(0))
    except Exception as exc:
        print("[caption] JSON parse failed: %s" % exc)
        return {}
    return {k: str(v).strip() for k, v in out.items()
            if isinstance(v, str) and v.strip()}


def _vx_is_question(fn):
    """The question slide, regardless of sort-prefix (q_ll.mp4, 00-q_ll.mp4, ...)."""
    base = re.sub(r'^[^A-Za-z]+', '', os.path.basename(fn)).lower()
    return base.startswith("q_") or base.startswith("q.")


def _vx_find_folder(folder):
    """Resolve a dropped gallery folder. HOT vault first (where the clips live),
    keeper second. Returns (folder_dir, port, device_id) or (None, None, None)."""
    for root in _VAULT_SEARCH_PATHS:
        if not root.is_dir():
            continue
        cand = root / folder
        if cand.is_dir():
            return str(cand), "hot", ""
        for sub in root.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                c = sub / folder
                if c.is_dir():
                    return str(c), "hot", ""
    try:
        import sys as _sys
        _sys.path.insert(0, str(MAESTRO_ROOT / "tools" / "keeper"))
        from discovery import list_keepers, get_volume_path
        for k in list_keepers():
            vp = get_volume_path(k.get("device_id", ""))
            if vp and os.path.isdir(os.path.join(vp, "processed", folder)):
                return os.path.join(vp, "processed", folder), "keeper", k.get("device_id", "")
    except Exception:
        pass
    return None, None, None


def _vx_ensure_spans(folder_dir, clips):
    """Ensure spans.json (per-clip transcript). Transcribes any missing clip with
    Whisper -- the literal 'check the footage' step -- and caches the result."""
    sp = os.path.join(folder_dir, "spans.json")
    spans = {}
    if os.path.isfile(sp):
        try:
            spans = json.load(open(sp, encoding="utf-8"))
        except Exception:
            spans = {}
    missing = [c for c in clips if not (spans.get(c) or "").strip()]
    if missing:
        try:
            model = get_whisper_model()
            for c in missing:
                try:
                    r = model.transcribe(os.path.join(folder_dir, c))
                    spans[c] = (r.get("text") or "").strip()
                    print("[spans] transcribed %s (%d chars)" % (c, len(spans[c])))
                except Exception as exc:
                    print("[spans] transcribe failed for %s: %s" % (c, exc))
            try:
                json.dump(spans, open(sp, "w"), ensure_ascii=False, indent=1)
            except Exception:
                pass
        except Exception as exc:
            print("[spans] whisper unavailable: %s" % exc)
    return spans


def _vx_clip_items(target_dir, clips, captions):
    """Build {filename: {role, transcript, caption}} -- grounds each caption in the
    clip's actual transcript (spans.json) and marks the question slide by role."""
    spans = {}
    sp = os.path.join(target_dir, "spans.json")
    if os.path.isfile(sp):
        try:
            spans = json.load(open(sp, encoding="utf-8"))
        except Exception:
            spans = {}
    return {fn: {
        "role": "question" if _vx_is_question(fn) else "answer",
        "transcript": spans.get(fn, "") or "",
        "caption": captions.get(fn, ""),
    } for fn in clips}


@app.route("/api/gallery/from-keeper", methods=["POST"])
def api_gallery_from_keeper():
    """Build one gallery from a question folder that already lives on a keeper.

    Clips are referenced in place (port=keeper, no re-upload); posters are
    emitted as a background byproduct; captions/title come from the folder's
    captions.json sidecar if present. One folder -> one gallery -> one
    contact sheet."""
    data = request.get_json() or {}
    folder = (data.get("folder") or "").strip().strip("/")
    if not folder:
        return jsonify({"ok": False, "error": "no folder"}), 400

    folder_dir, port, device_id = _vx_find_folder(folder)
    if not folder_dir:
        return jsonify({"ok": False, "error": "folder not found"}), 404

    clips = [f for f in os.listdir(folder_dir)
             if os.path.splitext(f)[1].lower() in _VX_VIDEO_EXTS]
    clips.sort(key=lambda n: (0, n) if _vx_is_question(n) else (1, n))
    if not clips:
        return jsonify({"ok": False, "error": "no clips in folder"}), 404

    cap_path = os.path.join(folder_dir, "captions.json")
    captions = {}
    if os.path.isfile(cap_path):
        try:
            captions = json.load(open(cap_path))
        except Exception:
            captions = {}
    title = captions.get("_title") or folder

    # Ground truth: transcribe each clip (cached), then write captions with the
    # better model, grounded strictly in that transcript + the steering file.
    spans = _vx_ensure_spans(folder_dir, clips)
    if not any(k != "_title" for k in captions):
        draft_items = {c: {
            "role": "question" if _vx_is_question(c) else "answer",
            "transcript": spans.get(c, "") or "", "caption": "",
        } for c in clips}
        drafted = _vx_batch_caption(draft_items, _vx_voice_card(folder_dir=folder_dir),
                                    "draft", engine="frontier")
        if drafted:
            captions.update(drafted)
            captions.setdefault("_title", title)
            try:
                json.dump(captions, open(cap_path, "w"), ensure_ascii=False, indent=1)
            except Exception:
                pass

    items = []
    for fn in clips:
        _vx_emit_poster(os.path.join(folder_dir, fn))  # background, idempotent
        base = os.path.splitext(fn)[0]
        item = {
            "slug": folder, "filename": fn, "port": port,
            "thumbnail": base + ".thumb.jpg", "type": "video",
            "caption": captions.get(fn) or _vx_caption_from_name(fn),
        }
        if port == "keeper":
            item["device"] = (device_id or "")[:8]
        items.append(item)

    try:
        import sqlite3 as _s
        conn = _s.connect(str(MAESTRO_ROOT / "vault-cold" / "vault.db"))
        conn.execute(
            "INSERT OR REPLACE INTO galleries (slug,title,images,source_path,steering) "
            "VALUES (?,?,?,?,?)",
            (folder, title, json.dumps(items), folder_dir, ""),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        return jsonify({"ok": False, "error": "persist failed: %s" % exc}), 500

    print("[from-keeper] gallery '%s' (%s) built: %d clips" % (folder, port, len(items)))
    return jsonify({
        "ok": True, "slug": folder, "title": title, "images": items,
        "spoken": "gallery ready: %s" % title,
    })


def _vx_caption_pass(slug, mode, notes="", engine="c2d2"):
    """One batched pass over a gallery's captions. mode 'align' rewrites to voice;
    'refine' also applies editor `notes`. engine 'c2d2' (local) or 'frontier'
    (Claude polish). Returns (payload_dict, status)."""
    target_dir, port, device_id = _vx_find_folder(slug)
    if not target_dir:
        return {"ok": False, "error": "folder not found"}, 404

    cap_path = os.path.join(target_dir, "captions.json")
    captions = {}
    if os.path.isfile(cap_path):
        try:
            captions = json.load(open(cap_path))
        except Exception:
            captions = {}
    current = {k: v for k, v in captions.items() if k != "_title"}
    if not current:
        return {"ok": False, "error": "no captions yet"}, 400

    items = _vx_clip_items(target_dir, list(current.keys()), captions)
    result = _vx_batch_caption(items, _vx_voice_card(folder_dir=target_dir), mode, notes, engine)
    if not result:
        return {"ok": False, "error": "model produced nothing"}, 502

    captions.update(result)
    try:
        json.dump(captions, open(cap_path, "w"), ensure_ascii=False, indent=1)
    except Exception:
        pass

    title = captions.get("_title") or slug
    clips = [f for f in os.listdir(target_dir)
             if os.path.splitext(f)[1].lower() in _VX_VIDEO_EXTS]
    clips.sort(key=lambda n: (0, n) if _vx_is_question(n) else (1, n))
    items = []
    for fn in clips:
        base = os.path.splitext(fn)[0]
        item = {
            "slug": slug, "filename": fn, "port": port,
            "thumbnail": base + ".thumb.jpg", "type": "video",
            "caption": captions.get(fn) or _vx_caption_from_name(fn),
        }
        if port == "keeper":
            item["device"] = (device_id or "")[:8]
        items.append(item)
    try:
        import sqlite3 as _s
        conn = _s.connect(str(MAESTRO_ROOT / "vault-cold" / "vault.db"))
        conn.execute(
            "INSERT OR REPLACE INTO galleries (slug,title,images,source_path,steering) "
            "VALUES (?,?,?,?,?)",
            (slug, title, json.dumps(items), target_dir, ""),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        return {"ok": False, "error": "persist failed: %s" % exc}, 500

    print("[caption] %s pass: %d captions for %s" % (mode, len(result), slug))
    return {"ok": True, "slug": slug, "title": title,
            "images": items, "count": len(result)}, 200


@app.route("/api/gallery/<slug>/caption", methods=["POST"])
def api_gallery_caption(slug):
    """Directly set one clip's caption (manual inline edit). Writes the captions.json
    sidecar and updates the gallery row. No model in the loop."""
    data = request.get_json() or {}
    fn = (data.get("filename") or "").strip()
    caption = (data.get("caption") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "no filename"}), 400
    folder_dir, port, device_id = _vx_find_folder(slug)
    if not folder_dir:
        return jsonify({"ok": False, "error": "folder not found"}), 404
    cap_path = os.path.join(folder_dir, "captions.json")
    captions = {}
    if os.path.isfile(cap_path):
        try:
            captions = json.load(open(cap_path))
        except Exception:
            captions = {}
    captions[fn] = caption
    try:
        json.dump(captions, open(cap_path, "w"), ensure_ascii=False, indent=1)
    except Exception as exc:
        return jsonify({"ok": False, "error": "write failed: %s" % exc}), 500
    try:
        import sqlite3 as _s
        conn = _s.connect(str(MAESTRO_ROOT / "vault-cold" / "vault.db"))
        row = conn.execute("SELECT images FROM galleries WHERE slug=?", (slug,)).fetchone()
        if row:
            imgs = json.loads(row[0])
            for im in imgs:
                if im.get("filename") == fn:
                    im["caption"] = caption
            conn.execute("UPDATE galleries SET images=? WHERE slug=?",
                         (json.dumps(imgs), slug))
            conn.commit()
        conn.close()
    except Exception as exc:
        print("[caption-edit] row update failed: %s" % exc)
    print("[caption-edit] %s / %s set" % (slug, fn))
    return jsonify({"ok": True})


def _vx_gallery_summary(captions, voice_card, title):
    """Frontier: a polished summary of the whole selection at three lengths.
    Returns {three, one, sentence} or {} on failure. Grounded in the captions."""
    lines = [c for k, c in captions.items()
             if k not in ("_title", "_summary") and isinstance(c, str) and c.strip()]
    if not lines:
        return {}
    task = ("Below are the captions for a selection of video clips titled %r. Write a "
            "polished summary of the whole selection at three lengths: a three-paragraph "
            "version, a one-paragraph version, and a one-sentence version. Ground it only "
            "in these captions -- do not invent anything. Return ONLY a JSON object with "
            "keys \"three\", \"one\", \"sentence\", nothing else." % title)
    system = (voice_card + "\n\n" + task) if voice_card else task
    resp = _vx_frontier_generate(system + "\n\nCaptions:\n" + "\n".join("- " + l for l in lines))
    m = re.search(r"\{.*\}", resp or "", re.DOTALL)
    if not m:
        return {}
    try:
        out = json.loads(m.group(0))
        return {k: str(out.get(k, "")).strip() for k in ("three", "one", "sentence")}
    except Exception:
        return {}


@app.route("/api/gallery/<slug>/summary", methods=["POST"])
def api_gallery_summary(slug):
    """Generate (frontier) and store a polished 3-tier summary of the selection.
    Shows up at the top of the contact sheet."""
    folder_dir, port, device_id = _vx_find_folder(slug)
    if not folder_dir:
        return jsonify({"ok": False, "error": "folder not found"}), 404
    cap_path = os.path.join(folder_dir, "captions.json")
    captions = {}
    if os.path.isfile(cap_path):
        try:
            captions = json.load(open(cap_path))
        except Exception:
            captions = {}
    title = captions.get("_title") or slug
    summary = _vx_gallery_summary(captions, _vx_voice_card(folder_dir=folder_dir), title)
    if not summary:
        return jsonify({"ok": False, "error": "summary produced nothing"}), 502
    captions["_summary"] = summary
    try:
        json.dump(captions, open(cap_path, "w"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    print("[summary] generated for %s" % slug)
    return jsonify({"ok": True, "summary": summary})


@app.route("/api/gallery/<slug>/restyle", methods=["POST"])
def api_gallery_restyle(slug):
    """Align a whole gallery's captions to the voice card -- one batched local call."""
    payload, status = _vx_caption_pass(slug, "align")
    return jsonify(payload), status


@app.route("/api/gallery/<slug>/refine", methods=["POST"])
def api_gallery_refine(slug):
    """Apply editor corrections (free text) to a gallery's captions, in voice --
    one batched local call. Body: {"notes": "Luna is a congresswoman, not ..."}"""
    data = request.get_json() or {}
    notes = (data.get("notes") or "").strip()
    if not notes:
        return jsonify({"ok": False, "error": "no notes"}), 400
    payload, status = _vx_caption_pass(slug, "refine", notes, engine="frontier")
    return jsonify(payload), status


@app.route("/api/gallery/<slug>/polish", methods=["POST"])
def api_gallery_polish(slug):
    """Polish a gallery's captions with the frontier model -- one batched paid call.
    Optional body {"notes": "..."} to also apply corrections in the same pass."""
    data = request.get_json(silent=True) or {}
    notes = (data.get("notes") or "").strip()
    mode = "refine" if notes else "align"
    payload, status = _vx_caption_pass(slug, mode, notes, engine="frontier")
    return jsonify(payload), status


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
    """Fetch a single gallery by slug with full image list. Includes
    steering instruction + source path when present (cube-pushed galleries
    have these; organic galleries leave them null)."""
    import sqlite3 as _gal_sqlite3
    db_path = MAESTRO_ROOT / "vault-cold" / "vault.db"
    try:
        conn = _gal_sqlite3.connect(str(db_path))
        # Probe schema for the cube-push columns (lazy-added; not on every db).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(galleries)").fetchall()]
        select_cols = ["slug", "title", "images", "created_at"]
        if "steering" in cols:
            select_cols.append("steering")
        if "source_path" in cols:
            select_cols.append("source_path")
        row = conn.execute(
            "SELECT " + ", ".join(select_cols) + " FROM galleries WHERE slug = ?",
            (slug,),
        ).fetchone()
        conn.close()
    except _gal_sqlite3.Error as exc:
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "not found"}), 404
    record = dict(zip(select_cols, row))
    try:
        imgs = json.loads(record.get("images") or "[]")
    except (ValueError, TypeError):
        imgs = []
    return jsonify({
        "slug": record["slug"],
        "title": record["title"],
        "images": imgs,
        "image_count": len(imgs),
        "created_at": record["created_at"],
        "steering": record.get("steering") or "",
        "source_path": record.get("source_path") or "",
    })


# ----- Cube push bridge -----
# Cube (the curation surface) pushes its assembled gallery here. We do two
# things with the payload:
#   1. Mint a fresh `type=gallery` token with a prose transcription as the
#      value, so when this token hydrates into the cue-vox agent's context
#      the assistant sees what's on stage. Full slide payloads ride along in
#      extra_fields.slides for any downstream consumer that wants the full
#      object, not just the prose summary.
#   2. Emit a chat response with a [GALLERY: …] block so the gallery appears
#      live in the chat window, matching how dropped-images galleries surface.
@app.route("/api/cube/push", methods=["POST"])
def api_cube_push():
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    title = (payload.get("title") or slug or "Cube Gallery").strip()
    vault = (payload.get("vault") or "").strip()
    slides = payload.get("slides") or []
    if not slug:
        return jsonify({"error": "slug required"}), 400
    # Source path = "<vault>/<slug>" — the canonical "where this came from"
    # identifier used in the chat header, token metadata, and gallery title.
    source_path = ("%s/%s" % (vault, slug)) if vault else slug

    # Build a prose transcription that an LLM reading the token can use to
    # understand the gallery without parsing the structured slides field.
    # Cube has already enriched each slide with the slim styled fields
    # (caption_styled for images, summary_styled for markdown). We use
    # those — the full markdown body is intentionally not in the
    # transcription, only the short summary.
    lines = []
    lines.append("Pushed from Cube: %s" % title)
    lines.append("slug: %s" % slug)
    lines.append("slides: %d" % len(slides))
    lines.append("")
    chat_images = []
    for i, slide in enumerate(slides, start=1):
        stype = slide.get("type", "markdown")
        if stype == "markdown":
            slide_title = slide.get("slide_title") or "(untitled)"
            styled_summary = (slide.get("summary_styled") or slide.get("summary_original") or "").strip()
            lines.append("[%d] markdown — %s" % (i, slide_title))
            if styled_summary:
                lines.append(styled_summary)
        elif stype == "image":
            styled_caption = (slide.get("caption_styled") or slide.get("caption_original") or "").strip()
            media = slide.get("media") or {}
            mpath = media.get("path") or ""
            murl = media.get("url") or ""
            lines.append("[%d] image — %s%s" % (
                i,
                styled_caption or "(no caption)",
                (" — " + mpath) if mpath else "",
            ))
            if mpath or murl:
                # Use `src` (not `url`) — that's the field name cue-vox's
                # resolveGalleryImageUrl falls back to when slug+filename
                # don't match the canonical /vault/<port>/<slug>/<filename>
                # path. `src` must be absolute since cue-vox runs on :3000
                # and the media lives on cube — cube rewrites the URL
                # before sending.
                #
                # caption_original + caption_styled both ride along so the
                # persisted gallery row keeps the source caption alongside
                # the styled one — `caption` is what the viewer renders by
                # default (= styled when steering ran, = original when not).
                orig_caption = (slide.get("caption_original") or "").strip()
                chat_images.append({
                    "filename": media.get("label") or os.path.basename(mpath or ""),
                    "src": murl,
                    "path": mpath,
                    "type": "image",
                    "caption": styled_caption,
                    "caption_original": orig_caption,
                    "caption_styled": styled_caption,
                })
        else:
            lines.append("[%d] %s" % (i, stype))

        # Notes (substrate pattern: slide.metadata.notes list) — raw, never
        # transformed by steering. Reasoning substrate for downstream
        # consumers; this is the user's own thinking about the slide.
        meta = slide.get("metadata") or {}
        notes_list = meta.get("notes") or []
        for n in notes_list:
            body = (n.get("body") or "").strip()
            if body:
                lines.append("  — " + body)
        # Legacy single-string note
        if not notes_list and (slide.get("note") or "").strip():
            lines.append("  — " + slide["note"].strip())

        lines.append("")

    transcription = "\n".join(lines).strip()
    label = "gallery_cube_%s" % re.sub(r"[^a-z0-9_]", "_", slug.lower())

    # Token value is the slim transcription — caption per slide, notes
    # inline. The full styled doc stays on cube as a sidecar (styled.md)
    # for Export to bundle; pushing the whole thing through to the chat
    # would just be wall-of-text noise.
    steering_instruction = (payload.get("steering_instruction") or "").strip()

    # Create the hot/fresh gallery token. Thermal: warmer base + shorter
    # half-life than a default gallery token so it dominates fresh context
    # right after a push and decays out over the next day.
    token_id = None
    if token_factory is not None:
        try:
            token_id = token_factory.create(
                token_type="gallery",
                label=label,
                value=transcription,
                tags=["gallery", "cube-push"],
                thermal={"base_temp": 85, "half_life_hours": 24, "floor_temp": 10},
                extra_fields={
                    "title": title,
                    "gallery_slug": slug,
                    "vault": vault,
                    "source_path": source_path,
                    "slide_count": len(slides),
                    "slides": slides,
                    "source": "cube",
                    "steering_instruction": steering_instruction,
                },
            )
        except Exception as exc:
            print("[cube/push] token_factory.create failed: %s" % exc)

    # Persist as a first-class gallery in vault.db so it shows up in
    # cue-vox's gallery list / sidebar. Slug uses the gallery's own slug
    # (the curation identity); the title carries the "vault/slug" source
    # path so the gallery row reads as "where this came from". INSERT OR
    # REPLACE — re-pushing the same gallery overwrites with the latest
    # curated captions, original captions, and steering instruction. The
    # post-steering captions are one-way: this row is the durable record
    # of how cube-vox saw the gallery; loading the same slug back into
    # Cube reads from the cold vault project.md (original captions only).
    try:
        import sqlite3 as _gal_sqlite3
        db_path = MAESTRO_ROOT / "vault-cold" / "vault.db"
        conn = _gal_sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS galleries (
                slug TEXT PRIMARY KEY, title TEXT, images TEXT, created_at TEXT
            )
        """)
        # Lazy migration: add steering + source_path columns if missing.
        # SQLite's only way to "ALTER ADD COLUMN IF NOT EXISTS" is try/except.
        for ddl in (
            "ALTER TABLE galleries ADD COLUMN steering TEXT",
            "ALTER TABLE galleries ADD COLUMN source_path TEXT",
        ):
            try:
                conn.execute(ddl)
            except _gal_sqlite3.OperationalError:
                pass  # column already exists
        gallery_title = "%s (from %s)" % (title, source_path) if vault else title
        conn.execute(
            "INSERT OR REPLACE INTO galleries (slug, title, images, created_at, steering, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                slug,
                gallery_title,
                json.dumps(chat_images),
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                steering_instruction,
                source_path,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print("[cube/push] gallery persist failed (non-fatal): %s" % exc)

    # Notify the cue-vox client(s) so the pinned/active tokens panel updates
    # and the chat shows the new gallery.
    socketio.emit("token_created", {
        "token_id": token_id or ("ctx_cube_push_%d" % int(time.time())),
        "type": "gallery",
        "label": label,
        "value": transcription,
        "title": title,
        "gallery_slug": slug,
        "slide_count": len(slides),
        "source": "cube",
    })

    # Push the gallery into the chat as an assistant message:
    #   - one-line header citing the source (vault/slug + steering). The
    #     source is a clickable link back to Cube with vault + gallery as
    #     query params — clicking re-opens the gallery in Cube for further
    #     curation. ("Grab the slug, drop it into Cube.")
    #   - the [GALLERY:…] block, which renders the image strip with the
    #     styled captions (caption per tile)
    # No styled prose body — that lives as styled.md on cube for Export to
    # bundle. The push surface stays focused: gallery + captions.
    cube_origin = (payload.get("origin") or "http://localhost:5052").rstrip("/")
    if vault:
        from urllib.parse import quote as _q
        cube_link = "%s/?vault=%s&gallery=%s" % (cube_origin, _q(vault), _q(slug))
        source_md = "[%s](%s)" % (source_path, cube_link)
    else:
        source_md = source_path

    # Steering deliberately not surfaced as text in the header — it's
    # captured in extra_fields + the persisted gallery row, and the styled
    # captions on the tiles ARE the visible evidence. No need to also dump
    # the instruction as a label.
    chat_parts = []
    header = "_Pushed from Cube: **%s** (%d slide%s)_" % (
        source_md,
        len(slides),
        "" if len(slides) == 1 else "s",
    )
    chat_parts.append(header)
    if chat_images:
        gallery_block = json.dumps({
            "title": title,
            "slug": slug,
            "source": "cube",
            "vault": vault,
            "images": chat_images,
        })
        chat_parts.append("[GALLERY: %s]" % gallery_block)

    socketio.emit("response", {
        "role": "assistant",
        "text": "\n\n".join(chat_parts),
        "tts_chunks": [],  # no TTS for a push — visual only
    })

    print("[cube/push] gallery '%s' → token %s (%d slides, %d images in chat)" % (
        slug, token_id, len(slides), len(chat_images)
    ))
    return jsonify({
        "ok": True,
        "token_id": token_id,
        "label": label,
        "slide_count": len(slides),
        "image_count_in_chat": len(chat_images),
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

        # Update UI state (not during a hold-listen -- that stays in 'holding').
        if not data.get('holding'):
            emit('state_change', {'state': 'transcribing'})

        # Transcribe with Whisper
        model = get_whisper_model()
        result = model.transcribe(temp_file.name)
        text = result["text"].strip()

        # HELD: every turn here is a CHILD SUBCHANNEL turn -- a full nested reply that runs
        # while the main reply stays parked underneath. It flows through the normal path
        # below and is logged, so it becomes context the resume folds in. The client remaps
        # the post-reply idle back to holding. Release is space-only (the 'resume' socket
        # event), never a spoken word, so nothing here exits the hold.
        if data.get('holding'):
            global _held_turns, _subchannel_log
            # A short authorize cue lifts the hold, exactly like the space bar. The simple
            # VAD carried it here; the server rules whether it is a release or a real turn.
            if _is_authorize(text):
                try:
                    os.remove(temp_file.name)
                except OSError:
                    pass
                _vlog('barge', 'AUTHORIZE "%s" -> release hold' % text[:40])
                emit('resumed', {})                              # client exits the held session
                emit('state_change', {'state': 'thinking'})      # regenerate under the thinking bed
                _release_hold()
                return
            _held_turns += 1
            _vlog('barge', 'subchannel turn %d while held: "%s"' % (_held_turns, text[:40]))

        # Benchmark command -> confirm first (standard policy: gate a moment-taking op
        # behind a yes/no so the user can opt out), then run on Yes. Not while held.
        # Capture the LIVE slider settings now so the run reflects them (no dictated sweep).
        if not data.get('holding') and _is_benchmark_command(text):
            try:
                os.remove(temp_file.name)
            except OSError:
                pass
            _confirm_slow_op(text, "benchmark", {
                "brevity": data.get('brevity'),
                "expressive": bool((data.get('voice') or {}).get('expressive')),
                "live": bool(data.get('live')),
            })
            return

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
        _vlog("turn", "audio in (live=%s expressive=%s)  \"%s\""
              % (bool(data.get('live')), bool((data.get('voice') or {}).get('expressive')), (text or "")[:60]))
        emit('state_change', {'state': 'thinking'})

        # --- Caption feedback on the active gallery routes to a refine pass ---
        # (cue-vox treats feedback about a clip's caption as a request to change it)
        active_gallery = (data.get("activeGallery") or "").strip()
        if active_gallery and _vx_is_caption_feedback(text):
            print("[caption-voice] '%s' -> refine %s" % (text, active_gallery))
            payload, _status = _vx_caption_pass(active_gallery, "refine", text, engine="frontier")
            try:
                Path(temp_file.name).unlink()
            except Exception:
                pass
            if payload.get("ok"):
                socketio.emit("caption_update", {
                    "slug": active_gallery,
                    "title": payload.get("title", active_gallery),
                    "images": payload.get("images", []),
                })
            else:
                msg = "I couldn't update those captions."
                emit("response", {"text": msg, "tts_chunks": tts_chunk_split(sanitize_for_tts(msg))})
            emit("state_change", {"state": "idle"})
            return

        # Calculate input length for response matching. The brevity dial, when
        # the client puts it on the wire, is the user's explicit demand signal
        # and supersedes the word-count inference -- same rule as the text path.
        input_word_count = get_input_word_count(text)
        aperture_constraint = get_aperture_constraint(
            data.get('brevity'), data.get('aperture_hex')
        )
        length_constraint = aperture_constraint or get_response_length_constraint(input_word_count)

        # Live voice prosody (window.VOICE from the client): tempo / brightness /
        # gain, plus expressive mode (route synthesis to Chatterbox). Set once per
        # turn; the synth worker reads these globals.
        global _expressive_mode, _exaggeration, _TONE, _prev_live, _live_mode
        _v = data.get('voice') or {}
        live = bool(data.get('live'))
        # Live mode opens a private channel -> reset to the breathy "hey" floor.
        if live and not _prev_live:
            _TONE = 0.0
        _prev_live = live
        _live_mode = live

        if _kokoro_available:
            kokoro_voice.set_prosody(speed=_v.get('speed'), bright=_v.get('bright'), gain=_v.get('gain'))
        _expressive_mode = bool(_v.get('expressive'))

        if _v.get('autotone', True):
            try:
                lvl = float(data.get('input_level') or 0.0)
            except (TypeError, ValueError):
                lvl = 0.0
            # Loudness above a normal-quiet floor SPENDS energy into the pool -- being
            # loud is a capital expenditure, not a free jump. Excited words add too.
            loud_energy = max(0.0, lvl - _LOUD_FLOOR) * _LOUD_GAIN
            _TONE = _TONE * _TONE_DECAY + _turn_energy(text) + loud_energy
            # Concave-up: you EARN the right to be loud. Whisper is the strong default,
            # the top registers cost progressively more sustained energy.
            norm = min(1.0, _TONE / _TONE_CAP)
            style = norm ** _TONE_GAMMA
            _exaggeration = 1.0 + style
            print("[TONE] pool=%.2f norm=%.2f -> style=%.2f dial=%.2f (loud +%.2f)"
                  % (_TONE, norm, style, _exaggeration, loud_energy), flush=True)
        else:
            try:
                _exaggeration = float(_v.get('exaggeration', 0.6))
            except (TypeError, ValueError):
                _exaggeration = 0.6

        # Autotone writes the register render-state (the ONE mutator). It maps the energy
        # dial to a register KEY and keeps its own continuous exaggeration for Chatterbox
        # (apply_exag=False). Floor clamp lives inside set_render_register. This is the
        # dumb stand-in the async model manager replaces in step 3.
        if _kokoro_available:
            keys = _register_keys()
            if keys:
                frac = max(0.0, min(1.0, _exaggeration - 1.0))
                i = min(len(keys) - 1, int(frac * len(keys)))
                set_render_register(keys[i], "autotone", apply_exag=False)

        # Inject temporal context if query is time-related
        enhanced_text = inject_temporal_context(text)

        # Light callback: if this turn explicitly calls back to a sidebar, reheat it (via a
        # modifier) and re-inject its derived line up top so the agent applies it now.
        _cb = _maybe_callback(text)
        _cb_section = ("[CALLBACK] The user is calling back to an earlier sidebar (\"%s\"). "
                       "Its derived point: \"%s\". Apply that to this turn."
                       % (_cb.get("label", ""), _cb.get("value", ""))) if _cb else ""

        # Inject instance identity, speech consumption, variables, input history, engagement, and rolling summary context
        context_sections = [
            ("callback", _cb_section),
            ("mode", get_mode_context()),
            ("brevity_stance", get_brevity_stance(data.get('brevity'))),
            ("identity", get_instance_identity()),
            ("recent_conversation", get_recent_conversation_context()),
            ("flux", get_flux_capacitor_context()),
            ("handoff", get_upstream_handoff_context()),
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

        # A subchannel (held) turn also accretes onto the sidebar, which becomes the token.
        if data.get('holding'):
            _subchannel_log.append((text, clean_response))

        tts_text = sanitize_for_tts(clean_response)
        tts_chunks = tts_chunk_split(tts_text)
        response_data = {"text": clean_response, "tts_chunks": tts_chunks}
        if _last_cue and _last_cue.get("warmed"): response_data["cue"] = _last_cue
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


def _try_fast_path_note_add(answer, recent_logs):
    """Fast-path for note-add YES_NOs: if the last assistant turn carried a
    [PIN_NOTE: {...}] sibling block and the user clicked Yes, write the note
    directly via POST /api/notes and return the templated confirmation.
    Returns None to fall through to the normal LLM path -- on No, on missing
    PIN_NOTE block, on malformed JSON, or on HTTP failure.
    """
    if answer != "Yes" or not recent_logs:
        return None

    last_assistant_msg = recent_logs[-1].get('assistant', '') or ''
    m = re.search(r'\[PIN_NOTE:\s*(\{[\s\S]+?\})\s*\]', last_assistant_msg)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError) as exc:
        print("[FAST-PATH] PIN_NOTE block found but JSON parse failed: %s" % exc)
        return None

    node_type = (data.get('node_type') or '').strip()
    node_id = (data.get('node_id') or '').strip()
    body = (data.get('body') or '').strip()
    display = (data.get('display_name') or node_id).strip()
    if not node_type or not node_id or not body:
        print("[FAST-PATH] PIN_NOTE missing required fields, falling through")
        return None

    pipeline_url = os.environ.get('PIPELINE_URL', 'http://localhost:5050')
    payload = json.dumps({
        'node_type': node_type,
        'node_id': node_id,
        'author': 'cue-vox',
        'body': body,
    }).encode('utf-8')
    req = urllib.request.Request(
        pipeline_url + '/api/notes',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            note = json.loads(resp.read())
    except Exception as exc:
        print("[FAST-PATH] POST /api/notes failed (%s) -- falling through to LLM" % exc)
        return None

    note_id = note.get('id') or '?'
    print("[FAST-PATH] Note landed on %s -- id %s (bypassed LLM)" % (display, note_id))
    return "Note landed on %s — id %s." % (display, note_id)


def _try_pin_for_ninja_direction(final_value, recent_logs):
    """Fast-path for 'pin for ninja': if the last assistant turn carried a
    [PIN_NINJA: {...}] sibling block and the user ratified it (via ANY input
    shape -- Yes, a dialed slider, a typed value, a chosen option), deposit the
    direction southbound on the handoff chassis (intent='direction') carrying the
    RATIFIED value, and return the templated confirmation. Returns None to fall
    through to the normal LLM path -- on explicit No, missing block, malformed
    JSON, or deposit failure.

    Per ninja-direction-channel: cue-vox AUTHORED the direction and PROPOSED the
    shape+value (the PIN_NINJA block); final_value is the user's ratification --
    it is the content the user dialed to, not the direction itself.
    """
    # Conversational modes do not pin for Ninja (and the agent is told not to offer).
    if _gated_mode():
        return None
    if not recent_logs:
        return None
    # Explicit rejection of a yes/no direction deposits nothing.
    if isinstance(final_value, str) and final_value.strip() == "No":
        return None

    last_assistant_msg = recent_logs[-1].get('assistant', '') or ''
    m = re.search(r'\[PIN_NINJA:\s*(\{[\s\S]+?\})\s*\]', last_assistant_msg)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError) as exc:
        print("[PIN-NINJA] block found but JSON parse failed: %s" % exc)
        return None

    headline = (data.get('headline') or '').strip()
    body = (data.get('body') or '').strip()
    ratify = data.get('ratify') if isinstance(data.get('ratify'), dict) else {}
    related_files = data.get('related_files') or None
    related_tokens = data.get('related_tokens') or None
    if not headline or not body:
        print("[PIN-NINJA] missing headline/body, falling through")
        return None

    # Record the ratified shape + value on the direction (the dial result). For a
    # plain "Yes" the proposed value stands; otherwise the user's dial overrides.
    shape = ratify.get("type") or "yes_no"
    ratified = final_value if not (isinstance(final_value, str) and final_value == "Yes") \
        else ratify.get("value", "Yes")
    body_with_value = "%s\n[ratified: %s = %s]" % (body, shape, ratified)

    handoff_lib = MAESTRO_ROOT / "core" / "handoff"
    if str(handoff_lib) not in sys.path:
        sys.path.insert(0, str(handoff_lib))
    try:
        import handoff as handoff_core
        result = handoff_core.leave(
            intent="direction",
            headline=headline,
            body=body_with_value,
            related_files=related_files,
            related_tokens=related_tokens,
        )
    except Exception as exc:
        print("[PIN-NINJA] handoff deposit failed (%s) -- falling through" % exc)
        return None

    slug = result.get('slug', '?')
    print("[PIN-NINJA] direction deposited for ninja -- slug %s (%s=%s)" % (slug, shape, ratified))
    return "Direction for ninja — slug %s. %s" % (slug, headline)


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

        # Fast-path 0: a pending slow-op confirmation (standard policy -- confirm before a
        # moment-taking op so the user can opt out). Yes runs it, No drops it, no LLM.
        global _pending_slow_op
        if _pending_slow_op:
            op = _SLOW_OPS.get(_pending_slow_op)
            _pending_slow_op = None
            if answer == "Yes" and op:
                op["run"]()          # self-driving: emits + speaks its own walk
            else:
                _respond_and_speak(answer, "Okay, skipped.")
            return

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

        # Fast-path 1: a ratified [PIN_NINJA: ...] direction deposits southbound
        # on the handoff chassis and skips the LLM (per ninja-direction-channel).
        # Checked first -- a PIN_NINJA block is unambiguous.
        pin_response = _try_pin_for_ninja_direction(answer, recent_logs)
        if pin_response is not None:
            response = pin_response
            used_fallback = False
        else:
            # Fast-path 2: if the previous assistant turn carried a [PIN_NOTE: ...]
            # block and the user said Yes, write the note directly and skip the
            # Claude subprocess entirely. Drops note-add latency from seconds to
            # ~HTTP round-trip. Falls through to the LLM path on any failure.
            fast_response = _try_fast_path_note_add(answer, recent_logs)
            if fast_response is not None:
                response = fast_response
                used_fallback = False
            else:
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
        if _last_cue and _last_cue.get("warmed"): response_data["cue"] = _last_cue
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
    if _last_cue and _last_cue.get("warmed"): response_data["cue"] = _last_cue
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

        # Pin-for-ninja: a slider/text/choice submission ratifies a direction too
        # (not just Yes/No). If the last turn carried a [PIN_NINJA: ...] block,
        # deposit the direction with the dialed value and skip the LLM round-trip.
        pin_recent_logs = load_recent_logs(limit=5)
        pin_response = _try_pin_for_ninja_direction(str(user_message), pin_recent_logs)
        if pin_response is not None:
            _respond_and_speak(str(user_message), pin_response)
            return

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
        if _last_cue and _last_cue.get("warmed"): response_data["cue"] = _last_cue
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


# --- Live-mode form walker (deterministic; no model runs during a walk) --------------
# gate.py is the form; form_walk walks it as conversation. A trigger opens a form; while a
# walk is active, every turn is a field value (or an abort). Submit only fires when every
# required field is valid (gate.submit). Runs in Live (yes/no only, per capability) and in
# Baseline (all kinds). Spec: docs/design/party-in-a-bucket-spec.md; gate.py.
_FORM_WALK_TRIGGERS = ("enter form", "start the form", "start form", "walk the form",
                       "walk a form", "fill out the form", "start check in",
                       "start the check-in", "start check-in", "check me in", "form walk")
_FORM_WALK_ABORTS = ("cancel form", "cancel the form", "quit form", "stop the form",
                     "abandon form", "exit form", "never mind the form")

# v1 demo form: a quick check-in. All yes/no, so it runs under the Live ceiling.
_DEMO_FORM = {
    "title": "Quick check-in",
    "fields": [
        {"name": "focus", "kind": "y_n", "prompt": "Are you starting a focused work block?"},
        {"name": "blockers", "kind": "y_n", "prompt": "Anything blocking you right now?"},
        {"name": "ship_today", "kind": "y_n", "prompt": "Do you plan to ship something today?"},
    ],
}


def _is_form_walk_command(text):
    t = (text or "").strip().lower()
    return any(trig in t for trig in _FORM_WALK_TRIGGERS)


def _is_form_walk_abort(text):
    t = (text or "").strip().lower()
    return any(a in t for a in _FORM_WALK_ABORTS)


def _walk_result(intent):
    """Wrap a form_walk intent as a normal turn result so the walk rides the existing speak
    path (text, voice, and HTTP all funnel through _assemble_and_respond)."""
    say = intent.get("say", "") or ""
    card = intent.get("card", say) or say
    tts = sanitize_for_tts(say)
    return {"clean_response": card, "tts_text": tts, "tts_chunks": tts_chunk_split(tts),
            "snr_hex": None, "used_fallback": False, "input_word_count": 0}


def _emit_walk_side(intent):
    """Push a paired browser the fill for the field just accepted, and walk_ready on done.
    Broadcast emit is context-free, so it fires from any turn path (text, voice, HTTP)."""
    f = intent.get("filled")
    if f:
        socketio.emit("walk_fill", {"name": f["name"], "value": f["value"]})
    if intent.get("done"):
        socketio.emit("walk_ready", {"valid": bool(intent.get("sustained")),
                                     "values": intent.get("values") or {}})


def _assemble_and_respond(text, brevity=None, aperture_hex=None):
    """Run one conversation turn's reasoning and return the computed response.

    This is the shared core of a turn: temporal + identity + flux + summary
    context assembly, the Claude/C2D2 call, logging, and TTS shaping. It does
    NOT emit -- callers own the emit target. The socketio text handler emits to
    the requesting client; the HTTP /api/converse push broadcasts via
    socketio.emit into the live conversation surface. Same reasoning, two
    delivery paths. See docs/policies/manage-notes.md (Injection Surface).

    Returns a dict with clean_response / tts_text / tts_chunks / snr_hex /
    used_fallback / input_word_count, or None if the response was voided.
    """
    # Live-mode form walker: deterministic, no model. If a walk is active this turn is a
    # field value (or an abort); if the text opens a walk, start it. Either way we speak the
    # walk, not Claude. Submit fires only when every field is valid (gate.py).
    if form_walk.active():
        if _is_form_walk_abort(text):
            form_walk.abandon()
            socketio.emit("walk_ready", {"valid": False, "aborted": True})
            return _walk_result({"say": "Okay, dropped the form.", "card": "Form cancelled."})
        intent = form_walk.step(text)
        _emit_walk_side(intent)
        return _walk_result(intent)
    if _is_form_walk_command(text):
        return _walk_result(form_walk.start(_DEMO_FORM["fields"], title=_DEMO_FORM["title"]))

    input_word_count = get_input_word_count(text)
    # The brevity dial is the user's explicit demand signal; when present it
    # supersedes the word-count heuristic (an inference). Absent (older client),
    # fall back to matching the user's input length. aperture_hex carries the
    # dial's VRGB coordinate (the color on the thumb) straight into the directive.
    aperture_constraint = get_aperture_constraint(brevity, aperture_hex)
    length_constraint = aperture_constraint or get_response_length_constraint(input_word_count)

    # Inject temporal context if query is time-related
    enhanced_text = inject_temporal_context(text)

    # Inject instance identity, speech consumption, variables, input history, engagement, and rolling summary context
    context_sections = [
        ("brevity_stance", get_brevity_stance(brevity)),
        ("identity", get_instance_identity()),
        ("recent_conversation", get_recent_conversation_context()),
        ("flux", get_flux_capacitor_context()),
        ("handoff", get_upstream_handoff_context()),
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
        print("[VOID] Claude response discarded (turn was voided)")
        return None

    # Log conversation with input length
    _, clean_response, snr_hex = log_conversation(text, response, input_length=input_word_count)

    tts_text = sanitize_for_tts(clean_response)
    tts_chunks = tts_chunk_split(tts_text)
    return {
        "clean_response": clean_response,
        "tts_text": tts_text,
        "tts_chunks": tts_chunks,
        "snr_hex": snr_hex,
        "used_fallback": used_fallback,
        "input_word_count": input_word_count,
    }


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

        # @c2d2 sigil -> the eval bench (bypasses the whole conversational layer).
        # Checked before anything else so a bench post never touches flux/Claude.
        if _c2d2_bench_lines(text):
            _run_c2d2_bench(text)
            return

        # Benchmark command -> confirm first (standard policy: gate a moment-taking op
        # behind a yes/no so the user can opt out), then run on Yes. Bypasses the model.
        # Capture the LIVE slider settings now so the run reflects them (no dictated sweep).
        if _is_benchmark_command(text):
            _confirm_slow_op(text, "benchmark", {
                "brevity": data.get('brevity'),
                "expressive": bool((data.get('voice') or {}).get('expressive')),
                "live": bool(data.get('live')),
            })
            return

        # Brevity dial (0 = brief/deliverable, 1 = reflective); None on older
        # clients -> word-count fallback downstream. aperture_hex is the dial's
        # VRGB coordinate (the thumb color) for the geometric directive.
        brevity = data.get('brevity')
        aperture_hex = data.get('aperture_hex')

        emit('state_change', {'state': 'thinking'})

        result = _assemble_and_respond(text, brevity=brevity, aperture_hex=aperture_hex)

        if result is None:
            emit('state_change', {'state': 'idle'})
            return

        if result["used_fallback"]:
            emit('fallback_active', {'backend': 'c2d2'})

        clean_response = result["clean_response"]
        tts_text = result["tts_text"]
        response_data = {"text": clean_response, "tts_chunks": result["tts_chunks"]}
        if result["snr_hex"]:
            response_data["snr_hex"] = result["snr_hex"]
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
        dispatcher_path = MAESTRO_ROOT / "core" / "cue-dispatcher" / "dispatch.py"
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

        dispatcher_path = MAESTRO_ROOT / "core" / "cue-dispatcher" / "dispatch.py"
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
    _speech_queue.put(("text", text, 0, False, _speech_gen))

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

        # --- 1. Do-menu manifest -- the single source of truth for the menu
        #        list, generated by tools/do-menu.py from cue-sheets/*.yaml +
        #        deployments.yaml. Reading it here keeps the cue-vox (local) menu
        #        from drifting from Line One (public), which fetches the same
        #        do-menu.json. LAUNCH still parses the live yaml
        #        (handle_cuesheet_launch), so editing a cue-sheet's cues needs no
        #        regeneration -- only menu-metadata edits (name/icon/order/panel)
        #        require `python3 tools/do-menu.py`. ---
        import json as _json
        manifest_file = MAESTRO_ROOT / "playbook" / "do-menu.json"
        if manifest_file.is_file():
            manifest = _json.loads(manifest_file.read_text())
            for op in manifest.get("ops", []):
                cs = op.get("cuesheet_path")
                abs_path = str(MAESTRO_ROOT / cs) if cs else None
                # Every cue-sheet-backed op feeds the slug->path cache (launch).
                if abs_path:
                    all_sheets.append({"path": abs_path, "filename": Path(cs).name})
                # The local Do menu renders launchable, non-child ops only.
                if "local" not in op.get("surfaces", []) or op.get("parent"):
                    continue
                label = op.get("label") or op.get("id")
                entry = {
                    "path": abs_path,
                    "filename": Path(cs).name if cs else "",
                    "name": label,
                    "description": op.get("description", ""),
                    "icon": op.get("icon", ""),
                    "order": op.get("order", 999),
                    "is_child": bool(op.get("parent")),
                    "is_stub": False,
                }
                dims = op.get("panel") or {}
                panel_url = op.get("url_local") or op.get("url_public")
                if panel_url or dims:
                    entry["panel"] = {
                        "url": panel_url,
                        "width": dims.get("width", 600),
                        "height": dims.get("height", 400),
                        "title": label,
                    }
                sheets.append(entry)

        # --- 2. Active vault volumes (via Jeff) ---
        jeff_dir = MAESTRO_ROOT / "core" / "jeff"
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
            _speech_queue.put(("text", announce_text, 0, False, _speech_gen))

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
            ensure_script = MAESTRO_ROOT / "core" / "zapier" / "pull" / "scripts" / "_lib.sh"
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
            refresh_script = MAESTRO_ROOT / "core" / "refresh-memory.py"
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
            _speech_queue.put(("text", announce_text, 0, False, _speech_gen))

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
    _speech_queue.put(("text", text, 0, False, _speech_gen))
    _speech_queue.join()
    return {"ok": True}


@app.route('/api/converse', methods=['POST'])
def api_converse():
    """Inject a turn into the LIVE conversation surface and reason in place.

    Unlike /api/speak (TTS only), this runs the full reasoning pipeline -- the
    same context assembly as a typed turn -- and broadcasts both the response
    and conversation state to all connected clients via socketio.emit. It pushes
    INTO the conversation that's already open; it never spawns a new one. Surface
    affordances (e.g. the pipeline 'Manage Notes' button) POST here to laminate
    context into the live thread. See docs/policies/manage-notes.md.

    Speech runs in a background task so the POST returns promptly while cue-vox
    speaks; the conversation settles thinking -> speaking -> idle on its own.
    """
    text = ""
    if request.is_json:
        text = (request.json or {}).get("text", "")
    else:
        text = request.form.get("text", "")
    text = text.strip()
    if not text:
        return {"ok": False, "error": "no text"}, 400

    # Stop any in-flight speech, exactly as a fresh turn does.
    handle_speech_interruption()
    flush_speech_queue()
    global tts_interrupted
    tts_interrupted = False

    # ACK fast: emit 'thinking' now, then run the whole turn (reasoning +
    # speech) in the background so the caller isn't blocked on Claude latency.
    # The conversation surface settles thinking -> speaking -> idle on its own.
    socketio.emit('state_change', {'state': 'thinking'})

    def _run_turn(turn_text):
        try:
            result = _assemble_and_respond(turn_text)
            if result is None:
                socketio.emit('state_change', {'state': 'idle'})
                return
            if result["used_fallback"]:
                socketio.emit('fallback_active', {'backend': 'c2d2'})
            response_data = {
                "role": "assistant",
                "text": result["clean_response"],
                "tts_chunks": result["tts_chunks"],
            }
            if result["snr_hex"]:
                response_data["snr_hex"] = result["snr_hex"]
            socketio.emit("response", response_data)
            socketio.emit('state_change', {'state': 'speaking'})
            # Speak via the queue with emit_events=False -- HTTP-safe, no
            # context-bound emit (speak_chunked's trailing emit() would fail).
            start_speech_tracking(result["clean_response"])
            speak_text = strip_markdown_for_tts(result["tts_text"])
            if speak_text:
                _speech_queue.put(("text", speak_text, 0, False, _speech_gen))
                _speech_queue.join()
            finish_speech()
            socketio.emit('state_change', {'state': 'idle'})
        except Exception as exc:
            socketio.emit('error', {'message': str(exc)})
            socketio.emit('state_change', {'state': 'idle'})

    socketio.start_background_task(_run_turn, text)
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


@socketio.on('replay')
def handle_replay(data):
    """Re-synthesize existing text at runtime and play it: active memory, not a stored
    recording. No new chat card -- it re-synths (register resolved, weight applied)
    every time, which is why the client shows the 'synthing' state on replay."""
    text = (data or {}).get("text", "").strip()
    if not text:
        return
    flush_speech_queue()                 # interrupt anything currently playing
    speak_chunked(text)                  # emits voice_weight + tts_chunk_start/done
    emit('state_change', {'state': 'idle'})


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
def handle_disconnect(reason=None):
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
    # Select the active voice (voices.json, or CUE_VOX_VOICE override), then seed
    # the live tone from the maestro-owned deployed package (if configured).
    set_active_voice(os.environ.get("CUE_VOX_VOICE", _ACTIVE_VOICE))
    _load_deployed_voice()
    # Silence werkzeug per-request access logs -- ~13% of log volume, and every
    # line is a request path that can carry query PII. Warnings/errors still log.
    import logging as _logging
    _logging.getLogger('werkzeug').setLevel(_logging.WARNING)
    socketio.run(app, host='127.0.0.1', port=port, debug=False, allow_unsafe_werkzeug=True)
