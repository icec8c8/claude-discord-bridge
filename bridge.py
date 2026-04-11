"""
Discord -> headless Claude Code auto-reply bridge (Path A, v2.2 hardened).

Pipeline per Discord message:
  1. discord.py Gateway receives, filters by user_id + channel_id
  2. Prefix gate: must start with BRIDGE_PREFIX (default "claude>"), else 👀 + drop
  3. Strip prefix, append cleaned content to inbox.jsonl, react 📥
  4. Enqueue to async worker (single-thread, serialized)
  5. Worker: react 🤖, typing on, run `claude -p ... --resume <uuid>`
     with --allowed-tools whitelist + --output-format json + --model sonnet
  6. Parse JSON: result + usage + cost
  7. Chunk to <=1900 chars, post each as Discord reply
  8. React ✅ on success / ❌ on failure
  9. Append outcome (incl. token usage) to outbox.jsonl

Headless claude is a SEPARATE conversation from the interactive Claude
Code session 總裁 uses. Same Max sub, two universes.

Hardening over v2.1:
- BRIDGE_PREFIX gate (defends against compromised Discord account / accidental sends)
- CLAUDE_ALLOWED_TOOLS whitelist (read-only by default; reduces blast radius)
- CLAUDE_MODEL default sonnet (instead of opus default; ~3x cheaper)
- --output-format json so we capture token usage / cost per call
- outbox.jsonl now stores per-call token+cost data for usage_today() MCP tool

Designed to be safe under pythonw.exe (no console).
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

CLAUDE_EXE = os.environ.get("CLAUDE_EXE", "claude")  # relies on PATH; override via .env
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", str(BRIDGE_DIR))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")  # cheaper than opus default

# Rate limiting + daily cost cap — protect against runaway spend + compromised account DoS
MAX_MESSAGES_PER_HOUR = int(os.environ.get("MAX_MESSAGES_PER_HOUR", "30"))
MAX_DAILY_COST_USD = float(os.environ.get("MAX_DAILY_COST_USD", "5.00"))
_msg_timestamps: deque = deque()

# Whitelist of tools the headless claude is allowed to use. Read-only by default.
# Add Bash / Edit / Write to give the Discord bot more power (and more risk).
DEFAULT_ALLOWED_TOOLS = "Read Grep Glob WebFetch WebSearch Task TodoWrite"
CLAUDE_ALLOWED_TOOLS = os.environ.get("CLAUDE_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS)
ALLOWED_TOOLS_LIST = [t for t in CLAUDE_ALLOWED_TOOLS.split() if t]

# Prefix gate: every Discord message must start with this string. The bridge
# strips it before passing to claude. Set BRIDGE_PREFIX="" to disable the gate.
BRIDGE_PREFIX = os.environ.get("BRIDGE_PREFIX", "claude>")

INBOX = BRIDGE_DIR / "inbox.jsonl"
OUTBOX = BRIDGE_DIR / "outbox.jsonl"
LOG_FILE = BRIDGE_DIR / "bridge.log"
PID_FILE = BRIDGE_DIR / "bridge.pid"
SESSION_ID_FILE = BRIDGE_DIR / "bridge_session_id.txt"
SESSION_SEEDED = BRIDGE_DIR / "bridge_session_seeded.flag"

# Persistent UUID so each Discord message resumes the same headless claude convo
if SESSION_ID_FILE.exists():
    SESSION_ID = SESSION_ID_FILE.read_text().strip()
else:
    SESSION_ID = str(uuid.uuid4())
    SESSION_ID_FILE.write_text(SESSION_ID)

# Logging — safe under pythonw.exe (sys.stdout may be None)
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


def check_rate_limit() -> tuple[str, int, int]:
    """Sliding-window rate limit. Returns (status, count, max_count).

    status values:
      - 'ok'    — under the limit, message accepted
      - 'warn'  — at or above 80% of the cap, message accepted with ⚠️ reaction
      - 'block' — at or above the cap, message rejected with 🛑 reaction

    Prunes timestamps older than 1 hour on every call. On 'block', does NOT
    record the current timestamp so retries don't keep filling the window.
    """
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
    """Sum total_cost_usd from outbox.jsonl for today's local date."""
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
    """Yield chunks of <= n chars, splitting on newline boundaries when possible."""
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


async def run_claude(prompt: str) -> tuple[bool, str, dict]:
    """Run claude -p headlessly. Returns (success, response_text, usage_dict)."""
    if SESSION_SEEDED.exists():
        session_flag = ["--resume", SESSION_ID]
        mode = "resume"
    else:
        session_flag = ["--session-id", SESSION_ID]
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
        "spawn claude (mode=%s, model=%s, tools=%d, session=%s..., len=%d)",
        mode, CLAUDE_MODEL, len(ALLOWED_TOOLS_LIST), SESSION_ID[:8], len(prompt),
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
        return False, f"⚠️ claude timed out after {CLAUDE_TIMEOUT}s", {}

    raw_out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return False, f"⚠️ claude exit {proc.returncode}\n```\n{err[:1500]}\n```", {}

    # Parse JSON result
    try:
        data = json.loads(raw_out)
    except json.JSONDecodeError as e:
        log.warning("json parse failed: %s; raw[:200]=%r", e, raw_out[:200])
        # Fallback: treat raw stdout as the response
        return True, raw_out or "(empty)", {}

    if data.get("is_error"):
        return False, f"⚠️ claude reported is_error\n```\n{json.dumps(data, ensure_ascii=False)[:1200]}\n```", data.get("usage", {}) or {}

    result_text = data.get("result", "") or ""
    usage = dict(data.get("usage", {}) or {})
    usage["total_cost_usd"] = data.get("total_cost_usd", 0)
    usage["model"] = next(iter(data.get("modelUsage", {})), CLAUDE_MODEL)

    if not SESSION_SEEDED.exists():
        SESSION_SEEDED.write_text("ok")
        log.info("session seeded; future calls will use --resume")

    log.info(
        "claude ok (out=%d, cost=$%.4f, in=%d cache_r=%d cache_c=%d out_tok=%d)",
        len(result_text), usage.get("total_cost_usd", 0),
        usage.get("input_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
        usage.get("output_tokens", 0),
    )
    return True, result_text, usage


async def worker():
    log.info("worker started, session=%s seeded=%s", SESSION_ID, SESSION_SEEDED.exists())
    log.info("model=%s, allowed_tools=%s, prefix=%r",
             CLAUDE_MODEL, " ".join(ALLOWED_TOOLS_LIST), BRIDGE_PREFIX)
    while True:
        item = await queue.get()
        message, cleaned = item
        t0 = dt.datetime.now()
        try:
            try:
                await message.add_reaction("🤖")
            except Exception:
                pass
            async with message.channel.typing():
                ok, response, usage = await run_claude(cleaned)
            try:
                await message.remove_reaction("🤖", client.user)
            except Exception:
                pass
            for i, ch in enumerate(chunk(response or "(empty response)")):
                kwargs = {}
                if i == 0:
                    kwargs["reference"] = message
                    kwargs["mention_author"] = False
                await message.channel.send(ch, **kwargs)
            try:
                await message.add_reaction("✅" if ok else "❌")
            except Exception:
                pass
            elapsed = (dt.datetime.now() - t0).total_seconds()
            entry = {
                "ts": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
                "request_id": str(message.id),
                "ok": ok,
                "response_len": len(response or ""),
                "elapsed_s": round(elapsed, 2),
                "usage": usage,
            }
            with OUTBOX.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.info("worker done in %.1fs (ok=%s)", elapsed, ok)
        except Exception as e:
            log.exception("worker exception: %s", e)
            try:
                await message.add_reaction("❌")
                await message.channel.send(f"⚠️ bridge worker error: `{type(e).__name__}: {e}`")
            except Exception:
                pass
        finally:
            queue.task_done()


@client.event
async def on_ready():
    log.info("connected as %s (id=%s)", client.user, client.user.id)
    log.info("session_id=%s (seeded=%s)", SESSION_ID, SESSION_SEEDED.exists())
    log.info("claude_exe=%s", CLAUDE_EXE)
    log.info("claude_cwd=%s", CLAUDE_CWD)
    log.info("model=%s prefix=%r", CLAUDE_MODEL, BRIDGE_PREFIX)
    log.info("allowed_tools=%s", " ".join(ALLOWED_TOOLS_LIST))
    log.info("filters: user=%s channel=%s", ALLOWED_USER_ID, ALLOWED_CHANNEL_ID)
    log.info("rate_limit=%d msgs/hour, daily_cost_cap=$%.2f",
             MAX_MESSAGES_PER_HOUR, MAX_DAILY_COST_USD)
    asyncio.create_task(worker())


@client.event
async def on_message(message: discord.Message):
    if message.author.id == client.user.id:
        return
    if message.author.id != ALLOWED_USER_ID:
        return
    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    # Daily cost cap — hard stop; protects against runaway spend / compromised account
    cost_today = get_daily_cost_usd()
    if cost_today >= MAX_DAILY_COST_USD:
        log.warning("daily cost cap hit: $%.4f / $%.2f", cost_today, MAX_DAILY_COST_USD)
        try:
            await message.add_reaction("💰")
            await message.reply(
                f"⚠️ Daily cost cap reached: ${cost_today:.4f} / ${MAX_DAILY_COST_USD:.2f}. Resume tomorrow.",
                mention_author=False,
            )
        except Exception:
            pass
        return

    # Rate limit — sliding window, messages per hour
    rl_status, rl_count, rl_max = check_rate_limit()
    if rl_status == "block":
        log.warning("rate limit hit: %d/%d per hour", rl_count, rl_max)
        try:
            await message.add_reaction("🛑")
            await message.reply(
                f"⚠️ Rate limit: {rl_count}/{rl_max} messages in the last hour. Slow down.",
                mention_author=False,
            )
        except Exception:
            pass
        return
    if rl_status == "warn":
        try:
            await message.add_reaction("⚠️")
        except Exception:
            pass

    # Prefix gate (security: defends against compromised Discord account & accidental sends)
    if BRIDGE_PREFIX:
        if not message.content.startswith(BRIDGE_PREFIX):
            log.info("drop: missing prefix %r: %.60s", BRIDGE_PREFIX, message.content)
            try:
                await message.add_reaction("👀")
            except Exception:
                pass
            return
        cleaned = message.content[len(BRIDGE_PREFIX):].strip()
    else:
        cleaned = message.content

    if not cleaned:
        log.info("drop: empty content after prefix strip")
        try:
            await message.add_reaction("❓")
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
    log.info("inbox+1 (qsize=%d): %s", queue.qsize(), cleaned[:80])

    try:
        await message.add_reaction("📥")
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
