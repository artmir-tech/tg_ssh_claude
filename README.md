# tg_ssh_claude
Управление ssh-сервером с Клод Кодом через Телеграм (топики, статусы, лимиты, и пр.)

## Claude Control v0.1

A personal Telegram front end for many Claude Code sessions on this server.
One forum supergroup ("Claude Control"): the **General** topic is the dashboard, **every other
topic is exactly one Claude Code session**. The owner's guide in Russian is in [ИНСТРУКЦИЯ.md](ИНСТРУКЦИЯ.md).

## Architecture

```
Telegram (long polling) ─> bot.py ─> manager.py ─> claude.py ─> /usr/bin/claude (Agent SDK, stream-json)
                              │           │                          └─ existing claude.ai login (~/.claude)
                              └──── db.py (SQLite: data/claude-control.db)
```

| File | Responsibility |
|---|---|
| `claude_control/config.py` | Settings from `.env` / environment |
| `claude_control/db.py` | Registry: `sessions` (topic ↔ Claude session), `turns` (per-session queue), `kv`, update dedupe |
| `claude_control/telegram.py` | Thin Bot API client: token redaction, per-chat send budget (≈20 msg/min), 429 handling |
| `claude_control/claude.py` | Runs **one user turn** through `ClaudeSDKClient` (resume/new, background tasks, stop, permissions) |
| `claude_control/manager.py` | Worker pool (`MAX_CONCURRENT_RUNS`), per-session serialization, status machine, recovery |
| `claude_control/bot.py` | Commands, dashboard, progress messages, permission/question buttons, files, import/fork |
| `claude_control/render.py` | Markdown → Telegram HTML, splitting, human time formats |
| `tests/acceptance.py` | End-to-end tests with real Claude (haiku) and a fake Telegram (`tests/fake_telegram.py`) |

### Key decisions
- **One process per turn.** Each Telegram message = `claude` started with `resume=<id>` (or `session_id=<id>` for the
  first turn — the UUID is generated and stored in SQLite *before* the run). No idle processes; 25 sessions ≠ 25 processes.
- **Real Claude Code sessions.** Transcripts live in `~/.claude/projects/…` as usual. The service never edits them;
  it only uses the SDK's official `rename_session` / `fork_session` / `list_sessions` / `get_session_info`.
- **Visible in `claude --resume` / VS Code.** The CLI hides sessions whose entrypoint is `sdk-*`, so turns are
  started with `CLAUDE_CODE_ENTRYPOINT=claude-control` and the user message carries `origin: {"kind": "human"}`.
- **Background tasks.** If Claude backgrounds work, the run stays connected until the tasks finish and Claude's
  follow-up turn arrives (each `ResultMessage` is delivered to Telegram).
- **Permissions.** Mode `default` + `can_use_tool` → Telegram buttons (Allow once / Allow in this session / Deny,
  or a Telegram *reply* to the request = deny with feedback; other messages are queued; plain text answers an AskUserQuestion). "In this session" grants are stored in `sessions.grants` and passed as
  `allowed_tools` to later turns — never written to settings files. `AUTO_ALLOW_TOOLS` (default WebSearch, WebFetch)
  run without asking. `bypassPermissions` / `--dangerously-skip-permissions` are never used. `.env` and the DB are
  denied to Claude's Read/Edit/Write tools. Unanswered requests are denied after `PERMISSION_TIMEOUT_MIN`.
  `AskUserQuestion` becomes option buttons (status `WAITING`).
- **VS Code awareness.** `~/.claude/sessions/<pid>.json` (written by Claude Code itself, read-only here) gives the
  open interactive sessions and their `busy`/`idle` state. The dashboard counts them; a queued Telegram turn for a
  session that is `busy` in VS Code waits until it is idle (re-checked every 5 s).
- **Live dashboard.** One message in General (`kv.dashboard_msg_id`) is edited every 60 s and ~8 s after any
  status change (`Bot.dashboard_changed`). `/status` reposts it at the bottom. Commands in General are deleted;
  bot service replies there are ephemeral (`kv.ephemeral`, deleted after 90 s / 10 min).
- **UI conventions.** The topic panel (`panel_view`, pinned) and the General dashboard (`render_view`) are small
  in-place apps: buttons edit the same message, every sub-screen has «◂ Назад», archive/fork ask first.
  General holds only the dashboard: user messages there are deleted after handling, feedback is a 🔔 `flash`
  line; `_janitor` removes tracked General messages after 5 idle minutes; `sweep_general` (one-time at
  upgrade, or the «🧹» button) removes leftovers of old versions by forwarding unknown ids into a temporary
  topic and matching exact General-only texts. Topic service notices (`notice`) self-delete after 60 s.
- **Limits.** Two read-only sources, fresher wins per window: each run's `RateLimitEvent.raw.unifiedWindows`
  (`kv.rate_limit`) and Claude Code's own cache `cachedUsageUtilization` in `~/.claude.json` (incl. per-model week).
  Every 5 min (and on 🔄) the bot runs `claude -p /usage --no-session-persistence` — a local command, no model call,
  no tokens — which refreshes that cache. Times use `TIMEZONE` (IANA name) or server time.
- **Security.** Only `OWNER_IDS` can do anything; the bot ignores everyone else and leaves foreign chats.

### Session statuses
`NEW` (no turn yet) → `QUEUED` (waiting for a worker) → `RUNNING` ⇄ `WAITING_APPROVAL` / `WAITING` (question) →
`IDLE` (done) | `ERROR` (failed; Retry/Details buttons) | `STOPPED` (/stop or interrupted by a restart).
`ARCHIVED` hides it from the dashboard (transcript kept). Turn statuses: `queued running done error stopped
interrupted cancelled`.

### Recovery
On start, turns left `running` are marked `interrupted`, any surviving Claude process of that session is killed,
the session becomes `STOPPED`, and the topic gets "⚠️ … [▶️ Continue] [🔁 Retry]". Queued turns simply run.
Telegram updates are de-duplicated (`processed_updates`, UNIQUE(chat_id, message_id) on turns).

## Operations

```bash
systemctl --user status claude-control          # state
systemctl --user restart claude-control         # restart (running tasks get a Continue button)
journalctl --user -u claude-control -f          # live logs
journalctl --user -u claude-control -n 300 --no-pager
sqlite3 data/claude-control.db 'select id,title,status,claude_session_id from sessions'   # (python3 -c if no sqlite3)
bash deploy/install.sh                          # (re)install venv + unit, enable linger, restart
```

The service is a **systemd user unit** (`~/.config/systemd/user/claude-control.service`) with linger enabled,
so it runs without an SSH login and starts at boot. It runs as the account's own Linux user because it must use that user's Claude login.

### Configuration (`.env`, chmod 600, not in git)
| Key | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | bot token (never logged) |
| `OWNER_IDS` | — | comma-separated Telegram user ids allowed to control |
| `TELEGRAM_CHAT_ID` | — | the forum supergroup id (or bound by `/start` from an owner) |
| `MAX_CONCURRENT_RUNS` | 5 | parallel Claude processes |
| `DEFAULT_CWD` | `$HOME` | folder for new sessions (`/project` changes it at runtime) |
| `PROJECT_DIRS` | — | folders offered by `/project` |
| `AUTO_ALLOW_TOOLS` | `WebSearch,WebFetch` | tools that never ask |
| `PERMISSION_TIMEOUT_MIN` | 60 | unanswered permission → deny |
| `MAX_RUN_HOURS` | 8 | hard stop for one run |
| `CLAUDE_MODEL` | (Claude Code default) | e.g. `sonnet`; per session via `/model` |
| `TIMEZONE` | server time (UTC) | IANA zone for times shown in Telegram, e.g. `Asia/Tbilisi` |

## Another account on this server
See [ПЕРЕНОС.md](ПЕРЕНОС.md). `bash deploy/package.sh` builds `/tmp/claude-control.tar.gz` (code + docs, no `.env`/`data`);
the other user extracts it to `~/claude-control`, fills `.env` (own bot token, own owner id/group) and runs
`deploy/install.sh`. The unit uses `%h`, so it is identical for every account. Use a lower `MAX_CONCURRENT_RUNS`
when several systems share the server (each Claude process ≈ 300–400 MB RAM).

## Tests
```bash
env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 .venv/bin/python -m tests.acceptance        # all (~10 min)
env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 .venv/bin/python -m tests.acceptance T1 T9  # some
```
`env -i` matters when run from inside a Claude Code session (its env vars would leak into the child CLI).
Results go to `tests/report.json`; test sessions are deleted afterwards (`KEEP_SESSIONS=1` keeps them).

## Known limits (v0.1)
- Voice/video messages are not supported; files ≤ 20 MB (Bot API limit). Albums: each photo is saved; the
  caption triggers the turn.
- Telegram allows ~20 bot messages/min per group: progress edits are skipped when the budget is tight.
- If the same session is open in VS Code and used from Telegram at the same moment, both write to one transcript —
  the bot warns about it; avoid simultaneous use.
- Creating/renaming topics needs the bot's admin right "Manage topics".
