#!/usr/bin/env bash
# Install the Discord bridge LaunchAgent on a macOS host.
#
# This script reads a plist template, substitutes placeholders with values
# from the current environment, and installs the result into
# ~/Library/LaunchAgents/. It then bootstraps the agent into the Aqua
# session via launchctl.
#
# ⚠️ RUN FROM A GUI TERMINAL (Terminal.app opened on the machine's actual
# desktop, via Screen Sharing / VNC / physical keyboard). Do NOT run from a
# plain SSH shell — launchctl bootstrap into gui/<uid> requires an active
# Aqua session, AND the bridge needs the login keychain unlocked (which
# only happens in Aqua).
#
# Prerequisites:
#   1. ~/.claude-bridge/bridge.py / bridge_mcp.py / .env are in place
#   2. ~/.claude-bridge/venv has discord.py + python-dotenv + mcp[cli]
#   3. Claude Code CLI installed and logged in at least once from the
#      Aqua session (`claude` command works interactively from Terminal.app)
#   4. Any other bridge instance (Windows, Linux) is STOPPED, otherwise
#      the two will fight for the same Discord bot Gateway connection.
#
# Usage:
#   bash ~/.claude-bridge/install_aqua_launchagent.sh

set -euo pipefail

LABEL="dev.local.claude-discord-bridge"
BRIDGE_DIR="$HOME/.claude-bridge"
TEMPLATE="$BRIDGE_DIR/claude-discord-bridge.plist.template"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: template not found at $TEMPLATE" >&2
    exit 1
fi

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then
    echo "ERROR: 'claude' not on PATH. Install Claude Code first:" >&2
    echo "  npm install -g @anthropic-ai/claude-code" >&2
    exit 1
fi
NODE_BIN_DIR="$(dirname "$CLAUDE_BIN")"

# Sanity: warn if running from SSH (no Aqua session)
if [ -n "${SSH_CONNECTION:-}" ] && [ -z "${TERM_PROGRAM:-}" ]; then
    echo "WARNING: you appear to be in an SSH shell."
    echo "         LaunchAgent activation needs an Aqua session for the"
    echo "         login keychain to be unlocked. Open Terminal.app on the"
    echo "         actual macOS desktop (Screen Sharing / VNC / physical)"
    echo "         and re-run this script there."
    echo ""
    read -p "Continue anyway? [y/N] " yn
    case "$yn" in [yY]*) ;; *) exit 1 ;; esac
fi

echo "HOME         : $HOME"
echo "CLAUDE_BIN   : $CLAUDE_BIN"
echo "NODE_BIN_DIR : $NODE_BIN_DIR"
echo "LABEL        : $LABEL"
echo "DESTINATION  : $DST"
echo ""

# Substitute placeholders — use '|' as sed delimiter so paths with / are fine.
# Escape any '|' that might appear in values (paranoid).
esc() { printf '%s' "$1" | sed 's/|/\\|/g'; }

sed \
    -e "s|__HOME__|$(esc "$HOME")|g" \
    -e "s|__CLAUDE_BIN__|$(esc "$CLAUDE_BIN")|g" \
    -e "s|__NODE_BIN_DIR__|$(esc "$NODE_BIN_DIR")|g" \
    "$TEMPLATE" > "$DST.tmp"

# Validate the substituted plist
if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$DST.tmp" >/dev/null || { rm -f "$DST.tmp"; echo "plist invalid"; exit 1; }
fi

mkdir -p "$HOME/Library/LaunchAgents"
mv "$DST.tmp" "$DST"
echo "✓ Installed plist: $DST"

# Bootout previous instance if loaded
if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    echo "Previous instance loaded — bootout first..."
    launchctl bootout "gui/$(id -u)" "$DST" || true
fi

launchctl bootstrap "gui/$(id -u)" "$DST"
echo "✓ Bootstrapped into gui/$(id -u)"

sleep 2
echo ""
echo "=== Status ==="
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | head -30 || echo "(not loaded)"

echo ""
echo "=== Recent bridge logs ==="
tail -20 "$BRIDGE_DIR/bridge.log" 2>/dev/null || echo "(no bridge.log yet)"

echo ""
echo "Done. To unload later:"
echo "  launchctl bootout gui/\$(id -u) $DST"
