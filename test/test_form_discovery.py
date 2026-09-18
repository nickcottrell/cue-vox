#!/usr/bin/env python3
"""Deterministic test for form_discovery.py (no network: sample HTML in, manifest out).

Run: python3 test/test_form_discovery.py   (exit 0 = pass, 1 = fail)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import form_discovery  # noqa: E402

SAMPLE = """
<html><head><title>Careers</title></head><body>
  <h2>Apply for the role</h2>
  <form id="apply" action="/apply" method="post">
    <label for="fullname">Full name</label>
    <input id="fullname" name="fullname" type="text" required />
    <label for="email">Email address</label>
    <input id="email" name="email" type="email" required />
    <label for="role">Role</label>
    <select id="role" name="role" required>
      <option value="" disabled></option>
      <option value="engineer">Engineer</option>
      <option value="designer">Designer</option>
    </select>
    <label for="years">Years</label>
    <input id="years" name="years" type="number" min="0" max="40" required />
    <fieldset><legend>Work location</legend>
      <label><input type="radio" name="location" value="remote" required /> Remote</label>
      <label><input type="radio" name="location" value="onsite" /> On site</label>
    </fieldset>
    <button type="submit">Submit application</button>
  </form>

  <h3>Newsletter</h3>
  <form id="news" action="https://mail.example.com/subscribe" method="post">
    <input name="email" type="email" placeholder="you@example.com" required />
    <button type="submit">Subscribe</button>
  </form>
</body></html>
"""

_fails = []
def check(name, ok, detail=""):
    if not ok:
        _fails.append((name, detail))

m = form_discovery.discover("https://acme.test/careers", html_text=SAMPLE)

check("counts both forms", m["count"] == 2, repr(m["count"]))
check("both submittable", m["submittable_count"] == 2, repr(m["submittable_count"]))

apply = m["forms"][0]
check("apply id", apply["id"] == "apply", repr(apply["id"]))
check("apply title from heading", apply["title"] == "Apply for the role", repr(apply["title"]))
check("apply method", apply["method"] == "post")
check("apply action resolved absolute", apply["action"] == "https://acme.test/apply", repr(apply["action"]))
check("apply dest host", apply["dest"] == "acme.test", repr(apply["dest"]))
check("apply submit label", apply["submit_label"] == "Submit application", repr(apply["submit_label"]))
# fields: fullname(text) email(text) role(enum) years(scalar) location(enum radio group)
names = [f["name"] for f in apply["fields"]]
check("apply field count 5", apply["field_count"] == 5, repr(names))
kinds = {f["name"]: f["kind"] for f in apply["fields"]}
check("role is enum", kinds.get("role") == "enum", repr(kinds))
check("years is scalar", kinds.get("years") == "scalar", repr(kinds))
check("location radio grouped to enum", kinds.get("location") == "enum", repr(kinds))
role = next(f for f in apply["fields"] if f["name"] == "role")
check("role options captured", role.get("options") == ["engineer", "designer"], repr(role.get("options")))
loc = next(f for f in apply["fields"] if f["name"] == "location")
check("location options captured", loc.get("options") == ["remote", "onsite"], repr(loc.get("options")))

news = m["forms"][1]
check("news title from heading", news["title"] == "Newsletter", repr(news["title"]))
check("news dest is the cross-host action", news["dest"] == "mail.example.com", repr(news["dest"]))
check("news one field", news["field_count"] == 1, repr(news["field_count"]))

# only-http guard
try:
    form_discovery.fetch("ftp://nope")
    check("rejects non-http", False, "no error raised")
except ValueError:
    check("rejects non-http", True)

if _fails:
    print("form_discovery: %d FAIL" % len(_fails))
    for n, d in _fails:
        print("  FAIL  %s  %s" % (n, d))
    sys.exit(1)
print("form_discovery: all pass")
sys.exit(0)
