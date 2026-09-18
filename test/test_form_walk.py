#!/usr/bin/env python3
"""Deterministic test for the Live-mode form walker (form_walk.py over gate.py).

Run: python3 test/test_form_walk.py   (exit 0 = all pass, 1 = any fail)

No model, no server, no browser. Drives a full walk: invalid answers re-ask the same
field, valid answers advance, and submit fires only when every required field is valid.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import form_walk  # noqa: E402

_fails = []


def check(name, ok, detail=""):
    if not ok:
        _fails.append((name, detail))


# A real-form shape: text, enum (options + labels), scalar (range), y_n.
FIELDS = [
    {"name": "role", "kind": "enum", "prompt": "Your role?",
     "options": ["engineer", "designer", "product"],
     "optionLabels": [{"value": "engineer", "label": "Engineer"},
                      {"value": "designer", "label": "Designer"},
                      {"value": "product", "label": "Product"}]},
    {"name": "minutes", "kind": "scalar", "prompt": "How many minutes?", "match": [5, 180]},
    {"name": "focus", "kind": "y_n", "prompt": "Focused block?"},
]

# start -> first field presented, walk is active
i = form_walk.start(FIELDS, title="Check-in")
check("start presents first field", i.get("field") == "role" and not i.get("done"), repr(i))
check("title rides the first say", "Check-in" in i.get("say", ""))
check("enum say lists the labels", "Designer" in i.get("say", ""), repr(i))
check("walk is active after start", form_walk.active())

# enum resolves a natural spoken answer to the canonical option VALUE
i = form_walk.step("I'm a designer")
check("enum resolves to option value", i.get("filled") == {"name": "role", "value": "designer"}, repr(i.get("filled")))
check("advances to scalar field", i.get("field") == "minutes" and not i.get("done"), repr(i))

# scalar out of range -> re-ask with range hint, nothing filled
i = form_walk.step("500")
check("out-of-range scalar re-asks", i.get("reask") and i.get("field") == "minutes", repr(i))
check("re-ask fills nothing", i.get("filled") is None)
check("scalar hint shows the range", "At most 180" in i.get("say", ""), repr(i))

i = form_walk.step("45")
check("scalar filled canonical", i.get("filled") == {"name": "minutes", "value": "45"}, repr(i.get("filled")))
check("advances to y_n", i.get("field") == "focus", repr(i))

# invalid y_n -> re-ask
i = form_walk.step("maybe")
check("invalid y_n re-asks", i.get("reask") and i.get("field") == "focus", repr(i))

# valid y_n -> requirements met -> submit SUSTAINED with canonical values
i = form_walk.step("yes")
check("done after last valid field", i.get("done") is True, repr(i))
check("sustained on all-valid", i.get("sustained") is True, repr(i))
check("last fill reported", i.get("filled") == {"name": "focus", "value": "yes"}, repr(i.get("filled")))
check("values collected canonical",
      i.get("values") == {"role": "designer", "minutes": "45", "focus": "yes"}, repr(i.get("values")))
check("walk clears after submit", not form_walk.active())

# abandon path: a fresh walk can be dropped mid-way
form_walk.start(FIELDS)
check("active after restart", form_walk.active())
form_walk.abandon()
check("abandon clears the walk", not form_walk.active())

# report
if _fails:
    print("form_walk: %d FAIL" % len(_fails))
    for name, detail in _fails:
        print("  FAIL  %s  %s" % (name, detail))
    sys.exit(1)
print("form_walk: all pass")
sys.exit(0)
