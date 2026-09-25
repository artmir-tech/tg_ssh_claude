"""Claude adapter: run ONE user turn of a Claude Code session through the official Agent SDK.

Each turn spawns the system `claude` CLI (same binary and login as the terminal/VS Code),
in structured stream-json mode, with either `session_id` (first turn, id chosen by us) or
`resume` (later turns). Nothing here parses terminal output or edits transcripts.

Two details matter for compatibility with normal Claude Code:
  * CLAUDE_CODE_ENTRYPOINT=claude-control - the CLI hides "sdk-*" sessions from the
    `claude --resume` picker; with our own client name they show up like any other session.
  * the user message carries origin {"kind": "human"} - it really was typed by a person.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage, CanUseToolShadowedWarning, CLIConnectionError, CLINotFoundError, ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow, PermissionResultDeny, PermissionUpdate, ProcessError, RateLimitEvent,
    ResultMessage, SystemMessage, TextBlock, ToolPermissionContext, ToolUseBlock,
    get_session_info,
)

log = logging.getLogger("cc.claude")

# AUTO_ALLOW_TOOLS deliberately bypass the Telegram prompt; the SDK warns about that on every run.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

ENTRYPOINT = "claude-control"
BG_CONTINUATION_GRACE_S = 90  # after background tasks finish, wait this long for Claude's follow-up turn

EventCallback = Callable[..., Awaitable[None]]
PermissionCallback = Callable[[str, dict, ToolPermissionContext], Awaitable[Any]]


@dataclass
class TurnOutcome:
    status: str = "done"            # done | error | stopped
    answers: list[str] = field(default_factory=list)
    error: str | None = None        # short, human readable (Russian)
    details: str | None = None      # technical
    cost_usd: float | None = None
    duration_ms: int | None = None
    session_seen: str | None = None
    transcript_started: bool = False
    last_text: str = ""


SESSIONS_DIR = Path.home() / ".claude" / "sessions"   # Claude Code's own registry of open sessions


def live_sessions() -> dict[str, dict]:
    """Claude Code sessions open right now in VS Code / a terminal (read-only).

    Claude Code itself keeps ~/.claude/sessions/<pid>.json with the session id and a
    status ("busy" while working, "idle" otherwise). Our own runs are excluded."""
    out: dict[str, dict] = {}
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid = int(d.get("pid") or 0)
        except (OSError, ValueError):
            continue
        if d.get("sessionId") and d.get("entrypoint") != ENTRYPOINT and pid and Path(f"/proc/{pid}").exists():
            out[d["sessionId"]] = d
    return out


def transcript_exists(claude_session_id: str, cwd: str) -> bool:
    try:
        return get_session_info(claude_session_id, directory=cwd) is not None
    except Exception:  # noqa: BLE001 - unreadable/missing dir means "no transcript yet"
        return False


def tool_summary(name: str, inp: dict) -> str:
    """One short Russian line describing what Claude is doing right now (shown in the status message)."""
    def base(p: Any) -> str:
        return Path(str(p)).name if p else ""
    if name == "WebSearch":
        return f"🔎 Ищет: {inp.get('query', '')}"
    if name == "WebFetch":
        url = str(inp.get("url", ""))
        return f"🌐 Читает сайт: {url.split('/')[2] if url.count('/') >= 2 else url}"
    if name == "Read":
        return f"📄 Читает: {base(inp.get('file_path'))}"
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"✏️ Правит: {base(inp.get('file_path') or inp.get('notebook_path'))}"
    if name == "Bash":  # the description comes from Claude, often in English
        return f"⌨️ Выполняет команду: {inp.get('description') or str(inp.get('command', ''))[:60]}"
    if name in ("Grep", "Glob"):
        return f"🔍 Ищет в файлах: {inp.get('pattern', '')}"
    if name in ("Agent", "Task"):
        return f"🤖 Помощник: {inp.get('description', '')}"
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "📝 Планирует шаги"
    if name.startswith("mcp__"):
        return f"🔌 {name.split('__')[-1]}"
    return f"🛠 {name}"


def humanize_error(text: str) -> str:
    low = text.lower()
    if "usage limit" in low or "rate limit" in low or "limit reached" in low or "429" in low:
        return "Достигнут лимит использования Claude. Попробуйте позже."
    if "login" in low or "auth" in low or "401" in low or "credential" in low:
        return "Claude Code не может авторизоваться. Нужно войти заново в терминале сервера."
    if "overloaded" in low or "529" in low:
        return "Серверы Claude перегружены. Попробуйте ещё раз через пару минут."
    if "no conversation found" in low:
        return "Claude не нашёл историю этой сессии."
    return "Claude Code завершился с ошибкой."


class ClaudeTurn:
    def __init__(self, *, cli_path: str, cwd: str, claude_session_id: str, resume: bool, prompt: str,
                 model: str | None, permission_mode: str, allowed_tools: list[str],
                 disallowed_tools: list[str], add_dirs: list[str], max_seconds: float,
                 on_event: EventCallback, can_use_tool: PermissionCallback):
        self.cwd, self.claude_session_id, self.resume, self.prompt = cwd, claude_session_id, resume, prompt
        self.on_event, self.max_seconds = on_event, max_seconds
        self._stderr: list[str] = []
        self._client: ClaudeSDKClient | None = None
        self._stopping = False
        self.options = ClaudeAgentOptions(
            cli_path=cli_path,
            cwd=cwd,
            model=model,
            permission_mode=permission_mode,  # never bypassPermissions
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            add_dirs=add_dirs,
            can_use_tool=can_use_tool,
            env={"CLAUDE_CODE_ENTRYPOINT": ENTRYPOINT},
            stderr=self._on_stderr,
            **({"resume": claude_session_id} if resume else {"session_id": claude_session_id}),
        )

    def _on_stderr(self, line: str) -> None:
        self._stderr.append(line.rstrip())
        del self._stderr[:-40]

    @property
    def pid(self) -> int | None:
        try:
            return self._client._transport._process.pid  # type: ignore[union-attr]
        except AttributeError:
            return None

    async def stop(self) -> None:
        """Interrupt the current turn. History stays intact (verified: resume works after)."""
        self._stopping = True
        client = self._client
        if client is None:
            return
        try:
            await asyncio.wait_for(client.interrupt(), 10)
        except Exception as e:  # noqa: BLE001
            log.warning("interrupt failed (%s); terminating process", type(e).__name__)
            self._kill()

    def _kill(self) -> None:
        pid = self.pid
        if pid:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass

    async def run(self) -> TurnOutcome:
        out = TurnOutcome()
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self._run(out), self.max_seconds)
        except asyncio.TimeoutError:
            out.status, out.error = "error", f"Задача выполнялась дольше {self.max_seconds / 3600:g} ч и была остановлена."
            out.details = "max run time exceeded"
        except CLINotFoundError as e:
            out.status, out.error, out.details = "error", "Не найдена программа Claude Code на сервере.", str(e)
        except ProcessError as e:
            out.status = "stopped" if self._stopping else "error"
            out.error = humanize_error(f"{e} {e.stderr or ''} {' '.join(self._stderr)}")
            out.details = f"exit code: {e.exit_code}\n{e}\n\nstderr:\n" + "\n".join(self._stderr[-20:])
        except CLIConnectionError as e:
            out.status = "stopped" if self._stopping else "error"
            out.error = humanize_error(str(e))
            out.details = f"{type(e).__name__}: {e}\n\nstderr:\n" + "\n".join(self._stderr[-20:])
        except Exception as e:  # noqa: BLE001 - any failure must end as a clean ERROR state
            out.status = "stopped" if self._stopping else "error"
            out.error = "Внутренняя ошибка при работе с Claude."
            out.details = f"{type(e).__name__}: {e}\n\nstderr:\n" + "\n".join(self._stderr[-20:])
            log.exception("turn failed")
        finally:
            self._client = None
        if out.duration_ms is None:
            out.duration_ms = int((time.monotonic() - t0) * 1000)
        return out

    async def _run(self, out: TurnOutcome) -> None:
        async with ClaudeSDKClient(self.options) as client:
            self._client = client
            if self._stopping:  # /stop arrived while the process was starting
                out.status = "stopped"
                return
            await self.on_event("started", pid=self.pid)

            async def message():
                yield {"type": "user", "message": {"role": "user", "content": self.prompt},
                       "parent_tool_use_id": None, "origin": {"kind": "human"}}
            await client.query(message())

            bg_tasks: list = []
            turn_open = True           # a turn is in progress (a ResultMessage will follow)
            bg_emptied_at: float | None = None
            stream = client.receive_messages().__aiter__()
            while True:
                timeout = None
                if not turn_open and not bg_tasks:
                    # Background work finished; Claude normally starts a follow-up turn at once.
                    timeout = max(0.1, BG_CONTINUATION_GRACE_S - (time.monotonic() - (bg_emptied_at or 0)))
                try:
                    msg = await asyncio.wait_for(stream.__anext__(), timeout)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init":
                        turn_open = True
                        out.session_seen = msg.data.get("session_id")
                        out.transcript_started = True
                        if out.session_seen and out.session_seen != self.claude_session_id:
                            log.error("session id mismatch: expected %s got %s",
                                      self.claude_session_id[:8], out.session_seen[:8])
                    elif msg.subtype == "background_tasks_changed":
                        bg_tasks = list(msg.data.get("tasks") or [])
                        if not bg_tasks:
                            bg_emptied_at = time.monotonic()
                        await self.on_event("background", count=len(bg_tasks))
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            await self.on_event("tool", name=block.name, summary=tool_summary(block.name, block.input))
                        elif isinstance(block, TextBlock) and block.text.strip():
                            out.last_text = block.text
                elif isinstance(msg, RateLimitEvent):
                    await self.on_event("rate_limit", info=msg.rate_limit_info)
                elif isinstance(msg, ResultMessage):
                    turn_open = False
                    if msg.total_cost_usd is not None:
                        out.cost_usd = msg.total_cost_usd  # cumulative for this process
                    if self._stopping:
                        out.status = "stopped"
                        break
                    if msg.is_error:
                        text = msg.result or " ".join(msg.errors or []) or msg.subtype
                        out.status, out.error = "error", humanize_error(text)
                        out.details = (f"subtype: {msg.subtype}\nstop_reason: {msg.stop_reason}\n"
                                       f"api_error_status: {msg.api_error_status}\n{text}")
                        break
                    answer = (msg.result or out.last_text or "").strip()
                    out.answers.append(answer)
                    await self.on_event("answer", text=answer)
                    if not bg_tasks:
                        break
                    await self.on_event("background", count=len(bg_tasks))


def build_permission_result(decision: str, ctx: ToolPermissionContext, message: str = "") -> Any:
    """Map a Telegram decision to the SDK result. 'session' also applies the CLI's own
    suggestions for the rest of this run, but only in memory (destination=session) - it never
    writes to settings files shared with VS Code."""
    if decision == "once":
        return PermissionResultAllow()
    if decision == "session":
        updates = [PermissionUpdate(type=s.type, rules=s.rules, behavior=s.behavior, mode=s.mode,
                                    directories=s.directories, destination="session")
                   for s in ctx.suggestions]
        return PermissionResultAllow(updated_permissions=updates or None)
    return PermissionResultDeny(message=message or "The user denied this action from Telegram.",
                                interrupt=decision == "stop")


def grants_from_suggestions(ctx: ToolPermissionContext) -> list[dict]:
    """Serializable form of 'allow for this session' so later turns (new processes) keep it."""
    grants = []
    for s in ctx.suggestions:
        if s.type == "addRules" and s.behavior == "allow" and s.rules:
            for r in s.rules:
                grants.append({"rule": f"{r.tool_name}({r.rule_content})" if r.rule_content else r.tool_name})
        elif s.type == "setMode" and s.mode in ("acceptEdits",):
            grants.append({"mode": s.mode})
        elif s.type == "addDirectories" and s.directories:
            grants.extend({"dir": d} for d in s.directories)
    return grants
