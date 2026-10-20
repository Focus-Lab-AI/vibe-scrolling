#!/usr/bin/env bash
# Run the streaming host on your laptop (macOS or Linux) and scroll on your phone.
#
#   bash scripts/run_laptop.sh                      # platforms from config.json → stream.platforms
#   bash scripts/run_laptop.sh --platforms x,threads
#   bash scripts/run_laptop.sh --fake               # demo with the built-in fake feed, no logins
#
# Flags:
#   --platforms x,threads   real platforms to stream (x, threads, instagram, youtube, linkedin)
#   --fake                  add the local fake feed site (demo / smoke test)
#   --headless              hide the browser windows (you can't log in while headless)
#   --port N                host port (default 8765)
#   --token T               phone pairing token (default: random each run)
#   --curator K             heuristic | claude (needs ANTHROPIC_API_KEY) | cli
#   --workspace DIR         where browser profiles / streams live (default: config.json workspace_dir)
#   --no-caffeinate         macOS: let the laptop sleep while the host runs
#   --setup-only            create .venv + install deps + Chromium, then exit
#
# First run creates .venv (Python 3.11+, via uv when installed), installs
# requirements + requirements-app + anthropic + qrcode, and Playwright's Chromium.
# Re-runs skip the install unless the requirements files changed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

PORT=8765
TOKEN=""
CAFFEINATE=1
SETUP_ONLY=0
HOST_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --platforms) HOST_ARGS+=(--platforms "${2:?--platforms needs a value}"); shift 2 ;;
    --platforms=*) HOST_ARGS+=(--platforms "${1#*=}"); shift ;;
    --fake) HOST_ARGS+=(--fake); shift ;;
    --headless) HOST_ARGS+=(--headless); shift ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --token) TOKEN="${2:?--token needs a value}"; shift 2 ;;
    --curator) HOST_ARGS+=(--curator "${2:?--curator needs a value}"); shift 2 ;;
    --workspace) HOST_ARGS+=(--workspace "${2:?--workspace needs a value}"); shift 2 ;;
    --no-caffeinate) CAFFEINATE=0; shift ;;
    --setup-only) SETUP_ONLY=1; shift ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m[laptop]\033[0m %s\n' "$*"; }

# --- 1. virtualenv (Python 3.11+) ------------------------------------------------
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

# A dangling/looping .venv symlink (e.g. left over from a git worktree) would
# make venv creation fail with "too many levels of symbolic links".
if [ -L "$VENV" ] && [ ! -x "$PY" ]; then
  say "removing broken .venv symlink"
  rm -f "$VENV"
fi
if [ -x "$PY" ] && ! py_ok "$PY"; then
  say ".venv uses Python < 3.11 — recreating it"
  rm -rf "$VENV"
fi
if [ ! -x "$PY" ]; then
  if command -v uv >/dev/null 2>&1; then
    say "creating .venv with uv (Python 3.11+)"
    uv venv --python '>=3.11' "$VENV"
  else
    BASE=""
    for c in python3.13 python3.12 python3.11 python3; do
      if command -v "$c" >/dev/null 2>&1 && py_ok "$(command -v "$c")"; then BASE="$(command -v "$c")"; break; fi
    done
    if [ -z "$BASE" ]; then
      echo "Python 3.11+ not found. Install it (macOS: 'brew install python@3.12' or https://python.org) or install uv: https://docs.astral.sh/uv/" >&2
      exit 1
    fi
    say "creating .venv with $BASE"
    "$BASE" -m venv "$VENV"
  fi
fi

# --- 2. dependencies (skipped when requirements are unchanged) -------------------
EXTRA_DEPS="anthropic"
STAMP="$VENV/.vs_laptop_deps"
want_stamp="$(cat requirements.txt requirements-app.txt; echo "$EXTRA_DEPS")"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$want_stamp" ]; then
  say "installing Python dependencies"
  # shellcheck disable=SC2086
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -r requirements.txt -r requirements-app.txt $EXTRA_DEPS
  else
    "$PY" -m pip install --upgrade pip >/dev/null
    "$PY" -m pip install -r requirements.txt -r requirements-app.txt $EXTRA_DEPS
  fi
  printf '%s' "$want_stamp" > "$STAMP"
fi

# --- 3. Chromium (fallback when Google Chrome isn't installed) ---------------------
# Install where the host looks for it: the app cache dir, unless already set.
if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
  PLAYWRIGHT_BROWSERS_PATH="$("$PY" -c 'from app.paths import BROWSERS_PATH; print(BROWSERS_PATH)')"
  export PLAYWRIGHT_BROWSERS_PATH
fi
say "checking Playwright Chromium (in $PLAYWRIGHT_BROWSERS_PATH)"
"$PY" -m playwright install chromium

if [ "$SETUP_ONLY" = 1 ]; then
  say "setup done"
  exit 0
fi

# --- 4. run ----------------------------------------------------------------------
if [ -z "$TOKEN" ]; then
  TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(9))')"
fi

if [ "$(uname -s)" = "Darwin" ]; then
  say "macOS: if a firewall prompt asks to accept incoming connections for Python, click Allow."
fi

CMD=("$PY" scripts/run_stream_host.py --host 0.0.0.0 --port "$PORT" --token "$TOKEN" --laptop --qr)
if [ ${#HOST_ARGS[@]} -gt 0 ]; then CMD+=("${HOST_ARGS[@]}"); fi

if [ "$(uname -s)" = "Darwin" ] && [ "$CAFFEINATE" = 1 ] && command -v caffeinate >/dev/null 2>&1; then
  say "keeping the Mac awake while the host runs (caffeinate -i; --no-caffeinate to skip)"
  exec caffeinate -i "${CMD[@]}"
fi
exec "${CMD[@]}"
