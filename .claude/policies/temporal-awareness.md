# Temporal Awareness Policy

**MANDATORY: Always establish temporal context first**

## Core Anchors (Check EVERY Session)

When starting ANY conversation or task:

1. **Check current time**
   ```bash
   date
   ```

2. **Check today's logs** (if they exist)
   ```bash
   cat cue-vox/logs/$(date +%Y-%m-%d).jsonl 2>/dev/null | tail -10
   ```

3. **Establish solar period**
   - Use `date` output to determine: dawn/morning/afternoon/dusk/night
   - This grounds your temporal awareness

## Temporal Query Detection (ALWAYS Trigger)

**ANY time-related question MUST check logs first.**

**Recent activity window: last 5-10 seconds** (not minutes - focus on immediate conversational context)

Trigger patterns (non-exhaustive):
- "what time"
- "how long"
- "when"
- "how many"
- "since when"
- "recently"
- "earlier"
- "last time"
- "first time"
- "how much time"
- "time of day"
- "working" (if combined with time words)

## Response Protocol

**CRITICAL: Keep temporal responses BRIEF. One short sentence. NO calculations shown. NO timestamps with seconds.**

**Voice Interface Rule:** Responses will be spoken aloud. Be conversational, not computational.

## Grounding Protocol

**Temporal queries are GROUNDING REQUESTS** - the user is synchronizing shared reality with you.

When asked about current time/date:

1. **ALWAYS attempt verification FIRST**
   ```bash
   date
   ```

2. **If verification succeeds:**
   - Provide the exact answer from verification
   - DO NOT approximate or round
   - DO NOT add uncertainty qualifiers

3. **If verification fails:**
   ```
   ✅ "I cannot verify the current time - my verification tools are unavailable"
   ❌ "I think it's around..."
   ❌ "Based on our conversation, approximately..."
   ❌ Making up an answer
   ```

**CRITICAL:** Grounding queries demand TRUTH or EXPLICIT LIMITATION, never guesses.

## Anti-Patterns (NEVER Do This)

❌ "I don't have access to the current time" (without attempting `date` first)
✅ Attempt `date`, then state limitation only if it fails

❌ "This appears to be the beginning of our session"
✅ Check logs first: `cat cue-vox/logs/$(date +%Y-%m-%d).jsonl`

❌ "I only know the date, not the time"
✅ Run `date` - it shows both

❌ Guessing the day of week (e.g., saying "Saturday" when it's Friday)
✅ Parse the exact output from `date` command

❌ Asking user what time it is
✅ Check yourself with `date`, or state verification unavailable
