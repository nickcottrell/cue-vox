"""cache_in.py -- mint a valid gate submit into the local pool as a signed CaaS card.

The SVG bucket is a TEMPLATE, never the minter. A valid submit runs this pipeline and
our system holds the pen:

    scrub -> validate (gate.py) -> discover coord (caas F1, optional) -> mint (cue-mem) -> receipt

See docs/design/party-in-a-bucket-spec.md. Reuses:
  gate.py                      the type-checked ruling (SUSTAINED/OVERRULED + preponderance + proof)
  cue-mem/lib tokens.create_token   the local pool mint (thermal, signed, propagates)
  caas encode_f1 / boundary    optional VRGB coordinate + the scrub boundary

Standalone testable:  python3 cache_in.py            (dry run, no mint)
                      python3 cache_in.py --mint     (really mints a demo token)
"""
import os, sys, re, json, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))


def _maestro_root(start=HERE):
    p = start
    for _ in range(8):
        if os.path.isdir(os.path.join(p, "cue-mem", "lib")):
            return p
        nxt = os.path.dirname(p)
        if nxt == p:
            break
        p = nxt
    return None


# ---- lazy substrate wiring (all optional except the mint) ---------------------------
_ROOT = _maestro_root()
_CAAS = os.environ.get("CAAS_PATH", os.path.expanduser("~/Repositories/caas"))

# Pin the token store to the maestro pool. cue-mem resolves .claude/tokens by walking up
# from cwd, and core/cue-vox has its OWN .claude -- without this a cache-in would land in
# the microservice's local pool and never propagate to recent_context / both instances.
if _ROOT and not os.environ.get("CUE_MEM_HOME"):
    os.environ["CUE_MEM_HOME"] = _ROOT

for _pth in ([os.path.join(_ROOT, "cue-mem", "lib")] if _ROOT else []) + [HERE, _CAAS]:
    if _pth and os.path.isdir(_pth) and _pth not in sys.path:
        sys.path.insert(0, _pth)

try:
    import gate as _gate
    GATE_OK = True
except Exception:
    _gate = None
    GATE_OK = False

try:
    from tokens import create_token as _create_token
    MINT_OK = True
except Exception:
    _create_token = None
    MINT_OK = False

try:
    from encode import encode_f1 as _encode_f1
    ENCODE_OK = True
except Exception:
    _encode_f1 = None
    ENCODE_OK = False

try:
    from boundary import apply_boundary as _apply_boundary
    BOUNDARY_OK = True
except Exception:
    _apply_boundary = None
    BOUNDARY_OK = False


# ---- 1. scrub (fail-closed: always runs, even without the caas boundary) -------------
_FALLBACK_PII = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?\d[\s.-]?){10,}\b"), "[PHONE]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
]


def scrub(text):
    """Scrub-first. Uses the caas policy boundary when present, else a minimal fail-closed
    redactor. Never returns unscrubbed text."""
    text = "" if text is None else str(text)
    if BOUNDARY_OK:
        try:
            return _apply_boundary(text).scrubbed_text
        except Exception:
            pass
    out = text
    for pat, marker in _FALLBACK_PII:
        out = pat.sub(marker, out)
    return out


# ---- 2. validate against the field Type (via gate.py) --------------------------------
def _gate_spec(field):
    """Map a template field into a gate.py field spec. A template 'challenge' becomes an
    exact-match text field against the template's own answer (deterministic, no auto-gen)."""
    kind = field.get("kind", "text")
    name = field.get("name", kind)
    if kind == "challenge":
        return {"name": name, "kind": "text", "match": field.get("answer")}
    if kind == "scalar":
        m = field.get("answer")
        if m is None and (field.get("min") is not None or field.get("max") is not None):
            m = [field.get("min"), field.get("max")]
        return {"name": name, "kind": "scalar", "match": m}
    return {"name": name, "kind": kind, "match": field.get("match")}


def validate(field, value):
    """Return (valid, confidence, proof) for a value against a field Type. Falls back to a
    non-empty check if gate.py is unavailable."""
    if not GATE_OK:
        v = str(value).strip()
        return (len(v) > 0, 1.0 if v else 0.0, None)
    spec = _gate_spec(field)
    g = _gate.open_gate(fields=[spec])
    if not g:
        return (False, 0.0, None)
    ruling = _gate.rule(g["gate_id"], value)
    return (bool(ruling.get("sustained")), float(ruling.get("preponderance", 0.0)),
            ruling.get("proof"))


# ---- 3. discover the coordinate against the template basis (optional) ----------------
def discover(value, node, basis):
    anchors = (basis or {}).get("anchors")
    if not (ENCODE_OK and anchors):
        return None
    try:
        card = {"title": node.get("title", ""), "body": str(value)}
        return _encode_f1(card, anchors)
    except Exception:
        return None


# ---- provenance: the template's own fingerprint --------------------------------------
def identity_color(template):
    canon = json.dumps(template, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:6]


def _receipt_of(node):
    """The grounding that travels with the token: node knowledge + references + receipt."""
    r = {}
    if node.get("knowledge"):
        r["knowledge"] = node["knowledge"]
    if node.get("references"):
        r["references"] = node["references"]
    if node.get("receipt"):
        r["receipt"] = node["receipt"]
    return r


# ---- 1-3. the ingredient: a validated, coord-stamped card (NO mint) ------------------
def ingredient(node, value, template):
    """Run scrub -> validate -> discover for one field and return the validated card. This is
    the mint-boundary-free unit: the package (cue_bit.Runner) accumulates ingredients and mints
    the authorized whole. `valid` is False when the value does not satisfy the field type."""
    field = node.get("field", {"kind": "text", "name": node.get("id", "value")})
    clean = scrub(value)
    valid, preponderance, proof = validate(field, clean)
    coord = discover(clean, node, template.get("basis")) if valid else None
    return {
        "field": field.get("name", "value"),
        "type": field.get("kind", "text"),
        "value": clean,
        "valid": bool(valid),
        "preponderance": preponderance,
        "proof": proof,
        "coord": coord,
        "receipt": _receipt_of(node),
    }


def mint(label, value, token_type="text", visibility="shared", tags=None, metadata=None,
         base_temp=75):
    """Thin wrapper over the pinned cue-mem create_token. Returns the token dict or None. The
    pen: this is where our system signs into the shared pool (CUE_MEM_HOME pinned at import)."""
    if not MINT_OK:
        return None
    try:
        return _create_token(label=label, value=value, token_type=token_type,
                             visibility=visibility, base_temp=base_temp,
                             tags=tags or [], metadata=metadata or {})
    except Exception:
        return None


# ---- 4 + 5. degenerate one-field package: validate and mint directly (live-hold path) --
def cache_in(node, value, template, mint_it=True):
    """Cache a single valid submit as a one-field package (N=1). Returns a receipt. On an
    invalid item nothing is minted: {cached: false, reason, preponderance}. Multi-field
    traversals go through cue_bit.Runner, which mints the authorized package atomically."""
    ing = ingredient(node, value, template)
    if not ing["valid"]:
        return {"cached": False, "reason": "not sustained", "preponderance": ing["preponderance"]}

    tmpl_id = template.get("id", "template")
    ic = identity_color(template)
    coord = ing["coord"]
    tags = ["cache-in", tmpl_id, ing["field"]] + (["vrgb"] if coord else [])
    visibility = "shared" if template.get("trust", "public") == "public" else "private"

    metadata = {
        "coord": coord,
        "basis_id": (template.get("basis") or {}).get("id"),
        "template": {"id": tmpl_id, "identity_color": ic},
        "field": ing["field"], "type": ing["type"],
        "receipt": ing["receipt"],
        "preponderance": ing["preponderance"], "proof": ing["proof"],
    }
    receipt = {
        "cached": True, "value": ing["value"],
        "hex": (coord or {}).get("hex"), "rho": (coord or {}).get("rho"),
        "preponderance": ing["preponderance"], "proof": ing["proof"],
        "template": {"id": tmpl_id, "identity_color": ic}, "minted": False,
    }
    if mint_it:
        tok = mint("cache-in:" + ing["field"], ing["value"],
                   token_type="vrgb_token" if coord else "text",
                   visibility=visibility, tags=tags, metadata=metadata)
        if tok:
            receipt["minted"] = True
            receipt["token_id"] = tok.get("token_id")
            receipt["temperature"] = tok.get("temperature")
        else:
            receipt["mint_error"] = "cue-mem create_token unavailable"
    return receipt


def status():
    return {"maestro_root": _ROOT, "gate": GATE_OK, "mint": MINT_OK,
            "encode": ENCODE_OK, "boundary": BOUNDARY_OK, "caas_path": _CAAS}


if __name__ == "__main__":
    do_mint = "--mint" in sys.argv
    print("substrate:", json.dumps(status(), indent=2))
    template = {
        "id": "3-challenge", "trust": "public",
        "basis": {"id": "demo", "anchors": [
            {"id": "sure", "hex": "#3ddc84", "context": "resolved settled proven correct sustained"},
            {"id": "doubt", "hex": "#e0a53d", "context": "uncertain open unresolved objection maybe"},
        ]},
    }
    node = {"id": "obj", "title": "Objection", "field": {"name": "ruling", "kind": "challenge", "answer": "31"},
            "knowledge": [{"kind": "raw", "text": "18 + 13 = 31"}], "receipt": "arithmetic gate"}

    text_node = {"id": "who", "title": "Context", "field": {"name": "who", "kind": "text"},
                 "knowledge": [{"kind": "ref", "uri": "vault://portfolio", "cite": "portfolio vault"}]}

    print("\n--- invalid submit (wrong answer -> OVERRULED, no mint) ---")
    print(json.dumps(cache_in(node, "30", template, mint_it=do_mint), indent=2))
    print("\n--- valid challenge submit (correct answer -> cached, coord discovered) ---")
    print(json.dumps(cache_in(node, "31", template, mint_it=do_mint), indent=2))
    print("\n--- valid text submit (email scrubbed before mint, ref carried as receipt) ---")
    print(json.dumps(cache_in(text_node, "reach me at me@x.com about the cloud", template, mint_it=do_mint), indent=2))
