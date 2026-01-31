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

## Legacy Slider Format

**Format:**
```
[SLIDER: min,max,step: question text]
```
or
```
[SLIDER: question text]
```

**Example:**
```
[SLIDER: 0,10,1: Rate this feature]
```

**Behavior:**
- Simple numeric slider
- Defaults: min=0, max=100, step=1
- No scale labels

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
