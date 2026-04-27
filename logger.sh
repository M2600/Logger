#!/bin/bash

set -e

WORKING_DIR="$(dirname "$0")"
VENV_DIR=".venv"

install() {
	# exit if python is not installed
	if !(type "python3" > /dev/null 2>&1); then
		echo "python3 not found. Install it"
		exit 1
	fi
	
	# create virtual env
	python3 -m venv $working_dir/$VENV_DIR
	
	# install pip packages
	"$WORKING_DIR/$VENV_DIR/bin/pip" install -r "$WORKING_DIR/requirements.txt"
}



export DISPLAY=:0

"$WORKING_DIR/$VENV_DIR/bin/python" "$WORKING_DIR/log.py" "$@"
