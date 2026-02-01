# CUE-VOX Frontend V2 - Implementation Summary

## Overview

A complete rewrite of the cue-vox frontend using Haberdash design system with support for structured interactive inputs (YES/NO, sliders, etc.).

## What We Built

### 1. Three-Layer CSS Architecture

**Layer 1: Haberdash Base**
- CDN: `https://nickcottrell.github.io/haberdash/haberdash.css`
- Provides base components (cards, buttons, forms, etc.)
- Includes default design tokens

**Layer 2: Theme Overrides** (`/static/css/cue-vox-theme.css`)
- Empty placeholder for VRGB theme customization
- Generated from: https://d103b89icpzdo3.cloudfront.net/color-picker.html
- Allows complete rebranding without touching layout

**Layer 3: Layout/Structure** (`/static/css/style_v2.css`)
- Layout-only CSS using Haberdash tokens
- No hardcoded colors (uses `var(--token, fallback)`)
- Fully themeable

### 2. UI Components

**Drawer Panel**
- 75% width, slides from left
- Blurred translucent background (`backdrop-filter: blur(20px)`)
- Contains conversation feed, status, and text input
- Auto-closes when clicking outside (except when pending input)

**Conversation Feed**
- Left-aligned user messages (80% width)
- Right-aligned assistant messages (80% width)
- Relative timestamps ("just now", "5m ago", etc.) updated every 10s
- Haberdash card components with hidden headers

**State Indicators**
- Main canvas: Large centered dot with colors (gray=idle, red=recording, orange=transcribing, blue=thinking, green=speaking)
- Drawer: Small status dot + text
- Color-coded states across both views

**Input Controls**
- Text input with Send button
- Spacebar hold-to-record voice input
- Both disabled when structured input is pending
- Red notification dot on drawer toggle when input pending

### 3. Structured Input Rendering

**YES/NO Questions**
```
[YES_NO: question text]
```
- Can be embedded anywhere in text (before/after explanatory text)
- Renders two buttons (primary/secondary)
- After selection: buttons disabled, "Selected: [choice]" shown
- Blocks all other input until answered

**Slider Inputs (Semantic)**
```
[INPUT: {"type": "slider", "question": "...", "scale": {...}, "semantic_label": "..."}]
```
- 0-100% range slider with semantic labels
- Shows percentage as "Selected: X%" after submission
- Can be embedded in text with explanations before/after

**Parser Features**
- Detects tags anywhere in message text
- Handles multi-line JSON
- Graceful error handling (shows in red if parse fails)
- Comprehensive console logging for debugging

### 4. Input Blocking System

When structured input is pending:
- ✅ Text input disabled
- ✅ Send button disabled
- ✅ Spacebar recording blocked
- ✅ Red pulsing dot on drawer toggle
- ✅ Console warning logged

After answering:
- ✅ All inputs re-enable automatically
- ✅ Notification cleared

### 5. Stop Audio Controls

- Button below main dot (only visible when speaking)
- Link to right of drawer status (only visible when speaking)
- Both emit `socket.emit('interrupt')` to stop TTS
- `e.stopPropagation()` prevents drawer from closing on click

## Files Modified/Created

### Templates
- `/templates/index_v2.html` - Main v2 UI template

### CSS
- `/static/css/cue-vox-theme.css` - Theme overrides (empty placeholder)
- `/static/css/style_v2.css` - Layout and structure

### JavaScript
- `/static/js/app_v2.js` - Full rewrite with:
  - Socket.IO integration
  - Audio recording (MediaRecorder API)
  - Message rendering with structured input parsing
  - State management
  - Input blocking logic
  - Timestamp updates

### Documentation
- `/static/INPUT_FORMATS.md` - Format specifications
- `/static/FRONTEND_V2_SUMMARY.md` - This file

## Backend Integration

### Routes
- `GET /v2` - Serves the v2 UI

### Socket.IO Events (received by frontend)
- `state_change` - Update UI state (idle/recording/transcribing/thinking/speaking)
- `transcription` - User speech transcribed
- `response` - Assistant text response
- `error` - Error message

### Socket.IO Events (sent by frontend)
- `audio_data` - Audio blob from recording
- `text_message` - User text input
- `interrupt` - Stop current audio playback

## Browser Compatibility

Tested features:
- ✅ ES6 modules (arrow functions, const/let, template strings)
- ✅ CSS custom properties (CSS variables)
- ✅ Flexbox layout
- ✅ backdrop-filter (with fallback)
- ✅ MediaRecorder API
- ✅ Regex with [\s\S] for multiline matching

## Debug Console Output

The frontend logs helpful debugging info:

```
📝 Rendering message: Here's a slider for...
✅ Detected INPUT tag at position 23
  → Adding text before: Here's a slider for
  → Parsed INPUT data: {type: "slider", question: "...", ...}
  → Creating slider: How urgent is this?
  → Adding text after: Let me know when done.
```

## Known Limitations

1. **Single pending input** - Only one structured input can block at a time
2. **No undo** - Once submitted, choices cannot be changed
3. **Backend-dependent** - Requires backend to generate proper format tags

## Backend Requirements

The backend must handle structured inputs specially:

### 1. TTS Handling
**Problem:** Backend tries to speak raw tags like `[YES_NO: ...]` which crashes TTS.

**Solution:** Before sending to TTS, check if message contains structured tags:
```python
import re

def should_speak(text):
    # Don't speak if entire message is a structured input
    if re.match(r'^\[YES_NO:', text):
        return False
    if re.match(r'^\[INPUT:', text):
        return False
    return True

def extract_speakable_text(text):
    # Remove INPUT tags but keep surrounding text
    text = re.sub(r'\[INPUT:\s*\{[^}]+\}\]', '', text)
    return text.strip()
```

### 2. Message Echo
- **Text input**: Backend should NOT echo back via `transcription` event (frontend adds immediately)
- **Voice input**: Backend SHOULD echo via `transcription` event (frontend doesn't know the transcribed text)
- **Structured responses**: Backend should NOT echo (frontend adds immediately with choice indicator)

### 3. State Management
Ensure `state_change` events are emitted correctly:
- `idle` - Waiting for input
- `recording` - User is recording
- `transcribing` - Processing audio
- `thinking` - Generating response
- `speaking` - Playing TTS audio

## Next Steps for Maestro Integration

1. Copy frontend files:
   - `index_v2.html`
   - `style_v2.css`
   - `app_v2.js`
   - `cue-vox-theme.css`

2. Update maestro backend to:
   - Generate `[INPUT: {...}]` tags for sliders
   - Generate `[YES_NO: ...]` tags for confirmations
   - Emit proper `state_change` events

3. Test all input types

4. Customize theme via VRGB color picker

## Separation of Concerns

✅ **HTML** - Semantic structure only
✅ **CSS** - Layout/positioning, no business logic
✅ **JS** - Behavior and interaction, backend-agnostic parsing
✅ **Theme** - Separate layer for visual customization

The frontend is fully decoupled from the backend - it simply looks for format tags in message text, regardless of source.
