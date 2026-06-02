#!/usr/bin/env bash
#
# Textual Crew OS - installer
#
# Idempotent setup for a single Linux workstation (developed on Debian 13).
# Creates a Python venv, installs the package, and wires up the local
# Ollama backend (models are pulled into Ollama, never vendored into git).
#
# Design principles:
#   * Fail safe: any missing prerequisite stops the script with a clear
#     message instead of limping forward.
#   * No surprises: the Ollama install command is PRINTED and CONFIRMED
#     before it ever runs. We never blindly pipe curl into a shell.
#   * Loopback only: Ollama and the dashboard stay on 127.0.0.1 by
#     default. Exposing them is an explicit, warned, opt-in action.
#   * Re-runnable: existing venv / models / .env are detected and reused.
#
# Usage:
#   ./install.sh                 full setup (interactive)
#   ./install.sh --dev           also install dev/test dependencies
#   ./install.sh --skip-models   skip `ollama pull` (deps + venv only)
#   ./install.sh --yes           assume "yes" for the Ollama install prompt
#   ./install.sh --help
#
set -euo pipefail

# ─────────────────────────────── constants ──────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
MODELFILE="${SCRIPT_DIR}/models/crew-defender.Modelfile"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11
OLLAMA_INSTALL_CMD='curl -fsSL https://ollama.com/install.sh | sh'

# ─────────────────────────────── flags ──────────────────────────────────
INSTALL_DEV=0
SKIP_MODELS=0
ASSUME_YES=0

# ─────────────────────────────── output ─────────────────────────────────
if [[ -t 1 ]]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[34m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
    C_RED=''; C_GRN=''; C_YLW=''; C_BLU=''; C_BLD=''; C_RST=''
fi

info()  { printf '%s[*]%s %s\n' "$C_BLU" "$C_RST" "$*"; }
ok()    { printf '%s[OK]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn()  { printf '%s[WARN]%s %s\n' "$C_YLW" "$C_RST" "$*" >&2; }
fail()  { printf '%s[FAIL]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }
step()  { printf '\n%s== %s ==%s\n' "$C_BLD" "$*" "$C_RST"; }

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

# ─────────────────────────────── arg parse ──────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --dev)         INSTALL_DEV=1 ;;
        --skip-models) SKIP_MODELS=1 ;;
        --yes|-y)      ASSUME_YES=1 ;;
        --help|-h)     usage ;;
        *)             fail "unknown argument: ${arg} (try --help)" ;;
    esac
done

confirm() {
    # confirm "question" -> 0 if yes. Honors --yes; refuses on non-TTY.
    local prompt="$1" reply
    if (( ASSUME_YES )); then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        warn "non-interactive shell and --yes not given; treating as 'no'."
        return 1
    fi
    read -r -p "${prompt} [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# Read a single KEY from .env safely (no `source`, no code execution).
read_env() {
    local key="$1" line
    [[ -f "$ENV_FILE" ]] || return 1
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n1 || true)"
    [[ -n "$line" ]] || return 1
    # strip "KEY=", surrounding quotes, and inline whitespace.
    printf '%s' "${line#*=}" | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' -e 's/[[:space:]]*$//'
}

# ─────────────────────────── 0. detect OS ───────────────────────────────
step "Detecting platform"
if [[ "$(uname -s)" != "Linux" ]]; then
    fail "this installer targets Linux only (found $(uname -s))."
fi
DISTRO="unknown"; DISTRO_VER=""
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO="${ID:-unknown}"; DISTRO_VER="${VERSION_ID:-}"
fi
info "OS: ${PRETTY_NAME:-Linux} (${DISTRO} ${DISTRO_VER})"
if [[ "$DISTRO" == "debian" && "${DISTRO_VER%%.*}" == "13" ]]; then
    ok "Debian 13 — the reference platform."
else
    warn "Not Debian 13 (the tested target). Should still work on modern Linux."
fi

# ─────────────────────── 1. check prerequisites ─────────────────────────
step "Checking prerequisites"
need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "'$1' not found. Install it first: ${2}"
}
need_cmd curl    "sudo apt install curl"
need_cmd python3 "sudo apt install python3"
ok "curl, python3 present"

# python venv + pip module availability (Debian splits these out).
if ! python3 -c 'import venv' >/dev/null 2>&1; then
    fail "python3 'venv' module missing. Install: sudo apt install python3-venv"
fi
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    fail "python3 'ensurepip' missing. Install: sudo apt install python3-venv"
fi

# python version gate (>= 3.11).
PY_OK="$(python3 - "$MIN_PY_MAJOR" "$MIN_PY_MINOR" <<'PY'
import sys
maj, mino = int(sys.argv[1]), int(sys.argv[2])
print("1" if sys.version_info[:2] >= (maj, mino) else "0")
PY
)"
PY_VER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
[[ "$PY_OK" == "1" ]] || fail "Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ required; found ${PY_VER}."
ok "Python ${PY_VER}"

# ─────────────────────────── 2. python venv ─────────────────────────────
step "Setting up Python virtual environment"
if [[ -d "$VENV_DIR" && -x "${VENV_DIR}/bin/python" ]]; then
    ok "venv already exists at .venv — reusing"
else
    info "Creating venv at .venv"
    python3 -m venv "$VENV_DIR"
    ok "venv created"
fi
# Use the venv's interpreter directly; never touch system Python.
VENV_PY="${VENV_DIR}/bin/python"
info "Upgrading pip inside the venv"
"$VENV_PY" -m pip install --upgrade pip >/dev/null
ok "pip upgraded"

step "Installing Python dependencies (inside venv)"
if [[ -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    info "Installing from requirements.txt"
    "$VENV_PY" -m pip install -r "${SCRIPT_DIR}/requirements.txt"
fi
if (( INSTALL_DEV )); then
    info "Installing project (editable) with dev extras"
    "$VENV_PY" -m pip install -e "${SCRIPT_DIR}[dev]"
else
    info "Installing project (editable, runtime deps)"
    "$VENV_PY" -m pip install -e "${SCRIPT_DIR}"
fi
ok "Python dependencies installed"

# ─────────────────────────────── 3. .env ────────────────────────────────
step "Configuration (.env)"
if [[ -f "$ENV_FILE" ]]; then
    ok ".env already present — leaving it untouched"
else
    [[ -f "$ENV_EXAMPLE" ]] || fail ".env.example missing; cannot bootstrap .env"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "Created .env from .env.example (chmod 600)"
    warn "Review .env before first run. Most defaults are safe; set these if needed:"
    printf '      - %s\n' \
        "CREW_ANTHROPIC_API_KEY  (optional; leave empty to run fully offline)" \
        "CREW_DATA_DIR           (optional; defaults to ~/.local/share/textual-crew-os)" \
        "CREW_*_MODEL            (only if you changed the matching agent defaults)"
fi

# Resolve effective config from .env (fallback to safe defaults).
OLLAMA_HOST="$(read_env CREW_OLLAMA_HOST || true)"; OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
WEB_HOST="$(read_env CREW_WEB_HOST || true)";       WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="$(read_env CREW_WEB_PORT || true)";       WEB_PORT="${WEB_PORT:-8765}"

# ─────────────────────────────── 4. Ollama ──────────────────────────────
step "Ollama backend"
if command -v ollama >/dev/null 2>&1; then
    ok "Ollama already installed ($(ollama --version 2>/dev/null | head -n1))"
else
    warn "Ollama is not installed."
    printf '\n  The official install method is:\n\n      %s%s%s\n\n' "$C_BLD" "$OLLAMA_INSTALL_CMD" "$C_RST"
    printf '  This downloads and runs a script from ollama.com with your user privileges.\n'
    printf '  Review it at https://ollama.com/install.sh before agreeing.\n\n'
    if confirm "Run the Ollama install command now?"; then
        info "Installing Ollama..."
        eval "$OLLAMA_INSTALL_CMD"
        command -v ollama >/dev/null 2>&1 || fail "Ollama install did not put 'ollama' on PATH."
        ok "Ollama installed"
    else
        fail "Ollama is required. Install it manually, then re-run ./install.sh"
    fi
fi

# Verify the Ollama service / API is reachable.
info "Verifying Ollama service"
OLLAMA_UP=0
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet ollama 2>/dev/null; then
    ok "systemd unit 'ollama' is active"
    OLLAMA_UP=1
fi
if curl -fsS --max-time 5 "${OLLAMA_HOST%/}/api/tags" >/dev/null 2>&1; then
    ok "Ollama API reachable at ${OLLAMA_HOST}"
    OLLAMA_UP=1
fi
if (( ! OLLAMA_UP )); then
    warn "Ollama is installed but not responding at ${OLLAMA_HOST}."
    if command -v systemctl >/dev/null 2>&1; then
        printf '      Start it with:  sudo systemctl enable --now ollama\n'
    else
        printf '      Start it with:  ollama serve &\n'
    fi
    fail "Cannot continue without a running Ollama. Start it and re-run."
fi

# Loopback safety check: warn loudly if Ollama is exposed beyond localhost.
EXPOSED_HOST=""
if command -v systemctl >/dev/null 2>&1; then
    EXPOSED_HOST="$(systemctl show ollama -p Environment 2>/dev/null | grep -oE 'OLLAMA_HOST=[^ ]+' | cut -d= -f2- || true)"
fi
EXPOSED_HOST="${EXPOSED_HOST:-${OLLAMA_HOST}}"
if printf '%s' "$EXPOSED_HOST" | grep -qE '0\.0\.0\.0|\[::\]'; then
    warn "Ollama appears bound to a public interface (${EXPOSED_HOST})."
    warn "This lets anyone on your network use your GPU and loaded models."
    warn "Recommended: bind to 127.0.0.1 only. This installer will NOT expose it for you."
else
    ok "Ollama bound to loopback (no 0.0.0.0 exposure detected)"
fi

# ─────────────────────────── 5. pull models ─────────────────────────────
step "Local models"
if (( SKIP_MODELS )); then
    warn "--skip-models given; not pulling any models."
else
    # Model names come from .env (not hardcoded here), per security policy.
    declare -a MODELS=()
    for key in CREW_ROUTER_MODEL CREW_CODER_MODEL CREW_AUDITOR_MODEL \
               CREW_SUPERVISOR_LOCAL_MODEL CREW_EMBED_MODEL CREW_DEFENDER_BASE_MODEL; do
        val="$(read_env "$key" || true)"
        [[ -n "$val" ]] && MODELS+=("$val")
    done
    if (( ${#MODELS[@]} == 0 )); then
        warn "No CREW_*_MODEL entries found in .env; skipping pulls."
    else
        installed_list="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}' || true)"
        for m in "${MODELS[@]}"; do
            if grep -qFx "$m" <<<"$installed_list"; then
                ok "present: ${m}"
            else
                info "pulling: ${m}  (this can take a while)"
                ollama pull "$m" || fail "failed to pull ${m}"
                ok "pulled: ${m}"
            fi
        done
    fi

    # Build the custom defensive model from the Modelfile (idempotent).
    DEFENDER_NAME="$(read_env CREW_DEFENDER_MODEL || true)"; DEFENDER_NAME="${DEFENDER_NAME:-crew-defender}"
    if [[ -f "$MODELFILE" ]]; then
        installed_list="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}' || true)"
        if grep -qE "^${DEFENDER_NAME}(:|$)" <<<"$installed_list"; then
            ok "custom model '${DEFENDER_NAME}' already built"
        else
            info "building custom model '${DEFENDER_NAME}' from Modelfile"
            ollama create "$DEFENDER_NAME" -f "$MODELFILE" || fail "failed to build ${DEFENDER_NAME}"
            ok "built: ${DEFENDER_NAME}"
        fi
    else
        warn "Modelfile not found at ${MODELFILE}; skipping custom defender build."
    fi
fi

# ─────────────────────── 6. optional env check ──────────────────────────
step "Environment health check"
if [[ -f "${SCRIPT_DIR}/scripts/check_env.py" ]]; then
    set +e
    "$VENV_PY" "${SCRIPT_DIR}/scripts/check_env.py"
    rc=$?
    set -e
    if (( rc == 0 )); then
        ok "check_env.py: OK"
    else
        warn "check_env.py reported issues (exit ${rc}); review above."
    fi
else
    info "scripts/check_env.py not found; skipping."
fi

# ─────────────────────────────── done ───────────────────────────────────
step "Done"
cat <<EOF
${C_GRN}Textual Crew OS is installed.${C_RST}

Activate the environment and launch:

    ${C_BLD}source .venv/bin/activate${C_RST}

    crew status                 # Ollama / GPU / agents overview
    crew dashboard              # serve + open http://${WEB_HOST}:${WEB_PORT}
    crew run "implement add(a, b)"

Configuration lives in ${C_BLD}.env${C_RST} (loopback-only by default).
Security model and LAB_MODE policy: see SECURITY.md.
EOF
