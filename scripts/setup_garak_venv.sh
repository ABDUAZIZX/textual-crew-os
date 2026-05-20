#!/usr/bin/env bash
# Create an isolated virtualenv for garak (LLM vulnerability scanner).
#
# garak has heavy, sometimes-conflicting dependencies, so it lives in its
# own venv and is invoked via subprocess (see crew_os/agents/auditor.py).
# Default location: $CREW_DATA_DIR/garak-venv (or the XDG data dir).

set -euo pipefail

DATA_DIR="${CREW_DATA_DIR:-$HOME/.local/share/textual-crew-os}"
VENV_DIR="$DATA_DIR/garak-venv"

if ! command -v python3 >/dev/null 2>&1; then
    printf 'python3 not found on PATH\n' >&2
    exit 1
fi

mkdir -p "$DATA_DIR"

if [[ -x "$VENV_DIR/bin/python" ]]; then
    printf 'garak venv already exists at %s\n' "$VENV_DIR"
else
    printf 'creating garak venv at %s\n' "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet garak

printf 'garak installed: '
"$VENV_DIR/bin/python" -m garak --version 2>/dev/null || printf '(version check failed)\n'

printf '\nSet this in your .env if the location differs:\n'
printf '  CREW_GARAK_PYTHON=%s/bin/python\n' "$VENV_DIR"
