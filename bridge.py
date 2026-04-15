"""
Discord -> headless Claude Code auto-reply bridge (v2.3 fan-out + progress).

v2.3 (2026-04-15) changes over v2.2:
- Fan-out N parallel workers (BRIDGE_WORKERS, default 3) — each holds its own
  persistent --resume session (session_ids.json). Discord messages are dispatched
  round-robin; a long task on worker-0 no longer blocks worker-1/2.
- Progress heartbeat: every PROGRESS_INTERVAL seconds (default 60s) a worker
  that's still running claude posts a short reply with elapsed time, so Discord
  never looks "timed out" on long tasks.
- CLAUDE_TIMEOUT default raised to 3600s (1 hour). Long-running tasks are OK as
  long as claude actually makes progress; typing indicator + heartbeat keep the
  Discord UX alive.
- SIGHUP-style: edit session_ids.json or .env and `launchctl kickstart -k`
  gui/$(id -u)/dev.local.claude-discord-bridge to reload.

Pipeline per Discord message:
  1. discord.py Gateway receives, filters by user_id + channel_id
  2. Prefix gate (BRIDGE_PREFIX), else 👀 + drop
  3. Strip prefix, append to inbox.jsonl, react 📥
  4. Enqueue — first free worker (react 🤖1..N) picks it up
  5. Worker runs `claude -p ... --resume <worker-uuid>` with --output-format json
  6. Progress heartbeat posts elapsed time every PROGRESS_INTERVAL seconds
  7. Chunk result <=1900 chars, post each as Discord reply
  8. React ✅/❌, append outcome (incl. usage + worker_id) to outbox.jsonl

Headless claudes are SEPARATE conversations from 總裁's interactive session.
Same Max sub, N+1 universes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time
import uuid
from collections import deque

import discord
from dotenv import load_dotenv

BRIDGE_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(BRIDGE_DIR / ".env")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
ALLOWED_CHANNEL_ID = int(os.environ["ALLOWED_CHANNEL_ID"])

CLAUDE_EXE = os.environ.get("CLAUDE_EXE", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "3600"))
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", str(BRIDGE_DIR))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")

BRIDGE_WORKERS = max(1, int(os.environ.get("BRIDGE_WORKERS", "3")))
PROGRESS_INTERVAL = max(15, int(os.environ.get("PROGRESS_INTERVAL", "60")))

MAX_MESSAGES_PER_HOUR = int(os.environ.get("MAX_MESSAGES_PER_HOUR", "60"))
MAX_DAILY_COST_USD = float(os.environ.get("MAX_DAILY_COST_USD", "10.00"))
_msg_timestamps: deque = deque()

DEFAULT_ALLOWED_TOOLS = "Read Grep Glob WebFetch WebSearch Task TodoWrite Bash Edit Write"
CLAUDE_ALLOWED_TOOLS = os.environ.get("CLAUDE_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS)
ALLOWED_TOOLS_LIST = [t for t in CLAUDE_ALLOWED_TOOLS.split() if t]

BRIDGE_PREFIX = os.environ.get("BRIDGE_PREFIX", "")

INBOX = BRIDGE_DIR / "inbox.jsonl"
OUTBOX = BRIDGE_DIR / "outbox.jsonl"
LOG_FILE = BRIDGE_DIR / "bridge.log"
PID_FILE = BRIDGE_DIR / "bridge.pid"
SESSIONS_FILE = BRIDGE_DIR / "session_ids.json"

# Legacy v2.2 single-session files — migrated into session_ids.json on first boot
LEGACY_SESSION_ID_FILE = BRIDGE_DIR / "bridge_session_id.txt"
LEGACY_SESSION_SEEDED = BRIDGE_DIR / "bridge_session_seeded.flag"


def _load_sessions() -> list[dict]:
    """Load or create N worker sessions. Returns list of {uuid, seeded} dicts."""
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text())
            workers = data.get("workers", [])
        except Exception:
            workers = []
    else:
        workers = []

    # Migrate v2.2 single session into worker 0
    if not workers and LEGACY_SESSION_ID_FILE.exists():
        workers.append({
            "uuid": LEGACY_SESSION_ID_FILE.read_text().strip() or str(uuid.uuid4()),
            "seeded": LEGACY_SESSION_SEEDED.exists(),
        })

    while len(workers) < BRIDGE_WORKERS:
        workers.append({"uuid": str(uuid.uuid4()), "seeded": False})
    workers = workers[:BRIDGE_WORKERS]
    _save_sessions(workers)
    return workers


def _save_sessions(workers: list[dict]) -> None:
    SESSIONS_FILE.write_text(json.dumps({"workers": workers}, indent=2))


SESSIONS = _load_sessions()

_handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    try:
        sys.stdout.fileno()
        _handlers.append(logging.StreamHandler(sys.stdout))
    except (OSError, AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("bridge")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

queue: asyncio.Queue = asyncio.Queue()
_worker_busy: list[bool] = [False] * BRIDGE_WORKERS

WORKER_EMOJI = ["1\u20e3", "2\u20e3", "3\u20e3", "4\u20e3", "5\u20e3",
                "6\u20e3", "7\u20e3", "8\u20e3", "9\u20e3", "\U0001f51f"]


def check_rate_limit() -> tuple[str, int, int]:
    now = time.time()
    cutoff = now - 3600
    while _msg_timestamps and _msg_timestamps[0] < cutoff:
        _msg_timestamps.popleft()
    count = len(_msg_timestamps)
    if count >= MAX_MESSAGES_PER_HOUR:
        return "block", count, MAX_MESSAGES_PER_HOUR
    _msg_timestamps.append(now)
    count += 1
    if count >= max(1, int(MAX_MESSAGES_PER_HOUR * 0.8)):
        return "warn", count, MAX_MESSAGES_PER_HOUR
    return "ok", count, MAX_MESSAGES_PER_HOUR


def get_daily_cost_usd() -> float:
    if not OUTBOX.exists():
        return 0.0
    today = dt.date.today().isoformat()
    total = 0.0
    try:
        with OUTBOX.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not row.get("ts", "").startswith(today):
                    continue
                usage = row.get("usage") or {}
                total += usage.get("total_cost_usd", 0) or 0
    except Exception as e:
        log.warning("get_daily_cost_usd error: %s", e)
    return total


def chunk(text: str, n: int = 1900):
    if not text:
        yield "(empty response)"
        return
    while text:
        if len(text) <= n:
            yield text
            return
        cut = text.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        yield text[:cut]
        text = text[cut:].lstrip("\n")


async def _heartbeat(message: discord.Message, worker_id: int, t0: float,
                     cancel_evt: asyncio.Event) -> None:
    """Every PROGRESS_INTERVAL s, post a short 'still working' reply."""
    try:
        while not cancel_evt.is_set():
            try:
                await asyncio.wait_for(cancel_evt.wait(), timeout=PROGRESS_INTERVAL)
                return  # cancelled cleanly
            except asyncio.TimeoutError:
                pass
            elapsed = int(time.time() - t0)
            mins, secs = divmod(elapsed, 60)
            try:
                await message.channel.send(
                    f"\u23f3 worker-{worker_id} still working \u2014 {mins}m{secs:02d}s elapsed",
                    reference=message, mention_author=False,
                )
            except Exception as e:
                log.warning("heartbeat send failed: %s", e)
    except asyncio.CancelledError:
        return


async def run_claude(prompt: str, worker_id: int) -> tuple[bool, str, dict]:
    """Run claude -p headlessly on the given worker's persistent session."""
    session = SESSIONS[worker_id]
    if session["seeded"]:
        session_flag = ["--resume", session["uuid"]]
        mode = "resume"
    else:
        session_flag = ["--session-id", session["uuid"]]
        mode = "create"
    args = [
        CLAUDE_EXE,
        "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", CLAUDE_MODEL,
        "--allowedTools", *ALLOWED_TOOLS_LIST,
        *session_flag,
    ]
    log.info(
        "w%d spawn claude (mode=%s, model=%s, tools=%d, session=%s..., len=%d)",
        worker_id, mode, CLAUDE_MODEL, len(ALLOWED_TOOLS_LIST),
        session["uuid"][:8], len(prompt),
    )
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=CLAUDE_CWD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, f"\u26a0\ufe0f worker-{worker_id} claude timed out after {CLAUDE_TIMEOUT}s", {}

    raw_out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return False, f"\u26a0\ufe0f claude exit {proc.returncode}\n```\n{err[:1500]}\n```", {}

    try:
        data = json.loads(raw_out)
    except json.JSONDecodeError as e:
        log.warning("w%d json parse failed: %s; raw[:200]=%r", worker_id, e, raw_out[:200])
        return True, raw_out or "(empty)", {}

    if data.get("is_error"):
        return False, f"\u26a0\ufe0f claude reported is_error\n```\n{json.dumps(data, ensure_ascii=False)[:1200]}\n```", data.get("usage", {}) or {}

    result_text = data.get("result", "") or ""
    usage = dict(data.get("usage", {}) or {})
    usage["total_cost_usd"] = data.get("total_cost_usd", 0)
    usage["model"] = next(iter(data.get("modelUsage", {})), CLAUDE_MODEL)

    if not session["seeded"]:
        session["seeded"] = True
        _save_sessions(SESSIONS)
        log.info("w%d session seeded; future calls will --resume", worker_id)

    log.info(
        "w%d claude ok (out=%d, cost=$%.4f, in=%d cache_r=%d cache_c=%d out_tok=%d)",
        worker_id, len(result_text), usage.get("total_cost_usd", 0),
        usage.get("input_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
        usage.get("output_tokens", 0),
    )
    return True, result_text, usage


async def worker(worker_id: int):
    log.info("w%d started, session=%s seeded=%s",
             worker_id, SESSIONS[worker_id]["uuid"], SESSIONS[worker_id]["seeded"])
    emoji_tag = WORKER_EMOJI[worker_id] if worker_id < len(WORKER_EMOJI) else "\U0001f916"
    while True:
        item = await queue.get()
        message, cleaned = item
        _worker_busy[worker_id] = True
        t0 = time.time()
        cancel_evt = asyncio.Event()
        hb_task = asyncio.create_task(_heartbeat(message, worker_id, t0, cancel_evt))
        try:
            try:
                await message.add_reaction(emoji_tag)
            except Exception:
                pass
            async with message.channel.typing():
                ok, response, usage = await run_claude(cleaned, worker_id)
            cancel_evt.set()
            try:
                await hb_task
            except Exception:
                pass
            try:
                await message.remove_reaction(emoji_tag, client.user)
            except Exception:
                pass
            for i, ch in enumerate(chunk(response or "(empty response)")):
                kwargs = {}
                if i == 0:
                    kwargs["reference"] = message
                    kwargs["mention_author"] = False
                await message.channel.send(ch, **kwargs)
            try:
                await message.add_reaction("\u2705" if ok else "\u274c")
            except Exception:
                pass
            elapsed = time.time() - t0
            entry = {
                "ts": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
                "request_id": str(message.id),
                "worker_id": worker_id,
                "ok": ok,
                "response_len": len(response or ""),
                "elapsed_s": round(elapsed, 2),
                "usage": usage,
            }
            with OUTBOX.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.info("w%d done in %.1fs (ok=%s)", worker_id, elapsed, ok)
        except Exception as e:
            cancel_evt.set()
            log.exception("w%d exception: %s", worker_id, e)
            try:
                await message.add_reaction("\u274c")
                await message.channel.send(f"\u26a0\ufe0f worker-{worker_id} error: `{type(e).__name__}: {e}`")
            except Exception:
                pass
        finally:
            _worker_busy[worker_id] = False
            queue.task_done()


@client.event
async def on_ready():
    log.info("connected as %s (id=%s)", client.user, client.user.id)
    log.info("workers=%d timeout=%ds progress=%ds", BRIDGE_WORKERS, CLAUDE_TIMEOUT, PROGRESS_INTERVAL)
    for i, s in enumerate(SESSIONS):
        log.info("  w%d session=%s seeded=%s", i, s["uuid"], s["seeded"])
    log.info("claude_exe=%s cwd=%s", CLAUDE_EXE, CLAUDE_CWD)
    log.info("model=%s prefix=%r", CLAUDE_MODEL, BRIDGE_PREFIX)
    log.info("allowed_tools=%s", " ".join(ALLOWED_TOOLS_LIST))
    log.info("filters: user=%s channel=%s", ALLOWED_USER_ID, ALLOWED_CHANNEL_ID)
    log.info("rate_limit=%d/hr, daily_cost_cap=$%.2f",
             MAX_MESSAGES_PER_HOUR, MAX_DAILY_COST_USD)
    for i in range(BRIDGE_WORKERS):
        asyncio.create_task(worker(i))


@client.event
async def on_message(message: discord.Message):
    if message.author.id == client.user.id:
        return
    if message.author.id != ALLOWED_USER_ID:
        return
    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    cost_today = get_daily_cost_usd()
    if cost_today >= MAX_DAILY_COST_USD:
        log.warning("daily cost cap hit: $%.4f / $%.2f", cost_today, MAX_DAILY_COST_USD)
        try:
            await message.add_reaction("\U0001f4b0")
            await message.reply(
                f"\u26a0\ufe0f Daily cost cap reached: ${cost_today:.4f} / ${MAX_DAILY_COST_USD:.2f}.",
                mention_author=False,
            )
        except Exception:
            pass
        return

    rl_status, rl_count, rl_max = check_rate_limit()
    if rl_status == "block":
        log.warning("rate limit: %d/%d per hour", rl_count, rl_max)
        try:
            await message.add_reaction("\U0001f6d1")
            await message.reply(
                f"\u26a0\ufe0f Rate limit: {rl_count}/{rl_max} messages/hour.",
                mention_author=False,
            )
        except Exception:
            pass
        return
    if rl_status == "warn":
        try:
            await message.add_reaction("\u26a0\ufe0f")
        except Exception:
            pass

    if BRIDGE_PREFIX:
        if not message.content.startswith(BRIDGE_PREFIX):
            log.info("drop: missing prefix %r: %.60s", BRIDGE_PREFIX, message.content)
            try:
                await message.add_reaction("\U0001f440")
            except Exception:
                pass
            return
        cleaned = message.content[len(BRIDGE_PREFIX):].strip()
    else:
        cleaned = message.content.strip()

    if not cleaned:
        log.info("drop: empty content")
        try:
            await message.add_reaction("\u2753")
        except Exception:
            pass
        return

    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "message_id": str(message.id),
        "author": str(message.author),
        "author_id": str(message.author.id),
        "channel_id": str(message.channel.id),
        "content": cleaned,
        "raw_content": message.content,
        "attachments": [a.url for a in message.attachments],
    }
    with INBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    busy = sum(1 for b in _worker_busy if b)
    log.info("inbox+1 (qsize=%d, busy=%d/%d): %s",
             queue.qsize(), busy, BRIDGE_WORKERS, cleaned[:80])

    try:
        await message.add_reaction("\U0001f4e5")
        if busy >= BRIDGE_WORKERS:
            await message.add_reaction("\u23f8\ufe0f")
    except Exception:
        pass
    await queue.put((message, cleaned))


def write_pid():
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception as e:
        log.warning("could not write pid file: %s", e)


async def main():
    write_pid()
    try:
        await client.start(TOKEN)
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
