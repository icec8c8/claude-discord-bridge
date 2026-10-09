#!/usr/bin/env bash
# Keep `claude remote-control` (server mode) running so the Claude mobile app
# can always reach this machine. Started by the LaunchAgent that
# install_remote_control_launchagent.sh installs — you normally don't run
# this by hand (on Linux you can, inside tmux/screen).
#
# Configuration comes from the environment (the plist sets it):
#   RC_WORKDIR     folder the sessions start in (required). Not $HOME:
#                  Claude Code never saves trust for the home directory, so
#                  the server could not start unattended there.
#   CLAUDE_EXE     path to claude (default: claude on PATH)
#   RC_NAME        optional session title shown in the app
#   RC_EXTRA_ARGS  optional extra flags, e.g. "--permission-mode acceptEdits"
#
# Same behaviour as remote_control_loop.ps1:
#   - stdin is /dev/null, so a missing first-run confirmation or workspace
#     trust fails fast instead of waiting forever on a prompt nobody can see
#   - all output is appended to remote-control.log next to this script
#     (rotated to remote-control.log.1 at 5 MB)
#   - restarts claude whenever it exits (server mode gives up after ~10 min
#     without network): 10 s after a long run, doubling up to 10 min while
#     it keeps failing fast

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$BRIDGE_DIR/remote-control.log"
MAX_LOG_BYTES=$((5 * 1024 * 1024))
CLAUDE_EXE="${CLAUDE_EXE:-claude}"

log() { printf '%s [loop] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }

rotate() {
    [ -f "$LOG" ] || return 0
    local size
    size=$(wc -c <"$LOG")
    if [ "${size// /}" -gt "$MAX_LOG_BYTES" ]; then mv -f "$LOG" "$LOG.1"; fi
}

if [ -z "${RC_WORKDIR:-}" ]; then
    log "RC_WORKDIR is not set"
    exit 1
fi
cd "$RC_WORKDIR" || { log "cannot cd to $RC_WORKDIR"; exit 1; }

# Remote Control only works with a claude.ai login. With any of these set,
# claude authenticates with the key/token instead and the server refuses to
# start, so drop them for this process only.
for var in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN; do
    if [ -n "${!var:-}" ]; then
        log "unsetting $var for this process (Remote Control needs a claude.ai login)"
        unset "$var"
    fi
done

args=(remote-control)
[ -n "${RC_NAME:-}" ] && args+=(--name "$RC_NAME")
read -r -a extra <<<"${RC_EXTRA_ARGS:-}"
args+=(${extra[@]+"${extra[@]}"})

child=""
on_stop() {
    log "stop signal received; stopping claude${child:+ (pid $child)}"
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null
        wait "$child"
    fi
    exit 0
}
trap on_stop TERM INT

delay=10
while true; do
    rotate
    log "starting: $CLAUDE_EXE ${args[*]} (cwd $RC_WORKDIR)"
    started=$(date +%s)
    "$CLAUDE_EXE" "${args[@]}" </dev/null >>"$LOG" 2>&1 &
    child=$!
    wait "$child"
    code=$?
    child=""

    ran=$(( $(date +%s) - started ))
    if [ "$ran" -ge 300 ]; then
        delay=10
    else
        delay=$(( delay * 2 > 600 ? 600 : delay * 2 ))
    fi
    log "exited with code $code after ${ran}s; restarting in ${delay}s"
    # Background sleep + wait so a stop signal is handled right away
    sleep "$delay" &
    wait $!
done
