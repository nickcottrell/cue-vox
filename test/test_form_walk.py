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


FIELDS = [
    {"name": "focus", "kind": "y_n", "prompt": "Focused block?"},
    {"name": "blockers", "kind": "y_n", "prompt": "Blockers?"},
    {"name": "minutes", "kind": "scalar", "prompt": "How many minutes?", "match": [5, 180]},
]

# start -> first field presented, walk is active
i = form_walk.start(FIELDS, title="Check-in")
check("start presents first field", i.get("field") == "focus" and not i.get("done"))
check("title rides the first say", "Check-in" in i.get("say", ""))
check("walk is active after start", form_walk.active())

# invalid y_n -> re-ask the SAME field, still active
i = form_walk.step("maybe")
check("invalid y_n re-asks", i.get("reask") and i.get("field") == "focus", repr(i))
check("re-ask carries a hint", "Yes or no" in i.get("say", ""))

# valid y_n -> advance
i = form_walk.step("yes")
check("advances to second field", i.get("field") == "blockers" and not i.get("done"), repr(i))

i = form_walk.step("no")
check("advances to scalar field", i.get("field") == "minutes", repr(i))

# scalar out of range -> re-ask with range hint
i = form_walk.step("500")
check("out-of-range scalar re-asks", i.get("reask") and i.get("field") == "minutes", repr(i))
check("scalar hint shows the range", "between 5 and 180" in i.get("say", ""), repr(i))

# valid scalar -> requirements met -> submit SUSTAINED with collected values
i = form_walk.step("45")
check("done after last valid field", i.get("done") is True, repr(i))
check("sustained on all-valid", i.get("sustained") is True, repr(i))
check("values collected by type",
      i.get("values") == {"focus": "yes", "blockers": "no", "minutes": "45"}, repr(i.get("values")))
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
