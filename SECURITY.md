# Security Policy

## Supported versions

This project is published as-is from a single-author repository. Only the latest commit on `main` is supported. There is no formal back-port policy.

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue. Instead, open a private [GitHub Security Advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) against the repository. Include:

- What you found
- Steps to reproduce
- The commit SHA you observed the issue on
- Impact you think it has (does it leak secrets? widen the tool whitelist unintentionally? allow unauthorized Discord users to invoke the bridge?)

I will acknowledge receipt within 7 days. Please allow reasonable time for a fix before any public disclosure.

## Threat model

The bridge is explicitly designed as a **single-user** tool. The security boundaries it attempts to enforce are:

1. Only a specific Discord user ID can invoke the bridge.
2. Only from a specific Discord channel ID.
3. Only with a specific message prefix.
4. The headless `claude -p` subprocess is restricted to a read-only tool whitelist by default.

What is **in scope** for security reports:

- Ways to bypass any of the four layers above without modifying `.env`.
- Ways to extract `.env` contents or other secrets from a running bridge.
- Ways to cause the headless `claude -p` to run tools that are not on the whitelist.
- Ways for an attacker without access to `.env` to cause the bridge to send messages as the bot.

What is **out of scope**:

- An attacker who already has write access to `.env`, the bridge source, or the Python environment can obviously do anything. That is not a vulnerability; that is a prerequisite.
- Prompt injection on the underlying Claude model itself. The bridge does not claim to be safer than Claude; it claims to gate access to Claude.
- Rate-limit / cost exhaustion attacks from the allowed user (they are the allowed user — they set their own budget).
- Social engineering of the bot owner.

## Hardening recommendations beyond the defaults

If you are running this in any environment you care about, consider:

- Dropping `WebFetch` from `CLAUDE_ALLOWED_TOOLS` to cut the exfiltration vector.
- Dropping `Read` from `CLAUDE_ALLOWED_TOOLS` and using Claude Code's path allow-listing via `.claude/settings.json` instead.
- Setting `CLAUDE_MODEL=haiku` to cap the per-call blast radius further.
- Running the bridge inside a dedicated OS user with restricted filesystem access.
- Adding an explicit per-day rate limit around `on_message` so a compromised account cannot burn through the Claude quota.

## Credentials involved

The following credentials are read by the bridge at runtime and must never be committed:

- `DISCORD_BOT_TOKEN` — from `.env`
- Claude Code OAuth / API credentials — from the user's local Claude Code installation (`~/.claude/` on Linux/macOS, `%USERPROFILE%\.claude\` on Windows, plus the macOS login keychain entry `Claude Code-credentials`)

None of these should be in git history. Before every `git push`, run the repo security grep in the README.
