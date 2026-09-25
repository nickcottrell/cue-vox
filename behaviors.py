"""Behavior cards runtime -- pure stacking + brief. Dumb-by-design.

A behavior card is a TRIGGER (a condition in the room) + a STANCE (a pre-designed move).
At runtime the card carries a machine `signal` (the trigger, made matchable); the agent
LOADS the cards whose signal is active and STACKS them -- it never infers the behavior from
the trigger prose. This module is pure: state (which signals are live) lives in web.py; here
we only stack cards against a signal set and render the stance brief that rides the prompt.

Signals are explicit, not vibes: e.g. `live` (hands-free mode), `barge_in` (a hold/interrupt),
`quiet`, `low_key`, and `always`. A card with signal `always` is always stacked.
"""


def stack(cards, signals):
    """The cards whose `signal` is active (or 'always'), in library order. `cards` is the
    {id: card} library; `signals` is the active signal set. Returns [(id, card), ...]."""
    active = set(signals or [])
    out = []
    for bid, card in (cards or {}).items():
        if not isinstance(card, dict):
            continue
        sig = (card.get("signal") or "").strip()
        if sig == "always" or (sig and sig in active):
            out.append((bid, card))
    return out


def brief(stacked):
    """The stance brief injected into the reply prompt: the loaded cards, stacked, as posture
    the voice speaks in. Empty string when nothing is stacked (so it adds no prompt weight)."""
    if not stacked:
        return ""
    lines = ["[BEHAVIOR] Situational stance card(s) are active for this moment. "
             "Speak in this posture (it is how you behave, not what you say):"]
    for bid, card in stacked:
        stance = (card.get("stance") or "").strip()
        if stance:
            reg = (card.get("register") or "").strip()   # optional target, not a delivery knob
            tag = (" (when in the %s register)" % reg) if reg else ""
            lines.append("- %s%s: %s" % (card.get("label") or bid, tag, stance))
    return "\n".join(lines) if len(lines) > 1 else ""
