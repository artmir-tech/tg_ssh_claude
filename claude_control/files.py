"""Files from the server to Telegram: which files may leave the server, which are sent on their
own, and the `send_file` tool Claude can call.

Sending a file to Telegram takes it off the server, so:
  * secrets never leave (even if the owner approves): .env files, SSH keys, Claude/GitHub logins,
    this bot's own data;
  * files Claude writes with the Write tool are sent automatically after its answer - except code
    inside git work trees, hidden/temporary files and anything in the secrets list;
  * `send_file` for files inside the session folder needs no approval; other paths ask the owner.
"""

from __future__ import annotations

import fnmatch
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

SERVER_NAME = "claude_control"
SEND_TOOL = f"mcp__{SERVER_NAME}__send_file"

_SECRET_NAMES = (".env", ".env.*", "*.env", "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.pem", "*.key", "*.p12",
                 ".netrc", ".git-credentials", ".credentials.json", ".claude.json")
_SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".config/gh", ".docker", "claude-control/data")


def resolve(path: str, cwd: str) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else Path(cwd) / p).resolve()


def is_secret(p: Path) -> bool:
    if any(fnmatch.fnmatch(p.name, pat) for pat in _SECRET_NAMES):
        return True
    home = Path.home()
    return any(p.is_relative_to(home / d) for d in _SECRET_DIRS)


def in_git_tree(p: Path) -> bool:
    return any((parent / ".git").exists() for parent in p.parents)


def auto_sendable(p: Path) -> bool:
    """Should a file Claude wrote be sent without being asked for?"""
    if is_secret(p) or not p.is_file():
        return False
    if any(part.startswith(".") for part in p.parts[1:]):      # hidden files or folders (.claude, .cache...)
        return False
    if p.is_relative_to("/tmp") or p.is_relative_to(tempfile.gettempdir()):
        return False
    return not in_git_tree(p)                                    # code projects: ask explicitly instead


def human_size(n: int) -> str:
    for unit, size in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f} {unit}".replace(".0 ", " ")
    return f"{n} Б"


def zip_files(paths: list[Path], dest_dir: Path) -> Path:
    dest = dest_dir / "files.zip"
    names: set[str] = set()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            name, n = p.name, 1
            while name in names:                                  # same name from different folders
                name, n = f"{p.stem}-{n}{p.suffix}", n + 1
            names.add(name)
            z.write(p, name)
    return dest


def send_tool_server(send: Callable[[str, str], Awaitable[str]]) -> Any:
    """An in-process MCP server with one tool; `send(path, caption)` delivers the file to the topic."""

    @tool("send_file",
          "Send a file from the server to the user's Telegram chat (this conversation's topic). Use it when the "
          "user asks to send, share or show a file, or to deliver a finished result produced by a command "
          "(report, image, table, archive). Files you create with the Write tool are sent automatically after "
          "your answer - do not send those again.",
          {"path": Annotated[str, "Absolute path, or relative to the working directory"],
           "caption": Annotated[str, "Optional short caption"]})
    async def send_file(args: dict) -> dict:
        text = await send(args["path"], args.get("caption") or "")
        return {"content": [{"type": "text", "text": text}], "is_error": text.startswith("Not sent")}

    return create_sdk_mcp_server(SERVER_NAME, tools=[send_file])
