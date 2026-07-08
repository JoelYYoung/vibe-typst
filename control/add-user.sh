#!/usr/bin/env bash
# Usage: ./add-user.sh <username> <password>
set -e
CTRL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$CTRL_DIR/.venv"
export CONTROL_DATA="${CONTROL_DATA:-$CTRL_DIR/data}"
"$VENV/bin/python" "$CTRL_DIR/main.py" create-user "$1" "$2"
