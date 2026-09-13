"""The gate primitive: a gate IS a form.

A gate holds enumerated fields (typed vars: arithmetic / text / scalar / y_n), you FILL
them, and it SUBMITS (the ruling) when the requirements are met. The one-field arithmetic
gate is the degenerate case (fill the one field correctly = submit = SUSTAINED). A therapy
intake or a sprint round is the same primitive with more fields. Composition is recursive:
a walk fills a gate field by field; preponderance is the submit threshold at any level.

Built on cue-mem/lib/challenge.py for the 'arithmetic' field kind (generate -> present,
verify -> valid/confidence/proof; confidence IS preponderance, proof IS the signed weight).

Courtroom shape:  open_gate (OBJECT, hold) -> fill... -> submit (RULING).
SUSTAINED resumes the held context WITH the filled form; OVERRULED releases it.
"""
import time

try:
    import challenge as _challenge     # cue-mem/lib is on sys.path inside web.py
    _AVAILABLE = True
except Exception:                      # pragma: no cover
    _challenge = None
    _AVAILABLE = False

_open = {}   # gate_id -> {gate_id, fields, context, weight, opened_ms}


def available():
    return _AVAILABLE


def _new_id():
    return "gate_%d" % int(time.time() * 1e6)


def _make_field(spec):
    """Normalize a field spec into a field dict. spec keys: name, kind, prompt, match,
    required. kind in {arithmetic, text, scalar, y_n}. arithmetic auto-generates a
    challenge (its prompt + expected answer)."""
    kind = spec.get("kind", "text")
    f = {"name": spec.get("name", kind), "kind": kind,
         "required": spec.get("required", True),
         "match": spec.get("match"),
         "value": None, "filled": False, "valid": False, "confidence": 0.0, "proof": None}
    if kind == "arithmetic" and _AVAILABLE:
        ch = _challenge.generate("arithmetic")
        f["challenge"] = ch
        f["issued_ms"] = time.time() * 1000.0
        f["prompt"] = ch["prompt"]                      # raw "18 + 13"; the UI adds " = ?"
    else:
        f["prompt"] = spec.get("prompt", spec.get("name", ""))
    return f


def open_gate(fields=None, context=None, weight=1.0, kind=None):
    """OBJECT: open a form-gate and HOLD `context` pending submit.
    Pass `fields` (list of specs) for a multi-field form, or `kind='arithmetic'` (or leave
    both) for the degenerate one-field challenge. Returns {gate_id, weight, fields:[...]}
    and, for the one-field case, top-level {prompt, kind} for convenience. None if a
    challenge field is requested but the engine is unavailable."""
    if fields is None:
        fields = [{"kind": kind or "arithmetic", "name": "answer"}]
    needs_challenge = any(f.get("kind", "text") == "arithmetic" for f in fields)
    if needs_challenge and not _AVAILABLE:
        return None
    fdicts = [_make_field(s) for s in fields]
    gate_id = _new_id()
    _open[gate_id] = {"gate_id": gate_id, "fields": fdicts, "context": context,
                      "weight": float(weight), "opened_ms": time.time() * 1000.0}
    out = {"gate_id": gate_id, "weight": float(weight),
           "fields": [{"name": f["name"], "prompt": f["prompt"], "kind": f["kind"]} for f in fdicts]}
    if len(fdicts) == 1:                                 # one-field convenience (barge path)
        out["prompt"] = fdicts[0]["prompt"]
        out["kind"] = fdicts[0]["kind"]
    return out


def _validate(field, value):
    """(valid, confidence) for a filled field, by kind."""
    kind = field["kind"]
    m = field.get("match")
    if kind == "arithmetic" and _AVAILABLE and field.get("challenge"):
        rt = time.time() * 1000.0 - field.get("issued_ms", time.time() * 1000.0)
        valid, confidence, proof = _challenge.verify(field["challenge"], value, rt)
        field["proof"] = proof
        return bool(valid), float(confidence)
    if kind == "y_n":
        v = str(value).strip().lower()
        ok = v in ("yes", "no", "y", "n", "true", "false")
        if m is not None:
            ok = ok and v[:1] == str(m).strip().lower()[:1]
        return ok, (1.0 if ok else 0.0)
    if kind == "scalar":
        try:
            num = float(value)
        except (TypeError, ValueError):
            return False, 0.0
        if isinstance(m, (list, tuple)) and len(m) == 2:
            ok = m[0] <= num <= m[1]
        elif m is not None:
            ok = num == float(m)
        else:
            ok = True
        return ok, (1.0 if ok else 0.0)
    # text
    v = str(value).strip()
    if m is None:
        return (len(v) > 0), (1.0 if v else 0.0)
    if isinstance(m, (list, tuple, set)):
        ok = v.lower() in [str(x).lower() for x in m]
    else:
        ok = v.lower() == str(m).strip().lower()
    return ok, (1.0 if ok else 0.0)


def fill(gate_id, field_name=None, value=None):
    """Fill one field (by name, or the first unfilled if name omitted). Returns
    {ok, field, valid, requirements_met, missing}."""
    g = _open.get(gate_id)
    if not g:
        return {"ok": False, "reason": "no such gate"}
    field = None
    if field_name:
        field = next((f for f in g["fields"] if f["name"] == field_name), None)
    if field is None:
        field = next((f for f in g["fields"] if not f["filled"]), None)
    if field is None:
        return {"ok": False, "reason": "no fillable field"}
    valid, confidence = _validate(field, value)
    field.update(value=value, filled=True, valid=valid, confidence=confidence)
    return {"ok": True, "field": field["name"], "valid": valid,
            "requirements_met": _requirements_met(g), "missing": _missing(g)}


def _missing(g):
    return [f["name"] for f in g["fields"] if f["required"] and not (f["filled"] and f["valid"])]


def _requirements_met(g):
    return len(_missing(g)) == 0


def submit(gate_id):
    """RULING: submit the gate. SUSTAINED if requirements met, else not. On SUSTAINED the
    held context resumes WITH the filled values; preponderance = mean field confidence x
    the gate weight."""
    g = _open.get(gate_id)
    if not g:
        return {"sustained": False, "reason": "no such gate", "preponderance": 0.0, "context": None}
    if not _requirements_met(g):
        return {"sustained": False, "reason": "requirements not met",
                "missing": _missing(g), "preponderance": 0.0, "context": None}
    _open.pop(gate_id, None)
    confs = [f["confidence"] for f in g["fields"] if f["filled"]]
    avg = sum(confs) / len(confs) if confs else 0.0
    return {
        "sustained": True,
        "values": {f["name"]: f["value"] for f in g["fields"]},
        "confidence": round(avg, 3),
        "weight": g["weight"],
        "preponderance": round(avg * g["weight"], 3),
        "proof": next((f["proof"] for f in g["fields"] if f.get("proof")), None),
        "context": g["context"],
    }


def rule(gate_id, response, attention=None):
    """One-field shorthand (the barge path): fill the first field and submit. Preserves
    the {sustained, confidence, weight, preponderance, proof, context} shape."""
    g = _open.get(gate_id)
    if not g:
        return {"sustained": False, "reason": "no such gate", "preponderance": 0.0, "context": None}
    fill(gate_id, g["fields"][0]["name"], response)
    ruling = submit(gate_id)
    ruling["answer"] = response
    return ruling


def abandon(gate_id):
    """Drop an open gate without ruling; returns its held context."""
    g = _open.pop(gate_id, None)
    return g["context"] if g else None


def open_count():
    return len(_open)
