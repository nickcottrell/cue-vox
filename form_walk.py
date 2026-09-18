"""form_walk.py -- the Live-mode form walker.

gate.py IS the form: enumerated typed fields, fill + validate, submit only if every
required field is valid. This walks that form as a conversation: present one field, take
the user's next turn as its value, validate by kind, re-ask on invalid, advance on valid,
and submit when the requirements are met.

State is turn-based (start -> step -> step ...), not a blocking loop, so it lives at the
top of the shared turn core (_assemble_and_respond in web.py) and rides the normal speak
path. Text, voice, and the HTTP push all funnel through that core, so all three get the
walk for free. No model runs during a walk (dumb-by-design: it is 20 questions).

Live ceiling (capability_matrix.json): yes/no fields only. Baseline also allows scalar
and text. The walk itself is kind-agnostic; the caller picks a form that fits the mode.

Pure: no emit, no mint. Each step returns an intent carrying a ready-to-speak `say` and a
`card` to show; the caller performs the side effects. Unit-tested in test/test_form_walk.py.

Spec: docs/design/party-in-a-bucket-spec.md. Built on gate.py.
"""
import gate

_walk = None   # single active walk


def active():
    return _walk is not None


def _hint(spec):
    """A short, kind-specific nudge spoken before a re-ask."""
    kind = spec.get("kind", "text")
    if kind == "y_n":
        return "Yes or no."
    if kind == "scalar":
        m = spec.get("match")
        if isinstance(m, (list, tuple)) and len(m) == 2:
            return "A number between %s and %s." % (m[0], m[1])
        return "A number."
    return "A few words."


def start(fields, title=None, template=None, context=None):
    """Open a form and present the first field. `fields` is a list of gate field specs
    (name, kind, prompt, match, required). Returns the first field's intent, or {ok:False}."""
    global _walk
    opened = gate.open_gate(fields=fields, context=context)
    if not opened:
        return {"ok": False, "reason": "gate unavailable"}
    _walk = {
        "gid": opened["gate_id"],
        "specs": list(fields),
        "order": [f["name"] for f in opened["fields"]],
        "prompts": {f["name"]: f["prompt"] for f in opened["fields"]},
        "idx": 0, "tries": 0,
        "title": title, "template": template, "context": context,
    }
    intent = _present()
    if title:
        intent["say"] = ("%s. " % title) + intent["say"]
    return intent


def _present():
    w = _walk
    name = w["order"][w["idx"]]
    prompt = w["prompts"][name]
    n, total = w["idx"] + 1, len(w["order"])
    return {"ok": True, "done": False, "reask": False, "field": name,
            "index": n, "total": total, "say": prompt,
            "card": "Q%d/%d: %s" % (n, total, prompt)}


def step(text):
    """Take the user's turn as the current field's value. Returns one of:
      reask   {done:False, reask:True, ...}   invalid; ask the same field again
      next    {done:False, reask:False, ...}  valid; here is the next field
      done    {done:True, sustained:bool, values:{...}, ...}  all valid -> submitted"""
    global _walk
    if _walk is None:
        return {"ok": False, "reason": "no active walk"}
    w = _walk
    name = w["order"][w["idx"]]
    r = gate.fill(w["gid"], name, text)
    if not r.get("valid"):
        w["tries"] += 1
        spec = next((s for s in w["specs"] if s.get("name") == name), {})
        prompt = w["prompts"][name]
        return {"ok": True, "done": False, "reask": True, "field": name,
                "tries": w["tries"], "say": "%s %s" % (_hint(spec), prompt),
                "card": "(needs a valid answer) " + prompt}
    w["tries"] = 0
    w["idx"] += 1
    if r.get("requirements_met") or w["idx"] >= len(w["order"]):
        ruling = gate.submit(w["gid"])
        out = {"ok": True, "done": True, "sustained": bool(ruling.get("sustained")),
               "values": ruling.get("values") or {}, "confidence": ruling.get("confidence"),
               "context": w["context"], "template": w["template"], "title": w["title"],
               "reason": ruling.get("reason"), "missing": ruling.get("missing")}
        _walk = None
        if out["sustained"]:
            out["say"] = _confirm(out["values"], out["title"])
        else:
            out["say"] = "That did not pass. " + (out.get("reason") or "")
        out["card"] = out["say"]
        return out
    return _present()


def _confirm(values, title):
    parts = ", ".join("%s %s" % (k, v) for k, v in values.items())
    head = ("%s complete. " % title) if title else "Form complete. "
    return head + parts + "."


def abandon():
    """Drop the active walk (the user bailed). Returns the held context, or None."""
    global _walk
    if _walk is None:
        return None
    ctx = gate.abandon(_walk["gid"])
    _walk = None
    return ctx
