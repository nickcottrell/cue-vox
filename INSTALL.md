# Installation

## Requirements

- macOS (for `say` command)
- Python 3.8+
- Microphone
- Claude Code CLI installed

## Install

```bash
# 1. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Test it works
python3 vox.py
```

## First Run

- Fullscreen black window appears with gray dot
- Hold SPACE → dot turns red, recording starts
- Release SPACE → dot turns orange (transcribing) → blue (Claude thinking) → green (speaking)
- Press SPACE mid-response to interrupt
- Press ESC to exit

## Troubleshooting

**No microphone access:**
- macOS will prompt for permission first time
- Go to System Preferences → Security & Privacy → Microphone

**Whisper model download:**
- First run downloads ~150MB base model
- Stored in `~/.cache/whisper/`

**Claude command not found:**
- Install Claude Code CLI first
- Verify with: `which claude`
