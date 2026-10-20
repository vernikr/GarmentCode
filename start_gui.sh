#!/bin/bash
# GarmentCode GUI launcher (local fork)
# Usage:  bash start_gui.sh     (open http://localhost:8080)
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
export PYTHONPATH="$(pwd):$PYTHONPATH"
echo "GarmentCode GUI: http://localhost:8080  (Ctrl+C to stop)"
python gui.py
