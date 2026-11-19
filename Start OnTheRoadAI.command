#!/bin/bash
# Double-click to start OnTheRoadAI. Keep this window open while using it.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it free from https://www.python.org/downloads/"
  read -r -p "Press Enter to close"
  exit 1
fi
python3 launch.py
