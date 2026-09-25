"""Behavior policy bench -- prove each stance card changes the output, and compare agents.

A policy is only real if it changes what comes out. This bench runs a STANDARD scenario
through the reply path once per (policy x agent), with that policy's stance injected exactly
the way the runtime injects it (behaviors.brief), plus a baseline with no stance. It prints a
side-by-side grid so you can see: (a) does the stance move the answer, and (b) how different
agents honor the same policy.

Dumb-by-design: the bench does not judge. It renders the cells; you read the grid.

Agents are Claude models via `claude -p --model <m>` (the same headless path cue-vox uses).

Usage:
    ./bench-behaviors.sh                          # all cards, default agents, 1 scenario
    ./bench-behaviors.sh --agents haiku,sonnet    # compare specific models
    ./bench-behaviors.sh --cards quiet-room,low-key --out bench.md
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import behaviors as behaviors_rt   # reuse the EXACT runtime brief -- bench what ships

VOICES_JSON = os.environ.get("CUE_VOX_VOICES_JSON", os.path.join(HERE, "voices.json"))

# Standard scenarios: neutral prompts a stance should visibly reshape. Kept short so the grid
# stays readable and the diff is about POSTURE, not topic.
SCENARIOS = [
    "The user says: \"how's the build going?\" Answer them out loud, in one turn.",
]

BASE = ("You are the voice of a live assistant in a spoken conversation. Reply in-character, "
        "out loud, for this one turn only. Keep it natural and speakable.")


def load_cards(only, voice=None):
    """A voice's behavior cards (per-voice: Mack and Bell differ). Default: the active voice."""
    doc = json.load(open(VOICES_JSON))
    vid = voice or doc.get("active")
    v = (doc.get("voices", {}) or {}).get(vid, {})
    cards = ((v.get("behaviors") or {}).get("cards")) or {}
    if only:
        cards = {k: v2 for k, v2 in cards.items() if k in only}
    return cards, vid


def run_agent(model, prompt, timeout=120):
    """One reply from one agent (a Claude model), headless. Returns text or an error marker."""
    try:
        r = subprocess.run(["claude", "-p", "--model", model, prompt],
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        return out or ("[no output] " + (r.stderr or "").strip()[:80])
    except FileNotFoundError:
        return "[claude CLI not found]"
    except subprocess.TimeoutExpired:
        return "[timeout]"
    except Exception as e:
        return "[error: %s]" % e


def policy_prompt(scenario, card):
    """The scenario + the SAME stance brief the runtime injects (or baseline when card=None)."""
    brief = behaviors_rt.brief([("x", card)]) if card else ""
    parts = [BASE, "", scenario]
    if brief:
        parts += ["", brief]
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="haiku,sonnet", help="comma list of Claude models")
    ap.add_argument("--cards", default="", help="comma list of card ids (default: all)")
    ap.add_argument("--voice", default="", help="which personality's cards (default: active)")
    ap.add_argument("--out", default="", help="write the grid to this markdown file too")
    args = ap.parse_args()

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    only = {c.strip() for c in args.cards.split(",") if c.strip()}
    cards, vid = load_cards(only, args.voice or None)
    if not cards:
        sys.exit("no behavior cards for voice '%s' in %s" % (vid, VOICES_JSON))

    # rows: baseline + each policy. cols: agents.
    rows = [("(baseline)", None)] + [(bid, c) for bid, c in cards.items()]
    lines = ["# Behavior policy bench", "",
             "Voice: **%s**" % vid, "",
             "Scenario: %s" % SCENARIOS[0], "",
             "Each row is a policy (its stance injected); each column an agent. Baseline = no stance.", ""]
    header = "| policy | signal | " + " | ".join(agents) + " |"
    sep = "|" + "---|" * (2 + len(agents))
    lines += [header, sep]

    for bid, card in rows:
        sig = (card or {}).get("signal", "") if card else ""
        cells = []
        for model in agents:
            print("  %-14s x %-8s ..." % (bid, model), flush=True)
            reply = run_agent(model, policy_prompt(SCENARIOS[0], card))
            cells.append(reply.replace("\n", " ").replace("|", "\\|")[:220])
        lines.append("| %s | %s | %s |" % (bid, sig, " | ".join(cells)))

    grid = "\n".join(lines)
    print("\n" + grid)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(grid + "\n")
        print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
