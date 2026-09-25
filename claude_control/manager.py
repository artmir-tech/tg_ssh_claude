"""Session manager: queueing, the worker pool, permission requests and crash recovery.

Rules enforced here:
  * at most cfg.max_concurrent Claude processes at once (global FIFO over queued turns);
  * at most ONE active run per session - later messages wait in that session's queue;
  * every state change is written to SQLite first, so a restart can reconstruct everything.
The Telegram side (bot.py) is reached only through the `ui` object.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from claude_agent_sdk import fork_session, get_session_info, rename_session

from . import db as D
from .claude import (ClaudeTurn, TurnOutcome, build_permission_result, grants_from_suggestions, live_sessions,
                     transcript_exists)
from .config import Config
from .files import SERVER_NAME, SEND_TOOL, auto_sendable, is_secret, resolve, send_tool_server

log = logging.getLogger("cc.manager")

CONTINUE_PROMPT = "Продолжи с того места, где остановился."


@dataclass
class ActiveRun:
    session_id: int
    turn_id: int
    started_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    claude: ClaudeTurn | None = None
    stopping: bool = False
    activity: str = ""
    steps: int = 0
    background: int = 0
    waiting: str | None = None          # None | "approval" | "question"
    status_msg_id: int | None = None
    last_render: str = ""
    last_edit: float = 0.0
    answers_sent: int = 0
    written: list[str] = field(default_factory=list)   # files Claude wrote this run (sent after the answer)
    sent: set[str] = field(default_factory=set)        # files already delivered to the topic this run


@dataclass
class PendingRequest:
    id: str
    session_id: int
    kind: str                           # "perm" | "ask"
    tool_name: str
    input: dict
    ctx: ToolPermissionContext | None = None
    future: asyncio.Future | None = None
    message_id: int | None = None
    questions: list = field(default_factory=list)
    q_index: int = 0
    answers: dict = field(default_factory=dict)
    selected: set = field(default_factory=set)
    text: str = ""                      # rendered request, re-used when the verdict is added

    @property
    def question(self) -> dict:
        return self.questions[self.q_index] if self.questions else {}


class Manager:
    def __init__(self, cfg: Config, db: D.Registry):
        self.cfg, self.db = cfg, db
        self.ui: Any = None                 # bot.Bot, set after construction
        self.active: dict[int, ActiveRun] = {}
        self.requests: dict[str, PendingRequest] = {}
        self.started_at = time.time()

    # ---- sessions ------------------------------------------------------------------------
    def default_cwd(self) -> str:
        return self.db.kv_get("default_cwd") or self.cfg.default_cwd

    def create_session(self, chat_id: int, topic_id: int, title: str, *, title_source: str = "user",
                       cwd: str | None = None, claude_session_id: str | None = None, started: bool = False,
                       origin: str = "telegram") -> D.Session:
        s = self.db.create_session(
            chat_id=chat_id, topic_id=topic_id, claude_session_id=claude_session_id or str(uuid.uuid4()),
            title=title, title_source=title_source, cwd=cwd or self.default_cwd(), started=started,
            origin=origin, status=D.IDLE if started else D.NEW)
        if origin != "telegram":
            self.db.update_session(s.id, title_synced=1)
        if self.ui:
            self.ui.dashboard_changed()
        log.info("session created id=%s topic=%s claude=%s origin=%s cwd=%s",
                 s.id, topic_id, s.claude_session_id[:8], origin, s.cwd)
        return self.db.get_session(s.id)

    def inbox_dir(self, s: D.Session) -> Path:
        d = self.cfg.inbox_root / str(s.id)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def set_status(self, session_id: int, status: str, **extra: Any) -> None:
        s = self.db.get_session(session_id)
        if s and s.status != status:
            log.info("session %s status %s -> %s", session_id, s.status, status)
            if self.ui:
                self.ui.dashboard_changed()
        self.db.update_session(session_id, status=status, **extra)

    def _settle_status(self, session_id: int) -> None:
        """Status for a session with no active run: QUEUED if work is waiting."""
        if session_id in self.active:
            return
        s = self.db.get_session(session_id)
        if s is None:
            return
        if s.archived:
            self.set_status(session_id, D.ARCHIVED)
        elif self.db.queued_count(session_id):
            self.set_status(session_id, D.QUEUED)
        elif s.status in (D.QUEUED, *D.ACTIVE_STATUSES):
            self.set_status(session_id, D.IDLE)

    # ---- queue ---------------------------------------------------------------------------
    def enqueue(self, s: D.Session, prompt: str, chat_id: int | None = None,
                message_id: int | None = None) -> D.Turn | None:
        turn = self.db.add_turn(s.id, prompt, chat_id, message_id)
        if turn is None:
            log.info("duplicate message %s ignored (session %s)", message_id, s.id)
            return None
        if s.archived:
            self.db.update_session(s.id, archived=0)
        self.db.touch(s.id)
        log.info("turn %s queued for session %s (%d chars)", turn.id, s.id, len(prompt))
        self._settle_status(s.id)
        return turn

    def queue_position(self, session_id: int) -> int | None:
        """1-based place among sessions waiting for a free worker (None if running/not queued)."""
        if session_id in self.active:
            return None
        blocked = self.busy_elsewhere()
        waiting = [sid for sid in self.db.queued_sessions_in_order() if sid not in self.active and sid not in blocked]
        return waiting.index(session_id) + 1 if session_id in waiting else None

    def busy_elsewhere(self) -> set[int]:
        """Registry ids of our sessions that Claude Code is running right now in VS Code/terminal."""
        ids = set()
        for claude_id, d in live_sessions().items():
            if d.get("status") != "idle":
                s = self.db.session_by_claude_id(claude_id)
                if s:
                    ids.add(s.id)
        return ids

    def schedule(self) -> None:
        blocked = self.busy_elsewhere() if self.db.turns_with_status("queued") else set()
        while len(self.active) < self.cfg.max_concurrent:
            turn = self.db.next_runnable_turn(set(self.active) | blocked)
            if turn is None:
                break
            self._start(turn)
        for sid in self.db.queued_sessions_in_order():
            self._settle_status(sid)

    def _start(self, turn: D.Turn) -> None:
        ar = ActiveRun(session_id=turn.session_id, turn_id=turn.id, status_msg_id=turn.status_msg_id)
        self.active[turn.session_id] = ar
        now = time.time()
        self.db.update_turn(turn.id, status="running", started_at=now)
        self.set_status(turn.session_id, D.RUNNING, run_started_at=now, last_error=None)
        log.info("worker start session=%s turn=%s (%d/%d busy)", turn.session_id, turn.id,
                 len(self.active), self.cfg.max_concurrent)
        ar.task = asyncio.create_task(self._run(ar, turn), name=f"run-{turn.session_id}")

    # ---- one run ---------------------------------------------------------------------------
    def _claude_options(self, s: D.Session) -> dict[str, Any]:
        grants = s.grant_list
        mode = next((g["mode"] for g in reversed(grants) if "mode" in g), s.permission_mode)
        if mode == "bypassPermissions":  # never, whatever is stored
            mode = "default"
        deny = []
        for p in self.cfg.secret_paths:
            deny += [f"Read(/{p})", f"Edit(/{p})", f"Write(/{p})"]
        return dict(
            model=s.model or self.cfg.model,
            permission_mode=mode,
            allowed_tools=self.cfg.auto_allow_tools + [g["rule"] for g in grants if "rule" in g],
            disallowed_tools=deny,
            add_dirs=[str(self.inbox_dir(s))] + [g["dir"] for g in grants if "dir" in g],
        )

    async def _run(self, ar: ActiveRun, turn: D.Turn) -> None:
        sid = ar.session_id
        outcome = TurnOutcome(status="error", error="Внутренняя ошибка.")
        try:
            s = self.db.get_session(sid)
            try:
                await self.ui.run_started(s, turn, ar)
            except Exception:  # noqa: BLE001 - Telegram trouble must not block the run
                log.exception("run_started UI failed")
            resume = bool(s.started) or transcript_exists(s.claude_session_id, s.cwd)
            if resume and not s.started:
                self.db.update_session(sid, started=1)
            if ar.stopping:
                outcome = TurnOutcome(status="stopped")
            else:
                ar.claude = ClaudeTurn(
                    cli_path=self.cfg.claude_cli, cwd=s.cwd, claude_session_id=s.claude_session_id,
                    resume=resume, prompt=turn.prompt, max_seconds=self.cfg.max_run_hours * 3600,
                    on_event=self._event_handler(ar, turn), can_use_tool=self._permission_handler(ar),
                    mcp_servers={SERVER_NAME: send_tool_server(self._file_sender(ar))},
                    **self._claude_options(s))
                log.info("claude run session=%s claude=%s mode=%s", sid, s.claude_session_id[:8],
                         "resume" if resume else "new")
                outcome = await ar.claude.run()
            log.info("claude exit session=%s turn=%s status=%s duration=%ss cost=%s",
                     sid, turn.id, outcome.status, (outcome.duration_ms or 0) // 1000, outcome.cost_usd)
        except asyncio.CancelledError:
            # Service shutdown: leave the turn 'running' so recover() reports it after restart.
            self.active.pop(sid, None)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("run crashed session=%s", sid)
            outcome = TurnOutcome(status="error", error="Внутренняя ошибка Claude Control.",
                                  details=f"{type(e).__name__}: {e}")
        await self._finish(ar, turn, outcome)

    async def _finish(self, ar: ActiveRun, turn: D.Turn, outcome: TurnOutcome) -> None:
        sid = ar.session_id
        now = time.time()
        try:
            if outcome.transcript_started:
                self.db.update_session(sid, started=1)
            self.db.update_turn(turn.id, status=outcome.status, finished_at=now, error=outcome.error,
                                details=outcome.details, cost_usd=outcome.cost_usd, duration_ms=outcome.duration_ms)
            status = {"done": D.IDLE, "stopped": D.STOPPED}.get(outcome.status, D.ERROR)
            self.active.pop(sid, None)
            self.set_status(sid, status, current_pid=None, run_started_at=None, last_activity_at=now,
                            last_error=outcome.error if status == D.ERROR else None)
            if self.db.queued_count(sid) and status != D.ERROR:
                self.set_status(sid, D.QUEUED)
            s = self.db.get_session(sid)
            try:
                await self._flush_files(ar)   # also after an error/stop: what was written is there
                await self.ui.run_finished(s, turn, outcome, ar)
            except Exception:  # noqa: BLE001
                log.exception("run_finished UI failed")
            if outcome.status == "done":
                await self._after_success(s)
            elif not s.title_synced and s.title_source == "user":   # stopped/failed: still keep names in sync
                await self.sync_title(s)
        finally:
            self.active.pop(sid, None)
            self.schedule()

    def _event_handler(self, ar: ActiveRun, turn: D.Turn):
        async def on_event(kind: str, **d: Any) -> None:
            if kind == "started":
                self.db.update_session(ar.session_id, current_pid=d.get("pid"))
                log.info("claude process pid=%s session=%s", d.get("pid"), ar.session_id)
            elif kind == "tool":
                ar.steps += 1
                ar.activity = d["summary"]
                if d["name"] == "Write" and (d.get("input") or {}).get("file_path"):
                    path = str(resolve(d["input"]["file_path"], self.db.get_session(ar.session_id).cwd))
                    if path not in ar.written:
                        ar.written.append(path)
            elif kind == "background":
                ar.background = d["count"]
            elif kind == "answer":
                ar.answers_sent += 1
                self.db.touch(ar.session_id)
                await self.ui.deliver_answer(self.db.get_session(ar.session_id), turn, d["text"], ar)
                await self._flush_files(ar)
            elif kind == "rate_limit":
                info = d["info"]
                self.db.kv_set("rate_limit", json.dumps({
                    "status": info.status, "type": info.rate_limit_type, "utilization": info.utilization,
                    "resets_at": info.resets_at, "raw": info.raw, "at": time.time()}))
        return on_event

    # ---- files -----------------------------------------------------------------------------
    def _file_sender(self, ar: ActiveRun):
        async def send(path: str, caption: str) -> str:   # the send_file tool (runs inside this process)
            return await self.ui.send_files(self.db.get_session(ar.session_id), [path], caption=caption,
                                            ar=ar, requested=True)
        return send

    async def _flush_files(self, ar: ActiveRun) -> None:
        """Send files Claude wrote with the Write tool during this run (final versions)."""
        s = self.db.get_session(ar.session_id)
        written, ar.written = ar.written, []
        if not s.send_files:
            return
        fresh = []
        for p in written:
            path = Path(p)
            try:
                if p not in ar.sent and auto_sendable(path) and path.stat().st_mtime >= ar.started_at - 2:
                    fresh.append(p)
            except OSError:
                continue
        if fresh:
            await self.ui.send_files(s, fresh, ar=ar)

    async def _after_success(self, s: D.Session) -> None:
        """Topic auto-naming and keeping the Claude session title in sync with the topic."""
        try:
            if s.title_source == "placeholder":
                info = await asyncio.to_thread(get_session_info, s.claude_session_id, s.cwd)
                title = info and (info.custom_title or info.summary)
                if title and len(title) <= 80:
                    await self.ui.rename_topic(s, title)
                    self.db.update_session(s.id, title=title, title_source="auto", title_synced=1)
                    log.info("session %s auto-named", s.id)
            elif not s.title_synced and s.started:
                await asyncio.to_thread(rename_session, s.claude_session_id, s.title, s.cwd)
                self.db.update_session(s.id, title_synced=1)
        except Exception:  # noqa: BLE001 - naming is cosmetic
            log.exception("title sync failed for session %s", s.id)

    # ---- permissions & questions -------------------------------------------------------------
    def _permission_handler(self, ar: ActiveRun):
        async def can_use_tool(tool_name: str, inp: dict, ctx: ToolPermissionContext):
            if ar.stopping:
                return PermissionResultDeny(message="Stopped by the user.", interrupt=True)
            if tool_name == "AskUserQuestion":
                return await self._ask(ar, inp)
            if tool_name == SEND_TOOL:   # files leaving the server
                s = self.db.get_session(ar.session_id)
                path = resolve(str(inp.get("path", "")), s.cwd)
                if is_secret(path):
                    return PermissionResultDeny(message="This file holds secrets (keys, tokens, logins) and is "
                                                        "never sent out of the server.")
                if path.is_relative_to(Path(s.cwd).resolve()) or path.is_relative_to(self.inbox_dir(s).resolve()):
                    return PermissionResultAllow()
                # anywhere else: the owner decides with the usual buttons
            req = PendingRequest(id=secrets.token_hex(4), session_id=ar.session_id, kind="perm",
                                 tool_name=tool_name, input=inp, ctx=ctx,
                                 future=asyncio.get_running_loop().create_future())
            self.requests[req.id] = req
            ar.waiting = "approval"
            self.set_status(ar.session_id, D.WAITING_APPROVAL)
            log.info("permission requested session=%s tool=%s req=%s", ar.session_id, tool_name, req.id)
            try:
                await self.ui.permission_request(self.db.get_session(ar.session_id), req)
                decision, message = await asyncio.wait_for(req.future, self.cfg.permission_timeout_s)
            except asyncio.TimeoutError:
                decision, message = "deny", "The user did not answer in time; the action was not performed."
                await self.ui.request_expired(req)
            finally:
                self.requests.pop(req.id, None)
                ar.waiting = None
                if ar.session_id in self.active:
                    self.set_status(ar.session_id, D.RUNNING)
            if decision == "session":
                self._save_grants(ar.session_id, grants_from_suggestions(ctx))
            log.info("permission %s session=%s tool=%s", decision, ar.session_id, tool_name)
            return build_permission_result(decision, ctx, message)
        return can_use_tool

    def _save_grants(self, session_id: int, new: list[dict]) -> None:
        s = self.db.get_session(session_id)
        grants = s.grant_list
        grants += [g for g in new if g not in grants]
        self.db.update_session(session_id, grants=json.dumps(grants, ensure_ascii=False))

    async def _ask(self, ar: ActiveRun, inp: dict):
        questions = [q for q in inp.get("questions") or [] if q.get("question")]
        req = PendingRequest(id=secrets.token_hex(4), session_id=ar.session_id, kind="ask",
                             tool_name="AskUserQuestion", input=inp, questions=questions)
        self.requests[req.id] = req
        ar.waiting = "question"
        self.set_status(ar.session_id, D.WAITING)
        try:
            for i, q in enumerate(questions):
                req.q_index, req.selected = i, set()
                req.future = asyncio.get_running_loop().create_future()
                await self.ui.ask_question(self.db.get_session(ar.session_id), req)
                decision, answer = await asyncio.wait_for(req.future, self.cfg.permission_timeout_s)
                if decision == "stop":
                    return PermissionResultDeny(message="Stopped by the user.", interrupt=True)
                req.answers[q["question"]] = answer
        except asyncio.TimeoutError:
            await self.ui.request_expired(req)
            return PermissionResultDeny(message="The user did not answer in time.")
        finally:
            self.requests.pop(req.id, None)
            ar.waiting = None
            if ar.session_id in self.active:
                self.set_status(ar.session_id, D.RUNNING)
        return PermissionResultAllow(updated_input={**inp, "answers": req.answers})

    def resolve(self, req_id: str, decision: str, message: str = "") -> PendingRequest | None:
        req = self.requests.get(req_id)
        if req is None or req.future is None or req.future.done():
            return None
        req.future.set_result((decision, message))
        return req

    def pending_request(self, session_id: int) -> PendingRequest | None:
        return next((r for r in self.requests.values() if r.session_id == session_id), None)

    # ---- user actions ---------------------------------------------------------------------
    async def stop(self, session_id: int) -> tuple[bool, list[D.Turn]]:
        """Stop the current run and drop queued turns. Returns (was_running, cancelled turns)."""
        cancelled = self.db.cancel_queued(session_id)
        ar = self.active.get(session_id)
        for req in [r for r in self.requests.values() if r.session_id == session_id]:
            if self.resolve(req.id, "stop"):
                await self.ui.close_request(req, "⏹ Запрос отменён — задача остановлена.")
        if ar:
            ar.stopping = True
            log.info("stop requested session=%s", session_id)
            if ar.claude:
                await ar.claude.stop()
        else:
            self._settle_status(session_id)
        return ar is not None, cancelled

    def retry(self, turn_id: int) -> D.Turn | None:
        t = self.db.get_turn(turn_id)
        if t is None:
            return None
        s = self.db.get_session(t.session_id)
        new = self.enqueue(s, t.prompt)
        self.schedule()
        return new

    def continue_session(self, session_id: int) -> D.Turn | None:
        new = self.enqueue(self.db.get_session(session_id), CONTINUE_PROMPT)
        self.schedule()
        return new

    def fork(self, s: D.Session, title: str) -> str:
        res = fork_session(s.claude_session_id, directory=s.cwd, title=title)
        log.info("session %s forked -> %s", s.id, res.session_id[:8])
        return res.session_id

    async def sync_title(self, s: D.Session) -> None:
        """Write the topic title into the Claude session, so VS Code shows the same name. Safe while
        the session is running: the SDK appends one line with O_APPEND (atomic next to Claude's writes)."""
        if s.started or transcript_exists(s.claude_session_id, s.cwd):
            try:
                await asyncio.to_thread(rename_session, s.claude_session_id, s.title, s.cwd)
                self.db.update_session(s.id, title_synced=1)
            except Exception:  # noqa: BLE001
                log.exception("rename_session failed for %s", s.id)

    # ---- startup / shutdown ------------------------------------------------------------------
    async def sync_pending_titles(self) -> None:
        for s in self.db.sessions(include_archived=True):
            if not s.title_synced and (s.started or transcript_exists(s.claude_session_id, s.cwd)):
                await self.sync_title(s)
                log.info("session %s title synced to Claude", s.id)

    async def recover(self) -> None:
        """After a restart: runs that were in flight are gone - mark them and tell the user."""
        for turn in self.db.turns_with_status("running"):
            s = self.db.get_session(turn.session_id)
            self._kill_orphan(s)
            self.db.update_turn(turn.id, status="interrupted", finished_at=time.time(),
                                error="Сервис был перезапущен во время выполнения.")
            started = s.started or transcript_exists(s.claude_session_id, s.cwd)
            self.db.update_session(s.id, status=D.STOPPED, current_pid=None, run_started_at=None,
                                   started=int(bool(started)))
            log.warning("recovered interrupted run session=%s turn=%s", s.id, turn.id)
            try:
                await self.ui.run_interrupted(self.db.get_session(s.id), turn)
            except Exception:  # noqa: BLE001
                log.exception("notify interrupted failed")
        for s in self.db.sessions(include_archived=True):
            if s.status in D.ACTIVE_STATUSES or (s.status == D.QUEUED and not self.db.queued_count(s.id)):
                self.set_status(s.id, D.IDLE, current_pid=None)
        await self.sync_pending_titles()
        self.schedule()

    def _kill_orphan(self, s: D.Session) -> None:
        """If a Claude process of this session survived our crash, stop it (we can't reattach)."""
        pid = s.current_pid
        if not pid:
            return
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except OSError:
            return
        if s.claude_session_id in cmdline:
            os.kill(pid, 15)
            log.warning("terminated orphan claude pid=%s session=%s", pid, s.id)

    async def shutdown(self) -> None:
        tasks = [ar.task for ar in self.active.values() if ar.task]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
