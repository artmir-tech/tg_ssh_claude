"""Configuration: read from the process environment, falling back to the .env file.

systemd passes .env via EnvironmentFile; a manual `python -m claude_control` run reads the
same file directly, so both paths see identical settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _int_set(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def _list(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Config:
    bot_token: str
    owner_ids: set[int]
    chat_id: int | None                 # the forum supergroup; None until bound via /start
    max_concurrent: int = 5
    default_cwd: str = str(Path.home())
    project_dirs: list[str] = field(default_factory=list)
    claude_cli: str = "/usr/bin/claude"
    model: str | None = None            # None = whatever ~/.claude/settings.json says
    auto_allow_tools: list[str] = field(default_factory=lambda: ["WebSearch", "WebFetch"])
    permission_timeout_s: int = 3600
    max_run_hours: float = 8.0
    data_dir: Path = ROOT / "data"
    tg_api_base: str = "https://api.telegram.org"
    timezone: str | None = None         # IANA name for times shown in Telegram; None = server time
    voice_engine: str = ""              # "gigaam" = transcribe voice messages (needs .venv-voice); "" = off
    voice_max_min: int = 15             # longer voice/audio is not transcribed

    @property
    def db_path(self) -> Path:
        return self.data_dir / "claude-control.db"

    @property
    def inbox_root(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def voice_python(self) -> Path:
        return ROOT / ".venv-voice" / "bin" / "python"

    @property
    def secret_paths(self) -> list[Path]:
        """Files Claude sessions must never read or edit (defence in depth)."""
        return [ROOT / ".env", self.db_path]


def load_config(env_file: Path | None = None) -> Config:
    file_values = _read_env_file(env_file or ROOT / ".env")

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_values.get(key) or default

    token = get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env)")
    chat = get("TELEGRAM_CHAT_ID")
    cfg = Config(
        bot_token=token,
        owner_ids=_int_set(get("OWNER_IDS")),
        chat_id=int(chat) if chat else None,
        max_concurrent=int(get("MAX_CONCURRENT_RUNS", "5")),
        default_cwd=get("DEFAULT_CWD", str(Path.home())),
        project_dirs=_list(get("PROJECT_DIRS")),
        claude_cli=get("CLAUDE_CLI", "/usr/bin/claude"),
        model=get("CLAUDE_MODEL") or None,
        auto_allow_tools=_list(get("AUTO_ALLOW_TOOLS", "WebSearch,WebFetch")),
        permission_timeout_s=int(get("PERMISSION_TIMEOUT_MIN", "60")) * 60,
        max_run_hours=float(get("MAX_RUN_HOURS", "8")),
        data_dir=Path(get("DATA_DIR", str(ROOT / "data"))),
        tg_api_base=get("TELEGRAM_API_BASE", "https://api.telegram.org"),
        timezone=get("TIMEZONE") or None,
        voice_engine=get("VOICE_ENGINE").lower(),
        voice_max_min=int(get("VOICE_MAX_MIN", "15")),
    )
    if cfg.default_cwd not in cfg.project_dirs:
        cfg.project_dirs.insert(0, cfg.default_cwd)
    return cfg
