#!/bin/bash
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  python3 app.py
else
  echo "Python 3 not found. Install it from https://www.python.org/downloads/ then double-click this file again."
fi
read -p "Press Enter to close..."
