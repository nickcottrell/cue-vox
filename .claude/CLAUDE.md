# cue-vox - Instructions for Claude Code

## About This Repository

**cue-vox-dev** is an open source voice interface for Claude Code that can be deployed in ANY Claude Code project.

It provides a web-based UI with:
- Voice input via Whisper
- Text-to-speech output
- Structured input protocol (sliders, YES/NO, text input, approval gates)
- Real-time Socket.IO communication

**Integration:** When running, cue-vox executes Claude commands from its parent directory, enabling voice-controlled workflow automation.

---

## Claude Code Policies

**AUTO-SYNCED from `claude-init/policies/` - Foundation policies are always active**


---

## Policy Management

Claude Code policies for this project are managed via the `claude-init` system.

**To view or update policies:**
```bash
cd claude-init
cat policies/*.md
./update-claude-policies.sh
```
