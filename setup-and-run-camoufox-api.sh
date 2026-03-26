#!/usr/bin/env bash
# setup-and-run-camoufox-api.sh — macOS / Linux (Ubuntu and similar)
#
# Create .venv, install requirements, camoufox fetch, verify Camoufox binary via pkgman,
# then run: api_solver.py --browser_type camoufox
#
# Usage:
#   chmod +x setup-and-run-camoufox-api.sh
#   ./setup-and-run-camoufox-api.sh --solver-debug
#   AUTO_INSTALL=0 ./setup-and-run-camoufox-api.sh   # disable apt/brew auto-install
#
# By default, if python3 is missing, attempts: apt (Debian/Ubuntu), dnf (Fedora-like), or brew (macOS).
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_CMD=""
VENV_DIR=".venv"
SKIP_VENV=0
SKIP_FETCH=0
ONLY_SETUP=0
LIST_PY=0
BIND_ADDRESS="0.0.0.0"
BIND_PORT="5000"
SOLVER_DEBUG=0
HEADLESS=0
AUTO_INSTALL=1

info() { echo "[*] $*" >&2; }
warn() { echo "[!] $*" >&2; }
err()  { echo "[x] $*" >&2; }

usage() {
  cat >&2 <<'EOF'
Usage: setup-and-run-camoufox-api.sh [options]

Options:
  --python PATH           Use this interpreter (must exist and be executable)
  --venv-dir DIR          Virtualenv directory (default: .venv)
  --skip-venv             Use resolved Python directly (no venv)
  --skip-fetch            Skip: python -m camoufox fetch
  --only-setup            Install + fetch + verify only; do not start API
  --list-python-candidates Show python3* on PATH and exit
  --bind-address ADDR     Default 0.0.0.0
  --bind-port PORT        Default 5000
  --solver-debug          Pass --debug to api_solver.py
  --headless              Pass --headless to api_solver.py
  --no-auto-install       Do not run apt/brew/dnf to install Python
  -h, --help              This help
EOF
}

list_python_candidates() {
  echo "=== command -v (python variants) ===" >&2
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      echo "  $c -> $(command -v "$c")" >&2
    fi
  done
  echo "" >&2
  echo "=== which -a python3 (if available) ===" >&2
  if command -v which >/dev/null 2>&1; then
    which -a python3 2>/dev/null >&2 || true
  fi
  echo "" >&2
  echo "Install hints:" >&2
  echo "  Ubuntu/Debian: sudo apt update && sudo apt install -y python3.12-venv python3-pip" >&2
  echo "  macOS (Homebrew): brew install python@3.12" >&2
  exit 0
}

try_resolve_python() {
  if [[ -n "$PYTHON_CMD" ]]; then
    if [[ ! -f "$PYTHON_CMD" ]] || [[ ! -x "$PYTHON_CMD" ]]; then
      return 1
    fi
    echo "$PYTHON_CMD"
    return 0
  fi
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

auto_install_system_python() {
  [[ "$AUTO_INSTALL" == "1" ]] || return 1

  if [[ "$(uname -s)" == "Darwin" ]]; then
    if ! command -v brew >/dev/null 2>&1; then
      err "Homebrew not found. Install from https://brew.sh then re-run."
      return 1
    fi
    info "Installing Python via Homebrew (python@3.12) ..."
    HOMEBREW_NO_ANALYTICS=1 NONINTERACTIVE=1 brew install python@3.12 || true
    for p in /opt/homebrew/opt/python@3.12/bin /usr/local/opt/python@3.12/bin; do
      if [[ -d "$p" ]]; then
        export PATH="$p:$PATH"
        info "Prepended to PATH: $p"
      fi
    done
    return 0
  fi

  local SUDO=( )
  if [[ "$EUID" -ne 0 ]]; then
    SUDO=(sudo)
    info "You may be prompted once for sudo to install python3."
  fi

  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
  fi

  if command -v apt-get >/dev/null 2>&1; then
    if [[ "${ID:-}" == "debian" || "${ID:-}" == "ubuntu" || "${ID_LIKE:-}" == *"debian"* || "${ID_LIKE:-}" == *"ubuntu"* ]]; then
      info "Installing python3, python3-venv, python3-pip via apt-get ..."
      "${SUDO[@]}" apt-get update -qq
      "${SUDO[@]}" apt-get install -y python3 python3-venv python3-pip
      return 0
    fi
  fi

  if command -v dnf >/dev/null 2>&1; then
    info "Installing python3 via dnf ..."
    "${SUDO[@]}" dnf install -y python3
    return 0
  fi

  warn "No supported package manager (apt/dnf) found for auto-install."
  return 1
}

ensure_python() {
  local p
  if p="$(try_resolve_python)"; then
    echo "$p"
    return 0
  fi
  if [[ "$AUTO_INSTALL" == "1" ]]; then
    info "Python not on PATH; attempting automatic OS install ..."
    auto_install_system_python || true
  fi
  if p="$(try_resolve_python)"; then
    echo "$p"
    return 0
  fi
  err "Could not find python3. Run: $0 --list-python-candidates"
  return 1
}

verify_camoufox_binary() {
  "$1" -c 'from camoufox.pkgman import launch_path
import os, sys
p = launch_path()
if not os.path.isfile(p):
    print("Camoufox binary missing:", p, file=sys.stderr)
    sys.exit(1)
print("Camoufox OK:", p)
'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PYTHON_CMD="$2"; shift 2 ;;
    --venv-dir) VENV_DIR="$2"; shift 2 ;;
    --skip-venv) SKIP_VENV=1; shift ;;
    --skip-fetch) SKIP_FETCH=1; shift ;;
    --only-setup) ONLY_SETUP=1; shift ;;
    --list-python-candidates) LIST_PY=1; shift ;;
    --bind-address) BIND_ADDRESS="$2"; shift 2 ;;
    --bind-port) BIND_PORT="$2"; shift 2 ;;
    --solver-debug) SOLVER_DEBUG=1; shift ;;
    --headless) HEADLESS=1; shift ;;
    --no-auto-install) AUTO_INSTALL=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      err "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ "$LIST_PY" -eq 1 ]]; then
  list_python_candidates
fi

if [[ -n "$PYTHON_CMD" ]]; then
  if [[ ! -f "$PYTHON_CMD" ]]; then
    err "File not found: $PYTHON_CMD"
    err "Run: $0 --list-python-candidates"
    exit 1
  fi
  if [[ ! -x "$PYTHON_CMD" ]]; then
    err "Not executable: $PYTHON_CMD"
    exit 1
  fi
fi

PY="$(ensure_python)" || exit 1
info "Using Python: $PY"

if [[ "$SKIP_VENV" -eq 0 ]]; then
  VENV_ROOT="$PROJECT_ROOT/$VENV_DIR"
  VENV_PY="$VENV_ROOT/bin/python"
  if [[ ! -x "$VENV_PY" ]]; then
    info "Creating venv at $VENV_ROOT"
    "$PY" -m venv "$VENV_ROOT"
  fi
  PY="$VENV_PY"
fi

info "Upgrading pip"
"$PY" -m pip install --upgrade pip

REQ="$PROJECT_ROOT/requirements.txt"
if [[ ! -f "$REQ" ]]; then
  err "Missing requirements.txt in $PROJECT_ROOT"
  exit 1
fi
info "Installing requirements from requirements.txt"
"$PY" -m pip install -r "$REQ"

if [[ "$SKIP_FETCH" -eq 0 ]]; then
  info "Running: python -m camoufox fetch"
  "$PY" -m camoufox fetch
fi

info "Verifying Camoufox binary (launch_path)"
if ! verify_camoufox_binary "$PY"; then
  if [[ "$SKIP_FETCH" -eq 0 ]]; then
    warn "Re-running camoufox fetch once ..."
    "$PY" -m camoufox fetch
    if ! verify_camoufox_binary "$PY"; then
      err "Camoufox binary still missing after fetch. See camoufox docs or check disk/network."
      exit 1
    fi
  else
    err "Camoufox verification failed. Re-run without --skip-fetch or: $PY -m camoufox fetch"
    exit 1
  fi
fi

if [[ "$ONLY_SETUP" -eq 1 ]]; then
  info "Only setup: done."
  exit 0
fi

API_ARGS=(api_solver.py --browser_type camoufox --host "$BIND_ADDRESS" --port "$BIND_PORT")
[[ "$SOLVER_DEBUG" -eq 1 ]] && API_ARGS+=(--debug)
[[ "$HEADLESS" -eq 1 ]] && API_ARGS+=(--headless)

info "Starting API: $PY ${API_ARGS[*]}"
exec "$PY" "${API_ARGS[@]}"
