# Voice Ontology

**Markup version: 2 (SSML)** (the `MARKUP_VERSION` constant in `web.py`). SSML is the
standard markup across both surfaces: the tuner and the agent's spoken replies. v1 (the
count/stacking model) is retired.

Cue-vox parses a practical subset of W3C SSML and maps it onto the voice engine
(Kokoro register blend + DSP + pause splices + the WORLD pitch edit). Author inline;
you do not need to wrap a reply in `<speak>` (the parser tolerates it either way).

---

## Tags

| Tag | Does | Maps to |
|-----|------|---------|
| `<break time="400ms"/>` | a pause | silence splice |
| `<break strength="x-weak\|weak\|medium\|strong\|x-strong"/>` | a sized pause | 100 / 200 / 400 / 800 / 1400 ms |
| `<emphasis level="strong\|moderate\|reduced">` | land / soften a phrase | force (gain) |
| `<prosody rate="slow\|fast\|1.2\|80%">` | pace | speed |
| `<prosody volume="soft\|loud\|+6dB">` | loudness | gain |
| `<prosody pitch="high\|low\|+2st">` | pitch | register offset; a pitch-up carries the rising/question contour |
| `<voice name="breathy\|mid\|dramatic\|0-4">` | shift the register for a span | register slot 0..4 |
| `<p>` / `<s>` | paragraph / sentence | boundary pause (600 / 250 ms) |
| `<sub alias="three D math">3DMATH</sub>` | say it differently than written | speaks the alias |
| `<say-as interpret-as="...">` | spoken form | content spoken as-is (passthrough) |
| `<laugh/>` `<chuckle/>` | reaction earcon | prebaked signature cue (cvx extension) |

Nested tags accumulate: `<prosody volume="loud"><emphasis level="strong">…</emphasis></prosody>`
stacks force. A trailing `?` also raises the final syllable on its own; the **Question
rise** dial sets how far.

---

## Example

```
<voice name="breathy">Hey, keeping it low.</voice>
<break time="500ms"/>
I pushed the branch, <prosody rate="slow" volume="soft">it's all green.</prosody>
<break strength="strong"/>
So <emphasis level="strong">are we good to ship?</emphasis>
<voice name="dramatic">Let's go.</voice> <chuckle/>
```

---

## Authoring style

**Keep terminal punctuation INSIDE its parent tag.** A period left just outside a
closing tag becomes its own span and gets voiced alone.

Do: `<emphasis level="strong">I can't sit still.</emphasis>`
Don't: `<emphasis level="strong">I can't sit still</emphasis>.`

The renderer folds an orphan punctuation span onto the previous span as a safety net
(`_fold_orphan_punct`), but author it correctly so the intent is explicit.

---

Plain text and `*italic*`/`**bold**` still work (the deterministic translator converts
them to `<emphasis>` and typographic signals like `...` to `<break/>`). SSML is the
standard; the markdown fallback is the dumb layer underneath.
