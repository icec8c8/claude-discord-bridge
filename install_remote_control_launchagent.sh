#!/usr/bin/env bash
# Install a macOS LaunchAgent that keeps `claude remote-control` running, so
# the Claude mobile app (Code tab) or claude.ai/code can start Claude Code
# sessions on this Mac.
#
# ⚠️ Same rule as install_aqua_launchagent.sh: RUN FROM A GUI TERMINAL
# (Terminal.app on the actual desktop), not a plain SSH shell — Claude Code's
# login lives in the login keychain, which is only unlocked in Aqua.
#
# ONE-TIME SETUP FIRST (needs a real terminal, cannot be done unattended):
#   mkdir -p ~/claude-remote && cd ~/claude-remote
#   claude remote-control
#   -> answer y to "Enable Remote Control?" and to "Trust <folder>?",
#      wait until the session URL shows, then press Ctrl+C
#
# Usage:
#   bash install_remote_control_launchagent.sh
#   bash install_remote_control_launchagent.sh --workdir ~/projects
#   bash install_remote_control_launchagent.sh --name "Mac mini" --extra-args "--permission-mode acceptEdits"
#   bash install_remote_control_launchagent.sh --uninstall
#
# Options:
#   --workdir DIR      folder the sessions start in (default: ~/claude-remote).
#                      Not $HOME itself: Claude Code never saves trust for it,
#                      so the server could not start unattended there.
#   --name NAME        session title shown in the app (default: <hostname>-<random>)
#   --extra-args ARGS  extra `claude remote-control` flags, plain words only

set -euo pipefail

LABEL="dev.local.claude-remote-control"
BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$BRIDGE_DIR/claude-remote-control.plist.template"
LOOP="$BRIDGE_DIR/remote_control_loop.sh"
LOG="$BRIDGE_DIR/remote-control.log"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

WORKDIR="$HOME/claude-remote"
NAME=""
EXTRA_ARGS=""
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --workdir)    WORKDIR="$2"; shift 2 ;;
        --name)       NAME="$2"; shift 2 ;;
        --extra-args) EXTRA_ARGS="$2"; shift 2 ;;
        --uninstall)  UNINSTALL=1; shift ;;
        -h|--help)    sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown option $1 (see --help)" >&2; exit 1 ;;
    esac
done

if [ "$UNINSTALL" = 1 ]; then
    if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)" "$DST" || true
        echo "✓ Unloaded $LABEL (claude remote-control stopped)"
    fi
    rm -f "$DST"
    echo "✓ Removed $DST"
    exit 0
fi

for f in "$TEMPLATE" "$LOOP"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: $f not found" >&2
        exit 1
    fi
done

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then
    echo "ERROR: 'claude' not on PATH. Install Claude Code first." >&2
    exit 1
fi

if [ ! -d "$WORKDIR" ]; then
    echo "ERROR: $WORKDIR does not exist. Do the one-time setup at the top of this script first." >&2
    exit 1
fi
WORKDIR="$(cd "$WORKDIR" && pwd)"
if [ "$WORKDIR" = "$(cd "$HOME" && pwd)" ]; then
    echo "ERROR: --workdir cannot be your home directory: Claude Code never saves" >&2
    echo "       trust for it, so the server could not start unattended." >&2
    echo "       Use a subfolder such as ~/claude-remote." >&2
    exit 1
fi

# The "Enable Remote Control?" answer is stored as remoteDialogSeen in
# ~/.claude.json. Without it the server can't start unattended.
GLOBAL_CONFIG="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"
if ! grep -Eq '"remoteDialogSeen"[[:space:]]*:[[:space:]]*true' "$GLOBAL_CONFIG" 2>/dev/null; then
    echo "WARNING: Remote Control has not been enabled on this Mac yet. Run this once first:"
    echo "  cd \"$WORKDIR\" && claude remote-control"
    echo "  (answer y to both questions, wait for the URL, then press Ctrl+C)"
    read -r -p "Continue anyway? [y/N] " yn
    case "$yn" in [yY]*) ;; *) exit 1 ;; esac
fi

# Sanity: warn if running from SSH (no Aqua session)
if [ -n "${SSH_CONNECTION:-}" ] && [ -z "${TERM_PROGRAM:-}" ]; then
    echo "WARNING: you appear to be in an SSH shell."
    echo "         LaunchAgent activation needs an Aqua session for the"
    echo "         login keychain to be unlocked. Open Terminal.app on the"
    echo "         actual macOS desktop and re-run this script there."
    echo ""
    read -r -p "Continue anyway? [y/N] " yn
    case "$yn" in [yY]*) ;; *) exit 1 ;; esac
fi

echo "BRIDGE_DIR  : $BRIDGE_DIR"
echo "CLAUDE_BIN  : $CLAUDE_BIN"
echo "WORKDIR     : $WORKDIR"
echo "LABEL       : $LABEL"
echo "DESTINATION : $DST"
echo ""

mkdir -p "$HOME/Library/LaunchAgents"
cp "$TEMPLATE" "$DST.tmp"
set_key() { plutil -replace "$1" -string "$2" "$DST.tmp"; }
set_key ProgramArguments.1              "$LOOP"
set_key WorkingDirectory                "$BRIDGE_DIR"
set_key StandardOutPath                 "$BRIDGE_DIR/remote-control.launchd.log"
set_key StandardErrorPath               "$BRIDGE_DIR/remote-control.launchd.log"
set_key EnvironmentVariables.PATH       "$PATH"
set_key EnvironmentVariables.CLAUDE_EXE "$CLAUDE_BIN"
set_key EnvironmentVariables.RC_WORKDIR "$WORKDIR"
set_key EnvironmentVariables.RC_NAME    "$NAME"
set_key EnvironmentVariables.RC_EXTRA_ARGS "$EXTRA_ARGS"

plutil -lint "$DST.tmp" >/dev/null || { rm -f "$DST.tmp"; echo "plist invalid" >&2; exit 1; }
mv "$DST.tmp" "$DST"
echo "✓ Installed plist: $DST"

# Bootout previous instance if loaded, so two servers never share one folder
if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    echo "Previous instance loaded — bootout first..."
    launchctl bootout "gui/$(id -u)" "$DST" || true
    sleep 2
fi

launchctl bootstrap "gui/$(id -u)" "$DST"
echo "✓ Bootstrapped into gui/$(id -u)"

sleep 20
echo ""
echo "=== Last lines of remote-control.log ==="
tail -20 "$LOG" 2>/dev/null || echo "(no log yet)"

echo ""
echo "If you see 'Workspace not trusted', 'Enable Remote Control?' or a login error,"
echo "fix it in a normal terminal (see the top of this script) and re-run the installer."
echo "Otherwise open the Claude app -> Code: this Mac shows up with a green dot."
echo ""
echo "Log     : $LOG"
echo "Restart : launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "Remove  : bash $0 --uninstall"
