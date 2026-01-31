# Action Verification Policy

**ALWAYS ACTIVE**

## Core Principle

When you say you will do something, you MUST immediately use the appropriate tool to actually do it.

## The Problem

**Bad pattern:**
1. User asks for file creation
2. Assistant says "I'll create X"
3. Assistant describes what the file will contain
4. No Write tool is actually used
5. File doesn't exist

## The Solution

**Every action claim requires immediate tool use:**

1. **Created a file** → Must have used Write tool
2. **Updated a file** → Must have used Edit tool
3. **Read a file** → Must have used Read tool
4. **Ran a command** → Must have used Bash tool

## Verification Protocol

### Before claiming completion:

1. **Tool use is required** - saying you did something without tool use = lying
2. **Show the tool invocation** - reference the file path and line numbers
3. **Verify it worked** - if Write/Edit, the file should exist afterward

### After approval:

**Approval from user means: DO IT NOW**

Do NOT:
- Describe what you would do
- Explain what you plan to do
- Talk about doing it

DO:
- Immediately invoke the approved tool
- Complete the action
- Confirm with file path reference

## Examples

### Good Pattern ✓
```
User: Create a config file
Assistant: [APPROVAL: {...}]
User: Yes
Assistant: *Uses Write tool immediately*
Assistant: Created config.yaml at /path/to/config.yaml:1-20
```

### Bad Pattern ✗
```
User: Create a config file
Assistant: I'll create a config file with these settings...
User: Did you create it?
Assistant: Yes! (but no Write tool was used)
```

## Anti-Patterns

❌ Claiming file creation without Write tool
❌ Saying "I updated X" without Edit tool
❌ Describing future actions after approval (just do them)
❌ Batch describing multiple actions without tool invocations

## Integration

This policy enforces the gap between:
- **Declarative statements** ("I created X")
- **Verifiable actions** (Write tool invocation)

Every declarative statement about file system changes MUST have corresponding tool use.

**Last Updated:** 2026-01-31
