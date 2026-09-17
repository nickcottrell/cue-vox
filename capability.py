"""Pure, dependency-free reading of capability_matrix.json.

Both web.py (runtime) and test/benchmark_capability.py (the policy loop) import these
functions, so the benchmark exercises the REAL logic instead of a mirror that could drift.
No heavy deps here: json + os only. web.py owns the mode globals and passes them in.
"""

import json
import os
import re

MATRIX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capability_matrix.json")

# Legacy fallback tag set, used ONLY when the matrix file fails to load.
LEGACY_GATED_TAGS = ("PIN_NINJA", "PIN_NOTE", "DOCUMENT", "INPUT", "APPROVAL", "GALLERY", "CUE", "YES_NO")


def load_matrix(path=MATRIX_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def active_mode(live, expressive):
    """The mode whose matrix row applies. Live wins over expressive over baseline."""
    if live:
        return "live"
    if expressive:
        return "expressive"
    return "baseline"


def gated_mode(matrix, mode):
    """True when the mode is a ceiling mode (simplified surface). Falls back to the
    legacy expressive-or-live check when the matrix is missing."""
    if matrix:
        return matrix.get("modes", {}).get(mode, {}).get("surface") == "ceiling"
    return mode in ("live", "expressive")


def blocked_tags(matrix, mode):
    """Tags that must NOT render or fire in this mode: every tool-class whose disposition
    is not 'full'. Baseline blocks nothing. Falls back to the legacy set (ceiling only)
    when the matrix is missing. Deduped, order preserved."""
    if not matrix:
        return LEGACY_GATED_TAGS if gated_mode(matrix, mode) else ()
    seen = set()
    out = []
    for cls, tag_list in matrix.get("tags", {}).items():
        if matrix.get("matrix", {}).get(cls, {}).get(mode, "full") != "full":
            for t in tag_list:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
    return tuple(out)


def deflect_classes(matrix, mode):
    """Tool-classes whose disposition is exactly 'deflect' in this mode (sorted)."""
    mtx = (matrix or {}).get("matrix", {})
    return sorted(c for c in mtx if mtx[c].get(mode) == "deflect")


def shades(matrix, mode):
    """The narrowing shades (yes_no / scalar / text) available (full) in this mode."""
    mtx = (matrix or {}).get("matrix", {})
    return [c for c in ("yes_no", "scalar", "text") if mtx.get(c, {}).get(mode) == "full"]


def brevity_band(matrix, brevity):
    """The brevity band for a scalar 0..1 (0 = max brevity), or None. Bands are ordered
    ascending by max_b; the first band whose max_b >= the value wins."""
    try:
        b = float(brevity)
    except (TypeError, ValueError):
        return None
    bands = (matrix or {}).get("brevity_stance", {}).get("bands", [])
    for band in bands:
        if b <= band.get("max_b", 1.0):
            return band
    return bands[-1] if bands else None


def mode_context(matrix, expressive, live):
    """Behavioral-layer prompt for a ceiling mode; empty on the full (Baseline) surface."""
    mode = active_mode(live, expressive)
    if not gated_mode(matrix, mode):
        return ""
    modes = []
    if expressive:
        modes.append("EXPRESSIVE")
    if live:
        modes.append("LIVE")
    if not matrix:
        return (
            "[VOICE MODE: %s]\n"
            "You are in a spoken, conversational mode. Be brief and natural, like talking.\n"
            "Do NOT use structured tools or pin for Ninja. Just talk.\n\n" % " + ".join(modes)
        )
    deflect = deflect_classes(matrix, mode)
    d = matrix.get("deflection", {})
    sp = matrix.get("surface_policy", {})
    return (
        "[VOICE MODE: %s]\n"
        "You are in a spoken, conversational mode. Be brief and natural, like talking.\n"
        "The only thing you can put on screen here is a yes/no, and it pops a dialog that "
        "interrupts the user. %s\n"
        "These do NOT render here: %s. When the user wants one of those, do not force it. "
        "Deflect gracefully: say '%s', take a beat, then offer a voice-doable alternative "
        "as a yes/no ('%s'). A No means drop it and keep talking. NEVER re-ask the same "
        "question. A Yes moves on. The tools and MCP rails still run underneath, you just "
        "do not surface their widgets. Inline citations are fine.\n\n"
        % (" + ".join(modes),
           sp.get("yes_no", "Surface it only at a genuine decision point; when in doubt, keep talking."),
           ", ".join(deflect) or "none",
           d.get("recognize", "I can't do that here."),
           d.get("offer_template", "From here, want to move forward with X?"))
    )


def brevity_stance(matrix, mode, brevity):
    """Baseline narrowing-pressure block; empty in ceiling modes or without a dial value."""
    if gated_mode(matrix, mode) or brevity is None:
        return ""
    band = brevity_band(matrix, brevity)
    stance = (band or {}).get("stance", "")
    return "[BREVITY STANCE]\n%s\n\n" % stance if stance else ""


def strip_blocked_tags(text, tag_types):
    """Remove [TAG: ...] blocks with bracket-balanced matching (handles ] inside JSON).
    This is the exact strip the pipeline runs; shared here so a probe witnesses the real
    production behavior, not a lookalike."""
    if not tag_types:
        return text
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


def apply_ceiling(matrix, mode, text):
    """The real ceiling transform for a mode: strip every blocked tag. Same code the
    pipeline runs in log_conversation, so a probe sees exactly what production does."""
    return strip_blocked_tags(text, blocked_tags(matrix, mode))


def probe_suite(matrix):
    """The benchmark as REAL probes. Each feeds a real input through the real function and
    records what actually happened (input, output, expected, passed). The deterministic
    test asserts every 'passed'; the walk stages this same list visibly. One suite, so what
    you watch (B) is what gets checked (A)."""
    APPROVAL = 'Done. [APPROVAL: {"action":"Write","target":"test.txt"}]'
    YESNO = 'Ship it? [YES_NO: ship it?]'
    probes = []

    out = apply_ceiling(matrix, "live", APPROVAL)
    probes.append({
        "label": "Live mode deflects an approval",
        "context": "live", "input": APPROVAL, "output": out,
        "expected": "the APPROVAL block is stripped, the words stay",
        "passed": ("APPROVAL" not in out) and ("Done." in out),
    })

    out = apply_ceiling(matrix, "live", YESNO)
    probes.append({
        "label": "Live mode keeps a yes or no",
        "context": "live", "input": YESNO, "output": out,
        "expected": "the YES_NO block survives",
        "passed": "YES_NO" in out,
    })

    out = apply_ceiling(matrix, "baseline", APPROVAL)
    probes.append({
        "label": "Baseline keeps the approval (full surface)",
        "context": "baseline", "input": APPROVAL, "output": out,
        "expected": "the APPROVAL block is kept",
        "passed": "APPROVAL" in out,
    })

    lock = brevity_stance(matrix, "baseline", 0.0)
    probes.append({
        "label": "Full brevity locks before changing the model",
        "context": "baseline, brevity 100%", "input": "brevity dial = 0.0",
        "output": lock or "(empty)",
        "expected": "the stance confirms before changing anything",
        "passed": "change anything" in lock.lower(),
    })

    expl = brevity_stance(matrix, "baseline", 0.9)
    probes.append({
        "label": "Low brevity opens up to explore",
        "context": "baseline, brevity ~10%", "input": "brevity dial = 0.9",
        "output": expl or "(empty)",
        "expected": "the stance says explore",
        "passed": "explore" in expl.lower(),
    })

    return probes
