"""The gate primitive: the composable atom for LIVE guided experiences.

A gate HOLDS the flow, presents a CHALLENGE (a typed question with a matching answer),
and on a correct match RESUMES with the held context plus the answer folded in. It is
the one element everything else composes from: a spoken command, a step in a script, a
whole game-show / therapy / sprint-planning "can" is just gates arranged in sequence.

Built on cue-mem/lib/challenge.py (generate -> present, verify -> match), so:
  - confidence  IS the preponderance contribution (timing + attention factored)
  - proof       IS the signed weight
  - weight      scales a gate's contribution to an action's required preponderance

Courtroom shape:  open_gate (OBJECT, hold) -> rule (RULING) -> sustained | overruled.
SUSTAINED resumes the held context WITH the answer; OVERRULED releases it unchanged.
"""
import time

try:
    import challenge as _challenge     # cue-mem/lib is on sys.path inside web.py
    _AVAILABLE = True
except Exception:                      # pragma: no cover - challenge lib absent
    _challenge = None
    _AVAILABLE = False

# Open gates awaiting a ruling: gate_id -> {challenge, context, weight, issued_ms}
_open = {}


def available():
    return _AVAILABLE


def open_gate(context=None, kind="arithmetic", weight=1.0):
    """OBJECT: raise a gate. Generate a challenge and hold `context` pending the ruling.
    `context` is whatever should resume on SUSTAINED (e.g. the held reply, a command).
    Returns {gate_id, prompt, kind, weight} for the cue card, or None if unavailable."""
    if not _AVAILABLE:
        return None
    ch = _challenge.generate(kind)
    gate_id = ch["challenge_id"]
    _open[gate_id] = {
        "challenge": ch,
        "context": context,
        "weight": float(weight),
        "issued_ms": time.time() * 1000.0,
    }
    return {"gate_id": gate_id, "prompt": ch["prompt"], "kind": kind, "weight": float(weight)}


def rule(gate_id, response, attention=None):
    """RULING: verify the answer against the held gate. Returns a ruling:
       {sustained, confidence, weight, preponderance, proof, context, answer}.
    SUSTAINED (valid answer) -> caller resumes `context` WITH `answer` folded in.
    OVERRULED -> caller resumes the prior flow unchanged. Response time is measured
    from open_gate, so a gate that takes a beat to answer scores honest confidence."""
    g = _open.pop(gate_id, None)
    if g is None:
        return {"sustained": False, "reason": "no such gate", "confidence": 0.0,
                "weight": 0.0, "preponderance": 0.0, "proof": None, "context": None}
    rt_ms = time.time() * 1000.0 - g["issued_ms"]
    valid, confidence, proof = _challenge.verify(g["challenge"], response, rt_ms, attention)
    weight = g["weight"]
    return {
        "sustained": bool(valid),
        "confidence": round(float(confidence), 3),        # timing+attention quality
        "weight": weight,
        "preponderance": round(float(confidence) * weight, 3),  # contribution to the tally
        "proof": proof,                                    # signed weight (None if overruled)
        "context": g["context"] if valid else None,        # the held thing, on sustained
        "answer": response,
    }


def abandon(gate_id):
    """Drop an open gate without ruling (e.g. timeout / superseded). Returns its held
    context so the caller can decide what to do with it."""
    g = _open.pop(gate_id, None)
    return g["context"] if g else None


def open_count():
    return len(_open)
