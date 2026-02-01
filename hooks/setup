#!/bin/bash
# Setup script for cue-vox

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎙️  Installing cue-vox dependencies"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# 1. Install system dependencies via Homebrew
echo "📦 Installing system dependencies..."
brew install portaudio python-tk@3.14

# 2. Create virtual environment with system packages
echo "🐍 Creating virtual environment..."
python3 -m venv .venv

# 3. Activate and install Python packages
echo "📚 Installing Python packages..."
source .venv/bin/activate
pip install --upgrade pip
pip install pyaudio keyboard openai-whisper

echo
echo "✅ Installation complete!"
echo
echo "Run with: python3 vox.py"
