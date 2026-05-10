# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A two-file Python project that bridges a private Discord channel to a headless `claude -p` CLI subprocess. The user types `claude> …` in Discord; the bridge spawns Claude in `--resume` mode and posts the reply back as a threaded message. Single-user by design (filters by one Discord user ID + one channel ID).

The README is authoritative for product behavior, security model, and setup. Do not duplicate those here. **Read `README.md` end-to-end before making any change that touches the security boundary** (filters, prefix gate, `--allowedTools`, `--dangerously-skip-permissions`, model cap).

## Architecture

Two peers that share state through files in the bridge directory — they never talk to each other directly.

- **`bridge.py`** — long-running daemon. discord.py Gateway listener feeds an `asyncio.Queue` consumed by `BRIDGE_WORKERS` (default 3) parallel workers. Each worker holds its own persistent Claude session UUID and spawns `claude -p` per message.
- **`bridge_mcp.py`** — short-lived stdio MCP server (FastMCP) launched on demand by an interactive Claude Code session. Exposes 6 tools (`inbox_list`, `outbox_list`, `bridge_status`, `send_message`, `tail_log`, `usage_today`). Reads the same JSONL files the daemon writes; sends Discord messages directly via the REST API rather than going through discord.py.

Shared on-disk state (all gitignored, regenerated at runtime):

| File | Writer | Purpose |
|---|---|---|
| `inbox.jsonl` | bridge.py | Append-only log of accepted Discord messages |
| `outbox.jsonl` | bridge.py | Append-only per-call result + `usage` + `total_cost_usd` |
| `bridge.log` | bridge.py | Plain-text logger output |
| `bridge.pid` | bridge.py | Daemon PID, used by `bridge_status()` for liveness |
| `session_ids.json` | bridge.py | `{"workers": [{"uuid", "seeded"}, …]}` — one entry per worker |
| `bridge_session_id.txt`, `bridge_session_seeded.flag` | legacy | Pre-fan-out single-session state; `_load_sessions()` migrates from these once |

### Worker session lifecycle (the part most likely to bite you)

Each worker has its own Claude session UUID persisted in `session_ids.json`. The CLI distinguishes:
- `--session-id <uuid>` — **create** a new session with this UUID (errors if it already exists)
- `--resume <uuid>` — continue an existing session

`run_claude()` chooses based on the per-worker `seeded` flag, and **seeds-on-spawn**: the moment claude is launched in `create` mode, `seeded` flips to `True` and is persisted, *before* claude returns. This is deliberate — the original failure mode (v2.4 → v2.5) was a killed `--session-id` run leaving the CLI with a half-registered session that blocked every subsequent call.

Two recognized error strings trigger one auto-retry:
- `"already in use"` → flip `seeded=True`, retry with `--resume`
- `"not found"` / `"does not exist"` / `"unknown session"` → rotate UUID, retry as create

See `_classify_session_error()` in `bridge.py`. If you change the CLI's error wording, update both branches.

### Process-group + stall detection

Workers spawn claude with `start_new_session=True` so the entire subprocess tree shares a pgid. `_kill_tree()` does `os.killpg(pgid, SIGTERM/SIGKILL)` — required because claude spawns find/grep/bash children that would otherwise orphan on a parent-only kill.

`_watch()` samples CPU time across the tree (via `psutil`) every `PROGRESS_INTERVAL` seconds. If CPU% stays below `STALL_CPU_THRESHOLD` for `STALL_WINDOW_MIN` consecutive samples, the run is declared stalled and the tree is killed with a 🚨 Discord reply. The first heartbeat is a "primer" — it doesn't compute CPU% (no baseline yet).

### Module-load environment requirements

`bridge.py` and `bridge_mcp.py` both read `DISCORD_BOT_TOKEN`, `ALLOWED_USER_ID`, `ALLOWED_CHANNEL_ID` from `os.environ` **at import time** via `load_dotenv(BRIDGE_DIR / ".env")`. Importing without these set raises `KeyError`. The CI smoke test uses dummy values (`ci-dummy-not-a-real-token`, `'0'`, `'0'`) — do the same in any new test or import-time check.

### `pythonw.exe` stdout

When launched by Windows Task Scheduler, `sys.stdout` is `None`. The conditional handler in `bridge.py:125-131` exists to prevent `logging.StreamHandler(sys.stdout)` from crashing on the first log line. Don't unconditionally `print()` from this code path; if you add a library that does, redirect stdout to `os.devnull` before importing it.

### `_pid_alive()` is Windows-only

`bridge_mcp.py:_pid_alive()` shells out to `tasklist`, which only exists on Windows. On macOS/Linux it returns `False`, so `bridge_status().alive` will always read False there even when the daemon is running. Keep this in mind before "fixing" status checks — make any cross-platform replacement preserve the existing behavior on Windows.

## Common commands

```bash
# Local dev environment (matches what users do per README)
python -m venv venv
./venv/bin/pip install -r requirements.txt

# Run the daemon in the foreground (needs a populated .env)
./venv/bin/python bridge.py

# CI-equivalent checks — run these before pushing
python -m py_compile bridge.py bridge_mcp.py

DISCORD_BOT_TOKEN=ci-dummy ALLOWED_USER_ID=0 ALLOWED_CHANNEL_ID=0 \
  python -c "import bridge, bridge_mcp; \
             tools=list(bridge_mcp.app._tool_manager._tools.keys()); \
             print(tools); assert len(tools)==6"

# Secret scan (CI fails the build on any hit)
git ls-files | xargs grep -lnE "MT[A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}" 2>/dev/null
git ls-files | xargs grep -lnE "sk-[A-Za-z0-9_-]{40,}" 2>/dev/null
git ls-files | xargs grep -lnE "AKIA[0-9A-Z]{16}" 2>/dev/null
```

There is no test suite, no linter, and no formatter configured. CI (`.github/workflows/ci.yml`) runs three jobs: syntax + import smoke across Python 3.11/3.12/3.13, secret scan, and a docs-structure check that requires all three READMEs (`README.md`, `README.zh-TW.md`, `README.ja.md`) plus `SECURITY.md`, `LICENSE`, `.env.example`, `.gitignore` to exist and that each README links to all three language variants.

## Conventions and gotchas

- **The MCP tool count is asserted in CI.** Adding or removing a `@app.tool()` in `bridge_mcp.py` requires updating the `expected = {…}` set in `.github/workflows/ci.yml`.
- **Three READMEs must stay in sync.** Any user-facing change in `README.md` should be mirrored in `README.zh-TW.md` and `README.ja.md`, or CI's docs job will still pass (it only checks structure) but the translations will silently drift. If a change is non-trivial and you can't translate it, flag it for the user.
- **`DEFAULT_ALLOWED_TOOLS` in `bridge.py:80` is broader than the README documents.** The code default includes `Bash Edit Write`; the README claims a read-only default of `Read Grep Glob WebFetch WebSearch Task TodoWrite`. This is a known divergence. Don't "fix" one to match the other without asking — the user may have intentionally widened the code default for their own deployment while keeping the README conservative for downstream forks.
- **`--dangerously-skip-permissions` is passed on every claude invocation** (`bridge.py:362`). Combined with the wide code default above, the only real safety boundary is the Discord user/channel filter and the prefix gate. Treat any change to `on_message()` filters, `BRIDGE_PREFIX` handling, or `ALLOWED_TOOLS_LIST` construction as security-sensitive.
- **Rate limiter and daily cost cap fire before the prefix gate.** Order in `on_message()`: author → channel → cost cap → rate limit → prefix → enqueue. Don't reorder without thinking through the consequence (e.g., moving the prefix gate earlier means typo'd messages count against the rate limit).
- **`outbox.jsonl` is the source of truth for cost.** `get_daily_cost_usd()` and the `usage_today` MCP tool both stream-parse it. Schema changes to the entry written in `worker()` (`bridge.py:503-511`) will silently break cost tracking — keep the `ts`, `ok`, `usage.total_cost_usd`, `usage.model` keys stable.
- **Heartbeat messages are real Discord sends.** They count against the rate limit on Discord's side and clutter the channel. Don't lower `PROGRESS_INTERVAL` below 15s without a reason — the bridge enforces a 15s floor (`bridge.py:69`).
- **Don't commit `.env`, `bridge.log`, `inbox.jsonl`, `outbox.jsonl`, `bridge.pid`, `session_ids.json`, or any `bridge_session_*` files.** They're gitignored and CI explicitly fails if any of them get tracked.
- **No emojis in code unless they were already there.** The existing emojis (📥 🤖 ✅ ❌ 👀 ⏸ ⏳ 🚨 💰 🛑 ⚠️) are part of the user-visible UX; new ones should only be added when the user asks.
