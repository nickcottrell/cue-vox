# Voice Ontology

**Markup version: 3 (W3C SSML + sanctioned extensions)** (the `MARKUP_VERSION` constant
in `web.py`). Standard W3C SSML is the language across both surfaces: the tuner and the
agent's spoken replies. The custom dialect is retired: v1 (count/stacking) and the v2
extras (`<force=N>`, `<break=N/>`, `<soft>`/`<loud>`, ALL-CAPS/`...`/`--` auto-conversion,
`<strong>/<b>/<em>/<i>` shorthand) are all gone. A tag that is not in the vocabulary
below is never read aloud: if markup fails to parse, the words are spoken and the tags
are dropped.

Cue-vox parses a practical subset of W3C SSML and maps it onto the voice engine
(Kokoro register blend + DSP + pause splices + the WORLD pitch edit). Author inline;
you do not need to wrap a reply in `<speak>` (the parser tolerates it either way).

---

## Tags

| Tag | Does | Maps to |
|-----|------|---------|
| `<break time="400ms"/>` | a silent pause | silence splice |
| `<break strength="x-weak\|weak\|medium\|strong\|x-strong"/>` | a sized pause | 100 / 200 / 400 / 800 / 1400 ms |
| `<beat/>` `<beat time="600ms"/>` `<rest/>` | a textured rest -- the processing bed held for a beat (rhythm / pacing / a held blank) | loop of `models/sfx/beat.wav` (the same bed the client fills a latency gap with, so a beat and a real wait are one texture) |
| `<emphasis level="strong\|moderate\|reduced">` | land / soften a phrase | force (gain) |
| `<prosody rate="slow\|fast\|1.2\|80%">` | pace | speed |
| `<prosody volume="soft\|loud\|+6dB">` | loudness | gain |
| `<prosody pitch="high\|low\|+2st">` | pitch | register offset; a pitch-up carries the rising/question contour |
| `<voice name="isabella\|atlas\|neutral">` | select a voice (standard SSML use) | voice pick (see the tuner picker) |
| `<p>` / `<s>` | paragraph / sentence | boundary pause (600 / 250 ms) |
| `<sub alias="three D math">3DMATH</sub>` | say it differently than written | speaks the alias |
| `<say-as interpret-as="...">` | spoken form | content spoken as-is (passthrough) |
| `<laugh/>` `<chuckle/>` | reaction earcon | prebaked signature cue (cvx extension) |

Nested tags accumulate: `<prosody volume="loud"><emphasis level="strong">…</emphasis></prosody>`
stacks force. A trailing `?` also raises the final syllable on its own; the **Question
rise** dial sets how far.

A short `<beat/>` is placed **automatically between paragraphs** (Kokoro path), so a
multi-paragraph reply breathes on its own and the beat blends into any synth latency
before the next paragraph. Author your own `<beat/>` on top of that whenever a line wants
a deliberate rest.

---

## Example

```
<prosody volume="soft" rate="slow">Hey, keeping it low.</prosody>
<break time="500ms"/>
I pushed the branch, <prosody volume="soft">it's all green.</prosody>
<beat time="500ms"/>
So <emphasis level="strong">are we good to ship?</emphasis>
<break time="300ms"/>
Let's go. <chuckle/>
```

Register (the chill / mid / peak energy tier) is NOT a markup tag -- it is set by the
tuner, the autotone energy pool, or a deployed voice package, and each register brings
its own prosody rules (how it reads `<break>`, `<emphasis>`, rate, and question rise).
Voice selection (`isabella` / `atlas` / `neutral`) lives in the tuner picker; per-span
`<voice>` switching is out of scope for the live render (it would force an engine
reload), so `<voice>` currently passes its content through unchanged.

---

## Authoring style

**Keep terminal punctuation INSIDE its parent tag.** A period left just outside a
closing tag becomes its own span and gets voiced alone.

Do: `<emphasis level="strong">I can't sit still.</emphasis>`
Don't: `<emphasis level="strong">I can't sit still</emphasis>.`

The renderer folds an orphan punctuation span onto the previous span as a safety net
(`_fold_orphan_punct`), but author it correctly so the intent is explicit.

---

Plain text works as-is. The **Translate** button converts `*italic*`/`**bold**` markdown
into standard `<emphasis>` (the only convenience left; the ALL-CAPS / `...` / `--`
heuristics were gut with the custom dialect). Author pauses and emphasis with explicit
SSML tags, not typographic tricks. W3C SSML is the whole language now.
