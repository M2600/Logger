#!/bin/bash
cd "$(dirname "$0")"

export DISPLAY=:0

./.venv/bin/python log.py --gui
