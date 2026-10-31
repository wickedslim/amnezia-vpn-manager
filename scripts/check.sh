#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
TASK_PYTHON="${TASK_PYTHON:-python3}"
"$TASK_PYTHON" -m compileall -q awg/manager awg/bot_manager.py
PYTHONPATH="$PWD/awg${PYTHONPATH:+:$PYTHONPATH}" "$TASK_PYTHON" -m unittest discover -s tests -v
