"""cue_bit.py -- the cue-bit runner: fill a form (metadata) in the background, minting the
authorized whole as a signed package (a CaaS take).

A form is just metadata: a graph of cue-bits. A node IS a cue-bit -- {ssml (the performance),
content/knowledge (the grounding), field/type (the target + validator), nd (the coordinate)}.
The user never sees the form. For each required field the runner:

  1. tries to fill from grounded context (schema-constrained extraction, pluggable),
  2. if empty, PERFORMS the cue-bit (returns the node's ssml to speak) to elicit a value,
  3. validates the value against the field type (cache_in.ingredient); valid -> ingredient,
     invalid -> re-bit,
  4. when every required field is filled, AUTHORIZES the valid data set and MINTS the package
     atomically as one signed take into the shared pool.

Auth roots in the source each value came from, the type-gate proof, and the system signer --
never a user click (the user never touches the form).

Spec: docs/design/party-in-a-bucket-spec.md

Standalone:  python3 cue_bit.py         (deterministic stub extractor, dry run)
             python3 cue_bit.py --mint  (really mints the package take)
"""
import os, sys, json, hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache_in   # pins CUE_MEM_HOME + wires gate/encode/boundary/mint on import


class Runner:
    def __init__(self, template, context="", extractor=None, signer="cue-vox", mint=True):
        self.t = template
        self.context = context or ""
        self.extractor = extractor        # fn(field_dict, context) -> value or None
        self.signer = signer
        self.mint = mint
        self.nodes = [n for n in template.get("nodes", []) if n.get("field")]
        self.filled = {}                  # node_id -> ingredient (+ source)
        self.attempts = {}                # node_id -> int
        self._package = None

    # -- selection ---------------------------------------------------------------------
    def _required(self):
        return [n for n in self.nodes if (n.get("field") or {}).get("required", True)]

    def _pending(self):
        return [n for n in self._required() if n["id"] not in self.filled]

    def _node(self, node_id):
        return next((n for n in self.nodes if n["id"] == node_id), None)

    # -- background fill from grounded context (no bit) --------------------------------
    def _context_fill(self, node):
        if not self.extractor:
            return None
        try:
            return self.extractor(node.get("field") or {}, self.context)
        except Exception:
            return None

    def _accept(self, node, value, source):
        ing = cache_in.ingredient(node, value, self.t)
        if ing["valid"]:
            ing["source"] = source
            self.filled[node["id"]] = ing
            return True
        return False

    # -- the loop: what happens next ---------------------------------------------------
    def next_action(self):
        """Fill everything context grounds, then either finish (authorize + mint the package)
        or hand back the next cue-bit to perform."""
        for node in list(self._pending()):
            cand = self._context_fill(node)
            if cand is not None:
                self._accept(node, cand, "context")
        if not self._pending():
            return {"done": True, "package": self._authorize_and_mint()}
        node = self._pending()[0]
        field = node.get("field") or {}
        return {"cue_bit": {                       # the bit to perform (presence)
            "node_id": node["id"],
            "ssml": node.get("ssml"),              # the performance
            "prompt": node.get("prompt") or field.get("placeholder") or node.get("title"),
            "field": field.get("name"),
            "attempts": self.attempts.get(node["id"], 0),
        }}

    def provide(self, node_id, value):
        """Feed a value elicited by a cue-bit. Valid -> filled + next_action. Invalid -> re-bit."""
        node = self._node(node_id)
        if not node:
            return {"ok": False, "reason": "no such cue-bit"}
        if self._accept(node, value, "elicited"):
            return {"ok": True, "filled": node_id, "next": self.next_action()}
        self.attempts[node_id] = self.attempts.get(node_id, 0) + 1
        return {"ok": False, "reason": "invalid",
                "re_bit": node.get("ssml") or node.get("prompt"),
                "attempts": self.attempts[node_id]}

    # -- authorize + mint the package (the take) ---------------------------------------
    def _dataset(self):
        out = []
        for n in self.nodes:                       # preserve template order
            ing = self.filled.get(n["id"])
            if ing:
                out.append({"field": ing["field"], "value": ing["value"],
                            "coord": ing["coord"], "source": ing.get("source"),
                            "receipt": ing["receipt"], "proof": ing["proof"]})
        return out

    def _authorize(self, dataset, identity_color):
        body = json.dumps({"ic": identity_color, "dataset": dataset},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hashlib.sha256(body).hexdigest()
        return {"signer": self.signer, "signature": sig[:16],
                "take_hash": sig[:6],              # the package's identity color (hash IS address)
                "anchor": "agent-fill",            # source-rooted, not a user click
                "proofs": [d.get("proof") for d in dataset if d.get("proof")]}

    def _authorize_and_mint(self):
        if self._package is not None:
            return self._package
        dataset = self._dataset()
        ic = cache_in.identity_color(self.t)
        auth = self._authorize(dataset, ic)
        summary = "; ".join("%s=%s" % (d["field"], d["value"]) for d in dataset)
        package = {
            "template": {"id": self.t.get("id"), "identity_color": ic},
            "basis_id": (self.t.get("basis") or {}).get("id"),
            "dataset": dataset, "auth": auth, "summary": summary, "minted": False,
        }
        if self.mint:
            visibility = "shared" if self.t.get("trust", "public") == "public" else "private"
            tok = cache_in.mint(
                label="package:" + str(self.t.get("id", "form")),
                value=summary, token_type="vrgb_package", visibility=visibility,
                tags=["cache-in", "package", str(self.t.get("id"))],
                metadata={"package": package},
            )
            if tok:
                package["minted"] = True
                package["token_id"] = tok.get("token_id")
                package["temperature"] = tok.get("temperature")
        self._package = package
        return package

    # -- convenience: run to completion with a fixed elicitation source ----------------
    def run(self, answers=None):
        """Drive the loop. `answers` maps node_id -> value to feed when a cue-bit fires (a stand
        in for the live user). Returns the final package (or the next cue-bit if unanswered)."""
        answers = answers or {}
        for _ in range(len(self.nodes) * 3 + 2):
            act = self.next_action()
            if act.get("done"):
                return act["package"]
            nid = act["cue_bit"]["node_id"]
            if nid in answers:
                self.provide(nid, answers.pop(nid))
            else:
                return act                          # waiting on this cue-bit
        return {"error": "runner did not converge"}


if __name__ == "__main__":
    do_mint = "--mint" in sys.argv

    # a form = metadata. Three cue-bits: two the context already grounds, one that must be
    # elicited (so a cue-bit fires).
    template = {
        "id": "intake-demo", "trust": "public",
        "basis": {"id": "demo", "anchors": [
            {"id": "who", "hex": "#2584f4", "context": "name person who identity called"},
            {"id": "what", "hex": "#f42584", "context": "build project work making thing"},
        ]},
        "nodes": [
            {"id": "name", "title": "Name", "field": {"name": "who", "kind": "text", "min": 2},
             "ssml": "<voice name=\"mid\">What should I call you?</voice>",
             "knowledge": [{"kind": "raw", "text": "asked in intake"}]},
            {"id": "proj", "title": "Project", "field": {"name": "what", "kind": "text", "min": 2},
             "ssml": "<voice name=\"mid\">What are you building?</voice>"},
            {"id": "ok", "title": "Consent", "field": {"name": "ok", "kind": "y_n", "match": "yes"},
             "ssml": "<voice name=\"mid\">Cache this in?</voice>"},
        ],
    }

    # deterministic stub extractor: grounds `who` and `what` from context, leaves `ok` empty
    ctx = "my name is Nick and I am building a context cloud"
    def stub(field, context):
        n = (field or {}).get("name")
        if n == "who" and "nick" in context.lower():
            return "Nick"
        if n == "what" and "building" in context.lower():
            return "a context cloud"
        return None                                  # `ok` is not in context -> cue-bit fires

    print("substrate:", json.dumps(cache_in.status(), indent=2))
    r = Runner(template, context=ctx, extractor=stub, mint=do_mint)

    print("\n--- next_action (grounds who+what, then a cue-bit fires for consent) ---")
    print(json.dumps(r.next_action(), indent=2))

    print("\n--- provide the elicited consent, package authorizes + mints ---")
    pkg = r.run(answers={"ok": "yes"})
    print(json.dumps(pkg, indent=2))
