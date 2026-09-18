"""form_walk.py -- the Live-mode form walker.

gate.py IS the form: enumerated typed fields, fill + validate, submit only if every
required field is valid. This walks that form as a conversation: present one field, take
the user's next turn as its value, resolve it to the field's canonical value, validate,
re-ask on invalid, advance on valid, and submit when the requirements are met.

State is turn-based (start -> step -> step ...), not a blocking loop, so it lives at the
top of the shared turn core (_assemble_and_respond in web.py) and rides the normal speak
path. Text, voice, and the HTTP push all funnel through that core. No model runs during a
walk (dumb-by-design: it is 20 questions).

Field kinds: text, scalar, y_n, and enum (options + labels). A real page form parsed by
readForm.js sends these; the resolver maps a spoken answer to a canonical value (an option
value for enum, "yes"/"no" for y_n, a number for scalar), so voice answers land cleanly.
gate.py stays the validator of record (submit-only-if-valid). Live ceiling (capability):
yes/no only; Baseline also allows the rest.

Each step reports the field it just filled ({name, value}) so the caller can push a
walk_fill event to a paired browser, which fills the real DOM field. On done it reports
sustained + values so the caller pushes walk_ready and the browser reveals the form.

Pure: no emit, no mint. Unit-tested in test/test_form_walk.py. Spec:
docs/design/party-in-a-bucket-spec.md. Built on gate.py.
"""
import re
import gate

_walk = None   # single active walk


def active():
    return _walk is not None


def _gate_spec(f):
    """Map a walker field spec to a gate.py field spec. The resolver owns range/enum/pattern
    checks; gate re-confirms membership (enum) and numeric/y_n kind, and gates the submit."""
    name = f.get("name")
    kind = f.get("kind", "text")
    prompt = f.get("prompt", name)
    required = f.get("required", True)
    if kind == "enum":
        return {"name": name, "kind": "text", "prompt": prompt, "required": required,
                "match": [str(o).lower() for o in (f.get("options") or [])] or None}
    if kind in ("yn", "y_n"):
        return {"name": name, "kind": "y_n", "prompt": prompt, "required": required}
    if kind == "scalar":
        return {"name": name, "kind": "scalar", "prompt": prompt, "required": required}
    return {"name": name, "kind": "text", "prompt": prompt, "required": required}


def _labels(spec):
    ol = spec.get("optionLabels")
    if ol:
        return [{"value": o.get("value"), "label": o.get("label", o.get("value"))} for o in ol]
    return [{"value": o, "label": o} for o in (spec.get("options") or [])]


def _resolve(spec, text):
    """Map a spoken answer to the field's canonical value. Returns (ok, value, hint)."""
    kind = spec.get("kind", "text")
    t = (text or "").strip()
    if kind in ("yn", "y_n"):
        v = t.lower()
        if re.search(r"\b(agree|accept|yes|yeah|yep|sure|correct|true|do)\b", v) or re.match(r"^(y|ok|okay)\b", v):
            return True, "yes", None
        if re.search(r"\b(no|nope|decline|refuse|false|not|don't|do not)\b", v):
            return True, "no", None
        return False, None, "Yes or no."
    if kind == "scalar":
        m = re.sub(r"[^0-9.\-]", "", t)
        if m == "":
            return False, None, "A number."
        try:
            n = float(m)
        except ValueError:
            return False, None, "A number."
        rng = spec.get("match")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            lo, hi = rng
            if lo is not None and lo != float("-inf") and n < float(lo):
                return False, None, "At least %s." % lo
            if hi is not None and hi != float("inf") and n > float(hi):
                return False, None, "At most %s." % hi
        return True, (str(int(n)) if n == int(n) else str(n)), None
    if kind == "enum":
        labels = _labels(spec)
        lv = t.lower()
        for o in labels:
            if str(o["value"]).lower() == lv or str(o["label"]).lower() == lv:
                return True, o["value"], None
        for o in labels:
            val, lab = str(o["value"]).lower(), str(o["label"]).lower()
            if val and (val in lv or lv in val):
                return True, o["value"], None
            if lab and (lab in lv or lv in lab):
                return True, o["value"], None
        return False, None, "Pick one: " + ", ".join(str(o["label"]) for o in labels) + "."
    # text
    if not t:
        return False, None, "A few words."
    pat = spec.get("pattern")
    if pat and not re.search(pat, t):
        return False, None, "That does not look right. Try again."
    return True, t, None


def _hint_prompt(spec, hint):
    return "%s %s" % (hint, spec.get("prompt", ""))


def start(fields, title=None, template=None, context=None):
    """Open a form and present the first field. `fields` is a list of walker field specs
    (name, kind, prompt, options/optionLabels, match, required). Returns the first field's
    intent, or {ok:False}."""
    global _walk
    gate_specs = [_gate_spec(f) for f in fields]
    opened = gate.open_gate(fields=gate_specs, context=context)
    if not opened:
        return {"ok": False, "reason": "gate unavailable"}
    _walk = {
        "gid": opened["gate_id"],
        "specs": {f.get("name"): f for f in fields},
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
    spec = w["specs"].get(name, {})
    prompt = w["prompts"][name]
    n, total = w["idx"] + 1, len(w["order"])
    if spec.get("kind") == "enum":
        prompt = prompt + " (" + ", ".join(str(o["label"]) for o in _labels(spec)) + ")"
    return {"ok": True, "done": False, "reask": False, "field": name,
            "index": n, "total": total, "say": prompt,
            "card": "Q%d/%d: %s" % (n, total, prompt), "filled": None}


def step(text):
    """Take the user's turn as the current field's value. Returns one of:
      reask   {done:False, reask:True, filled:None, ...}   invalid; ask again
      next    {done:False, reask:False, filled:{name,value}, ...}  valid; next field
      done    {done:True, sustained:bool, filled:{...}, values:{...}, ...}  all valid"""
    global _walk
    if _walk is None:
        return {"ok": False, "reason": "no active walk"}
    w = _walk
    name = w["order"][w["idx"]]
    spec = w["specs"].get(name, {})
    ok, value, hint = _resolve(spec, text)
    if not ok:
        w["tries"] += 1
        return {"ok": True, "done": False, "reask": True, "field": name, "filled": None,
                "tries": w["tries"], "say": _hint_prompt(spec, hint or "Try again."),
                "card": "(needs a valid answer) " + w["prompts"][name]}
    r = gate.fill(w["gid"], name, value)
    if not r.get("valid"):
        w["tries"] += 1
        return {"ok": True, "done": False, "reask": True, "field": name, "filled": None,
                "tries": w["tries"], "say": _hint_prompt(spec, "That did not validate."),
                "card": "(needs a valid answer) " + w["prompts"][name]}
    w["tries"] = 0
    filled = {"name": name, "value": value}
    w["idx"] += 1
    if r.get("requirements_met") or w["idx"] >= len(w["order"]):
        ruling = gate.submit(w["gid"])
        out = {"ok": True, "done": True, "sustained": bool(ruling.get("sustained")),
               "values": ruling.get("values") or {}, "confidence": ruling.get("confidence"),
               "context": w["context"], "template": w["template"], "title": w["title"],
               "reason": ruling.get("reason"), "missing": ruling.get("missing"), "filled": filled}
        _walk = None
        out["say"] = _confirm(out["values"], out["title"]) if out["sustained"] else \
            ("That did not pass. " + (out.get("reason") or ""))
        out["card"] = out["say"]
        return out
    nxt = _present()
    nxt["filled"] = filled
    return nxt


def _confirm(values, title):
    parts = ", ".join("%s %s" % (k, v) for k, v in values.items())
    head = ("%s complete. " % title) if title else "Form complete. "
    return head + parts + ". Review it and press submit yourself."


def abandon():
    """Drop the active walk (the user bailed). Returns the held context, or None."""
    global _walk
    if _walk is None:
        return None
    ctx = gate.abandon(_walk["gid"])
    _walk = None
    return ctx
