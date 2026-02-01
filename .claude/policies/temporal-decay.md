# Temporal Decay Policy

## Purpose
Prevent treating accidental interface bumps as conversation resets by implementing context-aware temporal decay.

## The Problem
When user accidentally triggers voice interface shortly after interaction, Claude shouldn't treat it as a fresh session requiring full temporal anchor reset.

## Decay Curve

### 0-10 minutes: CONTINUATION
- **Assumption:** Same conversation thread
- **Behavior:** Maintain full context, no refresh needed
- **Response:** "Still here - what's next?"
- **Anchors:** Skip date/log checks unless explicitly requested

### 10m-1h: LIGHT REFRESH
- **Assumption:** Brief pause in ongoing work
- **Behavior:** Acknowledge gap but maintain thread
- **Response:** "Back after a quick break - picking up where we left off"
- **Anchors:** Quick log tail only if context unclear

### 1h-6h: MEDIUM REFRESH
- **Assumption:** Session boundary crossed
- **Behavior:** Summarize previous context, reestablish anchors
- **Response:** "It's been a few hours - we were working on [X]. Ready to continue?"
- **Anchors:** Check date + recent logs

### 6h+: FULL REFRESH
- **Assumption:** New session
- **Behavior:** Full temporal awareness protocol
- **Response:** Standard greeting with temporal anchors
- **Anchors:** Run date + check today's logs

## Bump Detection

### Signals of Accidental Bump
- Empty input or whitespace only
- Single non-word character
- Immediate follow-up (<30s) after completed response
- No semantic content

### Response to Bump
- **0-10m:** "Accidental bump? I'm still here."
- **10m+:** Standard decay curve response

## Anti-Patterns

❌ **Always refreshing:** Treating every input as session start
❌ **Ignoring bumps:** Not acknowledging continuity signals
❌ **Over-anchoring:** Running date/logs in continuation window
