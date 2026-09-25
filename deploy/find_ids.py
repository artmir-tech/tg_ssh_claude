#!/usr/bin/env python3
"""Show the ids to put into .env (OWNER_IDS, TELEGRAM_CHAT_ID) after you wrote /start in the group.

Run it BEFORE the service is started (a running service reads the same updates):
    python3 deploy/find_ids.py
The bot token is read from .env and never printed.
"""

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("TELEGRAM_BOT_TOKEN не найден в .env")


def summarize(updates: list[dict]) -> tuple[dict, dict]:
    """(groups, people): id -> label, from messages and membership updates."""
    groups, people = {}, {}
    for u in updates:
        m = u.get("message") or u.get("my_chat_member") or {}
        chat, user = m.get("chat") or {}, m.get("from") or {}
        if chat.get("type") == "supergroup":
            groups[chat["id"]] = f"{chat.get('title', '')}" + ("" if chat.get("is_forum") else "  (темы НЕ включены!)")
        if user and not user.get("is_bot"):
            label = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) + \
                (f" @{user['username']}" if user.get("username") else "")
            people[user["id"]] = max(label, people.get(user["id"], ""), key=len)
    return groups, people


def main() -> None:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token()}/getUpdates?timeout=0", timeout=20) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Не удалось связаться с Telegram ({type(e).__name__}). Если сервис уже запущен — остановите его: "
                 "systemctl --user stop claude-control")
    groups, people = summarize(data.get("result", []))
    if not groups or not people:
        sys.exit("Ничего не найдено. Напишите /start в теме General вашей группы и запустите скрипт ещё раз.")
    print("Группы:")
    for gid, title in groups.items():
        print(f"  {title}\n    TELEGRAM_CHAT_ID={gid}")
    print("Люди, писавшие боту (владелец — это вы):")
    for uid, name in people.items():
        print(f"  {name}\n    OWNER_IDS={uid}")


if __name__ == "__main__":
    main()
