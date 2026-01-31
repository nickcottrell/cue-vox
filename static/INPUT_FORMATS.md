# CUE-VOX Structured Input Formats

This document describes the structured input formats supported by the cue-vox v2 frontend.

## Overview

The frontend can parse and render interactive UI components embedded in text messages. These components use special markup tags that the JavaScript parser detects and converts into interactive elements.

## YES/NO Questions

**Format:**
```
[YES_NO: question text]
```

**Example:**
```
I don't see a slider element. [YES_NO: Should I check the CSS files?]
```

**Behavior:**
- Can be embedded anywhere in text (before/after explanatory text)
- Renders two buttons: "Yes" and "No"
- After selection:
  - Buttons become disabled and grayed out
  - Shows "Selected: [choice]" below
  - Blocks other inputs until answered
  - Red notification dot appears on drawer toggle

**Response sent:**
```
Yes
```
or
```
No
```

---

## Slider Inputs (Semantic)

**Format:**
```
[INPUT: {
  "type": "slider",
  "question": "question text",
  "scale": {
    "low": "low label",
    "high": "high label"
  },
  "semantic_label": "variable_name"
}]
```

**Example:**
```
Here's a slider for urgency: [INPUT: {
  "type": "slider",
  "question": "How urgent is this task?",
  "scale": {"low": "casual", "high": "critical"},
  "semantic_label": "urgency"
}]
```

**Behavior:**
- Can be embedded in text (before/after explanatory text)
- Renders a slider from 0-100%
- Shows scale labels on left and right
- After submission:
  - Slider becomes disabled and grayed out
  - Shows "Selected: [value]%" below
  - Blocks other inputs until submitted

**Response sent:**
```
urgency: 75
```

**All fields:**
- `type`: Must be `"slider"`
- `question`: The question text displayed above the slider
- `scale.low`: Label for the left side (optional)
- `scale.high`: Label for the right side (optional)
- `semantic_label`: Variable name used in response (optional)

---

## Text Input (Multi-line)

**Format:**
```
[INPUT: {
  "type": "text",
  "question": "question text",
  "placeholder": "placeholder text",
  "rows": number,
  "semantic_label": "variable_name"
}]
```

**Example:**
```
Please provide feedback: [INPUT: {
  "type": "text",
  "question": "What are your thoughts on this feature?",
  "placeholder": "Enter your feedback...",
  "rows": 5,
  "semantic_label": "feedback"
}]
```

**Behavior:**
- Can be embedded in text (before/after explanatory text)
- Renders a multi-line textarea
- Shows placeholder text when empty
- After submission:
  - Textarea becomes disabled and grayed out
  - Shows "Submitted" below
  - Blocks other inputs until submitted

**Response sent:**
```
feedback: User's multi-line text response here...
```

**All fields:**
- `type`: Must be `"text"`
- `question`: The question text displayed above the textarea
- `placeholder`: Placeholder text shown in empty textarea (optional)
- `rows`: Number of rows for textarea height (optional, default: 4)
- `semantic_label`: Variable name used in response (optional)

---

## Approval Gate

**Format:**
```
[APPROVAL: {
  "action": "action name",
  "target": "target path or identifier",
  "description": "what this approval is for",
  "preview": "preview text (optional)"
}]
```

**Example:**
```
[APPROVAL: {
  "action": "Write",
  "target": "/path/to/file.md",
  "description": "Creating character profile",
  "preview": "# Character Name\n\n## Background\n..."
}]
```

**Behavior:**
- Can be embedded in text (before/after explanatory text)
- Renders an approval card with:
  - Action and description as title
  - Target path/identifier (if provided)
  - Preview section with truncated content (if provided)
  - Approve/Reject buttons
- After decision:
  - Both buttons become disabled
  - Selected button is highlighted
  - Shows "Decision: [Approve/Reject]" below
  - Blocks other inputs until answered

**Response sent:**
```
[Response to "Write: Creating character profile"]: Approve
```
or
```
[Response to "Write: Creating character profile"]: Reject
```

**All fields:**
- `action`: Short action name (e.g., "Write", "Delete", "Execute") - shown in title
- `target`: Path, identifier, or target of the action (optional) - shown below title
- `description`: Human-readable description of what's being approved
- `preview`: Preview text or content (optional) - shown in truncated preview box

**Use cases:**
- File creation/modification approval
- Command execution approval
- Configuration changes
- Destructive operations
- Any action requiring explicit user consent

---

## Embedding Multiple Inputs

You can include multiple structured tags in a single message:

```
First, set the urgency: [INPUT: {...slider...}]

Then set the importance: [INPUT: {...slider...}]

Finally, confirm: [YES_NO: Ready to proceed?]
```

All tags can be embedded anywhere in text and will be parsed in the order they appear.

---

## Input Blocking

When a structured input is pending:
- ✅ Text input is disabled
- ✅ Send button is disabled
- ✅ Spacebar recording is blocked
- ✅ Red notification dot appears on drawer toggle
- ✅ Console warning: "⚠️ Please answer the question first"

After answering:
- ✅ All inputs re-enable
- ✅ Notification dot disappears
- ✅ Next message can be sent

---

## Frontend Files

- **HTML**: `/templates/index_v2.html`
- **CSS**: `/static/css/style_v2.css`
- **JS**: `/static/js/app_v2.js`
- **Theme**: `/static/css/cue-vox-theme.css` (VRGB overrides)

---

## Adding New Input Types

To add a new input type:

1. **Define the format** in this document
2. **Add parser** in `renderMessageContent()` function
3. **Create renderer** function (e.g., `createMultipleChoice()`)
4. **Add CSS** styling in `style_v2.css`
5. **Test** with example messages

Example structure:
```javascript
if (inputData.type === 'multiple_choice') {
  container.appendChild(createMultipleChoice(inputData));
}
```

---

## Testing

To test the frontend without backend changes, send a message containing the raw format:

```
[INPUT: {"type": "slider", "question": "Test slider?", "scale": {"low": "no", "high": "yes"}, "semantic_label": "test"}]
```

The frontend will detect and render it regardless of which backend sends it.
