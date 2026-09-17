#!/usr/bin/env python3
"""Runnable benchmark for the capability-matrix policies (the loop's guardrail).

Run: python3 test/benchmark_capability.py   (exit 0 = all pass, 1 = any fail)

It imports capability.py -- the SAME module web.py runs -- so this exercises the real
logic, not a mirror. After any edit to capability_matrix.json or capability.py, run this
to confirm the policy invariants still hold. Deterministic: no model, no browser, no server.

Tiers:
  A structure     the matrix file is well-formed
  B dispositions  each mode strips/keeps the right blocks
  C shades        narrowing shades available per mode (yes_no / scalar / text)
  D deflection    the deflection shape + graceful-deflect prompt
  E surface       the 'when to surface a yes/no' policy (converge, do not spin)
  F brevity       the brevity dial maps to the right Baseline narrowing stance
  G prompt        get-mode-context wiring reflects the matrix
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import capability  # noqa: E402

MATRIX = capability.load_matrix()

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))


def expect(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


# --- A: structure ---------------------------------------------------------
check("A. matrix loads", MATRIX is not None)
for key in ("modes", "matrix", "tags", "dispositions", "deflection", "surface_policy", "brevity_stance"):
    check("A. has '%s'" % key, key in (MATRIX or {}))

# --- B: dispositions per mode --------------------------------------------
expect("B. baseline blocks nothing", capability.blocked_tags(MATRIX, "baseline"), ())
for mode in ("live", "expressive"):
    blk = set(capability.blocked_tags(MATRIX, mode))
    check("B. %s keeps YES_NO" % mode, "YES_NO" not in blk, sorted(blk))
    check("B. %s strips rich blocks" % mode,
          {"APPROVAL", "INPUT", "DOCUMENT", "GALLERY", "CUE", "PIN_NINJA", "PIN_NOTE"} <= blk,
          sorted(blk))
expect("B. expressive == live (same ceiling)",
       capability.blocked_tags(MATRIX, "expressive"), capability.blocked_tags(MATRIX, "live"))
check("B. baseline is not gated", not capability.gated_mode(MATRIX, "baseline"))
check("B. live is gated", capability.gated_mode(MATRIX, "live"))
check("B. expressive is gated", capability.gated_mode(MATRIX, "expressive"))
# MCP rails run unshown (not a rendered tag), never surface as a block:
check("B. mcp_tools not a strippable tag", "mcp_tools" not in MATRIX.get("tags", {}))

# --- C: narrowing shades --------------------------------------------------
expect("C. baseline = all three shades", capability.shades(MATRIX, "baseline"), ["yes_no", "scalar", "text"])
expect("C. live = yes/no only", capability.shades(MATRIX, "live"), ["yes_no"])
expect("C. expressive = yes/no only", capability.shades(MATRIX, "expressive"), ["yes_no"])
# scalar + text both ride the INPUT tag, so they move together technically:
check("C. scalar rides INPUT", MATRIX["tags"].get("scalar") == ["INPUT"])
check("C. text rides INPUT", MATRIX["tags"].get("text") == ["INPUT"])

# --- D: deflection shape --------------------------------------------------
d = MATRIX.get("deflection", {})
for f in ("recognize", "offer_template", "on_no", "on_yes"):
    check("D. deflection has '%s'" % f, bool(d.get(f)))
expect("D. No loops back", d.get("on_no"), "loop_back")
expect("D. Yes proceeds", d.get("on_yes"), "proceed")
ctx_live = capability.mode_context(MATRIX, expressive=False, live=True)
check("D. live prompt teaches the deflect list", "not render here" in ctx_live.lower())
check("D. live prompt carries the recognize line", d.get("recognize", "zzz") in ctx_live)

# --- E: surface policy (converge, do not spin) ---------------------------
sp = MATRIX.get("surface_policy", {}).get("yes_no", "")
check("E. surface_policy present", bool(sp))
check("E. anti-redundancy clause", "narrows nothing" in sp.lower())
check("E. no self-referential yes/no", "yes/no about" in sp.lower())
check("E. eager-to-converge framing", "executable" in sp.lower())
check("E. surface_policy is in the live prompt", sp in ctx_live)

# --- F: brevity -> Baseline narrowing stance -----------------------------
def band_of(b):
    band = capability.brevity_band(MATRIX, b)
    return band.get("brevity") if band else None

expect("F. b=0.0 -> 100%", band_of(0.0), "100%")
expect("F. b=0.10 -> 80-99%", band_of(0.10), "80-99%")
expect("F. b=0.20 -> 80-99%", band_of(0.20), "80-99%")
expect("F. b=0.50 -> 40-79%", band_of(0.50), "40-79%")
expect("F. b=0.95 -> 0-39%", band_of(0.95), "0-39%")
lock = capability.brevity_stance(MATRIX, "baseline", 0.0)
check("F. lock stance asks before changing", "are you sure you want to change anything" in lock.lower())
check("F. high brevity narrows hard", "narrow hard" in capability.brevity_stance(MATRIX, "baseline", 0.1).lower())
check("F. low brevity explores", "explore" in capability.brevity_stance(MATRIX, "baseline", 0.95).lower())
expect("F. brevity stance is empty in ceiling modes", capability.brevity_stance(MATRIX, "live", 0.0), "")
expect("F. brevity stance empty when no dial value", capability.brevity_stance(MATRIX, "baseline", None), "")

# --- G: prompt wiring -----------------------------------------------------
expect("G. baseline mode_context is empty", capability.mode_context(MATRIX, expressive=False, live=False), "")
check("G. live mode_context tags the mode", "[VOICE MODE: LIVE]" in ctx_live)
check("G. expressive mode_context tags the mode",
      "[VOICE MODE: EXPRESSIVE]" in capability.mode_context(MATRIX, expressive=True, live=False))

# --- H: real probes (the SAME suite the interface walk stages) ------------
for p in capability.probe_suite(MATRIX):
    check("H. probe -- %s" % p["label"], p["passed"],
          "IN=%r OUT=%r" % (p["input"][:50], (p["output"] or "")[:50]))

# --- report ---------------------------------------------------------------
passed = sum(1 for _, ok, _ in _results if ok)
failed = [(n, dt) for n, ok, dt in _results if not ok]
print("capability benchmark: %d/%d passed" % (passed, len(_results)))
for name, ok, detail in _results:
    if not ok:
        print("  FAIL  %s  (%s)" % (name, detail))
if failed:
    print("\n%d FAILING -- policy invariant broken." % len(failed))
    sys.exit(1)
print("all green.")
sys.exit(0)
