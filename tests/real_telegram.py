"""Smoke test against the REAL Telegram group (outbound is real, inbound owner messages are injected).

Creates a temporary "🧪" topic, runs real Claude turns through it, checks what Telegram accepted,
then deletes the topic. Uses its own temporary DB, so the production service is not affected.

    env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 .venv/bin/python -m tests.real_telegram
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import delete_session

from claude_control import db as D
from claude_control.bot import Bot
from claude_control.config import load_config
from claude_control.manager import Manager
from claude_control.telegram import TelegramAPI
from tests.acceptance import WORK

sent: list[dict] = []


class RecordingTelegram(TelegramAPI):
    async def call(self, method, **params):
        res = await super().call(method, **params)
        if method == "sendMessage" and isinstance(res, dict):
            sent.append({"id": res["message_id"], "thread": params.get("message_thread_id"),
                         "text": params.get("text", ""), "buttons": (params.get("reply_markup") or {})})
        return res


async def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    cfg = load_config()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="cc-real-"))
    cfg.default_cwd, cfg.model = str(WORK), "haiku"
    owner = next(iter(cfg.owner_ids))
    db = D.Registry(cfg.db_path)
    tg = RecordingTelegram(cfg.bot_token)
    m = Manager(cfg, db)
    bot = Bot(cfg, db, tg, m)
    bot.me = await tg.call("getMe")
    chat = bot.chat_id
    ids = iter(range(900000, 999999))

    async def inject(text: str, topic: int, reply_to: int | None = None) -> None:
        msg = {"message_id": next(ids), "from": {"id": owner, "is_bot": False}, "chat": {"id": chat, "type": "supergroup"},
               "message_thread_id": topic, "is_topic_message": True, "text": text, "date": int(time.time())}
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to}
        await bot._dispatch({"update_id": next(ids), "message": msg})

    async def wait_idle(topic: int) -> D.Turn:
        for _ in range(240):
            s = db.session_by_topic(chat, topic)
            if s and s.id not in m.active and not db.queued_count(s.id):
                t = db.last_turn(s.id)
                if t and t.status not in ("queued", "running"):
                    return t
            await asyncio.sleep(0.5)
        raise TimeoutError

    progress = asyncio.create_task(bot._progress_loop())
    topic = await tg.create_topic(chat, "🧪 Проверка Claude Control (удалится сама)")
    db.set_topic_name(chat, topic, "🧪 Проверка Claude Control")
    ok = True
    try:
        await inject("Запомни слово ЛИМОН. Ответь коротко по-русски: **готово**, и добавь маленькую таблицу "
                     "из 2 строк (колонки: шаг, статус) и строку кода `echo hi`.", topic)
        t1 = await wait_idle(topic)
        s = db.session_by_topic(chat, topic)
        await inject("Какое слово я просил запомнить? Ответь одним словом.", topic)
        t2 = await wait_idle(topic)
        s2 = db.session_by_topic(chat, topic)
        answers = [x["text"] for x in sent if x["thread"] == topic and not x["text"].startswith(("⚙️", "🆕"))]
        print("turn1:", t1.status, "| turn2:", t2.status, "| same session:", s.claude_session_id == s2.claude_session_id)
        print("answer1 (HTML accepted by Telegram):", answers[0][:300].replace("\n", " ⏎ "))
        print("answer2:", answers[-1][:80])
        ok &= t1.status == t2.status == "done" and "ЛИМОН" in answers[-1].upper() and s.claude_session_id == s2.claude_session_id

        await inject("Используй Bash и выполни: touch real-tg-test.txt . Потом ответь СДЕЛАНО или ОТКАЗ.", topic)
        for _ in range(120):
            if db.get_session(s.id).status == D.WAITING_APPROVAL:
                break
            await asyncio.sleep(0.5)
        req = [x for x in sent if x["thread"] == topic and x["text"].startswith("🔐")]
        print("permission request shown with buttons:", bool(req and req[-1]["buttons"]))
        if not req:
            print("messages in topic:", [x["text"][:120] for x in sent if x["thread"] == topic][-4:])
            raise SystemExit("no permission request")
        await inject("Нет, не создавай файлы", topic, reply_to=req[-1]["id"])
        t3 = await wait_idle(topic)
        print("reply-deny ->", t3.status, "| file created:", (WORK / "real-tg-test.txt").exists())
        ok &= bool(req) and not (WORK / "real-tg-test.txt").exists()

        text, buttons = bot.dashboard("main")
        msg = await tg.send_message(chat, text, thread_id=topic, buttons=buttons)
        print("dashboard rendered by Telegram: message", msg["message_id"])
        print("\nREAL TELEGRAM SMOKE TEST:", "PASS" if ok else "FAIL")
        print("claude session:", s.claude_session_id)
    finally:
        progress.cancel()
        await asyncio.sleep(20 if ok else 0)  # leave it visible for a moment
        await tg.call("deleteForumTopic", chat_id=chat, message_thread_id=topic)
        for sess in db.sessions(include_archived=True):
            delete_session(sess.claude_session_id, directory=sess.cwd)
        await tg.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
