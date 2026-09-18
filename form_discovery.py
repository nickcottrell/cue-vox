"""form_discovery.py -- drop a URL, get a manifest of the page's forms.

Fetch a public page (read-only GET, resilient: http/https only, timeout, size cap, honest
User-Agent), parse every <form> from the static HTML, and return a manifest: how many
forms, and for each one a plain-language read (a title from the nearest heading/legend, a
context blurb of nearby words), its destination (action host + method), its fields (name +
inferred kind + label + required), and its submit label. Submittable forms are flagged and
counted.

Static HTML only: JS-rendered forms will not appear. That is the boundary a browser plugin
closes later (it reads the live DOM). For now, a URL in, a manifest out.

Deterministic and dependency-free (urllib + html.parser). Unit-tested against sample HTML
in test/test_form_discovery.py (no network). Roots: resilient-integration (respectful
outbound), source-fidelity (report only what the HTML contains).
"""
import re
import urllib.request
import urllib.parse
from html.parser import HTMLParser

MAX_BYTES = 2_000_000
TIMEOUT = 8
_UA = "maestro-walker/0.1 (form discovery; read-only)"


def fetch(url):
    if not re.match(r"^https?://", url or "", re.I):
        raise ValueError("only http and https URLs are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read(MAX_BYTES)
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, errors="replace")


def _humanize(s):
    if not s:
        return ""
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return re.sub(r"\s+", " ", s).strip().lower()


class _FormParser(HTMLParser):
    """Collect each <form> with its controls and the nearby text that describes it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.cur = None
        self._in_form = 0
        self._grab = None          # what text we are currently capturing
        self._buf = ""
        self._last_heading = ""
        self._select = None
        self._option = None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "title"):
            self._grab = "heading"; self._buf = ""
        if tag == "form":
            self._in_form += 1
            self.cur = {"attrs": a, "controls": [], "words": [], "labels": [],
                        "legend": "", "submit": "", "heading": self._last_heading}
        if self.cur is None or self._in_form <= 0:
            return
        if tag == "input":
            t = (a.get("type") or "text").lower()
            if t in ("submit", "image", "button"):
                self.cur["submit"] = a.get("value") or self.cur["submit"] or "Submit"
            elif t not in ("hidden", "reset"):
                self.cur["controls"].append({"tag": "input", "type": t,
                                             "name": a.get("name") or a.get("id"),
                                             "required": "required" in a,
                                             "placeholder": a.get("placeholder"),
                                             "aria": a.get("aria-label"), "value": a.get("value")})
        elif tag == "select":
            self._select = {"tag": "select", "name": a.get("name") or a.get("id"),
                            "required": "required" in a, "options": []}
            self.cur["controls"].append(self._select)
        elif tag == "option":
            self._grab = "option"; self._buf = ""; self._option = {"value": a.get("value")}
        elif tag == "textarea":
            self.cur["controls"].append({"tag": "textarea", "name": a.get("name") or a.get("id"),
                                         "required": "required" in a, "placeholder": a.get("placeholder")})
        elif tag == "button":
            if (a.get("type") or "submit").lower() == "submit":
                self._grab = "button"; self._buf = ""
        elif tag == "label":
            self._grab = "label"; self._buf = ""
        elif tag == "legend":
            self._grab = "legend"; self._buf = ""

    def handle_data(self, data):
        if self._grab:
            self._buf += data
        elif self.cur is not None:
            d = data.strip()
            if d:
                self.cur["words"].append(d)

    def handle_endtag(self, tag):
        clean = re.sub(r"\s+", " ", self._buf).strip()
        if tag in ("h1", "h2", "h3", "h4", "title") and self._grab == "heading":
            self._last_heading = clean
            if self.cur is not None and not self.cur["heading"]:
                self.cur["heading"] = clean
            self._grab = None
            return
        if self.cur is None:
            return
        if tag == "option" and self._grab == "option":
            if self._option is not None and self._select is not None:
                val = self._option.get("value")
                if val is None:
                    val = clean
                if val != "" or clean:
                    self._select["options"].append({"value": val, "label": clean or val})
            self._option = None; self._grab = None
        elif tag == "select":
            self._select = None
        elif tag == "label" and self._grab == "label":
            if clean:
                self.cur["labels"].append(clean)
            self._grab = None
        elif tag == "legend" and self._grab == "legend":
            self.cur["legend"] = clean; self._grab = None
        elif tag == "button" and self._grab == "button":
            self.cur["submit"] = clean or self.cur["submit"]; self._grab = None
        elif tag == "form":
            self._in_form -= 1
            self.forms.append(self.cur); self.cur = None


def _kind(c):
    if c.get("tag") == "select":
        return "enum"
    if c.get("tag") == "textarea":
        return "text"
    t = c.get("type", "text")
    if t in ("number", "range"):
        return "scalar"
    if t == "checkbox":
        return "y_n"
    if t == "radio":
        return "enum"
    return "text"


def _fields(raw):
    fields, radios = [], {}
    for c in raw["controls"]:
        if c.get("tag") == "input" and c.get("type") == "radio":
            radios.setdefault(c.get("name"), []).append(c)
            continue
        k = _kind(c)
        f = {"name": c.get("name"), "kind": k,
             "label": c.get("placeholder") or c.get("aria") or _humanize(c.get("name")),
             "required": bool(c.get("required"))}
        if k == "enum":
            f["options"] = [o["value"] for o in c.get("options", [])]
        fields.append(f)
    for name, group in radios.items():
        fields.append({"name": name, "kind": "enum", "label": _humanize(name),
                       "required": any(g.get("required") for g in group),
                       "options": [g.get("value") for g in group]})
    return fields


def _manifest_form(raw, i, base_url):
    action = raw["attrs"].get("action") or base_url or ""
    action_abs = urllib.parse.urljoin(base_url or "", action) if base_url else action
    dest = urllib.parse.urlparse(action_abs).netloc
    if not dest and base_url:
        dest = urllib.parse.urlparse(base_url).netloc
    fields = _fields(raw)
    submit = raw["submit"]
    title = raw["heading"] or raw["legend"] or submit or ("Form %d" % (i + 1))
    words = " ".join(raw["words"])
    blurb = ((raw["heading"] + ". ") if raw["heading"] else "") + words
    return {
        "index": i,
        "id": raw["attrs"].get("id") or raw["attrs"].get("name") or ("form-%d" % (i + 1)),
        "title": title,
        "context": re.sub(r"\s+", " ", blurb).strip()[:200],
        "action": action_abs,
        "dest": dest,
        "method": (raw["attrs"].get("method") or "get").lower(),
        "field_count": len(fields),
        "fields": fields,
        "submit_label": submit or None,
        "submittable": bool(submit or raw["attrs"].get("action")),
    }


def discover(url, html_text=None):
    """Return a manifest of the page's forms. Pass html_text to skip the network (tests)."""
    text = html_text if html_text is not None else fetch(url)
    p = _FormParser()
    p.feed(text)
    forms = [_manifest_form(f, i, url) for i, f in enumerate(p.forms)]
    submittable = [f for f in forms if f["submittable"]]
    return {"url": url, "count": len(forms),
            "submittable_count": len(submittable), "forms": forms}
