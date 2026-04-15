"""
Discord -> headless Claude Code auto-reply bridge (v2.4 stall-resistant).

v2.4 (2026-04-15) changes over v2.3:
- Process-group spawn (start_new_session=True) — claude's subprocesses (find,
  grep, child bashes) now live in the same pgid. On timeout/stall we killpg()
  the whole tree instead of just the parent, fixing the "orphaned find loops"
  root cause of the 47-minute apparent stall on 2026-04-15 16:09.
- CPU stall detection via psutil — each heartbeat samples total CPU time across
  claude + all descendants. If the % CPU over STALL_WINDOW_MIN consecutive
  minutes stays below STALL_CPU_THRESHOLD, the run is declared stalled and
  killed with a 🚨 Discord reply. A living-but-idle run is now a detectable
  failure, not a silent timer drain.
- Heartbeats now include cpu% + child count so 總裁 can see work vs. no-work:
  "⏳ worker-2 3m01s elapsed, cpu=38% procs=4"
- --max-turns cap (CLAUDE_MAX_TURNS, default 30) — prevents infinite tool loops
  where claude keeps spawning find after find on missing paths.

v2.3 features retained:
- BRIDGE_WORKERS fan-out with per-worker persistent --resume sessions
- PROGRESS_INTERVAL heartbeat (now smarter — carries CPU info)
- CLAUDE_TIMEOUT hard cap (default 3600s)
- Per-worker emoji reaction + ⏸ pause reaction when all workers busy
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import signal
import sys
import time
import uuid
from collections import deque

import discord
import psutil
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
CLAUDE_MAX_TURNS = int(os.environ.get("CLAUDE_MAX_TURNS", "30"))

BRIDGE_WORKERS = max(1, int(os.environ.get("BRIDGE_WORKERS", "3")))
PROGRESS_INTERVAL = max(15, int(os.environ.get("PROGRESS_INTERVAL", "60")))

# Stall detection — if total CPU% across claude tree stays below this threshold
# for STALL_WINDOW_MIN consecutive heartbeats, the run is declared stalled.
STALL_CPU_THRESHOLD = float(os.environ.get("STALL_CPU_THRESHOLD", "2.0"))
STALL_WINDOW_MIN = max(2, int(os.environ.get("STALL_WINDOW_MIN", "5")))

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

LEGACY_SESSION_ID_FILE = BRIDGE_DIR / "bridge_session_id.txt"
LEGACY_SESSION_SEEDED = BRIDGE_DIR / "bridge_session_seeded.flag"


def _load_sessions() -> list[dict]:
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text())
            workers = data.get("workers", [])
        except Exception:
            workers = []
    else:
        workers = []

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


def _sample_tree(pid: int) -> tuple[float, int]:
    """Return (total_cpu_seconds, running_proc_count) across claude + descendants."""
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0.0, 0
    procs = [p]
    try:
        procs.extend(p.children(recursive=True))
    except psutil.NoSuchProcess:
        pass
    total = 0.0
    running = 0
    for proc in procs:
        try:
            t = proc.cpu_times()
            total += t.user + t.system
            if proc.is_running():
                running += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total, running


def _kill_tree(proc: asyncio.subprocess.Process, reason: str) -> None:
    """Kill claude + all descendants (they share a pgid thanks to start_new_session)."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    log.warning("kill_tree pid=%s pgid=%s reason=%s", proc.pid, pgid, reason)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            except OSError as e:
                log.warning("killpg %s: %s", sig, e)
        else:
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                break
        time.sleep(0.5)
        if proc.returncode is not None:
            return


async def _watch(message: discord.Message, worker_id: int, proc: asyncio.subprocess.Process,
                 t0: float, stall_evt: asyncio.Event) -> None:
    """Heartbeat + stall detection. Runs until proc exits or stall declared."""
    last_total = 0.0
    last_wall = t0
    below_streak = 0
    primed = False
    try:
        while proc.returncode is None:
            try:
                await asyncio.sleep(PROGRESS_INTERVAL)
            except asyncio.CancelledError:
                return
            if proc.returncode is not None:
                return
            now = time.time()
            total, n_procs = _sample_tree(proc.pid)
            if not primed:
                last_total, last_wall = total, now
                primed = True
                elapsed = int(now - t0)
                m, s = divmod(elapsed, 60)
                try:
                    await message.channel.send(
                        f"\u23f3 worker-{worker_id} {m}m{s:02d}s elapsed, sampling cpu\u2026",
                        reference=message, mention_author=False,
                    )
                except Exception as e:
                    log.warning("heartbeat send failed: %s", e)
                continue
            dt_wall = max(0.001, now - last_wall)
            dt_cpu = max(0.0, total - last_total)
            cpu_pct = (dt_cpu / dt_wall) * 100.0
            last_total, last_wall = total, now
            elapsed = int(now - t0)
            m, s = divmod(elapsed, 60)
            if cpu_pct < STALL_CPU_THRESHOLD:
                below_streak += 1
            else:
                below_streak = 0
            stalled = below_streak >= STALL_WINDOW_MIN
            tag = "\U0001f6a8 STALL" if stalled else ("\u26a0\ufe0f low-cpu" if below_streak else "\u23f3")
            try:
                await message.channel.send(
                    f"{tag} worker-{worker_id} {m}m{s:02d}s elapsed, cpu={cpu_pct:.1f}% procs={n_procs}"
                    + (f" (idle {below_streak}/{STALL_WINDOW_MIN})" if below_streak and not stalled else ""),
                    reference=message, mention_author=False,
                )
            except Exception as e:
                log.warning("heartbeat send failed: %s", e)
            if stalled:
                log.warning("w%d STALL detected (cpu<%.1f%% for %d/%d samples) \u2192 killing tree",
                            worker_id, STALL_CPU_THRESHOLD, below_streak, STALL_WINDOW_MIN)
                stall_evt.set()
                _kill_tree(proc, reason=f"stall cpu<{STALL_CPU_THRESHOLD}% for {STALL_WINDOW_MIN}min")
                try:
                    await message.channel.send(
                        f"\U0001f6a8 worker-{worker_id} stalled (cpu<{STALL_CPU_THRESHOLD}% for {STALL_WINDOW_MIN}min) \u2014 killed to free the slot",
                        reference=message, mention_author=False,
                    )
                except Exception:
                    pass
                return
    except asyncio.CancelledError:
        return


async def run_claude(prompt: str, worker_id: int,
                     message: discord.Message) -> tuple[bool, str, dict]:
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
        "--max-turns", str(CLAUDE_MAX_TURNS),
        "--allowedTools", *ALLOWED_TOOLS_LIST,
        *session_flag,
    ]
    log.info(
        "w%d spawn claude (mode=%s, model=%s, max_turns=%d, tools=%d, session=%s..., len=%d)",
        worker_id, mode, CLAUDE_MODEL, CLAUDE_MAX_TURNS, len(ALLOWED_TOOLS_LIST),
        session["uuid"][:8], len(prompt),
    )
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=CLAUDE_CWD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        start_new_session=True,
    )
    t0 = time.time()
    stall_evt = asyncio.Event()
    watch_task = asyncio.create_task(_watch(message, worker_id, proc, t0, stall_evt))
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        _kill_tree(proc, reason=f"timeout after {CLAUDE_TIMEOUT}s")
        try:
            await proc.communicate()
        except Exception:
            pass
        watch_task.cancel()
        try:
            await watch_task
        except Exception:
            pass
        return False, f"\u26a0\ufe0f worker-{worker_id} claude timed out after {CLAUDE_TIMEOUT}s (tree killed)", {}
    finally:
        watch_task.cancel()
        try:
            await watch_task
        except Exception:
            pass

    if stall_evt.is_set():
        return False, f"\U0001f6a8 worker-{worker_id} stalled \u2014 claude tree killed after cpu<{STALL_CPU_THRESHOLD}% for {STALL_WINDOW_MIN}min", {}

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
        try:
            try:
                await message.add_reaction(emoji_tag)
            except Exception:
                pass
            async with message.channel.typing():
                ok, response, usage = await run_claude(cleaned, worker_id, message)
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
    log.info("workers=%d timeout=%ds progress=%ds stall=cpu<%.1f%%/%dmin max_turns=%d",
             BRIDGE_WORKERS, CLAUDE_TIMEOUT, PROGRESS_INTERVAL,
             STALL_CPU_THRESHOLD, STALL_WINDOW_MIN, CLAUDE_MAX_TURNS)
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
