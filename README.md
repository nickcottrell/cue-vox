# cue-vox

**Give Claude a voice.**

---

## What Is This?

**cue-vox** is an open source voice interface for Claude Code. A localhost web interface that lets you talk to Claude using your voice.

**Features:**
- Push-to-talk voice input (hold SPACE)
- Local speech-to-text via Whisper
- Full Claude Code integration with file system access
- Text-to-speech via macOS `say`
- Visual state feedback (colored dot)
- Conversation history display
- Interrupt capability (press SPACE during response)

---

## Status

✅ **Functional** - Core features working

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run server
python3 web.py

# 3. Open browser
open http://localhost:3000

# 4. Hold SPACE to talk, release to process
```

See [INSTALL.md](INSTALL.md) for detailed setup instructions.

---

## Security Notice

**This is a local development tool for personal use.**

- Runs on localhost:3000 without authentication
- Provides voice access to Claude Code with file system permissions
- All local users can access the interface when server is running
- Voice input is transcribed and passed directly to Claude CLI

**Recommendations:**
- Only run on trusted machines
- Kill server when not in use if others have access to your machine
- Not intended for production, multi-user, or untrusted environments
- Review `.claude/settings.local.json.sample` for permission configuration

---

## Requirements

- macOS (for `say` command)
- Python 3.8+
- Microphone access
- Claude Code CLI installed (`claude`)
- ~150MB for Whisper base model (downloads on first use)

---

## How It Works

1. **Browser** → Records audio via MediaRecorder API
2. **Flask server** → Receives audio, transcribes with Whisper
3. **Claude CLI** → Processes transcription from parent directory (maestro)
4. **macOS say** → Speaks response
5. **Browser** → Updates conversation UI

---

## License

Apache 2.0

**Disclaimer:** This software is provided "AS IS" without warranty of any kind. Users assume all risks associated with its use.
