#!/bin/bash
# Dev mode - runs on port 3001 for UI development

export CUE_VOX_PORT=3001
source .venv/bin/activate
python3 web.py
