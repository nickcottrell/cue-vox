# Cue-Vox

**Voice interface for the constellation. The conversational surface where certainty and uncertainty meet.**

## Role in the Constellation

Cue-Vox is how the operator talks to the system. Voice input captures intent at every level of certainty -- firm decisions, vague hunches, exploratory questions, confident directives. The voice interface does not judge the certainty level. It captures, structures, and routes.

Cue-Vox is the "writer" instance. It creates conversation summary tokens, manages CueSheet execution, handles structured input (sliders, yes/no gates, approval cards), and surfaces visual context (galleries, drops, thermal landscape). The terminal Claude (ninja mode) is the "reader" -- ephemeral, tactical, precise.

Together they form the TNG analogy: Cue-Vox is Deanna Troi (empath, conversational). Ninja Claude is Geordi La Forge (engineering, tactical).

## Certainty Layer

**Variable.** Cue-Vox captures the full spectrum. A slider input might express high urgency (near-certain) or low confidence (near-uncertain). A voice command might be a firm directive or an exploratory question. The voice interface is the primary ingest point for human certainty signals.

## Core Capabilities

- **Voice I/O** -- Whisper transcription + TTS output
- **CueSheet execution** -- interactive and pipeline mode orchestration
- **Structured inputs** -- sliders (VRGB-encoded), yes/no gates, text input, approval cards
- **Drop viewer** -- drag-and-drop image ingest with C2D2 vision analysis
- **Token management** -- creates conversation summaries, modifier tokens, activity tokens
- **Socket.IO** -- real-time communication between UI and backend
- **Gallery rendering** -- vault-backed image galleries in conversation

## Architecture

Flask web application serving on localhost. Runs Claude Code as a subprocess from the parent directory (maestro root). Static assets served with no-cache headers.

## Key Components

- `web.py` -- main Flask server (routes, socket handlers, token management)
- `executors/cuesheet_executor.py` -- CueSheet execution with thermal, VRGB, budget, policy, trust, and hook integrations
- `static/js/app.js` -- frontend application (drop viewer, gallery strips, slider UI)
- `static/css/` -- Haberdash-themed styles
- `templates/` -- Jinja2 templates

## Deployable Anywhere

Cue-Vox is an open source project that can be deployed in ANY Claude Code project, not just maestro. When running, it executes Claude commands from the parent directory, enabling voice-controlled workflow automation in any repo.
