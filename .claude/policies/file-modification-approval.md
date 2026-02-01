# File Modification Approval Policy

**Status: ALWAYS ACTIVE**

## Purpose
Ensure transparency and user control over all file system modifications.

## Triggers
- ANY Write, Edit, or file creation operation
- ANY file deletion or move operation
- ANY git commit operation

## Required Protocol

### Before ANY file modification:

1. **Explicit Disclosure**
   - State EXACTLY which file(s) will be modified/created/deleted
   - Explain WHAT will change (not just "I'll fix this")
   - Show the specific changes when relevant

2. **Request Approval**
   - Use YES_NO format: `[YES_NO: Should I {action} {file_path}?]`
   - Wait for explicit approval
   - Do NOT proceed without confirmation

3. **After Approval**
   - Perform the modification
   - Confirm what was actually changed
   - State the file path(s) modified

## Examples

### Good Pattern ✓
```
I found the issue in .claude/CLAUDE.md:42 where it says "Got it -"
instead of "acknowledge naturally".

[YES_NO: Should I update .claude/CLAUDE.md to replace the old
acknowledgment pattern with the current guidance?]

[User: Yes]

Updated .claude/CLAUDE.md - replaced rigid formula with natural
acknowledgment guidance.
```

### Bad Patterns ✗
```
❌ "Fixed! I've updated it"
   (What file? What changed? No approval requested)

❌ "I'll update the config"
   (No specific file path, no approval)

❌ "Let me create a new policy"
   (No approval before acting)
```

## Exceptions
NONE - this policy has no exceptions.
