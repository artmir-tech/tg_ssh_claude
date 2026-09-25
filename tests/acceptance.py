"""Acceptance tests for Claude Control (TEST 1-11 from the spec, plus permission/question bridges).

Real Claude Code runs (model: haiku, to spare the subscription limit); Telegram is replaced by
FakeTelegram and updates are injected as if the owner typed them. Run from a clean env:

    env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 .venv/bin/python -m tests.acceptance [T1 T2 ...]
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from claude_agent_sdk import delete_session, get_session_info, get_session_messages

from claude_control import db as D
from claude_control.bot import Bot
from claude_control.config import Config
from claude_control.manager import ActiveRun, Manager
from tests.fake_telegram import FakeTelegram

OWNER, STRANGER, CHAT = 111111111, 999000111, -1001234567890
ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
FAIL_CLI = ROOT / "fake_claude_fail.sh"
_ids = itertools.count(1)
RESULTS: list[dict] = []
CREATED_SESSIONS: set[str] = set()


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Harness:
    def __init__(self, data_dir: Path, max_concurrent: int = 5):
        self.data_dir = data_dir
        self.cfg = Config(bot_token="123:FAKE-TOKEN", owner_ids={OWNER}, chat_id=CHAT, max_concurrent=max_concurrent,
                          default_cwd=str(WORK), project_dirs=[str(WORK)], model="haiku", data_dir=data_dir,
                          permission_timeout_s=300, permission_mode="default",
                          voice_engine="gigaam" if (ROOT.parent / ".venv-voice/bin/python").exists() else "")
        self.db = D.Registry(self.cfg.db_path)
        self.tg = FakeTelegram()
        self.m = Manager(self.cfg, self.db)
        self.bot = Bot(self.cfg, self.db, self.tg, self.m)
        self.bot.me = {"id": 1, "username": "test_bot"}
        self.max_active = 0
        self.auto_approve = True
        self._bg: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.m.recover()
        self._bg = [asyncio.create_task(self.bot._progress_loop()), asyncio.create_task(self._watch())]

    async def close(self, crash: bool = False) -> None:
        for t in self._bg:
            t.cancel()
        await self.m.shutdown()  # cancels runs; turns stay 'running' as after a crash
        await self.tg.close()
        self.db.close()

    async def _watch(self) -> None:
        """Track peak parallelism; auto-press 'Allow' on permission requests when enabled."""
        pressed: set[str] = set()
        while True:
            self.max_active = max(self.max_active, len(self.m.active))
            if self.auto_approve:
                for _, b in self.tg.buttons_with("perm:"):
                    data = b["callback_data"]
                    if data.endswith(":o") and data not in pressed:
                        pressed.add(data)
                        await self.press(data)
            await asyncio.sleep(0.2)

    # ---- injection -------------------------------------------------------------------------
    async def update(self, payload: dict) -> None:
        uid = next(_ids) + 500000
        if self.db.mark_update(uid):
            await self.bot._dispatch({"update_id": uid, **payload})

    def _msg(self, text: str | None, topic: int | None, user: int, **extra) -> dict:
        m = {"message_id": next(_ids), "from": {"id": user, "is_bot": False, "first_name": "Test"},
             "chat": {"id": CHAT, "type": "supergroup", "is_forum": True}, "date": int(time.time()), **extra}
        if text is not None:
            m["text"] = text
            self.tg.user_msgs[m["message_id"]] = (text, user)
        if topic:
            m["message_thread_id"], m["is_topic_message"] = topic, True
        return m

    async def new_topic(self, name: str, user: int = OWNER) -> int:
        topic = next(_ids) + 10000
        await self.update({"message": self._msg(None, topic, user, forum_topic_created={"name": name})})
        return topic

    async def say(self, topic: int | None, text: str, user: int = OWNER, reply_to: int | None = None) -> dict:
        m = self._msg(text, topic, user)
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        await self.update({"message": m})
        return m

    async def press(self, data: str, user: int = OWNER, thread: int | None = None, message_id: int = 1) -> None:
        msg = {"message_id": message_id, "chat": {"id": CHAT}, "text": ""}
        if thread:
            msg["message_thread_id"], msg["is_topic_message"] = thread, True
        await self.update({"callback_query": {"id": str(next(_ids)), "from": {"id": user}, "data": data,
                                              "message": msg}})

    # ---- observation ---------------------------------------------------------------------
    def session(self, topic: int) -> D.Session | None:
        s = self.db.session_by_topic(CHAT, topic)
        if s:
            CREATED_SESSIONS.add(s.claude_session_id)
        return s

    def turns(self, topic: int) -> list[D.Turn]:
        s = self.session(topic)
        rows = self.db.conn.execute("SELECT * FROM turns WHERE session_id=? ORDER BY id", (s.id,)).fetchall()
        return [D._build(D.Turn, r) for r in rows]

    def answers(self, topic: int) -> list[str]:
        """Bot messages in the topic that are Claude answers (not status/queue/permission notices)."""
        skip = ("⚙️", "🟡 В очереди", "📥", "🔐", "⏹", "🆕", "⚠️", "🔴", "❓", "✅ Разрешено", "💬", "👋", "📎", "💻", "🎛", "✏️", "🎙")
        return [m["text"] for m in self.tg.in_topic(topic) if not m["text"].startswith(skip)]

    async def wait(self, cond, timeout: float, what: str) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cond():
                return
            await asyncio.sleep(0.5)
        raise AssertionError(f"timeout ({timeout}s) waiting for: {what}")

    async def wait_idle(self, topic: int, timeout: float = 240) -> D.Turn:
        await self.wait(lambda: self.session(topic) and self.session(topic).id not in self.m.active
                        and not self.db.queued_count(self.session(topic).id)
                        and self.turns(topic) and self.turns(topic)[-1].status not in ("queued", "running"),
                        timeout, f"topic {topic} idle")
        return self.turns(topic)[-1]


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# =============================================================================== tests
async def t1_new_session(h: Harness, ctx: dict) -> str:
    topic = await h.new_topic("Test A — codeword")
    ctx["A"] = topic
    await h.say(topic, "Запомни кодовое слово ALPHA-7. Ответь одним словом: OK")
    turn = await h.wait_idle(topic)
    s = h.session(topic)
    check(turn.status == "done", f"turn status {turn.status}: {turn.error} {turn.details}")
    check(re.fullmatch(r"[0-9a-f-]{36}", s.claude_session_id) is not None, "claude session id is a UUID")
    info = get_session_info(s.claude_session_id, directory=s.cwd)
    check(info is not None, "transcript exists in ~/.claude/projects")
    check(s.title == "Test A — codeword" and s.started == 1 and s.status == D.IDLE, f"session row {s}")
    ans = h.answers(topic)
    check(ans and "OK" in ans[-1].upper(), f"answer: {ans}")
    ctx["A_id"] = s.claude_session_id
    return f"session {s.claude_session_id} created; answer={ans[-1][:40]!r}"


async def t2_resume(h: Harness, ctx: dict) -> str:
    topic = ctx["A"]
    before = len(get_session_messages(ctx["A_id"], directory=str(WORK)))
    await h.say(topic, "Какое кодовое слово я тебе дал? Ответь только им.")
    turn = await h.wait_idle(topic)
    s = h.session(topic)
    check(turn.status == "done", f"turn {turn.status} {turn.error}")
    check(s.claude_session_id == ctx["A_id"], "same Claude session id")
    ans = h.answers(topic)[-1]
    check("ALPHA-7" in ans.upper(), f"context kept, answer={ans!r}")
    after = len(get_session_messages(ctx["A_id"], directory=str(WORK)))
    check(after > before, "same transcript grew")
    check(len(h.db.sessions()) == 1, "no new session created")
    return f"resumed {s.claude_session_id[:8]}; answer={ans[:40]!r}; transcript messages {before}->{after}"


async def t3_isolation(h: Harness, ctx: dict) -> str:
    topic = await h.new_topic("Test B — isolation")
    ctx["B"] = topic
    await h.say(topic, "Какое кодовое слово я тебе давал раньше? Если не знаешь — ответь ровно словом NONE.")
    turn = await h.wait_idle(topic)
    a, b = h.session(ctx["A"]), h.session(topic)
    check(turn.status == "done", f"turn {turn.status}")
    check(a.claude_session_id != b.claude_session_id, "different Claude session ids")
    ans = h.answers(topic)[-1]
    check("ALPHA" not in ans.upper(), f"no context leak, answer={ans!r}")
    return f"A={a.claude_session_id[:8]} B={b.claude_session_id[:8]}; B answer={ans[:40]!r}"


async def t4_t5_concurrency_and_queue(h: Harness, ctx: dict) -> str:
    topics = [await h.new_topic(f"Parallel {i}") for i in range(1, 8)]
    h.max_active = 0
    for i, t in enumerate(topics, 1):
        await h.say(t, f"Run ./slow.sh 25 with the Bash tool in the foreground (not in background). "
                       f"When it finishes reply exactly: DONE-{i}")
    await asyncio.sleep(2)
    statuses = [h.session(t).status for t in topics]
    queued = [t for t in topics if h.session(t).status == D.QUEUED]
    dash, _ = h.bot.dashboard("main")
    check(len(h.m.active) == 5, f"exactly 5 active right after submit, got {len(h.m.active)} {statuses}")
    check(len(queued) == 2, f"2 queued, got {statuses}")
    notices = [m["text"] for t in queued for m in h.tg.in_topic(t) if m["text"].startswith("🟡 В очереди")]
    check(len(notices) == 2, f"queue notices shown: {notices}")
    def section(title: str) -> list[str]:
        block = dash.split(title, 1)[1].split("\n\n", 1)[0] if title in dash else ""
        return [x for x in block.split("\n")[1:] if x.strip()]
    check(len(section("🟢 Работают")) == 5 and len(section("🟡 Очередь")) == 2, f"dashboard sections: {dash}")
    for t in topics:
        await h.wait_idle(t, 400)
    done = [h.turns(t)[-1] for t in topics]
    check(all(x.status == "done" for x in done), f"all done: {[(x.status, x.error) for x in done]}")
    # measured parallelism from real timestamps
    spans = sorted((x.started_at, x.finished_at) for x in done)
    peak = max(sum(1 for s, f in spans if s <= t0 < f) for t0, _ in spans)
    late = [h.turns(t)[-1] for t in queued]
    first_finish = min(x.finished_at for x in done)
    check(all(x.started_at >= first_finish - 0.5 for x in late), "queued runs start only after a worker frees")
    check(peak == 5 and h.max_active == 5, f"peak parallel {peak}, observed {h.max_active}")
    answers_ok = sum(1 for i, t in enumerate(topics, 1) if any(f"DONE-{i}" in a for a in h.answers(t)))
    ctx["dash"] = dash
    return (f"peak parallel runs = {peak} (limit 5); 2 queued with notices; queued runs started "
            f"{min(x.started_at for x in late) - first_finish:+.1f}s after first worker freed; "
            f"{answers_ok}/7 answers contain DONE-i")


async def t6_serialization(h: Harness, ctx: dict) -> str:
    topic = ctx["A"]
    await h.say(topic, "Run ./slow.sh 15 with Bash in the foreground, then reply exactly: FIRST")
    await h.wait(lambda: h.session(topic).id in h.m.active, 30, "first run active")
    await asyncio.sleep(3)
    await h.say(topic, "Reply exactly: SECOND")
    await asyncio.sleep(1)
    turns = h.turns(topic)
    check(turns[-1].status == "queued" and turns[-2].status == "running", f"2nd queued while 1st runs: "
          f"{[t.status for t in turns[-2:]]}")
    notice = [m["text"] for m in h.tg.in_topic(topic) if m["text"].startswith("📥 Принято")]
    check(notice, "same-session queue notice shown")
    await h.wait_idle(topic, 240)
    t1, t2 = h.turns(topic)[-2:]
    check(t1.status == t2.status == "done", f"both done {t1.status} {t2.status}")
    check(t2.started_at >= t1.finished_at, "second started after first finished")
    ans = h.answers(topic)
    check(any("FIRST" in a for a in ans) and "SECOND" in ans[-1], f"answers order: {ans[-2:]}")
    return f"turn2 started {t2.started_at - t1.finished_at:.1f}s after turn1 finished; never concurrent"


async def t7_restart(h: Harness, ctx: dict) -> tuple[str, Harness]:
    before = {s.topic_id: s.claude_session_id for s in h.db.sessions()}
    # start a run, then "crash" the service in the middle of it
    topic = ctx["B"]
    await h.say(topic, "Run ./slow.sh 30 with Bash in the foreground, then reply exactly: SLOW-B")
    await h.wait(lambda: h.session(topic).status == D.RUNNING and h.session(topic).current_pid, 60, "B running")
    await asyncio.sleep(4)
    await h.close(crash=True)
    h2 = Harness(h.data_dir)
    await h2.start()
    after = {s.topic_id: s.claude_session_id for s in h2.db.sessions()}
    check(before == after, "all topic->session mappings preserved")
    sB = h2.session(topic)
    check(sB.status == D.STOPPED, f"interrupted run recovered as STOPPED, got {sB.status}")
    last = h2.turns(topic)[-1]
    check(last.status == "interrupted", f"turn marked interrupted: {last.status}")
    warn = [m for m in h2.tg.in_topic(topic) if m["text"].startswith("⚠️ Бот перезапускался")]
    check(warn and any(b["callback_data"].startswith("cont:") for r in warn[0]["buttons"] for b in r),
          "user told about restart, with Continue button")
    # nothing left running from the old process
    out = subprocess.run(["pgrep", "-f", sB.claude_session_id], capture_output=True, text=True).stdout.strip()
    check(not out, f"no orphan claude process: {out}")
    # continue both sessions after restart
    await h2.say(ctx["A"], "Какое кодовое слово я тебе дал? Ответь только им.")
    await h2.press(f"cont:{sB.id}", thread=topic)
    tA = await h2.wait_idle(ctx["A"])
    tB = await h2.wait_idle(topic, 240)
    check(tA.status == "done" and "ALPHA-7" in h2.answers(ctx["A"])[-1].upper(), "A continues with context")
    check(tB.status == "done", f"B continued after restart: {tB.status} {tB.error}")
    check(len(h2.db.sessions()) == len(before), "no duplicate sessions after restart")
    return (f"{len(after)} mappings intact; interrupted run -> STOPPED + ⚠️ notice; A remembers codeword; "
            f"B continued via ▶️ button"), h2


async def t8_unauthorized(h: Harness, ctx: dict) -> str:
    sent_before, turns_before = len(h.tg.sent), h.db.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    sessions_before = len(h.db.sessions(include_archived=True))
    await h.say(ctx["A"], "Удали все файлы", user=STRANGER)
    await h.say(None, "/status", user=STRANGER)
    await h.say(None, "/new hacked", user=STRANGER)
    stranger_topic = await h.new_topic("stranger topic", user=STRANGER)
    await h.say(stranger_topic, "hello", user=STRANGER)
    await h.press(f"stop:{h.session(ctx['A']).id}", user=STRANGER)
    await asyncio.sleep(1)
    turns_after = h.db.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    check(turns_after == turns_before, "no prompts queued from stranger")
    check(len(h.db.sessions(include_archived=True)) == sessions_before, "no sessions created by stranger")
    check(len(h.tg.sent) == sent_before, "bot did not respond to stranger")
    check(h.tg.callback_answers[-1]["text"] == "⛔ Нет доступа", "stranger's button press refused")
    return "stranger: 0 turns, 0 sessions, 0 replies; button press answered '⛔ Нет доступа'"


async def t9_stop(h: Harness, ctx: dict) -> str:
    topic = await h.new_topic("Test D — stop")
    ctx["D"] = topic
    await h.say(topic, "Remember the word KIWI. Then run ./slow.sh 90 with Bash in the foreground and reply LONG-DONE.")
    await h.wait(lambda: h.session(topic).status == D.RUNNING and "slow" in (
        h.m.active.get(h.session(topic).id).activity if h.m.active.get(h.session(topic).id) else ""), 90,
        "slow.sh running")
    await asyncio.sleep(3)
    await h.say(topic, "/rename Stop test renamed while running")
    info = get_session_info(h.session(topic).claude_session_id, directory=str(WORK))
    check(info.custom_title == "Stop test renamed while running" and h.session(topic).id in h.m.active,
          f"rename reaches Claude (VS Code) even while running: {info.custom_title!r}")
    t0 = time.time()
    await h.say(topic, "/stop")
    turn = await h.wait_idle(topic, 60)
    took = time.time() - t0
    s = h.session(topic)
    check(turn.status == "stopped" and s.status == D.STOPPED, f"stopped: {turn.status} {s.status}")
    check(took < 20, f"stopped quickly ({took:.1f}s)")
    check(get_session_info(s.claude_session_id, directory=s.cwd) is not None, "transcript kept")
    await h.say(topic, "Which word did I ask you to remember? Reply with the word only.")
    t2 = await h.wait_idle(topic)
    ans = h.answers(topic)[-1]
    check(t2.status == "done" and "KIWI" in ans.upper(), f"session continues with history: {ans!r}")
    return f"stopped in {took:.1f}s; status STOPPED; resumed and remembered: {ans[:30]!r}"


async def t10_vscode_compat(h: Harness, ctx: dict) -> str:
    sid = ctx["A_id"]
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    r = subprocess.run(["claude", "-p", "--resume", sid, "--model", "haiku",
                        "What codeword did I give you? Reply with it only."],
                       cwd=WORK, capture_output=True, text=True, timeout=180, env=env)
    check("ALPHA-7" in r.stdout.upper(), f"standard `claude --resume` sees the history: {r.stdout!r} {r.stderr[-300:]!r}")
    transcript = next(Path(os.environ["HOME"], ".claude/projects").glob(f"*/{sid}.jsonl"))
    entry = [json.loads(line) for line in transcript.read_text().splitlines()]
    eps = {e.get("entrypoint") for e in entry if e.get("type") == "user"}
    origins = {e.get("turnOrigin") for e in entry if e.get("type") == "user" and e.get("turnOrigin")}
    # the interactive picker (same list VS Code shows) must list the session by its title
    picker = _render_resume_picker(WORK)
    title = h.session(ctx["A"]).title
    listed = title[:15] in picker or "Test A" in picker
    check(listed, f"session visible in `claude --resume` picker: {picker[-600:]!r}")
    return (f"`claude -p --resume {sid[:8]}…` answered {r.stdout.strip()[:20]!r}; entrypoints={sorted(eps)}; "
            f"turnOrigin={sorted(origins)}; listed in interactive picker as {title!r}")


def _render_resume_picker(cwd: Path) -> str:
    sock = f"cc-accept-{os.getpid()}"
    env_cmd = (f"env -i HOME={os.environ['HOME']} PATH=/usr/bin:/bin LANG=C.UTF-8 TERM=xterm-256color "
               f"claude --resume")
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-x", "160", "-y", "40", "-c", str(cwd), env_cmd])
    time.sleep(6)
    screen = subprocess.run(["tmux", "-L", sock, "capture-pane", "-p"], capture_output=True, text=True).stdout
    if "trust this folder" in screen:
        subprocess.run(["tmux", "-L", sock, "send-keys", "Down", "Enter"])
        time.sleep(5)
        screen = subprocess.run(["tmux", "-L", sock, "capture-pane", "-p"], capture_output=True, text=True).stdout
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    return screen


async def t11_error_recovery(h: Harness, ctx: dict) -> str:
    topic = await h.new_topic("Test E — errors")
    real_cli = h.cfg.claude_cli
    h.cfg.claude_cli = str(FAIL_CLI)
    try:
        await h.say(topic, "Reply exactly: RECOVERED")
        turn = await h.wait_idle(topic, 60)
    finally:
        h.cfg.claude_cli = real_cli
    s = h.session(topic)
    check(turn.status == "error" and s.status == D.ERROR, f"error state: {turn.status} {s.status}")
    msg = [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔴")]
    check(msg, "human error message sent")
    text = msg[-1]["text"]
    check("Traceback" not in text and "exit" not in text.lower(), f"no raw technical text: {text!r}")
    datas = [b["callback_data"] for r in msg[-1]["buttons"] for b in r]
    check(any(d.startswith("retry:") for d in datas) and any(d.startswith("det:") for d in datas), "Retry + Details")
    n = len(h.tg.sent)
    await h.press(next(d for d in datas if d.startswith("det:")), thread=topic, message_id=msg[-1]["message_id"])
    e = h.tg.edits[-1]
    check(e["message_id"] == msg[-1]["message_id"] and "Технические детали" in e["text"] and len(h.tg.sent) == n,
          "Details expand inside the same message")
    await h.press(next(d for d in datas if d.startswith("retry:")), thread=topic)
    t2 = await h.wait_idle(topic)
    check(t2.status == "done" and "RECOVERED" in h.answers(topic)[-1].upper(), "Retry succeeded")
    check(h.session(topic).status == D.IDLE, "session back to IDLE")
    return f"user saw: {text.splitlines()[-1]!r} + [Повторить][Подробнее]; Retry -> done"


async def t12_permission_and_question(h: Harness, ctx: dict) -> str:
    topic = await h.new_topic("Test F — permissions")
    h.auto_approve = False
    try:
        await h.say(topic, "Use the Bash tool to run: touch perm-test.txt . Then reply exactly: TOUCHED or DENIED.")
        await h.wait(lambda: h.session(topic).status == D.WAITING_APPROVAL, 90, "permission request")
        req = [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")][-1]
        check("touch perm-test.txt" in req["text"] and req["text"].startswith("🔐 <b>Claude хочет выполнить команду"),
              f"human title + command shown: {req['text'][:80]!r}")
        # a plain message while the request is pending must be queued, not treated as an answer
        await h.say(topic, "Reply exactly: QUEUED-MSG")
        check(h.turns(topic)[-1].status == "queued" and h.session(topic).status == D.WAITING_APPROVAL,
              "plain message queued while permission pending")
        # a Telegram reply to the request = deny with feedback
        await h.say(topic, "Не надо ничего создавать", reply_to=req["message_id"])
        await h.wait_idle(topic)
        check(not (WORK / "perm-test.txt").exists(), "denied command did not run")
        check(any("QUEUED-MSG" in a for a in h.answers(topic)), "queued message ran afterwards")
        await h.say(topic, "Use the Bash tool to run: touch perm-test.txt . Then reply exactly: TOUCHED or DENIED.")
        await h.wait(lambda: len([m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")]) >= 2, 90, "2nd request")
        req = [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")][-1]
        deny = next(b["callback_data"] for r in req["buttons"] for b in r if b["callback_data"].endswith(":d"))
        await h.press(deny, thread=topic)
        await h.wait_idle(topic)
        check(not (WORK / "perm-test.txt").exists(), "Deny button: command did not run")
        closed = [e for e in h.tg.edits if e["message_id"] == req["message_id"]]
        check(closed and "Запрещено" in closed[-1]["text"] and not closed[-1]["buttons"],
              "verdict written into the request itself, buttons removed")
        await h.say(topic, "Use the AskUserQuestion tool to ask me which color I prefer (options: Red, Green). "
                           "Then reply with just the color I picked.")
        await h.wait(lambda: h.session(topic).status == D.WAITING, 90, "question asked")
        q = [m for m in h.tg.in_topic(topic) if m["text"].startswith("❓")][-1]
        green = next(b["callback_data"] for r in q["buttons"] for b in r if "Green" in b["text"])
        await h.press(green, thread=topic)
        await h.wait_idle(topic)
        ans = h.answers(topic)[-1]
        check("GREEN" in ans.upper(), f"answer used the button choice: {ans!r}")
    finally:
        h.auto_approve = True
    return (f"message during permission wait was queued; Reply-deny and Deny button blocked `touch`; "
            f"question answered by button -> {ans[:20]!r}")


async def t13_p1_features(h: Harness, ctx: dict) -> str:
    # /new without a name -> placeholder topic, auto-named after the first answer
    before = set(h.tg.topics)
    await h.say(None, "/new")
    topic = next(t for t in h.tg.topics if t not in before)
    check(h.tg.topics[topic].startswith("Новая сессия"), "placeholder topic created")
    await h.say(topic, "Дай три идеи названия для кофейни у моря. Коротко.")
    await h.wait_idle(topic)
    s = h.session(topic)
    check(s.title_source == "auto" and h.tg.topics[topic] == s.title and not s.title.startswith("Новая"),
          f"auto-named topic: {h.tg.topics[topic]!r} / {s.title_source}")
    # /rename -> Telegram topic + Claude session title
    await h.say(topic, "/rename Кофейня — нейминг")
    info = get_session_info(s.claude_session_id, directory=s.cwd)
    check(h.tg.topics[topic] == "Кофейня — нейминг" and info.custom_title == "Кофейня — нейминг",
          f"renamed in Telegram and Claude: {info.custom_title!r}")
    # file upload without caption -> saved, then used by the next prompt
    doc = h._msg(None, topic, OWNER, document={"file_id": "F1", "file_name": "../../etc/passwd notes.txt", "file_size": 30})
    await h.update({"message": doc})
    saved = h.session(topic).pending_file_list
    check(len(saved) == 1 and Path(saved[0]).parent == h.m.inbox_dir(s).resolve() and "passwd" in saved[0]
          and "/etc/" not in saved[0], f"file sanitized into inbox: {saved}")
    await h.say(topic, "Прочитай присланный файл и перескажи его содержимое одной фразой.")
    await h.wait_idle(topic)
    ans = h.answers(topic)[-1].lower()
    check(any(w in ans for w in ("hello", "привет", "f1", "telegram", "телеграм")), f"file used: {ans!r}")
    # /fork -> new topic, new Claude session with the same history
    before = set(h.tg.topics)
    await h.say(topic, "/fork")
    fork_topic = next(t for t in h.tg.topics if t not in before)
    fs = h.session(fork_topic)
    check(fs and fs.origin == "fork" and fs.claude_session_id != s.claude_session_id, "fork session registered")
    await h.say(fork_topic, "Какие названия ты предлагал? Перечисли через запятую.")
    await h.wait_idle(fork_topic)
    check(len(h.answers(fork_topic)[-1]) > 5, "fork continues with copied history")
    # /import -> an existing session not yet linked (session A deleted from registry view: use B's fork source)
    orphan = fs.claude_session_id
    h.db.conn.execute("DELETE FROM turns WHERE session_id=?", (fs.id,))
    h.db.conn.execute("DELETE FROM sessions WHERE id=?", (fs.id,))
    text, buttons = await h.bot.import_list(0)
    check(any(b["callback_data"] == f"imp:{orphan}" for r in buttons for b in r), "import list offers the session")
    await h.press(f"imp:{orphan}")
    imp = h.db.session_by_claude_id(orphan)
    CREATED_SESSIONS.add(orphan)
    check(imp and imp.origin == "import" and imp.started, "imported and linked to a new topic")
    await h.say(imp.topic_id, "Одним словом: о каком заведении мы говорили?")
    await h.wait_idle(imp.topic_id)
    ans = h.answers(imp.topic_id)[-1]
    check("коф" in ans.lower() or "coffee" in ans.lower(), f"imported session keeps context: {ans!r}")
    # /archive hides from dashboard, /unarchive brings back
    await h.say(topic, "/archive")
    check(h.session(topic).archived == 1 and f"/{topic}\"" not in h.bot.dashboard("all")[0], "archived hidden")
    await h.say(topic, "/unarchive")
    check(h.session(topic).archived == 0, "unarchived")
    await h.say(topic, "/info")
    info_msg = h.tg.in_topic(topic)[-1]
    datas = [b["callback_data"] for r in info_msg["buttons"] for b in r]
    check(f"pv:{s.id}:main" in datas, f"/info has «Назад»: {datas}")
    await h.press(f"pa:{s.id}:arch", thread=topic)
    check(h.session(topic).archived == 1 and h.session(topic).status == D.ARCHIVED, "archived by the panel")
    await h.say(topic, "Одним словом: какое заведение?")
    check(h.session(topic).archived == 0, "writing into an archived topic brings it back")
    await h.wait_idle(topic)
    return (f"auto-name {s.title!r}; /rename synced to Claude; file saved as {Path(saved[0]).name!r}; "
            f"/fork + /import keep history; archive/unarchive ok")


async def t14_vscode_and_limits(h: Harness, ctx: dict) -> str:
    import claude_control.claude as C
    s = h.session(ctx["A"])
    live = C.SESSIONS_DIR / "4242.json"
    info = {"pid": os.getpid(), "sessionId": s.claude_session_id, "cwd": s.cwd, "kind": "interactive",
            "entrypoint": "claude-vscode", "status": "busy", "statusUpdatedAt": int(time.time() * 1000)}
    live.write_text(json.dumps(info))
    try:
        dash, _ = h.bot.dashboard("main")
        check(any("Test A" in line and line.startswith("💻") for line in dash.split("🟢 Работают", 1)[-1].split("\n")),
              f"dashboard shows the session working in VS Code: {dash}")
        await h.say(ctx["A"], "Reply exactly: AFTER-VSCODE")
        notice = [m["text"] for m in h.tg.in_topic(ctx["A"]) if m["text"].startswith("💻")]
        check(notice, "user told the session is busy in VS Code")
        await asyncio.sleep(7)
        check(h.session(ctx["A"]).id not in h.m.active and h.turns(ctx["A"])[-1].status == "queued",
              "no parallel write while VS Code is working")
        live.write_text(json.dumps({**info, "status": "idle"}))
        turn = await h.wait_idle(ctx["A"], 120)
        check(turn.status == "done" and "AFTER-VSCODE" in h.answers(ctx["A"])[-1], "ran once VS Code went idle")
    finally:
        live.unlink(missing_ok=True)
    now = time.time()
    h.db.kv_set("rate_limit", json.dumps({"status": "allowed_warning", "type": "seven_day", "utilization": 0.77,
        "resets_at": now + 2 * 86400, "at": now, "raw": {"unifiedWindows": {
            "five_hour": {"utilization": 0.22, "resetsAt": now + 3530},
            "seven_day": {"utilization": 0.77, "resetsAt": now + 2 * 86400 + 600}}}}))
    limits = "\n".join(h.bot.limits_lines())
    check("5 часов" in limits and "22%" in limits and "Неделя" in limits and "77%" in limits, limits)
    check("через 58 мин" in limits and "через 2 дн" in limits, f"reset times: {limits}")
    return "VS Code-busy session waited, then ran; dashboard counts VS Code work; limits show 5 h 22% + week 77%"


async def t15_live_dashboard(h: Harness, ctx: dict) -> str:
    import claude_control.bot as B
    B.DASH_MIN_GAP_S = 0.5
    for k in ("dashboard_msg_id", "ephemeral", "dashboard_view"):
        h.db.conn.execute("DELETE FROM kv WHERE key=?", (k,))
    n = len(h.tg.sent)
    loop = asyncio.create_task(h.bot._dashboard_loop())
    await asyncio.sleep(0.5)
    dash_id = int(h.db.kv_get("dashboard_msg_id", "0"))
    check(dash_id and len(h.tg.sent) == n + 1 and h.tg.sent[-1]["silent"], "one silent dashboard message posted")
    e0 = len(h.tg.edits)
    topic = await h.new_topic("Test G — dashboard")
    await h.say(topic, "Reply exactly: DASH")
    await h.wait_idle(topic)
    await asyncio.sleep(1.5)
    dash_edits = [e for e in h.tg.edits[e0:] if e["message_id"] == dash_id]
    check(dash_edits and "Test G" in dash_edits[-1]["text"], "status changes re-render the same message")
    check(len([m for m in h.tg.sent[n:] if m["thread_id"] is None and m["text"].startswith("<b>📊")]) == 1,
          "no new dashboard messages in General")
    await h.press("dash:limits", message_id=dash_id)
    await asyncio.sleep(0.3)
    check(h.tg.edits[-1]["message_id"] == dash_id and "Лимиты" in h.tg.edits[-1]["text"], "view switch edits in place")
    cmd = await h.say(None, "/status")
    new_id = int(h.db.kv_get("dashboard_msg_id"))
    check(cmd["message_id"] in h.tg.deleted and dash_id in h.tg.deleted and new_id != dash_id
          and h.tg.sent[-1]["message_id"] == new_id, "/status: command deleted, dashboard moved to the bottom")
    await h.say(None, "/help")
    check("Как пользоваться" in h.tg.sent[-1]["text"] and h.tg.sent[-1]["message_id"] == int(h.db.kv_get("dashboard_msg_id")),
          "/help opens inside the dashboard")
    sent_before = len(h.tg.sent)
    text_msg = await h.say(None, "привет")
    check(text_msg["message_id"] in h.tg.deleted, "a stray message in General is removed at once")
    check(len(h.tg.sent) == sent_before and "🔔" in h.tg.edits[-1]["text"], "the hint appears inside the dashboard")
    loop.cancel()
    from claude_control.render import md_to_html
    from claude_control.claude import tool_summary
    table = md_to_html("| Путь | Налог |\n|---|---|\n| Virtual Zone | 0% |")
    check(table == "▪️ <b>Virtual Zone</b> — 0%", f"table becomes a list: {table!r}")
    check(tool_summary("Bash", {"description": "x"}).startswith("⌨️"), "commands use ⌨️, 💻 means VS Code")
    return (f"1 dashboard message; {len(dash_edits)} in-place edits on status changes; /status reposts at bottom and "
            f"deletes the command; help reply auto-deleted")


async def t16_control_panel(h: Harness, ctx: dict) -> str:
    topic = await h.new_topic("Test H — panel")
    await h.say(topic, "Reply exactly: PANEL")
    await h.wait_idle(topic)
    s = h.session(topic)
    card = next((m for m in h.tg.in_topic(topic) if m["text"].startswith("🎛")), None)
    check(card and s.control_msg_id == card["message_id"] and "pinChatMessage" in h.tg.calls, "panel posted and pinned")
    check(h.tg.in_topic(topic)[0] is card, "panel is the first message of the topic")
    pid, n_sent = card["message_id"], len(h.tg.sent)

    async def tap(data: str) -> dict:
        await h.press(data, thread=topic, message_id=pid)
        return h.tg.edits[-1]

    for view in ("info", "model", "fork", "arch", "help"):
        e = await tap(f"pv:{s.id}:{view}")
        datas = [b["callback_data"] for r in e["buttons"] for b in r]
        check(e["message_id"] == pid and f"pv:{s.id}:main" in datas, f"«{view}» opens inside the panel with «Назад»")
        e = await tap(f"pv:{s.id}:main")
        check("Управление сессией" in e["text"], "«Назад» returns to the panel")
    check(len(h.tg.sent) == n_sent, "navigating the panel posts no new messages")
    e = await tap(f"pa:{s.id}:model:sonnet")
    check(h.session(topic).model == "sonnet" and "Sonnet" in e["text"], "model choice returns to the panel")
    e = await tap(f"pv:{s.id}:arch")
    check("Убрать в архив?" in e["text"] and h.session(topic).archived == 0, "archive asks first")
    e = await tap(f"pa:{s.id}:arch")
    datas = [b["callback_data"] for r in e["buttons"] for b in r]
    check(h.session(topic).archived == 1 and f"pa:{s.id}:unarch" in datas, "archived; the panel offers «Вернуть»")
    await tap(f"pa:{s.id}:unarch")
    check(h.session(topic).archived == 0, "returned from the archive")
    await h.press(f"pa:{s.id}:ren", thread=topic, message_id=pid)
    check(h.tg.edits[-1]["message_id"] == pid and "Новое название" in h.tg.edits[-1]["text"], "rename prompt in the panel")
    answer = await h.say(topic, "Панель — новое имя")   # a plain message, no «Reply» needed
    check(h.tg.topics[topic] == "Панель — новое имя" and h.session(topic).title == "Панель — новое имя",
          "the next message renames the topic")
    check(answer["message_id"] in h.tg.deleted, "the message with the title is cleaned up")
    done = [m for m in h.tg.in_topic(topic) if m["text"].startswith("✏️ Тема переименована")][-1]
    eph = {x[0]: x[1] for x in json.loads(h.db.kv_get("ephemeral"))}
    check(done["message_id"] in eph and eph[done["message_id"]] - time.time() <= 61, "«переименовано» disappears in a minute")
    check(h.turns(topic)[-1].prompt != "Панель — новое имя", "the answer was not sent to Claude")
    _, buttons = h.bot.dashboard("all")
    general = {b["callback_data"] for r in buttons for b in r}
    check({"dash:import:0", "dash:proj", "dash:diag", "dash:main"} <= general, f"General actions on buttons: {general}")
    for view in ("import:0", "proj", "diag", "help"):
        _, rows = await h.bot.render_view(view)
        check("dash:main" in {b["callback_data"] for r in rows for b in r}, f"dashboard screen «{view}» has «Назад»")
    return ("panel screens open in place with «Назад», no new messages; archive asks first; rename cleans up; "
            "General screens open inside the dashboard with «Назад»")


async def t17_clean_general(h: Harness, ctx: dict) -> str:
    await h.bot.refresh_dashboard()
    dash = int(h.db.kv_get("dashboard_msg_id"))
    old_dash = (await h.tg.send_message(CHAT, "<b>📊 Claude Control</b>\nстарый дашборд"))["message_id"]
    old_help = (await h.tg.send_message(CHAT, "<b>Как пользоваться</b>\n\n1. Создайте тему → напишите задание."))["message_id"]
    topic_id = h.session(ctx["A"]).topic_id
    answer = (await h.tg.send_message(CHAT, "Как пользоваться Virtual Zone: подайте заявку…\n\n⏱ 12 с",
                                      thread_id=topic_id))["message_id"]
    answer2 = (await h.tg.send_message(CHAT, "📊 Claude Control\nэто Claude так начал ответ\n\n⏱ 3 с",
                                       thread_id=topic_id))["message_id"]
    old_cmd, user_text = next(h.tg._ids), next(h.tg._ids)
    h.tg.user_msgs[old_cmd] = ("/status", OWNER)
    h.tg.user_msgs[user_text] = ("просто текст в теме", OWNER)
    removed = await h.bot.sweep_general()
    gone = set(h.tg.deleted)
    check({old_dash, old_help, old_cmd} <= gone, f"old General messages removed ({removed})")
    check(answer not in gone and answer2 not in gone, "Claude's answers in topics are never touched")
    live_dash = int(h.db.kv_get("dashboard_msg_id"))
    check(user_text not in gone and live_dash not in gone and live_dash != dash,
          "unknown messages are kept; the dashboard is re-posted at the bottom")
    check(h.tg.deleted_topics, "temporary topic deleted")
    # typed /new in General: the command disappears, the confirmation is inside the dashboard
    sent_before = [m["message_id"] for m in h.tg.sent if m["thread_id"] is None]
    cmd = await h.say(None, "/new Чистый General")
    general_now = [m for m in h.tg.sent if m["thread_id"] is None and m["message_id"] not in sent_before]
    check(cmd["message_id"] in h.tg.deleted and not general_now, "no new messages in General besides the dashboard")
    check("Создана тема" in h.tg.edits[-1]["text"], "confirmation shown inside the dashboard")
    # janitor: 5 minutes of silence -> anything left in General is removed
    h.bot._track_general(888001)
    h.db.kv_set("general_activity", time.time() - 301)
    await h.bot._janitor()
    check(888001 in h.tg.deleted and json.loads(h.db.kv_get("general_msgs")) == [], "idle General is cleaned")
    return f"sweep removed {removed} old General messages (topics untouched); General gets no replies; janitor after 5 min"


async def t18_voice(h: Harness, ctx: dict) -> str:
    if not h.bot.voice:
        return "skipped: VOICE_ENGINE / .venv-voice not installed"
    sample = WORK / "voice-example.ogg"
    if not sample.exists():   # a Russian speech sample published with GigaAM
        import urllib.request
        wav = WORK / "voice-example.wav"
        urllib.request.urlretrieve("https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav", wav)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "libopus", "-b:a", "32k", str(sample)],
                       check=True)
        wav.unlink()
    real_download = h.tg.download

    async def download(file_id: str, dest: Path, max_bytes: int = 0) -> int:
        shutil.copy(sample, dest)
        return dest.stat().st_size
    h.tg.download = download
    try:
        topic = await h.new_topic("Test V — voice")
        m = h._msg(None, topic, OWNER, voice={"file_id": "V1", "duration": 11, "file_size": 45000,
                                              "mime_type": "audio/ogg"})
        await h.update({"message": m})
        turn = await h.wait_idle(topic, 180)
    finally:
        h.tg.download = real_download
    heard = [e["text"] for e in h.tg.edits if e["text"].startswith("🎙 <i>")]
    check(heard and "Ничьих не требуя похвал" in heard[-1], f"transcript shown in the topic: {heard[-1:]}")
    check("Ничьих не требуя похвал" in turn.prompt and "расшифровка голосового" in turn.prompt,
          "the transcript is what Claude received")
    check(turn.status == "done" and h.answers(topic), f"Claude answered: {turn.status}")
    check(not list(h.m.inbox_dir(h.session(topic)).glob("voice-*")), "the recording is deleted after transcription")
    await h.bot.voice.stop()
    return f"voice → «{heard[-1][5:60]}…» → Claude answered; recording deleted"


async def t19_files(h: Harness, ctx: dict) -> str:
    tdir = Path.home() / f"cc-files-test-{os.getpid()}"   # outside any git repo, like a real work folder
    tdir.mkdir(exist_ok=True)
    try:
        topic = await h.new_topic("Test F — files")
        s = h.m.create_session(CHAT, topic, "Test F — files", cwd=str(tdir))
        CREATED_SESSIONS.add(s.claude_session_id)
        sent_files = lambda: [f for m in h.tg.in_topic(topic) for f in m.get("files", [])]  # noqa: E731

        await h.say(topic, "Use the Write tool to create report.txt containing exactly: quarterly report ready. "
                           "Then reply exactly: DONE")
        await h.wait_idle(topic, 180)
        check("report.txt" in sent_files(), f"a file Claude wrote arrives by itself: {sent_files()}")
        (tdir / "existing.csv").write_text("a,b\n1,2\n")
        await h.say(topic, "Use your send_file tool to send me the file existing.csv. Then reply exactly: SENT")
        await h.wait_idle(topic, 180)
        check("existing.csv" in sent_files(), f"«send me a file» works: {sent_files()}")
        check(not [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐") and "existing.csv" in m["text"]],
              "files from the session folder need no approval")
        (tdir / ".env").write_text("SECRET_TOKEN=do-not-leak\n")
        await h.say(topic, "Use your send_file tool to send me the file .env from the working folder. Reply OK after.")
        await h.wait_idle(topic, 180)
        check(".env" not in sent_files(), "secrets never leave the server")

        await h.press(f"pa:{s.id}:files", thread=topic, message_id=h.session(topic).control_msg_id or 1)
        check(h.session(topic).send_files == 0, "panel switch turns automatic files off")
        await h.say(topic, "Use the Write tool to create second.txt containing: two. Then reply exactly: DONE")
        await h.wait_idle(topic, 180)
        check("second.txt" not in sent_files() and (tdir / "second.txt").exists(), "switched off: nothing is sent")
        await h.press(f"pa:{s.id}:files", thread=topic, message_id=h.session(topic).control_msg_id or 1)

        # an album of two photos with one caption becomes ONE task with both files
        group = f"album-{os.getpid()}"
        for i in range(2):
            m = h._msg(None, topic, OWNER, media_group_id=group,
                       photo=[{"file_id": f"P{i}", "file_size": 100, "width": 10, "height": 10}],
                       **({"caption": "Сколько файлов я прислал? Ответь цифрой."} if i == 0 else {}))
            await h.update({"message": m})
        await asyncio.sleep(3)
        turn = await h.wait_idle(topic, 180)
        check(turn.prompt.count("photo_") == 2 and "Сколько файлов" in turn.prompt, f"album -> one task: {turn.prompt!r}")

        # limits and many files (no Claude needed)
        before = len(h.tg.sent)
        h.cfg.max_send_mb = 0
        result = await h.bot.send_files(h.session(topic), [str(tdir / "existing.csv")], requested=True)
        h.cfg.max_send_mb = 50
        check(result.startswith("Not sent") and "больше предела" in h.tg.sent[-1]["text"], f"too big: {result}")
        many = []
        for i in range(12):
            (tdir / f"part{i}.txt").write_text(str(i))
            many.append(str(tdir / f"part{i}.txt"))
        await h.bot.send_files(h.session(topic), many)
        check(h.tg.sent[-1].get("files") == ["files.zip"] and len(h.tg.sent) == before + 2, "12 files -> one zip")
    finally:
        shutil.rmtree(tdir, ignore_errors=True)
        proj = Path.home() / ".claude/projects" / ("-" + str(tdir).strip("/").replace("/", "-"))
        for sid in list(CREATED_SESSIONS):
            try:
                delete_session(sid, directory=str(tdir))
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(proj, ignore_errors=True)
    return "written file arrives by itself; send_file works; .env blocked; switch off works; album = 1 task; zip; size limit"


async def t20_permission_modes(h: Harness, ctx: dict) -> str:
    # sessions from before v0.1.3 got 'default' automatically (nobody chose it) -> they follow PERMISSION_MODE
    legacy_path = h.data_dir / "legacy.db"
    reg = D.Registry(legacy_path)
    old = reg.create_session(chat_id=CHAT, topic_id=1, claude_session_id="legacy", title="old", cwd=str(WORK))
    reg.conn.execute("UPDATE sessions SET permission_mode='default'")
    reg.conn.execute("DELETE FROM kv WHERE key='perm_mode_follow'")
    reg.close()
    reg = D.Registry(legacy_path)
    check(reg.get_session(old.id).permission_mode == "", "legacy sessions switch to the .env default")
    reg.close()

    h.cfg.permission_mode = "auto"   # as in production
    h.auto_approve = False
    files = [WORK / "auto-dir", WORK / "m1.txt", WORK / "m2.txt"]
    try:
        topic = await h.new_topic("Test M — modes")
        s = h.m.create_session(CHAT, topic, "Test M — modes", cwd=str(WORK))
        CREATED_SESSIONS.add(s.claude_session_id)
        await h.press(f"pv:{s.id}:perm", thread=topic)
        e = h.tg.edits[-1]
        labels = [b["text"] for r in e["buttons"] for b in r]
        check("✅ 🤖 Авто" in labels and len(labels) == 4, f"picker: 3 modes + «Назад», Auto by default: {labels}")
        check("нет режима Авто" in e["text"], "with Haiku the picker warns that Auto is not available")
        check("🤖 Авто" in h.bot.panel_view(h.session(topic))[0], "the panel header shows the mode")

        h.db.update_session(s.id, model="sonnet")   # Auto mode needs Opus/Sonnet
        await h.say(topic, "Use the Bash tool to run: mkdir -p auto-dir && echo ok > auto-dir/f.txt . "
                           "Then reply exactly: DONE")
        turn = await h.wait_idle(topic, 240)
        check(turn.status == "done" and (WORK / "auto-dir/f.txt").exists(), f"auto: command ran ({turn.status})")
        check(not [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")], "auto: no permission request")
        outside = Path(tempfile.mkdtemp(prefix="cc-outside-")) / "notes.txt"   # not in the session folder
        outside.write_text("server notes")
        try:
            # the send_file tool itself asks before a file from elsewhere leaves - in Auto too
            # (called directly: Sonnet in Auto usually declines such a request on its own)
            sender = h.m._file_sender(ActiveRun(session_id=s.id, turn_id=0, status_msg_id=None))
            job = asyncio.create_task(sender(str(outside), ""))
            await h.wait(lambda: any(m["text"].startswith("🔐") for m in h.tg.in_topic(topic)), 20, "send_file request")
            req = [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")][-1]
            check("notes.txt" in req["text"], f"Auto still asks before sending a file from elsewhere: {req['text'][:80]!r}")
            deny = next(b["callback_data"] for r in req["buttons"] for b in r if b["callback_data"].endswith(":d"))
            await h.press(deny, thread=topic)
            result = await asyncio.wait_for(job, 20)
            check(result.startswith("Not sent") and "notes.txt" not in [f for m in h.tg.in_topic(topic)
                                                                        for f in m.get("files", [])], f"denied: {result}")
            h.m.set_status(s.id, D.IDLE)
        finally:
            shutil.rmtree(outside.parent, ignore_errors=True)

        await h.press(f"pa:{s.id}:perm:default", thread=topic)
        check(h.session(topic).permission_mode == "default", "owner switched the topic to «ask everything»")
        await h.say(topic, "Step 1: use the Bash tool to run: touch m1.txt . Step 2: only after step 1 succeeded, "
                           "use the Bash tool again to run: ls m1.txt && touch m2.txt . Then reply exactly: DONE")
        await h.wait(lambda: h.session(topic).status == D.WAITING_APPROVAL, 120, "permission request")
        await h.wait(lambda: len([m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")]) == 2, 30, "request sent")
        req = [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")][-1]
        auto_btn = next((b["callback_data"] for r in req["buttons"] for b in r if b["callback_data"].endswith(":a")), None)
        check(auto_btn is not None, f"the request offers «Разрешить и включить Авто»: {req['buttons']}")
        await h.press(auto_btn, thread=topic)
        turn = await h.wait_idle(topic, 240)
        asks = [m for m in h.tg.in_topic(topic) if m["text"].startswith("🔐")]
        check(turn.status == "done" and (WORK / "m1.txt").exists() and (WORK / "m2.txt").exists(),
              f"both commands ran ({turn.status})")
        check(len(asks) == 2, f"after «включить Авто» the running task asks no more: {len(asks) - 1} requests")
        check(h.session(topic).permission_mode == "auto", "the topic stays in Auto")
        closed = [x for x in h.tg.edits if x["message_id"] == req["message_id"]]
        check(closed and "режим Авто" in closed[-1]["text"], "the verdict says Auto was switched on")
        check("🛡 Разрешения: 🤖 Авто" in h.bot.session_card(h.session(topic)), "«О сессии» shows the mode")

        # many «Не спрашивать про … здесь» (a long working day): «О сессии» stays short and can reset them
        grants = [{"rule": f"Bash(grep -n pattern{i} file{i}.py)"} for i in range(40)] + [
            {"rule": "Bash(.venv/bin/python -m pyflakes x.py)"}, {"rule": "Skill(claude-api)"},
            {"rule": "Skill(claude-api:*)"}, {"mode": "acceptEdits"}, {"rule": "Read(//tmp/**)"}]
        h.db.update_session(s.id, grants=json.dumps(grants))
        card = h.bot.session_card(h.session(topic))
        line = next(x for x in card.splitlines() if x.startswith("♾"))
        check(len(line) < 140 and "41 команда (grep, python)" in line and line.count("claude-api") == 1,
              f"grants in one short line: {line!r}")
        await h.press(f"pv:{s.id}:info", thread=topic)
        reset = [b["callback_data"] for r in h.tg.edits[-1]["buttons"] for b in r if b["callback_data"].endswith(":grants")]
        check(reset, "«О сессии» offers a reset")
        await h.press(reset[0], thread=topic)
        check(h.session(topic).grant_list == [] and "♾" not in h.tg.edits[-1]["text"], "reset forgets them")
        # a message longer than Telegram allows is shortened, not lost
        msg = await h.tg.send_message(CHAT, "<b>x</b>" + "слово " * 1200, thread_id=topic)
        check(msg and len(h.tg.sent[-1]["text"]) <= 4096 and "сокращено" in h.tg.sent[-1]["text"], "too long -> shortened")
        h.tg.too_long.clear()
    finally:
        h.cfg.permission_mode = "default"
        h.auto_approve = True
        for f in files:
            shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
    return ("legacy sessions follow .env; picker with Haiku warning; Sonnet in Auto ran a command without asking, "
            "but still asked before sending a file from outside the folder; "
            "«Разрешить и включить Авто» switched the running task — second command not asked")


TESTS = [("T1", "new session", t1_new_session), ("T2", "resume", t2_resume), ("T3", "isolation", t3_isolation),
         ("T4+T5", "concurrency=5 + queue", t4_t5_concurrency_and_queue),
         ("T6", "same-session serialization", t6_serialization), ("T7", "service restart", t7_restart),
         ("T8", "unauthorized user", t8_unauthorized), ("T9", "stop", t9_stop),
         ("T10", "VS Code / CLI compatibility", t10_vscode_compat), ("T11", "error recovery", t11_error_recovery),
         ("T12", "permission + question buttons", t12_permission_and_question),
         ("T13", "P1: new/auto-name/rename/files/fork/import/archive", t13_p1_features),
         ("T14", "VS Code sessions + full limits", t14_vscode_and_limits),
         ("T15", "live dashboard + clean General", t15_live_dashboard),
         ("T16", "pinned control panel instead of commands", t16_control_panel),
         ("T17", "General stays clean (sweep, no replies, janitor)", t17_clean_general),
         ("T18", "voice message → GigaAM → Claude", t18_voice),
         ("T19", "files: Claude → chat automatically, send_file, albums, limits", t19_files),
         ("T20", "permission modes: Auto by default, picker, switch from a request", t20_permission_modes)]


async def main(selected: list[str]) -> int:
    WORK.mkdir(exist_ok=True)
    (WORK / "slow.sh").write_text('#!/bin/bash\nfor i in $(seq 1 ${1:-30}); do sleep 1; done\necho "slow-done-$1"\n')
    (WORK / "slow.sh").chmod(0o755)
    (WORK / "perm-test.txt").unlink(missing_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="cc-accept-"))
    import claude_control.claude as C   # isolate from the real VS Code sessions on this machine
    C.SESSIONS_DIR = data_dir / "live-sessions"
    C.SESSIONS_DIR.mkdir()
    h = Harness(data_dir)
    await h.start()
    ctx: dict = {}
    failed = 0
    for code, name, fn in TESTS:
        if selected and code not in selected:
            continue
        t0 = time.time()
        log(f"▶ {code} {name}")
        try:
            res = await fn(h, ctx)
            if isinstance(res, tuple):
                res, h = res
            RESULTS.append({"test": code, "name": name, "ok": True, "evidence": res, "sec": round(time.time() - t0)})
            log(f"✅ {code} PASS ({time.time() - t0:.0f}s): {res}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            RESULTS.append({"test": code, "name": name, "ok": False, "evidence": f"{type(e).__name__}: {e}",
                            "sec": round(time.time() - t0)})
            log(f"❌ {code} FAIL: {e}")
            traceback.print_exc()
    if h.tg.too_long:
        failed += 1
        RESULTS.append({"test": "LEN", "name": "no message over Telegram limits", "ok": False,
                        "evidence": f"rejected as too long: {h.tg.too_long}", "sec": 0})
        log(f"❌ LEN FAIL: messages over Telegram limits: {h.tg.too_long}")
    await h.close()
    (ROOT / "report.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    if os.environ.get("KEEP_SESSIONS") != "1":
        for sid in CREATED_SESSIONS:
            try:
                delete_session(sid, directory=str(WORK))
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(data_dir, ignore_errors=True)
        proj = Path(os.environ["HOME"], ".claude/projects", "-" + str(WORK).strip("/").replace("/", "-"))
        if proj.is_dir() and not list(proj.glob("*.jsonl")):
            shutil.rmtree(proj, ignore_errors=True)   # only Claude Code's empty per-project folders remain
    log(f"done: {len(RESULTS) - failed}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
