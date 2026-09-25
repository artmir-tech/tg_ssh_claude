# Claude Control

Telegram manager for Claude Code sessions (one forum topic = one session). Read README.md first.

- Service: `systemctl --user {status|restart} claude-control`; logs: `journalctl --user -u claude-control -n 200`.
- After code changes: run the tests, then `systemctl --user restart claude-control`
  (running tasks get a "Continue" button in Telegram).
- Tests (real Claude, haiku): `env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 .venv/bin/python -m tests.acceptance`
- Never use bypassPermissions / --dangerously-skip-permissions; never edit transcripts in ~/.claude/projects.
- Secrets live in `.env` (chmod 600): never print, log or commit the bot token.
- The owner is not a programmer: user-facing texts are in Russian and plain language.
- Another account on this server: ПЕРЕНОС.md (git clone from GitHub). Publish changes: commit + `git push` (repo is public; never commit .env/data).
