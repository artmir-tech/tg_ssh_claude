"""Telegram side: long polling, owner allowlist, commands, dashboard, buttons, progress messages.

Layout: one forum supergroup. The General topic is the dashboard; every other topic is
exactly one Claude Code session (see manager.py for the session logic).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import get_session_info, get_session_messages, list_sessions

from . import __version__
from . import db as D
from .claude import ENTRYPOINT, TurnOutcome, live_sessions
from .config import Config
from .manager import ActiveRun, Manager, PendingRequest
from .render import (TG_LIMIT, ago, clip, clip_words, clock, duration, esc, folder_label, md_to_html, ordinal, plural,
                     precise_duration, short_path, short_when,
                     split_markdown, tz_name, until, when)
from .telegram import TelegramAPI, TelegramError
from .voice import Transcriber

log = logging.getLogger("cc.bot")

ANON_ADMIN_ID = 1087968824  # "GroupAnonymousBot": messages from anonymous admins
PROGRESS_EDIT_EVERY_S = 25
DASH_REFRESH_S = 60          # the live dashboard is re-rendered at least this often
DASH_MIN_GAP_S = 8           # ...and at most this often when things change
DASH_VIEW_RESET_S = 600      # a non-main view falls back to the main one after 10 min
SHORT_TTL = 60               # service notices in topics ("renamed", "stopped"...) disappear after this
USAGE_REFRESH_S = 300        # how often Claude Code's usage (limits) cache is refreshed
FLASH_S = 60                 # a notice line on top of the dashboard stays this long
GENERAL_IDLE_CLEAN_S = 300   # General is swept this long after its last activity
RENAME_WAIT_S = 600          # after «Переименовать», the next message within this time becomes the title

# Texts only ever posted to General (used to recognise old messages when cleaning it up).
GENERAL_BOT_PREFIXES = ("📊 Claude Control\n", "📊 Claude Control · ", "📋 Все сессии · ", "📈 Лимиты Claude · ",
                        "Claude Control — пульт для сессий Claude Code", "Как пользоваться\n\n1. Создайте тему",
                        "Все команды\n\nВ General", "✅ Создана тема ", "✅ Подключена: ",
                        "Это общий чат-дашборд.", "✅ Claude Control запущен и проверен",
                        "📥 Сессии Claude Code на сервере", "📥 Подключить сессию из VS Code\n",
                        "🛠 Диагностика Claude Control", "📁 Выберите папку ", "📁 Папка для новых сессий\n",
                        "Не знаю такой команды. /help", "✅ Группа подключена.", "⚠️ Вы пишете анонимно",
                        "Эта сессия уже подключена: ", "Не нашёл эту сессию на сервере.",
                        "⚠️ У бота нет права «Управление темами»")
GENERAL_COMMANDS = {"start", "status", "sessions", "dashboard", "help", "new", "import", "project",
                    "diagnostics", "diag"}
MAX_CHUNKS = 4               # longer answers also arrive as an .md file

STATUS_VIEW = {
    D.RUNNING: ("🟢", "работает"),
    D.WAITING_APPROVAL: ("🔐", "ждёт разрешения"),
    D.WAITING: ("❓", "ждёт ответа"),
    D.QUEUED: ("🟡", "в очереди"),
    D.ERROR: ("🔴", "ошибка"),
    D.STOPPED: ("⏹", "остановлена"),
    D.IDLE: ("⚪", "свободна"),
    D.NEW: ("⚪", "новая"),
    D.ARCHIVED: ("🗄", "в архиве"),
}

GENERAL_HELP = """<b>Как пользоваться</b>

1. Создайте тему → напишите задание.
2. Одна тема = одна сессия Claude.
3. Дашборд всегда внизу General.

В теме: /stop — остановить, /info — о сессии.
Здесь: /import — сессия из VS Code."""

TOPIC_HELP = """<b>Эта тема — сессия Claude</b>
Пишите сообщения — Claude ответит и будет помнить весь разговор.

🎛 Кнопки управления — в закреплённом сообщении: нажмите на полоску вверху темы.
📎 Файлы и фото можно присылать прямо сюда. 🎙 Можно надиктовать голосом."""

MODEL_TEXT = ("🧠 <b>Модель для этой темы</b>\n\n<b>Opus</b> — самая сильная, быстрее тратит лимит\n"
              "<b>Sonnet</b> — быстрее, хватает для большинства задач\n<b>Haiku</b> — самая быстрая, для простого")
INTERRUPTED_TEXT = "⚠️ Бот перезапускался, и этот запрос прервался на середине.\nИстория сохранена."

ALL_COMMANDS = """<b>Все команды</b>

<b>В General</b>
/status — опустить дашборд вниз
/new Название — новая тема-сессия
/import — подключить сессию из VS Code
/project — папка для новых сессий
/diagnostics — техническая сводка

<b>В теме</b>
/stop — остановить задачу
/info — о сессии и как открыть её на компьютере
/rename Название — переименовать
/fork — копия разговора в новой теме
/archive, /unarchive — убрать в архив и вернуть
/model — модель Claude
/restart — заново выполнить последний запрос
/project — папка (только до первого сообщения)"""

TOOL_ACTIONS = {"Bash": "выполнить команду", "Write": "записать файл", "Edit": "изменить файл",
                "MultiEdit": "изменить файл", "NotebookEdit": "изменить блокнот", "WebFetch": "открыть сайт",
                "WebSearch": "искать в интернете", "ExitPlanMode": "перейти от плана к работе",
                "Agent": "запустить помощника", "Task": "запустить помощника", "Skill": "запустить навык"}


def tool_action(name: str) -> str:
    if name in TOOL_ACTIONS:
        return TOOL_ACTIONS[name]
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"использовать «{parts[-1]}» ({parts[1] if len(parts) > 2 else 'MCP'})"
    return f"использовать инструмент «{name}»"


def default_model_label() -> str:
    """The model Claude Code uses by default (from ~/.claude/settings.json), e.g. 'Opus'."""
    try:
        model = json.loads((Path.home() / ".claude" / "settings.json").read_text()).get("model") or ""
    except (OSError, ValueError):
        model = ""
    for name in ("opus", "sonnet", "haiku", "fable"):
        if name in model.lower():
            return name.capitalize()
    return model or "по умолчанию"


def grant_label(g: dict) -> str:
    if "mode" in g:
        return "правка файлов"
    if "dir" in g:
        return f"папка {Path(g['dir']).name}"
    rule = g.get("rule", "")
    m = re.match(r"^(\w+)\((.*)\)$", rule)
    if not m:
        return tool_action(rule)
    tool, content = m.group(1), m.group(2).removesuffix(":*").strip()
    if content.startswith("domain:"):
        return f"сайт {content[7:]}"
    return f"«{clip(content, 30)}»" if tool == "Bash" else f"{tool_action(tool)}: {clip(content, 30)}"


def shown_name(path: str) -> str:
    """Saved inbox files are '<date>-<time>_<name>'; show the user's own name."""
    name = Path(path).name
    return name.split("_", 1)[1] if re.match(r"^\d{8}-\d{6}_", name) else name


def parse_command(text: str, bot_username: str) -> tuple[str | None, str]:
    m = re.match(r"^/([A-Za-z_]+)(?:@(\w+))?(?:\s+(.*))?$", text.strip(), re.S)
    if not m or (m.group(2) and m.group(2).lower() != bot_username.lower()):
        return None, ""
    return m.group(1).lower(), (m.group(3) or "").strip()


def command_name(text: str) -> str:
    """'/status@bot args' -> 'status'; '' for anything that is not a command."""
    m = re.match(r"^/([A-Za-z_]+)", text.strip())
    return m.group(1).lower() if m else ""


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip(" ._")
    return (name or "file")[:80]


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


class Bot:
    def __init__(self, cfg: Config, db: D.Registry, tg: TelegramAPI, manager: Manager):
        self.cfg, self.db, self.tg, self.m = cfg, db, tg, manager
        manager.ui = self
        chat = cfg.chat_id or db.kv_get("chat_id")
        self.chat_id: int | None = int(chat) if chat else None
        self.me: dict = {}
        self.online: bool | None = None
        self.claude_version = "?"
        self.stopping = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[Any, asyncio.Lock] = {}
        self._anon_warned = False
        self._dash_kick = asyncio.Event()
        self._dash_last = 0.0
        self._live_sig: tuple = ()
        self._usage_at = 0.0
        self._flash: tuple[str, float] = ("", 0.0)
        self.voice: Transcriber | None = None
        if cfg.voice_engine == "gigaam":
            self.voice = Transcriber(cfg.voice_python)
            if not self.voice.available:
                log.error("VOICE_ENGINE=gigaam but %s is missing: run deploy/install.sh", cfg.voice_python)
                self.voice = None

    # =========================================================================== lifecycle
    async def run(self) -> None:
        while True:
            try:
                self.me = await self.tg.call("getMe")
                break
            except (ConnectionError, TelegramError) as e:
                log.warning("Telegram not reachable yet: %s", e)
                await asyncio.sleep(10)
        log.info("connected to Telegram as @%s, chat=%s, owners=%s", self.me.get("username"), self.chat_id,
                 sorted(self.cfg.owner_ids))
        if not self.cfg.owner_ids:
            log.error("OWNER_IDS is empty: nobody can control the bot")
        self.claude_version = await self._claude_version()
        await self._set_commands()
        await self.m.recover()
        self._spawn(self._progress_loop())
        self._spawn(self._dashboard_loop())
        self._spawn(self._usage_loop())
        self._spawn(self._cards_for_existing_sessions())
        if not self.db.kv_get("general_swept"):
            self._spawn(self.sweep_general())
        await self._poll_loop()

    async def stop(self) -> None:
        self.stopping.set()
        if self.voice:
            await self.voice.stop()

    async def _poll_loop(self) -> None:
        offset = int(self.db.kv_get("tg_offset", "0"))
        backoff = 1
        while not self.stopping.is_set():
            try:
                updates = await self.tg.get_updates(offset)
            except (ConnectionError, TelegramError) as e:
                if self.online is not False:
                    log.warning("Telegram connection lost: %s", e)
                self.online = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            if self.online is not True:
                log.info("Telegram connection OK")
            self.online, backoff = True, 1
            for u in updates:
                offset = u["update_id"] + 1
                if self.db.mark_update(u["update_id"]):
                    self._spawn(self._dispatch(u))
                self.db.kv_set("tg_offset", offset)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _claude_version(self) -> str:
        try:
            p = await asyncio.create_subprocess_exec(self.cfg.claude_cli, "--version", stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 30)
            return out.decode().strip()
        except Exception:  # noqa: BLE001
            return "unknown"

    async def _set_commands(self) -> None:
        if not self.chat_id:
            return
        cmds = [("status", "Дашборд"), ("new", "Новая сессия"), ("stop", "Остановить (в теме)"),
                ("info", "О сессии (в теме)"), ("import", "Сессия из VS Code"), ("help", "Помощь и все команды")]
        try:
            await self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in cmds],
                               scope={"type": "chat", "chat_id": self.chat_id})
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", e.description)

    # =========================================================================== dispatch
    async def _dispatch(self, u: dict) -> None:
        try:
            if "callback_query" in u:
                await self._on_callback(u["callback_query"])
            elif "message" in u:
                m = u["message"]
                key = (m["chat"]["id"], m.get("message_thread_id") if m.get("is_topic_message") else 0)
                async with self._locks.setdefault(key, asyncio.Lock()):
                    await self._on_message(m)
            elif "my_chat_member" in u:
                await self._on_membership(u["my_chat_member"])
        except Exception:  # noqa: BLE001
            log.exception("update %s failed", u.get("update_id"))

    def is_owner(self, user: dict | None) -> bool:
        return bool(user) and user.get("id") in self.cfg.owner_ids

    async def _on_membership(self, cm: dict) -> None:
        chat = cm["chat"]
        status = (cm.get("new_chat_member") or {}).get("status")
        log.info("bot membership in chat %s (%s): %s by user %s", chat["id"], chat.get("type"), status,
                 (cm.get("from") or {}).get("id"))
        if status in ("member", "administrator") and chat["type"] in ("group", "supergroup"):
            if chat["id"] != self.chat_id and (self.chat_id or not self.is_owner(cm.get("from"))):
                log.warning("leaving foreign chat %s", chat["id"])
                try:
                    await self.tg.call("leaveChat", chat_id=chat["id"])
                except TelegramError:
                    pass

    async def _on_message(self, m: dict) -> None:
        chat, user = m["chat"], m.get("from") or {}
        text = (m.get("text") or m.get("caption") or "").strip()
        cmd, args = parse_command(text, self.me.get("username", "")) if text.startswith("/") else (None, "")
        if chat["type"] == "private":
            if self.is_owner(user):
                await self.tg.send_message(chat["id"], "Я работаю в группе «Claude Control». Пишите там 🙂")
            else:
                log.warning("ignored private message from unauthorized user id=%s", user.get("id"))
            return
        if self.chat_id is None:
            if self.is_owner(user) and cmd == "start":
                await self._bind_chat(chat)
            return
        if chat["id"] != self.chat_id:
            return
        topic_id = m.get("message_thread_id") if m.get("is_topic_message") else None
        if topic_id is None:
            self._track_general(m["message_id"])
        if "forum_topic_created" in m and topic_id:
            self.db.set_topic_name(chat["id"], topic_id, m["forum_topic_created"]["name"])
            return
        if "forum_topic_edited" in m and topic_id:
            if self.is_owner(user) and m["forum_topic_edited"].get("name"):
                await self._topic_renamed_by_user(topic_id, m["forum_topic_edited"]["name"])
            return
        if user.get("id") == ANON_ADMIN_ID and not m.get("migrate_from_chat_id") and (text or "document" in m):
            if not self._anon_warned:
                self._anon_warned = True
                warning = ("⚠️ Вы пишете анонимно (как группа), поэтому я не могу проверить, что это вы. "
                           "Выключите «Анонимность» в правах администратора.")
                if topic_id is None:
                    await self.flash(warning)
                else:
                    await self.tg.send_message(self.chat_id, warning, thread_id=topic_id)
            return
        if not self.is_owner(user):
            if not user.get("is_bot") and (text or m.get("document") or m.get("photo")):
                log.warning("ignored message from unauthorized user id=%s", user.get("id"))
            return
        if cmd is None and not text and not any(k in m for k in ("document", "photo", "voice", "audio", "video",
                                                                  "video_note")):
            return  # service messages, stickers etc.
        if topic_id is None:
            await self._general(m, cmd, args)
        else:
            await self._topic(m, topic_id, cmd, args, text)

    async def _bind_chat(self, chat: dict) -> None:
        if not chat.get("is_forum"):
            await self.tg.send_message(chat["id"], "Включите «Темы» в настройках группы и снова напишите /start.")
            return
        self.chat_id = chat["id"]
        self.db.kv_set("chat_id", chat["id"])
        log.info("bound to chat %s", chat["id"])
        await self._set_commands()
        await self.tg.send_message(chat["id"], "✅ Группа подключена.\n\n" + GENERAL_HELP)

    # =========================================================================== General topic
    async def _general(self, m: dict, cmd: str | None, args: str) -> None:
        """General holds only the dashboard: the user's message is removed, answers appear inside it."""
        if not m.get("is_topic_message"):
            await self.tg.delete_message(self.chat_id, m["message_id"])
            self._untrack_general(m["message_id"])
        if cmd in ("start", "help"):
            await self.refresh_dashboard(view="help", repost=True)
        elif cmd in ("status", "sessions", "dashboard"):
            await self.refresh_dashboard(view="all" if cmd == "sessions" else "main", repost=True)
        elif cmd == "new":
            await self.new_session_topic(args)
        elif cmd == "import":
            await self.refresh_dashboard(view="import:0")
        elif cmd == "project":
            await self.refresh_dashboard(view="proj")
        elif cmd in ("diagnostics", "diag"):
            await self.refresh_dashboard(view="diag")
        elif cmd:
            await self.flash("Не знаю такой команды — все действия есть на кнопках дашборда.")
        else:
            await self.flash("Это общий чат-дашборд. Чтобы дать задание Claude, создайте тему "
                             "или нажмите «➕ Новая».")

    async def flash(self, html_text: str) -> None:
        """A short notice shown on top of the dashboard for a minute (instead of a message in General)."""
        self._flash = (html_text, time.time() + FLASH_S)
        await self.refresh_dashboard()

    # ---- General housekeeping ---------------------------------------------------------------
    def _track_general(self, message_id: int) -> None:
        ids = json.loads(self.db.kv_get("general_msgs", "[]"))
        if message_id not in ids:
            ids.append(message_id)
        self.db.kv_set("general_msgs", json.dumps(ids[-500:]))
        self.db.kv_set("general_activity", time.time())

    def _untrack_general(self, message_id: int) -> None:
        ids = json.loads(self.db.kv_get("general_msgs", "[]"))
        self.db.kv_set("general_msgs", json.dumps([i for i in ids if i != message_id]))

    async def _janitor(self) -> None:
        """5 minutes after the last activity in General, remove everything there except the dashboard."""
        ids = json.loads(self.db.kv_get("general_msgs", "[]"))
        if not ids or time.time() - float(self.db.kv_get("general_activity", "0")) < GENERAL_IDLE_CLEAN_S:
            return
        self.db.kv_set("general_msgs", "[]")
        dash = int(self.db.kv_get("dashboard_msg_id", "0"))
        doomed = [i for i in ids if i != dash]
        removed = await self._delete_many(doomed)
        if any(i > dash for i in doomed):
            await self.refresh_dashboard(repost=True)
        log.info("General cleaned: %d messages", removed)

    async def _delete_many(self, ids: list[int]) -> int:
        """Delete messages; returns how many were actually deleted. A batch is all-or-nothing in
        Telegram (one undeletable service message fails it), so a failed batch goes one by one."""
        deleted = 0
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            try:
                await self.tg.call("deleteMessages", chat_id=self.chat_id, message_ids=batch)
                deleted += len(batch)
            except TelegramError:
                for mid in batch:
                    deleted += bool(await self.tg.delete_message(self.chat_id, mid))
        return deleted

    async def sweep_general(self) -> int:
        """One-time cleanup of messages left in General by older versions. The Bot API cannot list or
        read old messages, so each unknown message is forwarded into a temporary topic to read it;
        only messages that are certainly General-only (old dashboards, help, confirmations, the
        owner's /commands) are deleted. The temporary topic is deleted with all copies at the end."""
        known: set[int] = {int(self.db.kv_get("dashboard_msg_id", "0"))}
        for s in self.db.sessions(include_archived=True):
            known |= {s.topic_id, s.control_msg_id or 0}
        known |= {r[0] for r in self.db.conn.execute("SELECT topic_id FROM topic_names")}
        for r in self.db.conn.execute("SELECT message_id, status_msg_id FROM turns"):
            known |= {r[0] or 0, r[1] or 0}
        try:
            tmp = await self.tg.create_topic(self.chat_id, "🧹 Уборка General — удалится сама")
        except TelegramError as e:
            log.warning("sweep: cannot create a temporary topic: %s", e.description)
            return 0
        known.add(tmp)
        doomed = []
        bot_id = self.me.get("id")
        for mid in range(1, tmp):
            if mid in known:
                continue
            try:
                fwd = await self.tg.call("forwardMessage", chat_id=self.chat_id, from_chat_id=self.chat_id,
                                         message_id=mid, message_thread_id=tmp, disable_notification=True)
            except TelegramError as e:
                if "can't be forwarded" in e.description.lower():
                    doomed.append(mid)   # a service message ("pinned", "group created"...), not a topic root
                continue
            text = (fwd.get("text") or fwd.get("caption") or "").strip()
            origin = fwd.get("forward_origin") or {}
            sender = (origin.get("sender_user") or {}).get("id")
            if sender == bot_id and text.startswith(GENERAL_BOT_PREFIXES) and "⏱ " not in text:
                doomed.append(mid)   # "⏱" marks Claude's answers (always in topics) - never touch those
            elif sender != bot_id and command_name(text) in GENERAL_COMMANDS:
                doomed.append(mid)
        try:
            await self.tg.call("deleteForumTopic", chat_id=self.chat_id, message_thread_id=tmp)
        except TelegramError as e:
            log.warning("sweep: temporary topic not deleted: %s", e.description)
        removed = await self._delete_many(doomed)
        self.db.kv_set("general_swept", time.time())
        log.info("General sweep: %d of %d old messages removed", removed, len(doomed))
        await self.refresh_dashboard(repost=True)
        return removed

    def ephemeral(self, message_id: int, ttl: int) -> None:
        """Schedule deletion of a General message (persisted, so it survives restarts)."""
        items = [x for x in json.loads(self.db.kv_get("ephemeral", "[]")) if x[0] != message_id]
        items.append([message_id, time.time() + ttl])
        self.db.kv_set("ephemeral", json.dumps(items))

    async def _cleanup_ephemeral(self) -> None:
        items = json.loads(self.db.kv_get("ephemeral", "[]"))
        due = [x for x in items if x[1] <= time.time()]
        if not due:
            return
        self.db.kv_set("ephemeral", json.dumps([x for x in items if x[1] > time.time()]))
        for message_id, _ in due:
            await self.tg.delete_message(self.chat_id, message_id)

    # ---- limits ---------------------------------------------------------------------------
    async def refresh_usage(self) -> None:
        """Run Claude Code's built-in `/usage` (local command: no model call, no tokens spent).
        It re-fetches the account's limits into ~/.claude.json, which the dashboard reads."""
        self._usage_at = time.time()
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"} | {"CLAUDE_CODE_ENTRYPOINT": ENTRYPOINT}
        try:
            p = await asyncio.create_subprocess_exec(
                self.cfg.claude_cli, "-p", "/usage", "--output-format", "json", "--no-session-persistence",
                cwd=str(Path.home()), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 60)
            if json.loads(out or b"{}").get("is_error"):
                log.warning("usage refresh: Claude Code reported an error")
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            log.warning("usage refresh failed: %s", type(e).__name__)
        self.dashboard_changed()

    async def _usage_loop(self) -> None:
        while not self.stopping.is_set():
            if time.time() - self._usage_at >= USAGE_REFRESH_S - 1:
                await self.refresh_usage()
            await asyncio.sleep(30)

    # ---- the live dashboard -------------------------------------------------------------
    def dashboard_changed(self) -> None:
        """Called by the manager on any status change: re-render soon (debounced)."""
        self._dash_kick.set()

    async def _dashboard_loop(self) -> None:
        await self.refresh_dashboard()
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self._dash_kick.wait(), DASH_REFRESH_S)
            except asyncio.TimeoutError:
                pass
            gap = DASH_MIN_GAP_S - (time.time() - self._dash_last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._dash_kick.clear()
            try:
                await self.refresh_dashboard()
            except Exception:  # noqa: BLE001 - never let the dashboard kill the loop
                log.exception("dashboard refresh failed")

    async def render_view(self, view: str) -> tuple[str, list[list[dict]]]:
        """Dashboard screens. Sub-screens open inside the same message and always have «◂ Назад»."""
        back = [btn("◂ Назад", "dash:main")]
        if view.startswith("import"):
            text, rows = await self.import_list(int(view.partition(":")[2] or 0))
            return text, rows + [back]
        if view == "proj":
            cur = self.m.default_cwd()
            rows = [[btn(("✅ " if p == cur else "") + folder_label(p).capitalize(), f"proj:d:{self._path_key(p)}")]
                    for p in self.known_projects()]
            return ("📁 <b>Папка для новых сессий</b>\n<i>Новые папки добавляются в настройках сервиса (PROJECT_DIRS).</i>",
                    rows + [back])
        if view == "diag":
            return await self.diagnostics(), [[btn("🔄 Обновить", "dash:diag"), back[0]]]
        if view == "help":
            return GENERAL_HELP, [[btn("Все команды ▸", "dash:helpall")], [back[0]]]
        if view == "helpall":
            return ALL_COMMANDS, [[btn("◂ Назад", "dash:help")]]
        return self.dashboard(view)

    async def refresh_dashboard(self, view: str | None = None, repost: bool = False) -> None:
        """Edit the single dashboard message in General (create it if missing).
        repost=True moves it to the bottom of General (used by /status)."""
        if not self.chat_id:
            return
        explicit = view is not None or repost   # user asked: must not be skipped for rate budget
        if view:
            self.db.kv_set("dashboard_view", view)
            self.db.kv_set("dashboard_view_at", time.time())
        view = self.db.kv_get("dashboard_view", "main")
        if view != "main" and time.time() - float(self.db.kv_get("dashboard_view_at", "0")) > DASH_VIEW_RESET_S:
            view = "main"
            self.db.kv_set("dashboard_view", view)
        text, buttons = await self.render_view(view)
        note, until = self._flash
        if note and time.time() < until:
            head, _, tail = text.partition("\n")
            text = f"{head}\n🔔 {note}\n{tail}"
        mid = int(self.db.kv_get("dashboard_msg_id", "0"))
        if mid and repost:
            await self.tg.delete_message(self.chat_id, mid)
            mid = 0
        if mid:
            try:
                await self.tg.edit_message(self.chat_id, mid, text, buttons=buttons, droppable=not explicit)
                self._dash_last = time.time()
                return
            except TelegramError as e:
                if "not found" not in e.description.lower() and "can't be edited" not in e.description.lower():
                    log.warning("dashboard edit failed: %s", e.description)
                    return
        msg = await self.tg.send_message(self.chat_id, text, buttons=buttons, silent=True)
        self.db.kv_set("dashboard_msg_id", msg["message_id"])
        self._dash_last = time.time()
        log.info("dashboard message posted (%s)", msg["message_id"])

    def topic_link(self, topic_id: int) -> str:
        cid = str(self.chat_id)
        return f"https://t.me/c/{cid[4:] if cid.startswith('-100') else cid.lstrip('-')}/{topic_id}"

    def _place(self, s: D.Session, live: dict[str, dict]) -> str:
        """📱 = Telegram, 💻 = VS Code: where it runs right now, otherwise where it came from."""
        if s.id in self.m.active:
            return "📱"
        if (live.get(s.claude_session_id) or {}).get("status") == "busy":
            return "💻"
        return "💻" if s.origin == "import" else "📱"

    def _link(self, s: D.Session, n: int = 34) -> str:
        return f'<a href="{self.topic_link(s.topic_id)}">{esc(clip_words(s.title, n))}</a>'

    def _busy_in_vscode(self, s: D.Session, live: dict[str, dict]) -> dict | None:
        d = live.get(s.claude_session_id)
        return d if d and d.get("status") == "busy" and s.id not in self.m.active else None

    def dashboard(self, view: str = "main") -> tuple[str, list[list[dict]]]:
        """The live dashboard. Section headers carry the state; each line starts with where the
        session is from (📱 Telegram / 💻 VS Code), so the left edge reads as one column."""
        if view not in ("main", "all", "limits"):
            view = "main"
        live = live_sessions()
        sessions = self.db.sessions()
        legend = "<i>📱 Telegram · 💻 VS Code</i>"
        home = [[btn("🔄 Обновить", f"dash:{view}"), btn("◂ Назад", "dash:main")]]
        if view == "all":
            home = [[btn("📥 Подключить из VS Code", "dash:import:0")],
                    [btn("📁 Папка для новых", "dash:proj"), btn("🛠 Диагностика", "dash:diag")],
                    [btn("🧹 Почистить General", "sweep")]] + home
        if view == "limits":
            return "\n".join(["<b>📈 Лимиты Claude</b> · " + clock(), ""] + self.limits_lines()), home
        if view == "all":
            return self._dashboard_all(sessions, live, legend), home

        linked = {s.claude_session_id for s in self.db.sessions(include_archived=True)}
        vs_busy = [(cid, d) for cid, d in live.items() if cid not in linked and d.get("status") == "busy"]
        attention = sorted((s for s in sessions if s.status in (D.WAITING_APPROVAL, D.WAITING, D.ERROR)),
                           key=lambda s: [D.WAITING_APPROVAL, D.WAITING, D.ERROR].index(s.status))
        running = [s for s in sessions if s.status == D.RUNNING]
        running += [s for s in sessions if s not in running and s not in attention and self._busy_in_vscode(s, live)]
        queued = sorted((s for s in sessions if s.status == D.QUEUED and s not in running),
                        key=lambda s: self.m.queue_position(s.id) or 999)
        free = [s for s in sessions if s not in running + attention + queued]
        vs_only_free = [(cid, d) for cid, d in live.items() if cid not in linked and d.get("status") != "busy"]

        def item(s: D.Session, detail: str = "") -> str:
            return f"{self._place(s, live)} {self._link(s)}" + (f" · {detail}" if detail else "")

        lines = [f"<b>📊 Claude Control</b> · {clock()}"]
        if not (attention or running or vs_busy or queued):
            lines.append("Сейчас ничего не работает." if sessions else
                         "Сессий пока нет. Создайте тему или нажмите «➕ Новая».")
        if attention:
            what = {D.WAITING_APPROVAL: "🔐 разрешение", D.WAITING: "❓ вопрос", D.ERROR: "🔴 ошибка"}
            lines += ["", "<b>⚡ Нужны вы</b>"] + [item(s, what[s.status]) for s in attention]
        if running or vs_busy:
            lines += ["", "<b>🟢 Работают</b>"]
            for s in running:
                vs = self._busy_in_vscode(s, live)
                since = (time.time() - (vs.get("statusUpdatedAt") or 0) / 1000) if vs \
                    else (time.time() - (s.run_started_at or s.updated_at))
                waiting = self.db.queued_count(s.id)
                lines.append(item(s, duration(since) + (f" · ещё {waiting} в очереди" if waiting else "")))
            for cid, d in vs_busy:
                info = get_session_info(cid)
                title = (info and (info.custom_title or info.summary)) or "без названия"
                since = time.time() - (d.get("statusUpdatedAt") or 0) / 1000
                lines.append(f"💻 {esc(clip_words(title, 34))} · {duration(since)}")
        if queued:
            lines += ["", "<b>🟡 Очередь</b>"] + [item(s, ordinal(self.m.queue_position(s.id) or 1)) for s in queued]
        if free or vs_only_free:
            rows = [(s.last_activity_at, item(s)) for s in free]
            for cid, d in vs_only_free:
                info = get_session_info(cid)
                title = (info and (info.custom_title or info.summary)) or "без названия"
                rows.append(((d.get("statusUpdatedAt") or 0) / 1000,
                             f"💻 {esc(clip_words(title, 34))} · <i>только в VS Code</i>"))
            rows.sort(key=lambda r: -r[0])
            lines += ["", "<b>⚪ Свободны</b>"] + [r[1] for r in rows[:6]]
            if len(rows) > 6:
                lines.append(f"<i>…и ещё {len(rows) - 6} — в «📋 Все сессии»</i>")
        limits = self.limits_compact()
        if limits:
            lines += [""] + limits
        lines += ["", legend]
        buttons = [[btn("🔄 Обновить", "dash:main"), btn("➕ Новая", "new")],
                   [btn("📋 Все сессии", "dash:all"), btn("📈 Лимиты", "dash:limits")]]
        if vs_busy or vs_only_free:
            buttons.append([btn("📥 Подключить из VS Code", "dash:import:0")])
        return "\n".join(lines), buttons

    def _dashboard_all(self, sessions: list[D.Session], live: dict[str, dict], legend: str) -> str:
        order = [D.WAITING_APPROVAL, D.WAITING, D.ERROR, D.RUNNING, D.QUEUED, D.STOPPED, D.IDLE, D.NEW]
        lines = [f"<b>📋 Все сессии</b> · {len(sessions)}", ""]
        for s in sorted(sessions, key=lambda s: (order.index(s.status) if s.status in order else 99, -s.last_activity_at)):
            emoji, label = STATUS_VIEW.get(s.status, ("⚪", s.status.lower()))
            if self._busy_in_vscode(s, live):
                detail = "🟢 работает в VS Code"
            elif s.status == D.RUNNING:
                detail = f"🟢 {duration(time.time() - (s.run_started_at or s.updated_at))}"
            elif s.status in (D.WAITING, D.WAITING_APPROVAL, D.QUEUED, D.ERROR):
                detail = f"{emoji} {label}"
            elif s.status == D.NEW:
                detail = "новая"
            else:
                detail = f"{ago(s.last_activity_at)} назад"
            lines.append(f"{self._place(s, live)} {self._link(s)} · {detail}")
        linked = {s.claude_session_id for s in self.db.sessions(include_archived=True)}
        others = [(cid, d) for cid, d in live.items() if cid not in linked]
        if others:
            lines += ["", "<b>Открыты в VS Code, не в Telegram</b>"]
            for cid, d in others:
                info = get_session_info(cid)
                title = (info and (info.custom_title or info.summary)) or "без названия"
                state = "🟢 работает" if d.get("status") == "busy" else "свободна"
                lines.append(f"💻 {esc(clip_words(title, 34))} · {state}")
            lines.append("<i>Подключить к Telegram — кнопка «📥 Подключить из VS Code» ниже.</i>")
        archived = len(self.db.sessions(include_archived=True)) - len(sessions)
        if archived:
            lines += ["", f"🗄 В архиве: {archived}"]
        return "\n".join(lines + ["", legend])

    def _limit_windows(self) -> tuple[list[dict], bool] | None:
        """All usage windows, merged from two read-only sources, the fresher one winning per window:
        the RateLimitEvent of our own runs (kv) and Claude Code's own cache in ~/.claude.json
        (updated by VS Code sessions too; it also has per-model weekly limits)."""
        found: dict[str, dict] = {}
        rejected = False
        names = {"five_hour": ("5 ч", "5 часов"), "seven_day": ("Неделя", "Неделя (все модели)"),
                 "seven_day_opus": ("Неделя Opus", "Неделя · Opus"), "seven_day_sonnet": ("Неделя Sonnet", "Неделя · Sonnet")}

        def put(key: str, short: str, full: str, pct: float, reset: float | None, at: float) -> None:
            if key not in found or found[key]["at"] < at:
                found[key] = {"short": short, "full": full, "pct": round(pct), "reset": reset, "at": at}

        raw_kv = self.db.kv_get("rate_limit")
        if raw_kv:
            r = json.loads(raw_kv)
            rejected = r.get("status") == "rejected"
            windows = dict((r.get("raw") or {}).get("unifiedWindows") or {})
            if not windows and r.get("type"):
                windows[r["type"]] = {"utilization": r.get("utilization"), "resetsAt": r.get("resets_at")}
            for key, w in windows.items():
                if key in names and w:
                    put(key, *names[key], (w.get("utilization") or 0) * 100, w.get("resetsAt"), r.get("at", 0))
        try:
            cache = json.loads((Path.home() / ".claude.json").read_text()).get("cachedUsageUtilization") or {}
        except (OSError, ValueError):
            cache = {}
        at = (cache.get("fetchedAtMs") or 0) / 1000
        for lim in (cache.get("utilization") or {}).get("limits") or []:
            try:
                reset = datetime.fromisoformat(lim["resets_at"]).timestamp() if lim.get("resets_at") else None
            except ValueError:
                reset = None
            kind, pct = lim.get("kind"), lim.get("percent") or 0
            if kind == "session":
                put("five_hour", *names["five_hour"], pct, reset, at)
            elif kind == "weekly_all":
                put("seven_day", *names["seven_day"], pct, reset, at)
            elif kind == "weekly_scoped":
                model = (((lim.get("scope") or {}).get("model") or {}).get("display_name")) or "модель"
                put(f"week_{model}", f"Неделя {model}", f"Неделя · {model}", pct, reset, at)
        if not found:
            return None
        order = ["five_hour", "seven_day"]
        keys = sorted(found, key=lambda k: order.index(k) if k in order else len(order))
        return [found[k] for k in keys], rejected

    def limits_compact(self) -> list[str]:
        """A small titled block for the main dashboard."""
        data = self._limit_windows()
        if not data:
            return []
        windows, rejected = data
        age = time.time() - max(w["at"] for w in windows)
        lines = ["<b>📈 Лимиты</b>" + (f" <i>· данные {duration(age)} назад</i>" if age > 1800 else "")]
        for w in windows:
            if w["reset"] and w["reset"] <= time.time():
                lines.append(f"{w['short']} — обновилось в {clock(w['reset'])}")
                continue
            pct = w["pct"]
            bar = "▰" * min(5, round(pct / 20)) + "▱" * (5 - min(5, round(pct / 20)))
            lines.append(f"{w['short']} {bar} <b>{pct}%</b>{' ⚠️' if pct >= 75 else ''}"
                         + (f" · сброс через {until(w['reset'])}" if w["reset"] else ""))
        if rejected:
            lines.append("⛔ Лимит исчерпан — задачи ждут сброса")
        return lines

    def limits_lines(self) -> list[str]:
        """Detailed view: one small block per window (name and %, bar, reset countdown + exact time)."""
        data = self._limit_windows()
        if not data:
            return ["Данных пока нет — появятся после первого запуска Claude."]
        windows, rejected = data
        lines = []
        for w in windows:
            if w["reset"] and w["reset"] <= time.time():
                lines += [f"<b>{w['full']}</b>", f"окно обновилось в {clock(w['reset'])}, свежие цифры — после следующей задачи", ""]
                continue
            pct = w["pct"]
            bar = "▰" * min(10, round(pct / 10)) + "▱" * (10 - min(10, round(pct / 10)))
            lines += [f"<b>{w['full']}</b> — {pct}%{' ⚠️' if pct >= 75 else ''}", bar]
            if w["reset"]:
                lines.append(f"сброс через {until(w['reset'])} · {when(w['reset'])}")
            lines.append("")
        if rejected:
            lines += ["⛔ Лимит исчерпан — новые задачи будут ждать сброса.", ""]
        age = time.time() - max(w["at"] for w in windows)
        lines.append("<i>данные " + ("только что" if age < 60 else f"{duration(age)} назад") + "</i>")
        return lines

    async def new_session_topic(self, title: str) -> None:
        name = clip(title, 100) if title else f"Новая сессия {clock()}"
        try:
            topic_id = await self.tg.create_topic(self.chat_id, name)
        except TelegramError as e:
            await self.flash(self._rights_hint(e))
            return
        s = self.m.create_session(self.chat_id, topic_id, name, title_source="user" if title else "placeholder")
        await self.ensure_control_card(s)
        await self.flash(f'Создана тема <a href="{self.topic_link(topic_id)}">{esc(name)}</a> — напишите туда задание.')

    def _rights_hint(self, e: TelegramError) -> str:
        if "rights" in e.description or "not enough" in e.description.lower():
            return ("⚠️ У бота нет права «Управление темами». Откройте группу → название вверху → Изм. → "
                    "Администраторы → бот → включите «Управление темами».")
        return f"⚠️ Telegram не дал это сделать: {esc(e.description)}"

    # =========================================================================== topics
    async def _topic(self, m: dict, topic_id: int, cmd: str | None, args: str, text: str) -> None:
        s = self.db.session_by_topic(self.chat_id, topic_id)
        if cmd is not None:
            await self._topic_command(m, topic_id, s, cmd, args)
            return
        spoken = m.get("voice") or m.get("audio") or m.get("video_note")
        from_voice = False
        if spoken and "document" not in m:
            if not self.voice:
                await self.notice(topic_id, "🎤 Расшифровка голосовых на сервере не включена — напишите текстом.")
                return
            if s is None:
                s = self._session_for_new_topic(m, topic_id)
                await self.ensure_control_card(s)
            heard = await self._transcribe(m, s, spoken)
            if heard is None:
                return
            text, from_voice = (text + "\n\n" if text else "") + heard, True
        elif m.get("video") and "document" not in m:
            await self.notice(topic_id, "🎬 Видео пока не поддерживаются — пришлите его файлом или опишите словами.")
            return
        if s and text and float(self.db.kv_get(f"rename_wait:{s.id}", "0")) > time.time():
            self.db.conn.execute("DELETE FROM kv WHERE key=?", (f"rename_wait:{s.id}",))
            await self.tg.delete_message(self.chat_id, m["message_id"])
            await self.rename(s, text)
            await self.show_panel(s)
            return
        if s is None:
            s = self._session_for_new_topic(m, topic_id)
            await self.ensure_control_card(s)
        # While Claude waits: plain text answers its question; for a permission request only a
        # Telegram *reply* to that request counts (= deny with feedback). Anything else is queued.
        req = self.m.pending_request(s.id)
        if req and text and not (m.get("document") or m.get("photo")):
            replied_to = (m.get("reply_to_message") or {}).get("message_id")
            mid, shown = req.message_id, req.text
            if req.kind == "ask":
                self.m.resolve(req.id, "answer", text)
                await self._close(mid, shown, f"💬 Ваш ответ: <b>{esc(clip(text, 200))}</b>")
                return
            if replied_to and replied_to == req.message_id:
                self.m.resolve(req.id, "deny", f"The user declined this action and wrote: {text}")
                await self._close(mid, shown, f"❌ <b>Отказано</b>: «{esc(clip(text, 200))}»")
                return
        files = await self._save_files(m, s)
        if files is None:
            return
        if files and not text:
            pending = s.pending_file_list + files
            self.db.update_session(s.id, pending_files=json.dumps(pending, ensure_ascii=False))
            names = ", ".join(shown_name(f) for f in files)
            await self.notice(topic_id, f"📎 Файл сохранён: <b>{esc(names)}</b>\nНапишите, что с ним сделать.")
            return
        if s.archived:
            await self.unarchive(s, quiet=True)
            s = self.db.get_session(s.id)
        attached = s.pending_file_list + files
        prompt = text + ("\n\n[Это расшифровка голосового сообщения — возможны ошибки распознавания.]"
                         if from_voice else "")
        if attached:
            prompt += "\n\n[Файлы от пользователя из Telegram:]\n" + "\n".join(f"- {p}" for p in attached)
            self.db.update_session(s.id, pending_files="[]")
        turn = self.m.enqueue(s, prompt, self.chat_id, m["message_id"])
        if turn is None:
            return
        self.m.schedule()
        await self._queue_notice(s, turn)

    async def _transcribe(self, m: dict, s: D.Session, spoken: dict) -> str | None:
        """Voice / round video / audio -> text via GigaAM. The recording is deleted afterwards."""
        seconds = spoken.get("duration") or 0
        if seconds > self.cfg.voice_max_min * 60 or (spoken.get("file_size") or 0) > 20 * 1024 * 1024:
            await self.notice(s.topic_id, f"🎤 Слишком длинная запись — расшифровываю до {self.cfg.voice_max_min} минут "
                                          "(и до 20 МБ). Разбейте на части или пришлите текстом.")
            return None
        status = await self._send_topic(s, "🎙 Расшифровываю…", reply_to=m["message_id"], silent=True)
        sid = status["message_id"] if status else None
        ext = re.sub(r"[^A-Za-z0-9]", "", Path(spoken.get("file_name") or "").suffix)[:5] \
            or ("mp4" if "video_note" in m else "ogg")
        dest = self.m.inbox_dir(s) / f"voice-{m['message_id']}.{ext}"
        started = time.time()
        try:
            await self.tg.download(spoken["file_id"], dest)
            text = await self.voice.transcribe(dest, seconds)
        except Exception as e:  # noqa: BLE001 - any failure -> a clear message, never a traceback
            log.warning("voice transcription failed for session %s: %s", s.id, e)
            if sid:
                await self._safe_edit(sid, "⚠️ Не получилось расшифровать голосовое. Попробуйте ещё раз или напишите текстом.")
                self.ephemeral(sid, SHORT_TTL)
            return None
        finally:
            dest.unlink(missing_ok=True)
        log.info("voice transcribed for session %s: %ss audio in %.1fs, %d chars", s.id, seconds,
                 time.time() - started, len(text))
        if not text:
            if sid:
                await self._safe_edit(sid, "🎙 Не разобрал слов — повторите, пожалуйста.")
                self.ephemeral(sid, SHORT_TTL)
            return None
        if sid:
            await self._safe_edit(sid, f"🎙 <i>{esc(clip_block(text, 3500))}</i>")
        return text

    def _session_for_new_topic(self, m: dict, topic_id: int) -> D.Session:
        name = self.db.topic_name(self.chat_id, topic_id)
        if not name:
            created = (m.get("reply_to_message") or {}).get("forum_topic_created") or {}
            name = created.get("name")
        return self.m.create_session(self.chat_id, topic_id, name or f"Сессия {topic_id}",
                                     title_source="user" if name else "placeholder")

    async def _queue_notice(self, s: D.Session, turn: D.Turn) -> None:
        if s.id in self.m.active and self.m.active[s.id].turn_id == turn.id:
            return  # started right away; run_started() shows progress
        if s.id in self.m.busy_elsewhere():
            text = ("💻 Эта сессия сейчас работает в VS Code.\n"
                    "Выполню ваше сообщение, когда она там освободится.")
        elif s.id in self.m.active:
            n = self.db.queued_count(s.id)
            text = f"📥 Принято. Отвечу после текущего ответа.\nВ очереди этой темы: {n}"
            req = self.m.pending_request(s.id)
            if req:
                text += ("\n🔐 Сначала Claude ждёт вашего решения выше ↑" if req.kind == "perm"
                         else "\n❓ Сначала Claude ждёт ответа на вопрос выше ↑")
        else:
            pos = self.m.queue_position(s.id) or 1
            text = (f"🟡 В очереди — {ordinal(pos)}.\nСейчас работают {self.cfg.max_concurrent} задач (это максимум). "
                    "Начну, как только одна закончится.")
        msg = await self._send_topic(s, text, reply_to=turn.message_id, silent=True)
        if not msg:
            return
        self.db.update_turn(turn.id, status_msg_id=msg["message_id"])
        ar = self.m.active.get(s.id)
        if ar and ar.turn_id == turn.id and ar.status_msg_id != msg["message_id"]:
            await self.tg.delete_message(self.chat_id, msg["message_id"])  # it started meanwhile

    async def _topic_renamed_by_user(self, topic_id: int, name: str) -> None:
        self.db.set_topic_name(self.chat_id, topic_id, name)
        s = self.db.session_by_topic(self.chat_id, topic_id)
        if s and s.title != name:
            self.db.update_session(s.id, title=name, title_source="user", title_synced=0)
            await self.m.sync_title(self.db.get_session(s.id))
            log.info("session %s renamed from Telegram", s.id)

    async def _topic_command(self, m: dict, topic_id: int, s: D.Session | None, cmd: str, args: str) -> None:
        reply = lambda text, **kw: self.tg.send_message(self.chat_id, text, thread_id=topic_id,  # noqa: E731
                                                        reply_to=m["message_id"], **kw)
        note = lambda text: self.notice(topic_id, text)  # noqa: E731
        self.ephemeral(m["message_id"], SHORT_TTL)
        if cmd in ("help", "start"):
            await reply(TOPIC_HELP, buttons=[[btn("Все команды ▸", "help:all")]])
            return
        if cmd in ("new", "import", "diagnostics", "diag", "sessions"):
            await self._general(m, cmd, args)
            return
        if cmd == "project":
            if s is None:
                s = self._session_for_new_topic(m, topic_id)
            if s.started or s.id in self.m.active:
                await note(f"📁 Папка этой сессии: <b>{esc(folder_label(s.cwd))}</b>\n"
                           "У начатой сессии папку сменить нельзя — создайте новую тему.")
            else:
                text, buttons = self.panel_view(s, "folder")
                await reply(text, buttons=buttons)
            return
        if s is None:
            await note("Эта тема ещё не связана с Claude. Напишите задание обычным сообщением — сессия создастся сама.")
            return
        if cmd == "status":
            await reply(self.session_card(s, brief=True))
        elif cmd == "info":
            text, buttons = self.panel_view(s, "info")
            await reply(text, buttons=buttons)
        elif cmd == "stop":
            running, cancelled = await self.m.stop(s.id)
            await self._drop_queue_notices(cancelled)
            if running:
                await note("⏹ Останавливаю…" + (f" Также отменено сообщений в очереди: {len(cancelled)}." if cancelled else ""))
            elif cancelled:
                await note(f"⏹ Отменено сообщений в очереди: {len(cancelled)}.")
            else:
                await note("Сейчас ничего не выполняется.")
        elif cmd == "restart":
            last = self.db.last_turn(s.id)
            if last is None:
                await note("Нечего перезапускать — в сессии ещё не было запросов.")
                return
            _, cancelled = await self.m.stop(s.id)
            await self._drop_queue_notices(cancelled)
            self.m.retry(last.id)
            await note("🔁 Перезапускаю последний запрос.")
        elif cmd == "rename":
            if args:
                await self.rename(s, args, reply_to=m["message_id"])
            else:
                await self.ask_new_title(s)
        elif cmd == "archive":
            await self.archive(s)
        elif cmd == "unarchive":
            await self.unarchive(s)
        elif cmd == "fork":
            await self.fork(s, topic_id, m["message_id"])
        elif cmd == "model":
            text, buttons = self.panel_view(s, "model")
            await reply(text, buttons=buttons)
        else:
            await note("Не знаю такой команды — все действия есть в закреплённой панели вверху темы.")

    def session_card(self, s: D.Session, brief: bool = False) -> str:
        live = live_sessions()
        emoji, label = STATUS_VIEW.get(s.status, ("⚪", s.status))
        lines = [f"{emoji} <b>{esc(s.title)}</b> {self._place(s, live)}"]
        ar = self.m.active.get(s.id)
        vs = self._busy_in_vscode(s, live)
        if ar:
            lines.append(f"Работает {duration(time.time() - ar.started_at)}"
                         + (f" · {esc(clip(ar.activity, 40))}" if ar.activity else ""))
        elif vs:
            lines.append(f"Сейчас работает в VS Code · {duration(time.time() - (vs.get('statusUpdatedAt') or 0) / 1000)}")
        elif s.status == D.ERROR:
            lines.append(f"Ошибка: {esc(s.last_error or '')}")
        elif s.status == D.NEW:
            lines.append("Ещё не начата — напишите задание.")
        else:
            lines.append(f"{label.capitalize()} · последняя активность {ago(s.last_activity_at)} назад")
        q = self.db.queued_count(s.id)
        if q:
            lines.append(f"В очереди: {plural(q, 'сообщение', 'сообщения', 'сообщений')}")
        if brief:
            return "\n".join(lines)
        origin = {"import": "подключена из VS Code", "fork": "ветка, создана"}.get(s.origin, "создана в Telegram")
        model = s.model.capitalize() if s.model else (self.cfg.model or f"как в Claude Code ({default_model_label()})")
        lines += ["", f"Запросов выполнено: {self.db.count_turns(s.id)} · {origin} {when(s.created_at)}",
                  f"Модель: {esc(model)} · папка: {esc(folder_label(s.cwd))}"]
        if s.grant_list:
            lines.append("Без вопросов в этой теме: " + esc(", ".join(grant_label(g) for g in s.grant_list)))
        lines += ["", "<blockquote expandable>💻 <b>Открыть на компьютере</b>\n"
                  f"VS Code → история разговоров Claude → «{esc(clip(s.title, 40))}»\nТерминал:\n"
                  f"<code>cd {esc(s.cwd)} &amp;&amp; claude --resume {s.claude_session_id}</code></blockquote>"]
        return "\n".join(lines)

    # ---- the pinned control panel of a topic -------------------------------------------------
    def panel_view(self, s: D.Session, view: str = "main") -> tuple[str, list[list[dict]]]:
        """Screens of the pinned panel. Every sub-screen has «◂ Назад»; irreversible actions ask first."""
        back = [btn("◂ Назад", f"pv:{s.id}:main")]
        if view == "info":
            return self.session_card(s), [[btn("🔄 Обновить", f"pv:{s.id}:info"), back[0]]]
        if view == "model":
            cur = s.model or self.cfg.model
            mark = lambda name: ("✅ " if cur == name else "") + name.capitalize()  # noqa: E731
            return MODEL_TEXT, [[btn(("✅ " if not cur else "") + f"Как в Claude Code ({default_model_label()})",
                                     f"pa:{s.id}:model:-")],
                                [btn(mark("opus"), f"pa:{s.id}:model:opus"), btn(mark("sonnet"), f"pa:{s.id}:model:sonnet"),
                                 btn(mark("haiku"), f"pa:{s.id}:model:haiku")], back]
        if view == "folder":
            rows = [[btn(("✅ " if p == s.cwd else "") + folder_label(p).capitalize(), f"pa:{s.id}:folder:{self._path_key(p)}")]
                    for p in self.known_projects()]
            return "📁 <b>Папка для этой темы</b>\n<i>Выбрать можно только до первого сообщения.</i>", rows + [back]
        if view == "fork":
            return ("🌿 <b>Создать ветку?</b>\nКопия всего разговора откроется в новой теме. Эта тема не изменится.",
                    [[btn("✅ Создать ветку", f"pa:{s.id}:fork")], back])
        if view == "arch":
            return ("🗄 <b>Убрать в архив?</b>\nСессия пропадёт с дашборда, тема закроется. История Claude сохранится — "
                    "вернуть можно в любой момент.", [[btn("✅ Убрать в архив", f"pa:{s.id}:arch")], back])
        if view == "help":
            return ALL_COMMANDS, [back]
        lines = ["🎛 <b>Управление сессией</b>"]
        if s.archived:
            lines.append("🗄 Сессия в архиве.")
        elif not s.started and s.id not in self.m.active:
            lines.append("👋 Напишите задание обычным сообщением — я начну.")
        model = s.model.capitalize() if s.model else (self.cfg.model or default_model_label())
        lines += [f"🧠 {esc(model)} · 📁 {esc(folder_label(s.cwd))}",
                  "<i>Закреплено — нажмите на полоску вверху темы, чтобы вернуться сюда.</i>"]
        rows = [[btn("ℹ️ О сессии", f"pv:{s.id}:info"), btn("⏹ Остановить", f"stop:{s.id}")],
                [btn("🧠 Модель", f"pv:{s.id}:model"), btn("✏️ Переименовать", f"pa:{s.id}:ren")],
                [btn("🌿 Ветка", f"pv:{s.id}:fork"),
                 btn("📤 Вернуть из архива", f"pa:{s.id}:unarch") if s.archived else btn("🗄 В архив", f"pv:{s.id}:arch")]]
        last = [btn("❔ Все команды", f"pv:{s.id}:help")]
        if not s.started and s.id not in self.m.active:
            last.insert(0, btn("📁 Папка", f"pv:{s.id}:folder"))
        return "\n".join(lines), rows + [last]

    async def show_panel(self, s: D.Session, view: str = "main", message_id: int | None = None) -> None:
        """Render a panel screen into the message the button belongs to (the pinned one by default)."""
        mid = message_id or s.control_msg_id
        if mid:
            text, buttons = self.panel_view(self.db.get_session(s.id), view)
            await self._safe_edit(mid, text, buttons)

    async def ensure_control_card(self, s: D.Session) -> None:
        """Post and pin the topic's control panel once, so nothing has to be typed as a command."""
        if s.control_msg_id:
            return
        text, buttons = self.panel_view(s)
        msg = await self._send_topic(s, text, silent=True, buttons=buttons)
        if not msg:
            return
        self.db.update_session(s.id, control_msg_id=msg["message_id"])
        try:
            await self.tg.call("pinChatMessage", chat_id=self.chat_id, message_id=msg["message_id"],
                               disable_notification=True)
        except TelegramError as e:
            log.warning("pin failed for session %s: %s", s.id, e.description)

    async def _cards_for_existing_sessions(self) -> None:
        """Panels for sessions created before panels existed; older panels get the current layout."""
        for s in self.db.sessions(include_archived=True):
            try:
                if s.control_msg_id:
                    await self.show_panel(s)
                elif not s.archived:
                    await self.ensure_control_card(s)
            except (TelegramError, ConnectionError) as e:
                log.warning("control panel for session %s failed: %s", s.id, e)

    async def ask_new_title(self, s: D.Session, message_id: int | None = None) -> None:
        """Wait for the next plain message in the topic and use it as the new title."""
        self.db.kv_set(f"rename_wait:{s.id}", time.time() + RENAME_WAIT_S)
        text = "✏️ <b>Новое название</b>\nНапишите его обычным сообщением в эту тему."
        buttons = [[btn("◂ Отмена", f"pa:{s.id}:rencancel")]]
        if message_id or s.control_msg_id:
            await self._safe_edit(message_id or s.control_msg_id, text, buttons)
        if not message_id:   # typed /rename: the panel may be far above - say it here too
            msg = await self._send_topic(s, text, silent=True, buttons=buttons)
            if msg:
                self.ephemeral(msg["message_id"], SHORT_TTL)

    async def rename(self, s: D.Session, name: str, reply_to: int | None = None) -> None:
        name = clip(name, 100)
        try:
            await self.tg.rename_topic(self.chat_id, s.topic_id, name)
        except TelegramError as e:
            await self.notice(s.topic_id, self._rights_hint(e))
            return
        self.db.set_topic_name(self.chat_id, s.topic_id, name)
        self.db.update_session(s.id, title=name, title_source="user", title_synced=0)
        await self.m.sync_title(self.db.get_session(s.id))
        self.dashboard_changed()
        await self.notice(s.topic_id, f"✏️ Тема переименована: «{esc(name)}» — так она называется и в VS Code.")

    async def archive(self, s: D.Session, announce: bool = True) -> bool:
        """Hide a session from the dashboard; the Claude transcript is untouched."""
        if s.id in self.m.active:
            await self.notice(s.topic_id, "Сначала остановите задачу (⏹ в панели) — потом можно убрать в архив.")
            return False
        self.db.cancel_queued(s.id)
        self.m.set_status(s.id, D.ARCHIVED, archived=1)
        log.info("session %s archived", s.id)
        if announce:
            await self.notice(s.topic_id, "🗄 <b>Сессия в архиве</b>\nОна скрыта с дашборда, история Claude сохранена.\n"
                              "Вернуть — кнопкой в закреплённой панели или просто напишите сюда.")
        try:
            await self.tg.call("closeForumTopic", chat_id=self.chat_id, message_thread_id=s.topic_id)
        except TelegramError:
            pass
        await self.show_panel(s)
        return True

    async def unarchive(self, s: D.Session, quiet: bool = False, message_id: int | None = None) -> None:
        self.m.set_status(s.id, D.IDLE, archived=0)
        log.info("session %s unarchived", s.id)
        try:
            await self.tg.call("reopenForumTopic", chat_id=self.chat_id, message_thread_id=s.topic_id)
        except TelegramError:
            pass
        if message_id:
            await self._safe_edit(message_id, "📤 Сессия снова на дашборде.")
            self.ephemeral(message_id, SHORT_TTL)
        elif not quiet:
            await self.notice(s.topic_id, "📤 Сессия снова на дашборде.")
        await self.show_panel(s)

    async def fork(self, s: D.Session, topic_id: int, reply_to: int | None) -> None:
        async def say(text: str) -> None:
            await self.notice(topic_id, text)
        if s.id in self.m.active:
            await say("Дождитесь окончания задачи (или /stop), затем повторите /fork.")
            return
        if not s.started:
            await say("Сессия ещё пустая — ветвить нечего.")
            return
        title = clip(f"{s.title} · ветка", 100)
        try:
            new_topic = await self.tg.create_topic(self.chat_id, title)
        except TelegramError as e:
            await say(self._rights_hint(e))
            return
        new_id = await asyncio.to_thread(self.m.fork, s, title)
        self.m.create_session(self.chat_id, new_topic, title, cwd=s.cwd, claude_session_id=new_id,
                              started=True, origin="fork")
        await self.tg.send_message(self.chat_id, f"🌿 Ветка от «{esc(s.title)}». Вся история скопирована — "
                                   "продолжайте здесь, исходная тема не изменится.", thread_id=new_topic, silent=True)
        await self.ensure_control_card(self.db.session_by_topic(self.chat_id, new_topic))
        await say(f'🌿 Создана ветка: <a href="{self.topic_link(new_topic)}">{esc(title)}</a>')

    # =========================================================================== files
    async def _save_files(self, m: dict, s: D.Session) -> list[str] | None:
        items: list[tuple[str, str, int]] = []
        if doc := m.get("document"):
            items.append((doc["file_id"], doc.get("file_name") or "file", doc.get("file_size") or 0))
        if photos := m.get("photo"):
            p = max(photos, key=lambda x: x.get("file_size") or 0)
            items.append((p["file_id"], f"photo_{m['message_id']}.jpg", p.get("file_size") or 0))
        saved: list[str] = []
        inbox = self.m.inbox_dir(s).resolve()
        for file_id, name, size in items:
            if size > 20 * 1024 * 1024:
                await self.notice(s.topic_id, "⚠️ Файл больше 20 МБ — Telegram не даёт боту скачать такой. "
                                              "Положите его на сервер другим способом.")
                return None
            dest = (inbox / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe_filename(name)}").resolve()
            if dest.parent != inbox:
                log.warning("rejected suspicious file name in session %s", s.id)
                return None
            try:
                await self.tg.download(file_id, dest)
                os.chmod(dest, 0o600)
            except (TelegramError, ConnectionError, ValueError, OSError) as e:
                log.warning("file download failed for session %s: %s", s.id, e)
                await self.notice(s.topic_id, "⚠️ Не получилось скачать файл. Попробуйте ещё раз.")
                return None
            log.info("file saved for session %s (%d bytes)", s.id, dest.stat().st_size)
            saved.append(str(dest))
        return saved

    # =========================================================================== run UI (called by Manager)
    async def notice(self, topic_id: int, text: str, **kw: Any) -> dict | None:
        """A service message in a topic ("renamed", "stopped"...) that disappears after a minute."""
        try:
            msg = await self.tg.send_message(self.chat_id, text, thread_id=topic_id, silent=True, **kw)
        except TelegramError as e:
            log.warning("notice failed: %s", e.description)
            return None
        self.ephemeral(msg["message_id"], SHORT_TTL)
        return msg

    async def _drop_queue_notices(self, turns: list[D.Turn]) -> None:
        for t in turns:
            if t.status_msg_id:
                await self.tg.delete_message(self.chat_id, t.status_msg_id)

    async def _send_topic(self, s: D.Session, text: str, reply_markup_extra: dict | None = None,
                          **kw: Any) -> dict | None:
        try:
            if reply_markup_extra:   # e.g. ForceReply ("answer this message with a new title")
                return await self.tg.call("sendMessage", chat_id=self.chat_id, message_thread_id=s.topic_id,
                                          text=text, parse_mode="HTML", reply_markup=reply_markup_extra)
            return await self.tg.send_message(self.chat_id, text, thread_id=s.topic_id, **kw)
        except TelegramError as e:
            if "thread not found" in e.description.lower() or "topic_deleted" in e.description.lower():
                log.warning("topic of session %s is gone; archiving", s.id)
                self.db.update_session(s.id, archived=1, status=D.ARCHIVED)
                return None
            raise

    def _status_text(self, s: D.Session, ar: ActiveRun) -> str:
        elapsed = duration(time.time() - ar.started_at)
        if ar.waiting == "approval":
            head = f"🔐 Жду вашего разрешения ↓ · {elapsed}"
        elif ar.waiting == "question":
            head = f"❓ Claude спрашивает ↓ · {elapsed}"
        elif ar.stopping:
            head = "⏹ Останавливаю…"
        elif ar.background and ar.answers_sent:
            head = f"⏳ Фоновые задачи: {ar.background} · {elapsed}"
        else:
            steps = f" · {plural(ar.steps, 'действие', 'действия', 'действий')}" if ar.steps >= 2 else ""
            head = f"⚙️ Работаю · {elapsed}{steps}"
        lines = [head]
        if ar.activity and not ar.waiting:
            lines.append(esc(clip(ar.activity, 46)))
        return "\n".join(lines)

    async def run_started(self, s: D.Session, turn: D.Turn, ar: ActiveRun) -> None:
        text = self._status_text(s, ar)
        if not s.started and s.origin == "telegram":
            text = f"🆕 Новая сессия · папка: {esc(folder_label(s.cwd))}\n" + text
        if s.claude_session_id in live_sessions():
            text += "\n⚠️ Открыта и в VS Code — не пишите в неё из двух мест сразу."
        buttons = [[btn("⏹ Остановить", f"stop:{s.id}")]]
        if ar.status_msg_id:
            try:
                await self.tg.edit_message(self.chat_id, ar.status_msg_id, text, buttons=buttons)
            except TelegramError:
                ar.status_msg_id = None
        if not ar.status_msg_id:
            msg = await self._send_topic(s, text, reply_to=turn.message_id, silent=True, buttons=buttons)
            if msg:
                ar.status_msg_id = msg["message_id"]
                self.db.update_turn(turn.id, status_msg_id=ar.status_msg_id)
        ar.last_render, ar.last_edit = text, time.time()

    async def _progress_loop(self) -> None:
        while not self.stopping.is_set():
            await asyncio.sleep(5)
            if len(self.m.active) < self.cfg.max_concurrent and self.db.turns_with_status("queued"):
                self.m.schedule()
            sig = tuple(sorted((k, d.get("status")) for k, d in live_sessions().items()))
            if sig != self._live_sig:
                self._live_sig = sig
                self.dashboard_changed()
            try:
                await self._cleanup_ephemeral()
                await self._janitor()
                if self.voice:
                    await self.voice.reap_idle()
            except Exception:  # noqa: BLE001
                log.exception("cleanup failed")
            for ar in list(self.m.active.values()):
                if not ar.status_msg_id:
                    continue
                s = self.db.get_session(ar.session_id)
                text = self._status_text(s, ar)
                if text == ar.last_render or time.time() - ar.last_edit < PROGRESS_EDIT_EVERY_S:
                    continue
                try:
                    if await self.tg.edit_message(self.chat_id, ar.status_msg_id, text, droppable=True,
                                                  buttons=[[btn("⏹ Остановить", f"stop:{s.id}")]]):
                        ar.last_render, ar.last_edit = text, time.time()
                except (TelegramError, ConnectionError) as e:
                    log.debug("progress edit failed: %s", e)

    async def deliver_answer(self, s: D.Session, turn: D.Turn, text: str, ar: ActiveRun) -> None:
        text = text or "✅ Готово."
        if not ar.background:
            text += f"\n\n_⏱ {precise_duration(time.time() - ar.started_at)}_"
        chunks = html_chunks(text)
        if len(chunks) > MAX_CHUNKS:
            await self._send_topic(s, chunks[0] + "\n\n<i>…ответ длинный — полностью в файле ниже.</i>",
                                   reply_to=turn.message_id)
            try:
                await self.tg.send_document(self.chat_id, f"answer-{turn.id}.md", text.encode(), thread_id=s.topic_id)
            except TelegramError:
                log.exception("sending answer file failed")
            return
        for i, chunk in enumerate(chunks):
            await self._send_topic(s, chunk, reply_to=turn.message_id if i == 0 else None)

    async def run_finished(self, s: D.Session, turn: D.Turn, outcome: TurnOutcome, ar: ActiveRun) -> None:
        if s.control_msg_id and self.db.count_turns(s.id) <= 1:
            await self.show_panel(s)
        if outcome.status == "done":
            if ar.status_msg_id and not await self.tg.delete_message(self.chat_id, ar.status_msg_id):
                await self._safe_edit(ar.status_msg_id, "✅ Готово")
        elif outcome.status == "stopped":
            text = "⏹ Остановлено.\nИстория сохранена — можно писать дальше."
            if ar.status_msg_id:
                await self._safe_edit(ar.status_msg_id, text)
                self.ephemeral(ar.status_msg_id, SHORT_TTL)
            else:
                await self.notice(s.topic_id, text)
        else:
            if ar.status_msg_id:
                await self.tg.delete_message(self.chat_id, ar.status_msg_id)
            await self._send_topic(
                s, self.error_text(s, outcome.error),
                reply_to=turn.message_id,
                buttons=[[btn("🔁 Повторить", f"retry:{turn.id}"), btn("🔎 Подробнее", f"det:{turn.id}")]])

    @staticmethod
    def error_text(s: D.Session, error: str | None) -> str:
        error = error or "Ошибка."
        hint = ("Повторите после сброса лимита — время сброса есть на дашборде (📈)." if "лимит" in error.lower()
                else "Нажмите «Повторить» — обычно помогает. Если ошибка повторится, «Подробнее» покажет детали.")
        return f"🔴 <b>Не получилось выполнить запрос</b>\n{esc(s.title)}\n\n{esc(error)} {hint}"

    async def run_interrupted(self, s: D.Session, turn: D.Turn) -> None:
        if turn.status_msg_id:
            await self.tg.delete_message(self.chat_id, turn.status_msg_id)
        await self._send_topic(
            s, INTERRUPTED_TEXT,
            reply_to=turn.message_id,
            buttons=[[btn("▶️ Продолжить с места остановки", f"cont:{s.id}")],
                     [btn("🔁 Выполнить запрос заново", f"retry:{turn.id}")]])

    async def _safe_edit(self, message_id: int, text: str, buttons: list | None = None) -> None:
        try:
            await self.tg.edit_message(self.chat_id, message_id, text, buttons=buttons)
        except (TelegramError, ConnectionError):
            pass

    async def rename_topic(self, s: D.Session, title: str) -> None:
        await self.tg.rename_topic(self.chat_id, s.topic_id, title)
        self.db.set_topic_name(self.chat_id, s.topic_id, title)

    # ---- permission requests / questions ---------------------------------------------------
    def _describe_tool(self, name: str, inp: dict) -> str:
        if name == "Bash":
            out = f"<pre>{esc(clip_block(inp.get('command', ''), 1500))}</pre>"
            if inp.get("description"):
                out += f"\n<i>{esc(inp['description'])}</i>"
            return out
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = Path(inp.get("file_path") or inp.get("notebook_path") or "")
            out = f"📄 <b>{esc(path.name)}</b>\n<i>в папке {esc(short_path(str(path.parent)))}</i>"
            body = inp.get("content") or inp.get("new_string") or ""
            if body:
                out += f"\n<blockquote expandable>{esc(clip_block(body, 700))}</blockquote>"
            return out
        if name == "WebFetch":
            return f"🌐 {esc(inp.get('url', ''))}"
        if name == "WebSearch":
            return f"🔎 {esc(inp.get('query', ''))}"
        if name == "ExitPlanMode":
            return f"<blockquote expandable>{esc(clip_block(inp.get('plan', ''), 2500))}</blockquote>"
        return f"<pre>{esc(clip_block(json.dumps(inp, ensure_ascii=False, indent=1), 1200))}</pre>"

    def _rule_label(self, ctx: Any) -> str:
        """Button text for 'allow for this session' that names what exactly stops being asked."""
        for sug in (ctx.suggestions if ctx else []):
            if sug.type == "addRules" and sug.rules:
                r = sug.rules[0]
                content = (r.rule_content or "").removesuffix(":*").removesuffix(" *").strip()
                if content.startswith("domain:"):
                    return f"Не спрашивать про сайт {clip(content[7:], 24)}"
                if content:
                    return f"Не спрашивать про «{clip(content, 22)}» здесь"
                return f"Всегда разрешать: {tool_action(r.tool_name)}"
            if sug.type == "setMode" and sug.mode == "acceptEdits":
                return "Разрешать правку файлов в этой теме"
            if sug.type == "addDirectories" and sug.directories:
                return f"Открыть доступ к папке «{Path(sug.directories[0]).name}»"
        return "Разрешать такое в этой теме"

    async def permission_request(self, s: D.Session, req: PendingRequest) -> None:
        req.text = f"🔐 <b>Claude хочет {esc(tool_action(req.tool_name))}</b>\n" + self._describe_tool(req.tool_name, req.input)
        text = req.text + "\n\n↩️ <i>Ответьте на это сообщение, чтобы отказать с пояснением.</i>"
        buttons = [[btn("✅ Разрешить", f"perm:{req.id}:o"), btn("❌ Запретить", f"perm:{req.id}:d")]]
        if req.ctx and req.ctx.suggestions:
            buttons.append([btn("♾ " + self._rule_label(req.ctx), f"perm:{req.id}:s")])
        msg = await self._send_topic(s, text, buttons=buttons)
        req.message_id = msg["message_id"] if msg else None

    async def ask_question(self, s: D.Session, req: PendingRequest) -> None:
        q = req.question
        multi = bool(q.get("multiSelect"))
        lines = ["❓ <b>Вопрос от Claude</b>" + (f" · {esc(q['header'])}" if q.get("header") else ""),
                 esc(q.get("question", ""))]
        for o in q.get("options", []):
            if o.get("description"):
                lines.append(f"• <b>{esc(o['label'])}</b> — {esc(o['description'])}")
        req.text = "\n".join(lines)
        lines.append("\n<i>" + ("Отметьте варианты и нажмите «Готово»" if multi else "Выберите вариант")
                     + " или ответьте текстом.</i>")
        msg = await self._send_topic(s, "\n".join(lines), buttons=self._ask_buttons(req))
        req.message_id = msg["message_id"] if msg else None

    def _ask_buttons(self, req: PendingRequest) -> list[list[dict]]:
        q = req.question
        multi = bool(q.get("multiSelect"))
        rows = []
        for i, o in enumerate(q.get("options", [])[:10]):
            mark = ("☑️ " if i in req.selected else "⬜ ") if multi else ""
            rows.append([btn(clip(mark + o.get("label", str(i)), 60), f"ask:{req.id}:{req.q_index}:{i}")])
        if multi:
            rows.append([btn("✅ Готово", f"ask:{req.id}:{req.q_index}:done")])
        return rows

    async def _close(self, message_id: int | None, text: str, verdict: str) -> None:
        """Write the decision into the request message itself and remove its buttons (no extra message)."""
        if not message_id:
            return
        try:
            await self.tg.edit_message(self.chat_id, message_id, f"{text}\n\n{verdict}")
        except (TelegramError, ConnectionError):
            try:
                await self.tg.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id,
                                   reply_markup={"inline_keyboard": []})
            except TelegramError:
                pass

    async def _close_request_message(self, req: PendingRequest, verdict: str) -> None:
        await self._close(req.message_id, req.text, verdict)

    async def close_request(self, req: PendingRequest, verdict: str) -> None:
        await self._close_request_message(req, verdict)

    async def request_expired(self, req: PendingRequest) -> None:
        await self._close_request_message(req, "⌛ <b>Время вышло</b> — действие не выполнено.")

    # =========================================================================== callbacks
    async def _on_callback(self, cq: dict) -> None:
        if not self.is_owner(cq.get("from")):
            log.warning("ignored button press from unauthorized user id=%s", (cq.get("from") or {}).get("id"))
            await self.tg.answer_callback(cq["id"], "⛔ Нет доступа")
            return
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        mid = msg.get("message_id")
        action, _, rest = data.partition(":")
        # buttons of panels / messages posted by older versions
        legacy = {"info": "pv:{}:info", "modelm": "pv:{}:model", "fork": "pv:{}:fork", "arch": "pv:{}:arch",
                  "projs": "pv:{}:folder", "ren": "pa:{}:ren", "import": "dash:import:0", "projg": "dash:proj",
                  "diag": "dash:diag", "impp": "dash:import:{}"}
        if action in legacy:
            action, _, rest = legacy[action].format(rest).partition(":")
        elif action == "card":
            what, _, sid = rest.partition(":")
            action, rest = "pv", f"{sid}:{'help' if what == 'help' else 'main'}"
        elif action == "help":
            action, rest = "dash", "helpall" if rest == "all" else "help"
        answer = await self._handle_button(action, rest, msg, mid)
        await self.tg.answer_callback(cq["id"], answer)

    async def _handle_button(self, action: str, rest: str, msg: dict, mid: int | None) -> str | None:
        if action == "dash":
            view = rest or "main"
            if view in ("main", "limits") and time.time() - self._usage_at > 60:
                self._spawn(self.refresh_usage())
            if mid == int(self.db.kv_get("dashboard_msg_id", "0")):
                await self.refresh_dashboard(view=view)
            else:  # an old dashboard message: remove it, the live one is the source of truth
                await self.tg.delete_message(self.chat_id, mid)
                await self.refresh_dashboard(view=view, repost=True)
            return None
        if action == "new":
            await self.new_session_topic("")
            return "Создаю новую тему…"
        if action == "sweep":
            self._spawn(self.sweep_general())
            return "Чищу General — займёт пару минут"
        if action == "perm":
            rid, _, choice = rest.partition(":")
            decision = {"o": "once", "s": "session", "d": "deny"}.get(choice, "deny")
            pending = self.m.requests.get(rid)
            req_mid, shown = (pending.message_id, pending.text) if pending else (None, "")
            req = self.m.resolve(rid, decision)
            if req is None:
                await self._safe_edit_markup(msg)
                return "Запрос уже неактуален"
            verdict = {"once": "✅ <b>Разрешено</b>", "deny": "❌ <b>Запрещено</b>",
                       "session": f"♾ <b>Разрешено</b>, дальше без вопросов: {esc(grant_label_from(req))}"}[decision]
            await self._close(req_mid, shown, f"{verdict} · {clock()}")
            return {"once": "Разрешено", "session": "Разрешено", "deny": "Запрещено"}[decision]
        if action == "ask":
            return await self._on_ask_button(rest, msg)
        if action == "imp":
            answer = await self.import_session(rest)
            await self.refresh_dashboard(view="main")
            return answer
        if action == "proj":   # default folder for new sessions (dashboard screen)
            _, _, key = rest.partition(":")
            path = next((p for p in self.known_projects() if self._path_key(p) == key), None)
            if path:
                self.db.kv_set("default_cwd", path)
                log.info("default project changed")
            await self.refresh_dashboard(view="all")
            return f"Папка для новых сессий: {folder_label(path)}" if path else "Папка не найдена"
        if action in ("pv", "pa", "stop", "unarch", "model", "retry", "cont", "det"):
            return await self._session_button(action, rest, msg, mid)
        return None

    async def _session_button(self, action: str, rest: str, msg: dict, mid: int | None) -> str | None:
        """Buttons that act on one session (panel screens and actions, stop, retry, details...)."""
        if action in ("retry", "cont", "det"):
            t = self.db.get_turn(int(rest)) if action != "cont" else None
            s = self.db.get_session(t.session_id if t else int(rest))
        else:
            s = self.db.get_session(int(rest.partition(":")[0]))
        if s is None:
            return "Сессия не найдена"
        if action == "pv":
            await self.show_panel(s, rest.partition(":")[2] or "main", mid)
            return None
        if action == "stop":
            running, cancelled = await self.m.stop(s.id)
            await self._drop_queue_notices(cancelled)
            return "Останавливаю…" if running else "Сейчас ничего не выполняется"
        if action == "unarch":   # «📤 Вернуть» under the archive notice
            await self.unarchive(s, message_id=mid)
            return "Сессия снова на дашборде"
        if action == "model":    # model picker sent by older versions
            name = rest.partition(":")[2]
            self.db.update_session(s.id, model=None if name == "-" else name)
            await self.show_panel(s, "main", mid)
            return f"Модель: {name.capitalize() if name != '-' else 'как в Claude Code'}"
        if action == "det":
            t = self.db.get_turn(int(rest))
            details = t.details or t.error or "нет данных"
            await self._safe_edit(mid, self.error_text(s, t.error) + "\n\n<b>Технические детали</b>\n"
                                  f"<blockquote expandable>{esc(clip_block(details, 3000))}</blockquote>",
                                  [[btn("🔁 Повторить", f"retry:{t.id}")]])
            return None
        if action in ("retry", "cont"):
            old = self.db.get_turn(int(rest)) if action == "retry" else None
            turn = self.m.retry(old.id) if action == "retry" else self.m.continue_session(s.id)
            base = INTERRUPTED_TEXT if (old is None or old.status == "interrupted") else self.error_text(s, old.error)
            await self._safe_edit(mid, base + ("\n\n▶️ Продолжаю с места остановки" if action == "cont"
                                               else "\n\n🔁 Выполняю запрос заново"))
            if turn:
                await self._queue_notice(self.db.get_session(s.id), turn)
            return "Поставлено в работу" if turn else "Не получилось"
        # pa: panel actions
        what, _, arg = rest.partition(":")[2].partition(":")
        if what == "model":
            self.db.update_session(s.id, model=None if arg == "-" else arg)
            await self.show_panel(s, "main", mid)
            return f"Модель: {arg.capitalize() if arg != '-' else 'как в Claude Code'}"
        if what == "folder":
            path = next((p for p in self.known_projects() if self._path_key(p) == arg), None)
            if s.started or s.id in self.m.active or path is None:
                await self.show_panel(s, "main", mid)
                return "У начатой сессии папку сменить нельзя"
            self.db.update_session(s.id, cwd=path)
            await self.show_panel(s, "main", mid)
            return f"Папка: {folder_label(path)}"
        if what == "fork":
            await self.show_panel(s, "main", mid)
            await self.fork(s, s.topic_id, None)
            return "Ветка создана — новая тема в списке"
        if what == "arch":
            done = await self.archive(s, announce=False)
            await self.show_panel(s, "main", mid)
            return "Убрано в архив" if done else "Сначала остановите задачу"
        if what == "unarch":
            await self.unarchive(s, quiet=True)
            await self.show_panel(s, "main", mid)
            return "Сессия снова на дашборде"
        if what == "ren":
            await self.ask_new_title(s, mid)
            return "Напишите новое название сообщением"
        if what == "rencancel":
            self.db.conn.execute("DELETE FROM kv WHERE key=?", (f"rename_wait:{s.id}",))
            await self.show_panel(s, "main", mid)
            return "Отменено"
        return None

    async def _safe_edit_markup(self, msg: dict) -> None:
        try:
            await self.tg.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=msg["message_id"],
                               reply_markup={"inline_keyboard": []})
        except (TelegramError, KeyError):
            pass

    async def _on_ask_button(self, rest: str, msg: dict) -> str:
        rid, qi, choice = (rest.split(":") + ["", "", ""])[:3]
        req = self.m.requests.get(rid)
        if req is None or str(req.q_index) != qi or req.future is None or req.future.done():
            await self._safe_edit_markup(msg)
            return "Вопрос уже неактуален"
        options = req.question.get("options", [])
        if req.question.get("multiSelect"):
            if choice == "done":
                labels = [options[i]["label"] for i in sorted(req.selected)]
                mid, shown = req.message_id, req.text
                self.m.resolve(rid, "answer", ", ".join(labels) or "(ничего не выбрано)")
                await self._close(mid, shown, "💬 Ваш ответ: <b>" + esc(", ".join(labels) or "ничего") + "</b>")
                return "Ответ отправлен"
            i = int(choice)
            req.selected ^= {i}
            try:
                await self.tg.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=msg["message_id"],
                                   reply_markup={"inline_keyboard": self._ask_buttons(req)})
            except TelegramError:
                pass
            return ""
        label = options[int(choice)]["label"]
        mid, shown = req.message_id, req.text
        self.m.resolve(rid, "answer", label)
        await self._close(mid, shown, f"💬 Ваш ответ: <b>{esc(label)}</b>")
        return "Ответ отправлен"

    # =========================================================================== projects
    def known_projects(self) -> list[str]:
        dirs = list(self.cfg.project_dirs)
        for s in self.db.sessions(include_archived=True):
            dirs.append(s.cwd)
        seen, out = set(), []
        for d in dirs:
            if d not in seen and Path(d).is_dir():
                seen.add(d)
                out.append(d)
        return out[:12]

    @staticmethod
    def _path_key(path: str) -> str:
        return hashlib.sha1(path.encode()).hexdigest()[:10]

    # =========================================================================== import
    async def import_list(self, page: int) -> tuple[str, list[list[dict]]]:
        infos = await asyncio.to_thread(list_sessions, None, 80)
        known = {s.claude_session_id for s in self.db.sessions(include_archived=True)}
        live = live_sessions()
        cands = [i for i in infos if i.session_id not in known and (i.file_size or 0) > 2000]
        per = 8
        chunk = cands[page * per:(page + 1) * per]
        if not chunk:
            return "📥 Все сессии Claude на сервере уже подключены к Telegram.", []
        rows = []
        for i in chunk:
            name = clip(i.custom_title or i.summary or i.first_prompt or "без названия", 26)
            parts = [("💻 " if i.session_id in live else "") + name]
            if i.session_id in live:
                parts.append("работает" if live[i.session_id].get("status") == "busy" else "открыта")
            parts.append(short_when(i.last_modified / 1000))
            if i.cwd and Path(i.cwd) != Path.home():
                parts.append(folder_label(i.cwd))
            rows.append([btn(" · ".join(parts), f"imp:{i.session_id}")])
        nav = []
        if page > 0:
            nav.append(btn("← Предыдущие", f"dash:import:{page - 1}"))
        if len(cands) > (page + 1) * per:
            nav.append(btn("Ещё →", f"dash:import:{page + 1}"))
        text = "📥 <b>Подключить сессию из VS Code</b>\nВыберите — для неё появится тема здесь."
        return text, rows + ([nav] if nav else [])

    async def import_session(self, claude_session_id: str) -> str:
        """Link an existing Claude Code session to a new topic. Returns a short result for the toast."""
        if self.db.session_by_claude_id(claude_session_id):
            return "Эта сессия уже подключена"
        info = await asyncio.to_thread(get_session_info, claude_session_id)
        if info is None:
            return "Не нашёл эту сессию на сервере"
        title = clip(info.custom_title or info.summary or "Импорт", 100)
        try:
            topic_id = await self.tg.create_topic(self.chat_id, title)
        except TelegramError as e:
            await self.flash(self._rights_hint(e))
            return "Не получилось создать тему"
        cwd = info.cwd or self.m.default_cwd()
        s = self.m.create_session(self.chat_id, topic_id, title, cwd=cwd, claude_session_id=claude_session_id,
                                  started=True, origin="import")
        last = await asyncio.to_thread(_last_assistant_text, claude_session_id, cwd)
        text = (f"📥 <b>Подключена сессия Claude Code</b>\n«{esc(title)}»\n📁 <code>{esc(short_path(cwd))}</code>\n")
        if last:
            text += f"\n<b>Последний ответ Claude:</b>\n<blockquote expandable>{esc(clip_block(last, 1500))}</blockquote>\n"
        if claude_session_id in live_sessions():
            text += "\n⚠️ Эта сессия сейчас открыта в VS Code — не пишите в неё из двух мест одновременно.\n"
        text += "\nПишите сюда, чтобы продолжить разговор."
        await self._send_topic(s, text)
        await self.ensure_control_card(self.db.get_session(s.id))
        return f"Подключена: {clip(title, 40)} — появилась новая тема"

    # =========================================================================== diagnostics
    async def diagnostics(self) -> str:
        import claude_agent_sdk
        all_s = self.db.sessions(include_archived=True)
        problems = []
        if not self.online:
            problems.append("нет связи с Telegram")
        recent = [t for t in self.db.recent_errors() if (t.finished_at or 0) > time.time() - 3600]
        if recent:
            problems.append(f"ошибок за час: {len(recent)}")
        lim = self._limit_windows()
        if lim and lim[1]:
            problems.append("лимит Claude исчерпан")
        lines = ["🛠 <b>Диагностика Claude Control</b>",
                 "✅ Всё работает" if not problems else "⚠️ Есть проблемы: " + ", ".join(problems), "",
                 f"Сервис: v{__version__} · работает {duration(time.time() - self.m.started_at)} · PID {os.getpid()}",
                 f"Claude Code: {esc(self.claude_version)} (<code>{esc(self.cfg.claude_cli)}</code>) · SDK {claude_agent_sdk.__version__}",
                 f"Telegram: {'✅ связь есть' if self.online else '⚠️ нет связи'} · бот @{esc(self.me.get('username', '?'))}",
                 f"Параллельность: {len(self.m.active)}/{self.cfg.max_concurrent} · очередь: "
                 f"{len(self.db.turns_with_status('queued'))} сообщ.",
                 f"Сессий: {len(all_s)} (в архиве: {sum(s.archived for s in all_s)})",
                 f"База: <code>{esc(str(self.cfg.db_path))}</code> ({self.cfg.db_path.stat().st_size // 1024} КБ)",
                 f"Папка по умолчанию: <code>{esc(self.m.default_cwd())}</code> · часовой пояс: {tz_name()}",
                 f"Без подтверждения: {esc(', '.join(self.cfg.auto_allow_tools) or 'ничего')} + чтение файлов",
                 "Голос: " + ("выключен" if not self.voice else "GigaAM v3 — " + (
                     "модель загружена" if self.voice.proc and self.voice.proc.returncode is None else "загрузится по первому голосовому"))]
        if self.m.active:
            lines.append("\n<b>Активные процессы</b>")
            for ar in self.m.active.values():
                s = self.db.get_session(ar.session_id)
                lines.append(f"• #{s.id} {esc(clip(s.title, 30))} — pid {s.current_pid or '?'}, "
                             f"{duration(time.time() - ar.started_at)}, claude <code>{s.claude_session_id[:8]}</code>")
        errs = self.db.recent_errors()
        if errs:
            lines.append("\n<b>Последние ошибки</b>")
            for t in errs:
                s = self.db.get_session(t.session_id)
                lines.append(f"• {when(t.finished_at or t.created_at)} #{s.id} {esc(clip(s.title, 25))}: {esc(t.error or '')}")
        lines += ["", "<b>📈 Лимиты Claude</b>"] + self.limits_lines()
        lines.append("\nЛоги: <code>journalctl --user -u claude-control -n 200</code>")
        return "\n".join(lines)


def html_chunks(markdown: str) -> list[str]:
    """Markdown -> Telegram HTML messages, each within Telegram's 4096-char limit."""
    limit = 3500
    while True:
        chunks = [md_to_html(c) for c in split_markdown(markdown, limit)]
        if all(len(c) <= TG_LIMIT for c in chunks) or limit < 500:
            return [c[:TG_LIMIT] for c in chunks if c.strip()] or ["✅"]
        limit //= 2


def grant_label_from(req: PendingRequest) -> str:
    from .claude import grants_from_suggestions
    grants = grants_from_suggestions(req.ctx) if req.ctx else []
    return ", ".join(grant_label(g) for g in grants) or tool_action(req.tool_name)


def clip_block(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _last_assistant_text(claude_session_id: str, cwd: str) -> str:
    try:
        msgs = get_session_messages(claude_session_id, directory=cwd)
    except Exception:  # noqa: BLE001
        return ""
    for sm in reversed(msgs):
        if sm.type != "assistant":
            continue
        content = (sm.message or {}).get("content")
        if isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                return text.strip()
    return ""
