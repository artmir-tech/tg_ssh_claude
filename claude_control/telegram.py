"""Minimal Telegram Bot API client over httpx.

Deliberately small instead of a framework: long polling, a handful of methods, and two
safety features that matter here:
  * the bot token is never allowed into logs or exception text (it is part of every URL);
  * a per-chat send budget (Telegram allows ~20 messages/minute in a group) with a
    "droppable" lane for progress edits so they never delay real answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("cc.tg")

# Every request URL contains the token; httpx logs URLs at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"{method}: {code} {description}")
        self.method, self.code, self.description, self.retry_after = method, code, description, retry_after


class ChatBudget:
    """Sliding one-minute window of API writes per chat."""

    def __init__(self, per_minute: int = 19):
        self.per_minute = per_minute
        self.stamps: dict[int, deque[float]] = {}
        self.blocked_until: dict[int, float] = {}

    def _window(self, chat_id: int) -> deque[float]:
        q = self.stamps.setdefault(chat_id, deque())
        cutoff = time.monotonic() - 60
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def try_take(self, chat_id: int, reserve: int = 0) -> bool:
        if time.monotonic() < self.blocked_until.get(chat_id, 0):
            return False
        q = self._window(chat_id)
        if len(q) >= self.per_minute - reserve:
            return False
        q.append(time.monotonic())
        return True

    async def take(self, chat_id: int) -> None:
        while not self.try_take(chat_id):
            await asyncio.sleep(0.5)

    def block(self, chat_id: int, seconds: float) -> None:
        self.blocked_until[chat_id] = time.monotonic() + seconds


class TelegramAPI:
    # Methods that post/edit content in a chat and therefore count against its budget.
    WRITES = {"sendMessage", "editMessageText", "sendDocument", "sendMediaGroup", "editMessageReplyMarkup",
              "createForumTopic", "editForumTopic", "closeForumTopic", "reopenForumTopic"}

    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self._token = token
        self._base = f"{base}/bot{token}"
        self._file_base = f"{base}/file/bot{token}"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=15.0))
        self.budget = ChatBudget()

    def redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, *, droppable: bool = False, _files: dict | None = None,
                   **params: Any) -> Any:
        """Call a Bot API method. droppable=True: skip (return None) instead of waiting for budget."""
        params = {k: v for k, v in params.items() if v is not None}
        chat_id = params.get("chat_id")
        if method in self.WRITES and isinstance(chat_id, int):
            if droppable:
                if not self.budget.try_take(chat_id, reserve=6):
                    return None
            else:
                await self.budget.take(chat_id)
        for attempt in range(5):
            try:
                if _files:
                    for _, fh in _files.values():   # a retry must re-read streamed files from the start
                        if hasattr(fh, "seek"):
                            fh.seek(0)
                    data = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in params.items()}
                    resp = await self._http.post(f"{self._base}/{method}", data=data, files=_files)
                else:
                    resp = await self._http.post(f"{self._base}/{method}", json=params)
                body = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                if method == "getUpdates" or attempt == 4:
                    raise ConnectionError(self.redact(f"{method}: {type(e).__name__}: {e}")) from None
                await asyncio.sleep(1 + attempt * 2)
                continue
            if body.get("ok"):
                return body.get("result")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            err = TelegramError(method, body.get("error_code", 0), body.get("description", ""), retry_after)
            if err.code == 429 and retry_after and not droppable and attempt < 4:
                log.warning("rate limited on %s, sleeping %ss", method, retry_after)
                if isinstance(chat_id, int):
                    self.budget.block(chat_id, retry_after)
                await asyncio.sleep(retry_after + 0.5)
                continue
            raise err
        raise TelegramError(method, 0, "retries exhausted")

    # ---- convenience wrappers ------------------------------------------------------------
    async def get_updates(self, offset: int, timeout: int = 50) -> list[dict]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query", "my_chat_member"])

    async def send_message(self, chat_id: int, text: str, *, thread_id: int | None = None,
                           reply_to: int | None = None, buttons: list[list[dict]] | None = None,
                           html: bool = True, silent: bool = False, preview: bool = False) -> dict:
        params: dict[str, Any] = dict(chat_id=chat_id, text=text, message_thread_id=thread_id,
                                      disable_notification=silent or None)
        if html:
            params["parse_mode"] = "HTML"
        if not preview:
            params["link_preview_options"] = {"is_disabled": True}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            return await self.call("sendMessage", **params)
        except TelegramError as e:
            if html and "parse entities" in e.description:
                # Our Markdown->HTML conversion produced something Telegram rejects: send raw text.
                params.pop("parse_mode")
                params["text"] = _strip_tags(text)
                return await self.call("sendMessage", **params)
            raise

    async def edit_message(self, chat_id: int, message_id: int, text: str, *,
                           buttons: list[list[dict]] | None = None, droppable: bool = False) -> bool:
        try:
            res = await self.call("editMessageText", droppable=droppable, chat_id=chat_id, message_id=message_id,
                                  text=text, parse_mode="HTML", link_preview_options={"is_disabled": True},
                                  reply_markup={"inline_keyboard": buttons or []})
            return res is not None
        except TelegramError as e:
            if "not modified" in e.description:
                return True
            if "parse entities" in e.description:
                await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=_strip_tags(text),
                                reply_markup={"inline_keyboard": buttons or []})
                return True
            raise

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            return bool(await self.call("deleteMessage", chat_id=chat_id, message_id=message_id))
        except TelegramError:
            return False

    async def answer_callback(self, callback_id: str, text: str | None = None, alert: bool = False) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert or None)
        except TelegramError as e:
            log.debug("answerCallbackQuery failed: %s", e.description)

    async def create_topic(self, chat_id: int, name: str) -> int:
        res = await self.call("createForumTopic", chat_id=chat_id, name=name[:128])
        return res["message_thread_id"]

    async def rename_topic(self, chat_id: int, topic_id: int, name: str) -> None:
        try:
            await self.call("editForumTopic", chat_id=chat_id, message_thread_id=topic_id, name=name[:128])
        except TelegramError as e:
            if "not modified" not in e.description and "TOPIC_NOT_MODIFIED" not in e.description:
                raise

    async def send_document(self, chat_id: int, filename: str, content: bytes, *, thread_id: int | None = None,
                            caption: str | None = None, reply_to: int | None = None) -> dict:
        params: dict[str, Any] = dict(chat_id=chat_id, message_thread_id=thread_id, caption=caption,
                                      parse_mode="HTML" if caption else None)
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return await self.call("sendDocument", _files={"document": (filename, content)}, **params)

    async def send_files(self, chat_id: int, paths: list[Path], *, thread_id: int | None = None,
                         caption: str | None = None, reply_to: int | None = None) -> None:
        """One file -> sendDocument; 2-10 -> one album (sendMediaGroup). Files are streamed from disk,
        sent as documents so images keep their quality. Caption (HTML) goes under the last file."""
        handles = [open(p, "rb") for p in paths]
        try:
            params: dict[str, Any] = dict(chat_id=chat_id, message_thread_id=thread_id)
            if reply_to:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if len(paths) == 1:
                await self.call("sendDocument", _files={"document": (paths[0].name, handles[0])},
                                caption=caption, parse_mode="HTML" if caption else None, **params)
                return
            media = [{"type": "document", "media": f"attach://f{i}"} for i in range(len(paths))]
            if caption:
                media[-1] |= {"caption": caption, "parse_mode": "HTML"}
            await self.call("sendMediaGroup", media=media,
                            _files={f"f{i}": (p.name, h) for i, (p, h) in enumerate(zip(paths, handles))}, **params)
        finally:
            for h in handles:
                h.close()

    async def download(self, file_id: str, dest: Path, max_bytes: int = 20 * 1024 * 1024) -> int:
        info = await self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise ValueError("file too large")
        if info.get("file_path", "").startswith("/"):   # a local Bot API server (--local) gives a path on disk
            import shutil
            shutil.copyfile(info["file_path"], dest)
            return dest.stat().st_size
        try:
            resp = await self._http.get(f"{self._file_base}/{info['file_path']}")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ConnectionError(self.redact(f"download failed: {type(e).__name__}")) from None
        dest.write_bytes(resp.content)
        return len(resp.content)


def _strip_tags(html_text: str) -> str:
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))
