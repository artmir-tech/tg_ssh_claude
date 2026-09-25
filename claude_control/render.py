"""Text helpers: Claude Markdown -> Telegram HTML, message splitting, human-friendly times."""

from __future__ import annotations

import html
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TG_LIMIT = 4096
CHUNK = 3500  # markdown chars per message; leaves room for the tags HTML conversion adds


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def attr(text: str) -> str:
    return html.escape(text, quote=True)


# ---- Markdown -> Telegram HTML ---------------------------------------------------------------
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def _inline(text: str) -> str:
    """Convert inline markdown in one line. Code spans are protected from other rules."""
    parts = _INLINE_CODE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(f"<code>{esc(part)}</code>")
            continue
        links: list[str] = []

        def keep_link(m: re.Match) -> str:
            links.append(f'<a href="{attr(m.group(2))}">{esc(m.group(1))}</a>')
            return f"\x00{len(links) - 1}\x00"

        s = _LINK.sub(keep_link, part)
        s = esc(s)
        s = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", s)
        s = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", s)
        s = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", s)
        s = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], s)
        out.append(s)
    return "".join(out)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table_as_list(rows: list[str]) -> str:
    """Markdown tables are unreadable on a phone as monospace text: one row becomes one bullet,
    '▪️ <b>first cell</b> — Header: value · Header: value'."""
    body = [r for r in rows if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", r)]
    if len(body) < 2:
        return "\n".join(f"▪️ {_inline(' · '.join(_cells(r)))}" for r in body)
    header, data = _cells(body[0]), [_cells(r) for r in body[1:]]
    lines = []
    for cells in data:
        first, rest = cells[0], cells[1:]
        if len(header) == 2:
            tail = " · ".join(c for c in rest if c)
        else:
            tail = " · ".join(f"{h}: {c}" if h else c for h, c in zip(header[1:], rest) if c)
        first = first.replace("**", "").replace("__", "")  # it is bolded anyway; nested <b> breaks Telegram
        lines.append(f"▪️ <b>{_inline(first)}</b>" + (f" — {_inline(tail)}" if tail else ""))
    return "\n".join(lines)


def md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    code: list[str] | None = None
    lang = ""
    table: list[str] = []
    quote: list[str] = []

    def flush_table() -> None:
        if table:
            out.append(_table_as_list(table))
            table.clear()

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote>" + "\n".join(_inline(q) for q in quote) + "</blockquote>")
            quote.clear()

    for line in lines:
        stripped = line.strip()
        if code is not None:
            if stripped.startswith("```"):
                body = esc("\n".join(code))
                out.append(f'<pre><code class="language-{attr(lang)}">{body}</code></pre>' if lang else f"<pre>{body}</pre>")
                code = None
            else:
                code.append(line)
            continue
        if stripped.startswith("```"):
            flush_table(); flush_quote()
            code, lang = [], re.sub(r"[^\w+#.-]", "", stripped[3:].strip())[:20]
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            flush_quote()
            table.append(line)
            continue
        flush_table()
        if stripped.startswith(">"):
            quote.append(stripped.lstrip(">").strip())
            continue
        flush_quote()
        if m := re.match(r"^\s*#{1,6}\s+(.*)$", line):
            out.append(f"<b>{_inline(m.group(1).strip('# '))}</b>")
        elif re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            out.append("──────────")
        elif m := re.match(r"^(\s*)[-*+]\s+(.*)$", line):
            out.append(f"{m.group(1)}• {_inline(m.group(2))}")
        else:
            out.append(_inline(line))
    if code is not None:
        body = esc("\n".join(code))
        out.append(f"<pre>{body}</pre>")
    flush_table(); flush_quote()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def split_markdown(md: str, limit: int = CHUNK) -> list[str]:
    """Split markdown into chunks at paragraph/line boundaries, never inside a code fence."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence: str | None = None
    for line in md.split("\n"):
        if len(line) > limit:  # pathological single line
            for i in range(0, len(line), limit):
                chunks_line = line[i:i + limit]
                if size + len(chunks_line) > limit and cur:
                    chunks.append(_close(cur, fence)); cur, size = _reopen(fence), 0
                cur.append(chunks_line); size += len(chunks_line) + 1
            continue
        if size + len(line) + 1 > limit and cur:
            chunks.append(_close(cur, fence))
            cur, size = _reopen(fence), 0
        cur.append(line)
        size += len(line) + 1
        if line.strip().startswith("```"):
            fence = None if fence is not None else line.strip()
    if cur and "".join(cur).strip():
        chunks.append("\n".join(cur))
    return chunks or [""]


def _close(lines: list[str], fence: str | None) -> str:
    return "\n".join(lines + (["```"] if fence is not None else []))


def _reopen(fence: str | None) -> list[str]:
    return [fence] if fence is not None else []


# ---- time --------------------------------------------------------------------------------
def duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} ч {m} мин" if m and h < 10 else f"{h} ч"
    return f"{s // 86400} дн"


def precise_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} с"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} мин {s} с" if s else f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин"


def ago(ts: float | None) -> str:
    return duration(time.time() - ts) if ts else "—"


_TZ: ZoneInfo | None = None   # None = server local time
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def set_timezone(name: str | None) -> None:
    global _TZ
    _TZ = ZoneInfo(name) if name else None


def tz_name() -> str:
    return _TZ.key if _TZ else (time.strftime("%Z") or "local")


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, _TZ) if _TZ else datetime.fromtimestamp(ts).astimezone()


def clock(ts: float | None = None) -> str:
    return _dt(time.time() if ts is None else ts).strftime("%H:%M")


def when(ts: float) -> str:
    """Past or future: 'сегодня 13:42' / 'вчера 18:20' / 'завтра 09:00' / 'сб 26.09 15:00'."""
    d, now = _dt(ts), _dt(time.time())
    days = (d.date() - now.date()).days
    hm = d.strftime("%H:%M")
    label = {0: "сегодня", -1: "вчера", 1: "завтра"}.get(days)
    return f"{label} {hm}" if label else f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')} {hm}"


def until(ts: float) -> str:
    """Time left until ts: '58 мин', '4 ч 10 мин', '2 дн 3 ч'."""
    s = int(ts - time.time())
    if s <= 0:
        return "0 мин"
    if s >= 86400:
        d, rest = divmod(s, 86400)
        return f"{d} дн {rest // 3600} ч"
    return duration(s)


def folder_label(path: str) -> str:
    """Human name of a working folder: 'домашняя' for $HOME, otherwise the folder's own name."""
    from pathlib import Path
    return "домашняя" if Path(path) == Path.home() else (Path(path).name or path)


def short_when(ts: float) -> str:
    """Compact past/future moment: '19:26', 'вчера', 'завтра 09:00', 'сб 18:38', '12.09'."""
    d, now = _dt(ts), _dt(time.time())
    days = (d.date() - now.date()).days
    if days == 0:
        return d.strftime("%H:%M")
    if days == -1:
        return "вчера"
    if days == 1:
        return "завтра " + d.strftime("%H:%M")
    if 1 < days < 7:
        return f"{WEEKDAYS[d.weekday()]} {d.strftime('%H:%M')}"
    return d.strftime("%d.%m")


def ordinal(n: int) -> str:
    return {1: "первая", 2: "вторая", 3: "третья"}.get(n, f"{n}-я")


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    word = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {word}"


def short_path(path: str) -> str:
    from pathlib import Path
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def clip_words(text: str, n: int) -> str:
    """Shorten to n chars at a word boundary (titles look broken when cut mid-word)."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    space = cut.rfind(" ")
    if space >= n * 0.6:
        cut = cut[:space]
    return cut.rstrip(" —-·,:;") + "…"


def clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"
