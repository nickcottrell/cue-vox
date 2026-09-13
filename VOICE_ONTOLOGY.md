# Voice Ontology

**Markup version: 1** (the `MARKUP_VERSION` constant in `web.py`). Bump it whenever a
tag's semantics change so packages record which language their text was authored
against. History: v1 = the count/stacking model below.

Semantic markup for spoken delivery. One idea runs through all of it:

> **One atomic tag plus a count. Stacking is counting.**

`<break/><break/><break/>` and `<break=3/>` mean the same thing. `<em><em>` and
`<em=2>` mean the same thing. There is no t-shirt sizing to memorize: a bare tag is
one step, and you get more by stacking the tag or writing a count.

Everything maps deterministically to the render engine (Kokoro register blend + DSP
+ pause splices). No hidden model magic.

---

## Pauses

| Form                         | Means                          |
|------------------------------|--------------------------------|
| `<break/>`                   | one beat (250ms)               |
| `<break/><break/><break/>`   | three beats                    |
| `<break=3/>`                 | three beats (same as above)    |
| `<break=2.5/>`               | fractional counts are fine     |

The **Break length** dial in `/tune` multiplies every beat, so one preset can run
airy and another tight without touching the text.

---

## Intensity

Each tag is one step; stack it or add `=n` for more. Steps compound, so
`<em><em>` and `<em=2>` land identically.

| Tag           | One step does            | Example        |
|---------------|--------------------------|----------------|
| `<em>`        | a light lift             | `<em=2>really</em>` |
| `<strong>`    | a heavier lift           | `<strong=2>no key, no cloud.</strong>` |
| `<force>`     | louder                   | `<force=2>out loud.</force>` |
| `<soft>`      | quieter (steps down)     | `<soft=2>barely there.</soft>` |

---

## Composites (semantic, everyday vocabulary)

Bundles that set several things at once. These stay word-shaped, not counted.

| Tag             | Means                        |
|-----------------|------------------------------|
| `<whisper>`     | hushed, close                |
| `<aside>`       | intimate set-aside           |
| `<declare>`     | full, theatrical             |
| `<ask>`         | question contour (pitch climbs on the final syllable) |
| `<laugh/>` `<chuckle/>` | reaction earcon (register-keyed) |

A trailing `?` also triggers the question rise. The **Question rise** dial sets how
far it climbs.

---

## Precise escape

When you need an exact value, `<prosody register= rate= gain=>` still takes raw
numbers. Reach for it rarely; counts cover almost everything.

---

## Example

```
<whisper>Hey, keeping it low.</whisper>
<break=2/>
I pushed the branch, <aside>it's all green.</aside>
<break=3/>
So <strong=2>are we good to ship?</strong>
<declare>Let's go.</declare> <chuckle/>
```

---

## Authoring style

**Keep terminal punctuation INSIDE its parent tag.** A period left just outside a
closing tag becomes its own span and gets voiced alone, which mangles the delivery.

Do:

    <aside>it is all green.</aside>
    <strong=2>I can't sit still.</strong>

Don't:

    <aside>it is all green</aside>.
    <strong=2>I can't sit still</strong>.

The renderer folds an orphan punctuation span onto the previous span as a safety net
(see `_fold_orphan_punct`), but author it correctly so the intent is explicit.

---

Plain text, `*italic*`/`**bold**`, and `[pause]` remain as low-effort fallbacks, and
legacy `size='xs..xl'` still parses so old scripts keep working. Counts are the
standard; the rest is the dumb layer underneath.
