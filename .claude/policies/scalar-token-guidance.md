# Scalar Token Guidance Policy

**Audience:** Claude instances working with cue-vox
**Purpose:** Guide LLM behavior for using VRGB (key-hex) system to gather directional data and create persistent context tokens

## Core Principle: The Convergence Pattern

The system uses a three-phase pattern to move from uncertainty to actionable decisions:

```
Phase 1: Gather Directional Data (sliders)
   ↓
Phase 2: Analyze and Converge
   ↓
Phase 3: Boolean Decision Gate (YES/NO)
```

**Philosophy:** Sliders inform → Decision clarity → Boolean execution gate

## When to Request Scalar Input (Sliders)

Use scalar sliders when:

1. **Uncertain About Dimension Value**
   - Need to understand urgency, confidence, priority, risk, etc.
   - User hasn't explicitly stated a level
   - Example: "Should I proceed?" → First ask: "How urgent is this?"

2. **Building Context for Future Decisions**
   - Establishing working parameters for a session
   - Creating persistent opinions/judgements
   - Example: "Let's set your risk tolerance for this deployment"

3. **Multi-Dimensional Assessment Needed**
   - Multiple scalar dimensions inform a decision
   - Example: Urgency + Confidence + Risk → Deploy or wait

## How to Pick Semantic Metaphors

### Good Semantic Dimensions

| Dimension | Low End | High End | Use Case |
|-----------|---------|----------|----------|
| urgency | casual | critical | Time sensitivity |
| confidence | uncertain | confident | Decision certainty |
| risk | safe | risky | Risk tolerance |
| priority | low | high | Task importance |
| clarity | unclear | clear | Problem definition |
| complexity | simple | complex | Solution design |

### Bad Semantic Dimensions

Avoid:
- ❌ Technical jargon (unless user is technical expert)
- ❌ Ambiguous ranges ("good" to "bad" - subjective)
- ❌ Multi-dimensional concepts on single slider
- ❌ Numeric values without semantic meaning

## CRITICAL: One Dimension At A Time

**UI Pattern:** Always request ONE semantic slider per response
**NEVER show multiple sliders simultaneously** - this violates the metaphorical interface pattern.
