"""In-memory stand-in for the Telegram Bot API, used by the acceptance tests.

It subclasses the real client and replaces only the HTTP call, so the wrappers
(send_message, edit_message, HTML fallback, ...) are exercised as in production.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path
from typing import Any

from claude_control.telegram import TelegramAPI, TelegramError, _strip_tags


class FakeTelegram(TelegramAPI):
    def __init__(self) -> None:
        super().__init__("123:FAKE-TOKEN", base="http://fake.invalid")
        self._ids = itertools.count(1000)
        self.sent: list[dict] = []        # every sendMessage / sendDocument
        self.edits: list[dict] = []
        self.deleted: list[int] = []
        self.callback_answers: list[dict] = []
        self.topics: dict[int, str] = {}
        self.calls: list[str] = []
        self.can_manage_topics = True
        self.user_msgs: dict[int, tuple[str, int]] = {}   # injected user messages: id -> (text, user id)
        self.deleted_topics: list[int] = []

    async def call(self, method: str, *, droppable: bool = False, _files: dict | None = None, **p: Any) -> Any:
        self.calls.append(method)
        if method == "getMe":
            return {"id": 1, "is_bot": True, "username": "test_bot"}
        if method in ("sendMessage", "sendDocument"):
            msg = {"message_id": next(self._ids), "method": method, "chat_id": p.get("chat_id"),
                   "thread_id": p.get("message_thread_id"), "text": p.get("text") or p.get("caption") or "",
                   "buttons": (p.get("reply_markup") or {}).get("inline_keyboard", []),
                   "reply_to": (p.get("reply_parameters") or {}).get("message_id"), "at": time.time(),
                   "silent": bool(p.get("disable_notification"))}
            self.sent.append(msg)
            return {"message_id": msg["message_id"], "chat": {"id": p.get("chat_id")}}
        if method == "editMessageText":
            self.edits.append({"message_id": p["message_id"], "text": p["text"], "at": time.time(),
                               "buttons": (p.get("reply_markup") or {}).get("inline_keyboard", [])})
            return True
        if method == "deleteMessage":
            self.deleted.append(p["message_id"])
            return True
        if method == "deleteMessages":
            self.deleted.extend(p["message_ids"])
            return True
        if method == "deleteForumTopic":
            self.topics.pop(p["message_thread_id"], None)
            self.deleted_topics.append(p["message_thread_id"])
            return True
        if method == "forwardMessage":   # like Telegram: the copy carries the plain text and the original sender
            mid = p["message_id"]
            src = next((m for m in self.sent if m["message_id"] == mid), None)
            if mid in self.deleted or (src is None and mid not in self.user_msgs):
                raise TelegramError(method, 400, "Bad Request: message to forward not found")
            text, sender = (_strip_tags(src["text"]), 1) if src else self.user_msgs[mid]
            return {"message_id": next(self._ids), "text": text,
                    "forward_origin": {"type": "user", "sender_user": {"id": sender}}}
        if method == "createForumTopic":
            if not self.can_manage_topics:
                raise TelegramError(method, 400, "Bad Request: not enough rights to create a topic")
            tid = next(self._ids)
            self.topics[tid] = p["name"]
            return {"message_thread_id": tid, "name": p["name"]}
        if method == "editForumTopic":
            self.topics[p["message_thread_id"]] = p["name"]
            return True
        if method == "answerCallbackQuery":
            self.callback_answers.append({"id": p["callback_query_id"], "text": p.get("text")})
            return True
        if method == "getFile":
            return {"file_id": p["file_id"], "file_path": "documents/x", "file_size": 42}
        return True

    async def download(self, file_id: str, dest: Path, max_bytes: int = 20 * 1024 * 1024) -> int:
        dest.write_bytes(b"hello from telegram file " + file_id.encode())
        return dest.stat().st_size

    # ---- helpers for assertions ----------------------------------------------------------
    def in_topic(self, thread_id: int | None) -> list[dict]:
        return [m for m in self.sent if m["thread_id"] == thread_id]

    def buttons_with(self, prefix: str) -> list[tuple[dict, dict]]:
        out = []
        for m in self.sent:
            for row in m["buttons"]:
                for b in row:
                    if b.get("callback_data", "").startswith(prefix):
                        out.append((m, b))
        return out
