#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CODEX_AVATAR_CONFIG:-$HOME/.dws/ai-autoreply/config.json}"
VENV_DIR="${CODEX_AVATAR_VENV:-$HOME/.dws/codex-avatar/venv}"
LAUNCHD_LABEL="${CODEX_AVATAR_LAUNCHD_LABEL:-com.dws.ai-autoreply.codex-avatar}"
DWS_BIN="${DWS_BIN:-}"
CODEX_BIN="${CODEX_BIN:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_LAUNCHD=0
START_SERVICE=0
SKIP_AUTH_CHECK=0

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Options:
  --config PATH          Runtime config path. Default: ~/.dws/ai-autoreply/config.json
  --venv PATH            Python virtualenv path. Default: ~/.dws/codex-avatar/venv
  --dws-bin PATH         dws CLI path. Default: DWS_BIN, PATH, then ~/.local/bin/dws
  --codex-bin PATH       codex CLI path. Default: CODEX_BIN, PATH, then ChatGPT.app bundled codex
  --install-launchd      Install a macOS LaunchAgent for the worker
  --start                Start or restart the LaunchAgent after installing it
  --label LABEL          LaunchAgent label. Default: com.dws.ai-autoreply.codex-avatar
  --skip-auth-check      Skip `dws auth status`
  -h, --help             Show this help

Examples:
  scripts/install.sh
  scripts/install.sh --install-launchd --start
  scripts/install.sh --config ~/.dws/ai-autoreply/winyai.config.json --install-launchd --start
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --venv)
      VENV_DIR="$2"
      shift 2
      ;;
    --dws-bin)
      DWS_BIN="$2"
      shift 2
      ;;
    --codex-bin)
      CODEX_BIN="$2"
      shift 2
      ;;
    --install-launchd)
      INSTALL_LAUNCHD=1
      shift
      ;;
    --start)
      START_SERVICE=1
      INSTALL_LAUNCHD=1
      shift
      ;;
    --label)
      LAUNCHD_LABEL="$2"
      shift 2
      ;;
    --skip-auth-check)
      SKIP_AUTH_CHECK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

resolve_bin() {
  local configured="$1"
  shift
  if [[ -n "$configured" && -x "$configured" ]]; then
    printf '%s\n' "$configured"
    return 0
  fi
  for candidate in "$@"; do
    if [[ "$candidate" == command:* ]]; then
      local name="${candidate#command:}"
      if command -v "$name" >/dev/null 2>&1; then
        command -v "$name"
        return 0
      fi
    elif [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

require_python() {
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "python3 is required but was not found." >&2
    exit 1
  fi
  "$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required.")
PY
}

xml_escape() {
  "$PYTHON_BIN" -c 'import html,sys; print(html.escape(sys.argv[1], quote=False))' "$1"
}

write_launchd_plist() {
  local avatar_bin="$1"
  local plist_path="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
  local out_log="$HOME/.dws/ai-autoreply/codex-avatar.out.log"
  local err_log="$HOME/.dws/ai-autoreply/codex-avatar.err.log"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.dws/ai-autoreply"
  cat > "$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "$LAUNCHD_LABEL")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$avatar_bin")</string>
    <string>--config</string>
    <string>$(xml_escape "$CONFIG_PATH")</string>
    <string>--dws-bin</string>
    <string>$(xml_escape "$DWS_BIN")</string>
    <string>--codex-bin</string>
    <string>$(xml_escape "$CODEX_BIN")</string>
    <string>--print-command</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$out_log")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$err_log")</string>
</dict>
</plist>
EOF
  plutil -lint "$plist_path" >/dev/null
  echo "$plist_path"
}

require_python

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"

DWS_BIN="$(resolve_bin "$DWS_BIN" "command:dws" "$HOME/.local/bin/dws" || true)"
if [[ -z "$DWS_BIN" ]]; then
  echo "dws CLI is required. Install/login dws first, or pass --dws-bin /path/to/dws." >&2
  exit 1
fi

CODEX_BIN="$(resolve_bin "$CODEX_BIN" "command:codex" "/Applications/ChatGPT.app/Contents/Resources/codex" || true)"
if [[ -z "$CODEX_BIN" ]]; then
  echo "codex CLI is required for reply_engine=codex. Install ChatGPT/Codex CLI, or pass --codex-bin /path/to/codex." >&2
  exit 1
fi

mkdir -p "$(dirname "$CONFIG_PATH")"
if [[ ! -f "$CONFIG_PATH" ]]; then
  cp "$ROOT_DIR/config/example.json" "$CONFIG_PATH"
  echo "Created config: $CONFIG_PATH"
  echo "Edit placeholder group IDs before starting the worker."
fi

if [[ "$SKIP_AUTH_CHECK" -eq 0 ]]; then
  "$DWS_BIN" auth status
fi

"$VENV_DIR/bin/codex-avatar" \
  --config "$CONFIG_PATH" \
  --dws-bin "$DWS_BIN" \
  --codex-bin "$CODEX_BIN" \
  --config-check

if [[ "$INSTALL_LAUNCHD" -eq 1 ]]; then
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "--install-launchd is only supported on macOS." >&2
    exit 1
  fi
  plist_path="$(write_launchd_plist "$VENV_DIR/bin/codex-avatar")"
  if [[ "$START_SERVICE" -eq 1 ]]; then
    if grep -q "cid_REPLACE" "$CONFIG_PATH"; then
      echo "Refusing to start: $CONFIG_PATH still contains placeholder group IDs." >&2
      exit 1
    fi
    launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist_path"
    launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"
    echo "LaunchAgent started: $LAUNCHD_LABEL"
  else
    echo "LaunchAgent plist installed but not started. Re-run with --start after editing the config."
  fi
  echo "LaunchAgent installed: $plist_path"
fi

echo "CodeX Avatar installed."
echo "Config: $CONFIG_PATH"
echo "dws: $DWS_BIN"
echo "codex: $CODEX_BIN"
echo "CLI: $VENV_DIR/bin/codex-avatar"
echo "UI:  $VENV_DIR/bin/codex-avatar-ui"
