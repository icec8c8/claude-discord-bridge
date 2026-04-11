# Claude Discord Bridge

[![CI](https://github.com/icec8c8/claude-discord-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/icec8c8/claude-discord-bridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/icec8c8/claude-discord-bridge)](https://github.com/icec8c8/claude-discord-bridge/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**[English](README.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md)**

A tiny daemon that lets you chat with a headless [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI from a private Discord channel, so you can drive your existing Claude subscription from anywhere your phone has Discord. No extra API key, no extra billing.

Type something like this in your Discord channel:

```
claude> read the project README and tell me what to work on next
```

…and a few seconds later Claude's reply shows up as a threaded message. Works on Windows (Scheduled Task) or macOS (LaunchAgent).

## Table of contents

1. [Architecture](#architecture)
2. [Features](#features)
3. [Requirements](#requirements)
4. [Setup](#setup) — Discord bot · Install · Windows auto-start · macOS auto-start
5. [Configuration](#configuration) — full list of environment variables
6. [Security model](#security-model) — **READ THIS BEFORE WIDENING THE TOOL WHITELIST**
7. [Validation checklist](#validation-checklist) — how to verify safety before use
8. [MCP server](#mcp-server)
9. [Troubleshooting](#troubleshooting) — the five dead-ends we hit while building this
10. [License](#license)

## Architecture

```
   Discord channel
        │
        ▼
  bridge.py (discord.py Gateway listener; pythonw.exe / launchd)
        │  filter user_id + channel_id  +  prefix gate "claude>"
        ▼
  inbox.jsonl  +  📥 reaction
        │
        ▼
  asyncio.Queue  ──►  worker  ──►  claude -p --resume <uuid>
                                      │    --output-format json
                                      │    --allowedTools <read-only whitelist>
                                      │    --model sonnet
                                      ▼
                                  result + usage + cost
                                      │
                                      ▼
                              Discord reply  +  ✅/❌ reaction
                              outbox.jsonl (per-call stats)
```

The headless `claude -p` subprocess is a **completely separate conversation** from any interactive Claude Code session you have open. It uses the same subscription, but it's a second universe — so triggering the bridge never touches a live TUI session.

## Features

- **Auto-reply** — every qualifying Discord message spawns a headless `claude -p` call and posts the response back as a threaded reply.
- **Persistent session** — a fixed session UUID (`bridge_session_id.txt`) means the headless Claude remembers previous Discord turns via `--resume`.
- **Prefix gate** — messages must start with `claude>` (configurable). Random chatter and compromised-account typing are ignored with a 👀 reaction.
- **Read-only tool whitelist by default** — `--allowedTools` is set to `Read Grep Glob WebFetch WebSearch Task TodoWrite`. No `Bash`, no `Edit`, no `Write`. You can widen it but **read the Security section first**.
- **Sonnet default** — `--model sonnet` is ~3× cheaper than Opus against Max-subscription rate limits. Override via `CLAUDE_MODEL`.
- **Token accounting** — every call captures `usage` and `total_cost_usd` from `--output-format json`. Daily totals are exposed via the bundled MCP server.
- **MCP server** — a sibling `bridge_mcp.py` exposes six tools (`inbox_list`, `outbox_list`, `bridge_status`, `send_message`, `tail_log`, `usage_today`) so another Claude Code session can inspect and control the bridge.
- **Auto-start** — Windows Scheduled Task at logon, or macOS LaunchAgent in the Aqua session.
- **Single-user by design** — filters by one Discord user ID and one channel ID. Everything else is hard-dropped.

## Requirements

| | |
|---|---|
| OS | Windows 10/11, macOS 13+ (Linux should work, LaunchAgent step excluded) |
| Python | 3.11+ (tested on 3.11 Windows and 3.13 macOS Homebrew) |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code`, logged in at least once |
| Node.js | only if you install Claude Code via npm |
| Discord developer account | to create your own bot |
| A private Discord channel | where the bot can read messages and post replies |

You also need an active Claude subscription (Max, Team, Console/API). The bridge uses whatever authentication your local `claude` CLI already has; it never asks you for an Anthropic API key.

## Setup

### Step 1: Create a Discord bot

1. Go to <https://discord.com/developers/applications> and click **New Application**. Give it any name.
2. Open the **Bot** tab. Click **Reset Token** and copy the token. This is the only chance you get to see the token — store it securely in a password manager.
3. On the same **Bot** page, scroll to **Privileged Gateway Intents** and enable **MESSAGE CONTENT INTENT**. Save changes. Without this, the bot cannot read your message text and the bridge crashes at startup with `PrivilegedIntentsRequired`.
4. Go to **OAuth2 → URL Generator**. Select the `bot` scope. In the bot permissions below, check `View Channels`, `Send Messages`, `Read Message History`. Do NOT check `Administrator` — the bridge does not need it. Copy the generated URL, paste it in a browser, and authorize the bot into your guild.

### Step 2: Find the IDs you need

1. In your Discord client, enable **User Settings → Advanced → Developer Mode**.
2. Right-click your own username anywhere and choose **Copy User ID**. This is `ALLOWED_USER_ID`.
3. Right-click the channel you want to use as the bridge channel and choose **Copy Channel ID**. This is `ALLOWED_CHANNEL_ID`.

### Step 3: Install the bridge

```bash
# Clone into a persistent location in your home directory
git clone https://github.com/<you>/claude-discord-bridge.git ~/.claude-bridge
cd ~/.claude-bridge

# Create a Python virtual environment
python -m venv venv

# Install dependencies
# Windows:
venv\Scripts\pip install -r requirements.txt
# macOS/Linux:
./venv/bin/pip install -r requirements.txt

# Copy the environment template and fill in your secrets
cp .env.example .env
# Edit .env and paste your DISCORD_BOT_TOKEN, ALLOWED_USER_ID, ALLOWED_CHANNEL_ID
```

On macOS/Linux you should now `chmod 600 .env` so the token file is owner-only.

On Windows, NTFS inheritance normally keeps your home directory private; if you're unsure, run `icacls .env /inheritance:r /grant:r "%USERNAME%:F"` to make the file readable only by your user.

### Step 4: Smoke-test the bridge

```bash
# Windows
venv\Scripts\python.exe bridge.py

# macOS / Linux
./venv/bin/python bridge.py
```

You should see `connected as <botname>` in `bridge.log` within ~2 seconds. Then go to Discord and send this in the configured channel:

```
claude> say the word OK and nothing else
```

The bot should react 📥 → 🤖 → ✅ and reply with `OK`. If that works, stop the process with `Ctrl+C` and move on to auto-start.

### Step 5a: Windows auto-start

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
```

This creates a Scheduled Task called `ClaudeDiscordBridge`. It runs `pythonw.exe bridge.py` at every user logon, hidden from the task bar, with restart-on-failure up to 999 times at one-minute intervals. No administrator rights are required — it is scoped to your own user.

Control the task with:

```powershell
Start-ScheduledTask      -TaskName ClaudeDiscordBridge
Stop-ScheduledTask       -TaskName ClaudeDiscordBridge
Disable-ScheduledTask    -TaskName ClaudeDiscordBridge
Get-ScheduledTask        -TaskName ClaudeDiscordBridge
Get-ScheduledTaskInfo    -TaskName ClaudeDiscordBridge
Unregister-ScheduledTask -TaskName ClaudeDiscordBridge -Confirm:$false
```

### Step 5b: macOS auto-start (Aqua session)

**This step must be run from a GUI Terminal.app window, NOT from an SSH shell.** macOS login keychain is locked in SSH contexts, and Claude Code stores its credentials there — running `claude -p` outside of an Aqua session fails with `Not logged in`. Use Screen Sharing, VNC, or a physical keyboard to get an actual desktop session, open Terminal.app there, and then:

```bash
bash install_aqua_launchagent.sh
```

The script reads your `$HOME`, locates `claude` via `which claude`, substitutes placeholders in `claude-discord-bridge.plist.template`, writes the result to `~/Library/LaunchAgents/dev.local.claude-discord-bridge.plist`, validates it with `plutil -lint`, and bootstraps it into `gui/$(id -u)`.

Before running the macOS installer, **stop the Windows scheduled task** (`Stop-ScheduledTask` + `Disable-ScheduledTask`) if you are running on Windows too. A Discord bot token can only have one Gateway connection at a time; two bridges with the same token will fight and cycle each other.

## Configuration

Everything knob lives in `.env`. See `.env.example` for the full list with inline documentation. The important variables:

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | — | yes | Your bot token from the Developer Portal |
| `ALLOWED_USER_ID` | — | yes | Only messages from this Discord user ID are processed |
| `ALLOWED_CHANNEL_ID` | — | yes | Only messages in this channel are processed |
| `BRIDGE_PREFIX` | `claude>` | no | Required prefix on every message; `""` disables the gate (not recommended) |
| `CLAUDE_MODEL` | `sonnet` | no | Alias or full model name |
| `CLAUDE_ALLOWED_TOOLS` | `Read Grep Glob WebFetch WebSearch Task TodoWrite` | no | Space-separated tool whitelist passed to `--allowedTools` |
| `CLAUDE_EXE` | `claude` | no | Path to the Claude Code binary; `claude` relies on `$PATH` |
| `CLAUDE_TIMEOUT` | `300` | no | Per-call timeout in seconds |
| `CLAUDE_CWD` | bridge dir | no | Working directory for the headless claude subprocess |

## Security model

The bridge has five layered defenses. **Before you widen any of them, read this section end-to-end.**

### Layered defenses

1. **User ID filter.** Only messages authored by `ALLOWED_USER_ID` are processed. Everything else is silently dropped at the very top of `on_message()`.
2. **Channel ID filter.** Only messages from `ALLOWED_CHANNEL_ID` are processed. A bot added to a server can see many channels; this filter pins the bridge to a single dedicated channel.
3. **Prefix gate.** Messages must start with `BRIDGE_PREFIX` (default `claude>`). Anything else gets a 👀 reaction and is dropped. This protects two things: accidental typos becoming real Claude turns, and a compromised Discord account typing innocuous-looking chatter that would otherwise be interpreted as commands.
4. **Tool whitelist.** The headless claude is invoked with `--allowedTools` restricted to read/search operations only. No `Bash`, no `Edit`, no `Write`, no `NotebookEdit`. The bridge *cannot* modify files on disk or execute shell commands unless you explicitly widen the whitelist in `.env`.
5. **Model cap.** `--model sonnet` by default limits per-call cost and capability relative to Opus.

### Things the bridge **can** do even with the default whitelist

- **Read any file the running user can read.** The `Read` tool is in the whitelist. If an attacker with your Discord account types `claude> read ~/.ssh/id_rsa`, the contents come back in a Discord reply. If that matters to you, add a custom `--allowedTools` list that drops `Read` or restrict it via path allow-listing in your project's Claude Code settings.
- **Fetch arbitrary URLs.** `WebFetch` is in the whitelist. An attacker could ask the bridge to read a local file and exfiltrate it via a `WebFetch` to a URL they control. If that matters, drop `WebFetch` from `CLAUDE_ALLOWED_TOOLS`.
- **Run web searches.** `WebSearch` is in the whitelist. Mostly harmless but does burn rate limits.
- **Spend your Claude subscription credit.** Any Discord message triggers a real Claude call that consumes tokens against your Max/Team/Console quota. Rate-limit yourself by setting a short per-day token budget (see `usage_today` MCP tool) or adding a cooldown around `on_message`.

### Things the bridge **cannot** do with the default whitelist

- Execute shell commands (`Bash` is excluded).
- Modify or create files on disk (`Edit`, `Write`, `NotebookEdit` are excluded).
- Send messages to channels other than `ALLOWED_CHANNEL_ID` through `discord.py`.
- Access a different user's files (it runs as your user and is bound by OS permissions).

### Safely widening the whitelist

If you want the bridge to do more, only add tools one at a time and test each addition from a separate test channel first. Example — add `Bash` only for `git status` / `git log`:

```
CLAUDE_ALLOWED_TOOLS=Read Grep Glob WebFetch WebSearch Task TodoWrite "Bash(git status:*)" "Bash(git log:*)"
```

The per-tool glob syntax `ToolName(pattern)` is Claude Code's standard fine-grained permission format. Prefer narrow globs over `Bash(*:*)`.

### Before you `git push` — repository security

This repo is designed to be public, but **you** are responsible for verifying that *your* clone contains no secrets before pushing anywhere. Checklist:

```bash
# 1. Make sure .env is git-ignored and not tracked
git check-ignore -v .env
git ls-files | grep -i '\.env$'  # should print only .env.example

# 2. Grep the tracked files for anything that looks like a token
git ls-files | xargs grep -nE 'MT[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|(token|secret|key|password)[[:space:]]*=' 2>/dev/null

# 3. Grep tracked files for your personal IDs
git ls-files | xargs grep -nE '<your-discord-user-id>|<your-channel-id>|<your-username>' 2>/dev/null

# 4. Inspect git history in case something was committed once and removed later
git log --all --full-history -p -- .env 2>/dev/null
git log --all -p | grep -nE 'MT[A-Za-z0-9]{20,}|sk-' 2>/dev/null
```

All four checks should come back empty. If `git log` shows a previously committed secret, **do not just delete it and commit again** — the secret is still in history. Rewrite history with `git filter-repo` or BFG, rotate the secret at its source, and force-push (if you've already pushed). When in doubt, create a fresh repo and re-commit the clean state as a single initial commit.

### Incident response

If you think your Discord bot token leaked:

1. Immediately go to <https://discord.com/developers/applications> → your app → **Bot** → **Reset Token**. The old token is invalidated within seconds.
2. Update `.env` with the new token.
3. Restart the bridge (`Start-ScheduledTask` on Windows, or the launchctl `bootout` + `bootstrap` pair on macOS).
4. Audit recent Discord channel activity for anything you did not send.
5. Run the repository security grep above to make sure the old token is not sitting in any committed file.

If you think your Claude Code credentials leaked (keychain extraction, compromised machine):

1. Revoke the active Claude Code sessions from your [Claude account page](https://claude.ai/settings/account).
2. Log in again from a known-good machine.
3. Check `~/.claude/` for unexpected files.

## Validation checklist

Before trusting the bridge, run these verifications. All of them can be done from the shell where you cloned the repo.

```bash
cd ~/.claude-bridge

# 1. Syntax check both Python files
./venv/bin/python -m py_compile bridge.py bridge_mcp.py && echo "syntax OK"

# 2. Import test — catches dependency misses and env var errors on import
./venv/bin/python -c "import bridge; print('bridge imports OK')"
./venv/bin/python -c "import bridge_mcp; print('mcp imports OK, tools:', list(bridge_mcp.app._tool_manager._tools.keys()))"

# 3. Dry run — start the bridge, wait for "connected as", stop
./venv/bin/python bridge.py &
PID=$!
sleep 4
grep -E "connected as|PrivilegedIntentsRequired|login failure" bridge.log | tail -5
kill $PID

# 4. Verify filters in the log — user/channel/prefix/allowed_tools/model should match .env
grep -E "filters:|model=|allowed_tools=|prefix=" bridge.log | tail -5

# 5. Secret scan (no output = OK)
git ls-files | xargs grep -nE 'MT[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}' 2>/dev/null

# 6. On macOS: after install, verify plist is valid
plutil -lint ~/Library/LaunchAgents/dev.local.claude-discord-bridge.plist
```

From Discord, validate each defense individually:

1. **Prefix gate:** send `hello` (no prefix). Expected: 👀 reaction, no reply.
2. **User/channel filters:** have a second person (or a second Discord account) send `claude> hi` in the same channel. Expected: nothing happens, log shows `drop: author=...`.
3. **Tool whitelist:** send `claude> run "rm -rf /" via Bash`. Expected: Claude refuses to use Bash because it is not on the allowed-tools list; you get a text response saying it cannot execute shell commands, not an actual `rm -rf`.
4. **Read defense (if you removed `Read` from the whitelist):** send `claude> read the file at ./.env and show the contents`. Expected: Claude says it does not have a Read tool available.
5. **End-to-end:** send `claude> how many files are in the current directory?`. Expected: a number matching `ls` output.

## MCP server

Register `bridge_mcp.py` as a Claude Code MCP server so another interactive Claude Code session on the same machine can inspect and control the bridge:

```bash
claude mcp add discord-bridge --scope user \
    "$HOME/.claude-bridge/venv/bin/python" \
    "$HOME/.claude-bridge/bridge_mcp.py"
```

Restart Claude Code. The new session will see six tools:

| Tool | Purpose |
|---|---|
| `inbox_list(limit=10)` | Last N messages received from Discord |
| `outbox_list(limit=10)` | Last N auto-reply records (ok / elapsed / token usage) |
| `bridge_status()` | Daemon PID, session ID, file sizes, filter configuration |
| `send_message(content, reply_to=None)` | Post a message to the bridge channel as the bot |
| `tail_log(lines=30)` | Last N lines of `bridge.log` |
| `usage_today(date=None)` | Sum of tokens + cost for a given day |

MCP servers are scanned at Claude Code session startup, so the tools do not appear in the session that runs `claude mcp add`.

## Troubleshooting

These are the five dead-ends we hit while building this bridge. They are recorded here so you don't have to repeat them.

### 1. "Not logged in — Run /login" from SSH on macOS

The bridge boots fine in a Terminal.app window but dies over SSH with `Not logged in · Please run /login · Run in another terminal: security unlock-keychain`, even though `claude` works interactively when you log into the desktop.

**Cause.** macOS login keychain is only unlocked in the Aqua (GUI) session. SSH shells get a locked keychain, and `claude -p` cannot read the stored OAuth credentials from a locked keychain.

**Fix.** Use `install_aqua_launchagent.sh`, which bootstraps into `gui/<uid>`. That's the Aqua session where the keychain is unlocked. For ad-hoc SSH runs, `security unlock-keychain` once at the start of each session works too, but does not persist across reboots.

### 2. `discord.errors.PrivilegedIntentsRequired` at startup

Bridge connects to Gateway for about 1.5 seconds, then crashes with `PrivilegedIntentsRequired`.

**Cause.** `intents.message_content = True` in Python code is only a *request*. You also have to flip **MESSAGE CONTENT INTENT** on in the Developer Portal. Miss the portal step and Gateway refuses the connection.

**Fix.** Developer Portal → your application → Bot → Privileged Gateway Intents → enable Message Content Intent → Save Changes → restart the bridge.

### 3. `Session ID <uuid> is already in use`

First Discord message succeeds, second fails with this error.

**Cause.** `claude --session-id <uuid>` means *create a new session with this UUID*, not *resume*. Once the session file exists, reusing `--session-id` errors out.

**Fix.** The bridge keeps a `bridge_session_seeded.flag` sentinel file. The first call uses `--session-id` to create; every subsequent call uses `--resume <uuid>` to continue. To start a brand-new conversation, delete the flag file and the corresponding `.jsonl` under `~/.claude/projects/<cwd-hash>/`.

### 4. `AttributeError: 'NoneType' object has no attribute 'write'` under `pythonw.exe`

The bridge works fine under `python.exe` in a console window, but crashes immediately when Task Scheduler launches it via `pythonw.exe`.

**Cause.** `pythonw.exe` is the Windows GUI-subsystem Python. It has no console, so `sys.stdout`, `sys.stderr`, and `sys.stdin` are all `None`. `logging.StreamHandler(sys.stdout)` crashes the moment the first log line is emitted.

**Fix.** Make the stream handler conditional on a real stdout. The bridge already does this:

```python
_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    try:
        sys.stdout.fileno()
        _handlers.append(logging.StreamHandler(sys.stdout))
    except (OSError, AttributeError, ValueError):
        pass
```

If you add a third-party library that `print()`s to stdout directly, redirect `sys.stdout = open(os.devnull, 'w')` before importing it.

### 5. `error: externally-managed-environment` on macOS Homebrew Python

`pip install discord.py` fails with a PEP 668 "externally-managed-environment" error on Homebrew Python.

**Cause.** Homebrew's Python is marked as system-managed to protect the Homebrew environment from accidental `pip install` pollution.

**Fix.** Use a virtual environment. The setup instructions already do this (`python -m venv venv`). Do not shortcut with `--user` or `--break-system-packages`.

## How this repository was built

The entire bridge was pair-programmed with [Claude Code](https://docs.claude.com/en/docs/claude-code) in a single interactive session. The conversation began with "can I control Claude from Discord?" and ended with a hardened, auto-starting daemon plus MCP server plus security audit plus this README. The five troubleshooting entries above are genuine dead-ends the pair programming hit live.

## License

MIT. See [LICENSE](LICENSE).
