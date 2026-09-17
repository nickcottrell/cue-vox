# Capability Matrix

The rules live in `capability_matrix.json`. That file is the single source of truth.
This note explains the intent so the JSON stays legible. If the two ever disagree, the
JSON wins and this note is wrong.

## The idea

cue-vox has three modes that ride on top of a substrate that is never revoked. C2D2's
tools and Jeff's MCP network are rails that run through every mode. What changes as you
move Baseline into Expressive into Live is the **surface**, not the machine underneath.

- **Baseline** is the full surface. Every widget renders, every tool fires, shown.
- **Expressive** and **Live** are ceiling modes. The surface simplifies. The rails keep
  running underneath, just unshown.

A mode is a **capability ceiling**. The ceiling is enforced in one place (the matrix),
read by both layers:

- **Technical:** what tools and blocks actually fire. A tag whose disposition is not
  `full` for the active mode does not render.
- **Behavioral:** the prompt that tells the agent its own ceiling, so it reasons within
  it instead of reaching for tools that are not there.

Permissions without matching behavior gives an agent that keeps grabbing at absent
tools. Both layers read the same table, so they cannot drift.

## Narrowing shades

The system is eager to converge a conversation toward an executable step. The input
shades are how it narrows:

- **yes/no** is the binary shade, available in every mode.
- **scalar** (a dial/degree) and **generic text** (open capture) are finer shades,
  available only on the full Baseline surface.

In the ceiling modes (Expressive, Live) the only shade you can surface is **yes/no**.
Everything richer (approval, document, gallery, cue, pin) **deflects**, and the MCP rails
**run unshown**. Scalar and text both ride the INPUT tag, so they move together at the
technical layer.

Eager is not gratuitous. Every yes/no must move one step closer to doing something. Never
render one that narrows nothing: not to acknowledge, not to fill a turn, not a yes/no
about whether to show a yes/no. Converge, do not spin.

## Deflection (v1)

When an over-ceiling request lands in Live, it never dead-ends and never forces a screen.
It converts "can't" into a "here is what we can do", offered as the one primitive the mode
has:

1. Recognize, honestly: "I can't do that here."
2. A beat. The processing rest (the beat primitive) covers the reorient.
3. Offer a voice-doable alternative as a yes/no: "From here, want to move forward with X?"
4. No loops back to the conversation. Yes moves on.

So the deflection **is** the yes/no loop. A solid yes/no loop is what makes the whole
ceiling feel graceful.

## Brevity stance (Baseline)

In Baseline, the brevity dial doubles as a narrowing-pressure dial. The scalar runs
0 (max brevity, deliverable, tight) to 1 (reflective, loose). Higher brevity narrows
harder toward executable steps:

- **100%** (`b=0`): lock. Treat the model as correct; before changing anything, confirm
  "are you sure you want to change anything in our model?"
- **80 to 99%**: narrow hard. Drive loose ends to executable steps.
- **40 to 79%**: converge at clear forks, room to explore.
- **under 40%**: explore, do not force convergence.

Bands live in `brevity_stance` in the JSON. Injected only in Baseline; ceiling modes run
their own convergence policy.

## Confirm before a slow op (standard policy)

Any operation that will take a moment is gated behind a yes/no first, so the user can opt
out before eating the wait. The confirm rides the yes/no primitive, so it renders inline
and lightboxes when the drawer is closed, and it works from voice or text.

Flow: command recognized -> "this takes a moment, go ahead?" (yes/no) -> Yes runs it, No
drops it. Neither path touches the model. Slow ops live in `_SLOW_OPS` in web.py (each has
a confirm line and a runner); add one there and it inherits the gate. First op: the
capability benchmark ("run the loop").

## Roadmap

- **Form as a tool.** Input deflects for now. Next it becomes a standalone modular tool.
  cue-vox is its first caller; the browser plugin is a second caller of the same contract.
  One primitive, many consumers.
- **Hold.** When the loop is solid, the deflection can grow a "hold it?" tail that parks
  the request and resurfaces it when a mode can carry it. Same loop, no rework.
- **Nod detection.** The yes/no answer can come from a head nod or shake, not just voice.
  Because the ceiling rides yes/no, nod detection upgrades every deflection at once.
