"""
MCP server for the Discord <-> Claude Code bridge.

Runs as a stdio MCP server (launched on demand by Claude Code).
Exposes tools for the LIVE Claude Code session 總裁 is in to:
  - inspect Discord traffic the bridge daemon has handled
  - check daemon health
  - send messages to the configured Discord channel without curl

This is a peer / sibling of bridge.py:
  bridge.py        — long-running daemon, listens to Discord, calls claude -p
  bridge_mcp.py    — short-lived MCP child process, exposes file/HTTP helpers

They share state via files in BRIDGE_DIR:
  inbox.jsonl              — what 總裁 has sent through Discord
  outbox.jsonl             — what the bridge has auto-replied
  bridge.pid               — daemon PID (used for liveness check)
  bridge_session_id.txt    — fixed UUID of headless claude session
  bridge_session_seeded.flag — set after the session is created
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

BRIDGE_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(BRIDGE_DIR / ".env")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ALLOWED_CHANNEL_ID = int(os.environ["ALLOWED_CHANNEL_ID"])
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

INBOX = BRIDGE_DIR / "inbox.jsonl"
OUTBOX = BRIDGE_DIR / "outbox.jsonl"
LOG_FILE = BRIDGE_DIR / "bridge.log"
PID_FILE = BRIDGE_DIR / "bridge.pid"
SESSION_ID_FILE = BRIDGE_DIR / "bridge_session_id.txt"
SESSION_SEEDED = BRIDGE_DIR / "bridge_session_seeded.flag"

app = FastMCP("discord-bridge")


def _read_jsonl_tail(path: pathlib.Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:]


def _pid_alive(pid: int) -> bool:
    """Check if a Windows PID is alive via tasklist."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        return f'"{pid}"' in r.stdout
    except Exception:
        return False


@app.tool()
def inbox_list(limit: int = 10) -> dict:
    """List the most recent Discord messages received by the bridge daemon.

    Returns JSON with the last N entries from inbox.jsonl, plus the total count.
    Use this to see what 總裁 has sent through Discord recently.

    Args:
        limit: How many recent messages to return (default 10, max 200).
    """
    limit = max(1, min(200, limit))
    msgs = _read_jsonl_tail(INBOX, limit)
    total = sum(1 for _ in INBOX.open("r", encoding="utf-8")) if INBOX.exists() else 0
    return {"total": total, "returned": len(msgs), "messages": msgs}


@app.tool()
def outbox_list(limit: int = 10) -> dict:
    """List the most recent auto-replies the bridge has dispatched (status records).

    Each entry tells you whether the underlying `claude -p` call succeeded,
    its response length, and how long it took.

    Args:
        limit: How many recent records to return (default 10, max 200).
    """
    limit = max(1, min(200, limit))
    rows = _read_jsonl_tail(OUTBOX, limit)
    total = sum(1 for _ in OUTBOX.open("r", encoding="utf-8")) if OUTBOX.exists() else 0
    return {"total": total, "returned": len(rows), "records": rows}


@app.tool()
def bridge_status() -> dict:
    """Check the bridge daemon's health: PID alive, session info, file sizes.

    Use this to verify bridge.py is actually running before assuming Discord
    messages will be picked up.
    """
    pid = None
    alive = False
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            alive = _pid_alive(pid)
        except Exception:
            pass
    return {
        "pid": pid,
        "alive": alive,
        "session_id": SESSION_ID_FILE.read_text().strip() if SESSION_ID_FILE.exists() else None,
        "session_seeded": SESSION_SEEDED.exists(),
        "inbox_size_bytes": INBOX.stat().st_size if INBOX.exists() else 0,
        "outbox_size_bytes": OUTBOX.stat().st_size if OUTBOX.exists() else 0,
        "log_size_bytes": LOG_FILE.stat().st_size if LOG_FILE.exists() else 0,
        "bridge_dir": str(BRIDGE_DIR),
        "channel_id": str(ALLOWED_CHANNEL_ID),
        "allowed_user_id": str(ALLOWED_USER_ID),
    }


@app.tool()
def send_message(content: str, reply_to: str | None = None) -> dict:
    """Post a message to the configured Discord channel via the bridge bot.

    Use this when you want to push something to 總裁 from the live Claude
    Code session — for example, "fyi I just finished X". The message arrives
    as the same bot that handles auto-replies.

    Args:
        content: Markdown text to send. Discord limit is 2000 chars per message;
                 longer text will be rejected by Discord (you should chunk first).
        reply_to: Optional Discord message id to reply (引用) to.
    """
    if len(content) > 2000:
        return {"ok": False, "error": f"content too long: {len(content)} > 2000"}
    body: dict = {"content": content}
    if reply_to:
        body["message_reference"] = {"message_id": reply_to}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{ALLOWED_CHANNEL_ID}/messages",
        data=data,
        headers={
            "Authorization": f"Bot {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "ClaudeCode-Bridge-MCP/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        return {
            "ok": True,
            "message_id": resp.get("id"),
            "channel_id": resp.get("channel_id"),
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "http_status": e.code, "error": e.read().decode("utf-8", "replace")[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.tool()
def tail_log(lines: int = 30) -> dict:
    """Tail the last N lines of bridge.log for diagnostics.

    Args:
        lines: How many lines from the end (default 30, max 500).
    """
    lines = max(1, min(500, lines))
    if not LOG_FILE.exists():
        return {"lines": [], "error": "log file does not exist"}
    with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    tail = all_lines[-lines:]
    return {"returned": len(tail), "lines": [ln.rstrip("\n") for ln in tail]}


@app.tool()
def usage_today(date: str | None = None) -> dict:
    """Sum token usage and cost from outbox.jsonl for a given day.

    Aggregates per-call token counts that bridge.py recorded after each
    `claude -p --output-format json` invocation. Useful to estimate how
    fast you're burning Max-subscription rate-limits or API budget.

    Args:
        date: ISO date string like "2026-04-11". Defaults to today (local time).
    """
    target = date or dt.date.today().isoformat()
    if not OUTBOX.exists():
        return {"date": target, "calls": 0, "errors": 0, "note": "outbox.jsonl missing"}
    sums = {
        "date": target,
        "calls": 0,
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_cost_usd": 0.0,
        "elapsed_s": 0.0,
        "models": {},
    }
    with OUTBOX.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts", "")
            if not ts.startswith(target):
                continue
            sums["calls"] += 1
            if not row.get("ok"):
                sums["errors"] += 1
            sums["elapsed_s"] += row.get("elapsed_s", 0) or 0
            usage = row.get("usage") or {}
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                sums[k] += usage.get(k, 0) or 0
            sums["total_cost_usd"] += usage.get("total_cost_usd", 0) or 0
            m = usage.get("model") or "unknown"
            sums["models"][m] = sums["models"].get(m, 0) + 1
    sums["total_cost_usd"] = round(sums["total_cost_usd"], 4)
    sums["elapsed_s"] = round(sums["elapsed_s"], 1)
    sums["total_input_equiv_tokens"] = (
        sums["input_tokens"] + sums["cache_creation_input_tokens"] + sums["cache_read_input_tokens"]
    )
    return sums


if __name__ == "__main__":
    app.run()
