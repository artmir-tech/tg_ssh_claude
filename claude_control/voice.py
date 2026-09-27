"""Speech to text for Telegram voice messages (GigaAM v3, CPU).

The model (~1.6 GB RAM) never lives inside the bot: it runs in a separate worker process from
its own venv (.venv-voice), started on the first voice message and stopped after a few idle
minutes. Protocol: one JSON object per line on stdin/stdout.

    python -m claude_control.voice                  # worker (run with .venv-voice/bin/python)
    python -m claude_control.voice FILE [-t] [-o X]  # one-off transcription (deploy/transcribe)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("cc.voice")

MODEL = "v3_e2e_rnnt"   # Russian, with punctuation and normalization
CHUNK_S = 22.0          # GigaAM's .transcribe handles up to 25 s per call
THREADS = 3             # leave a core for Claude processes
MAX_FILE_H = 4          # longest file the transcribe tool accepts


# ---------------------------------------------------------------- worker side (heavy imports inside)
def _duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def _cuts(path: str, total: float) -> list[tuple[float, float]]:
    """Split points at pauses so every piece is shorter than CHUNK_S."""
    err = subprocess.run(["ffmpeg", "-i", path, "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    pauses = [(float(a) + float(b)) / 2 for a, b in zip(re.findall(r"silence_start: ([\d.]+)", err),
                                                        re.findall(r"silence_end: ([\d.]+)", err))]
    cuts, start = [], 0.0
    while total - start > CHUNK_S:
        inside = [p for p in pauses if start + 5 < p <= start + CHUNK_S]
        end = inside[-1] if inside else start + CHUNK_S
        cuts.append((start, end))
        start = end
    cuts.append((start, total))
    return cuts


def _transcribe(model, path: str) -> list[list]:
    """[[start_s, end_s, text], ...] - one entry per piece with speech."""
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "all.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-ar", "16000", "-ac", "1", wav],
                       check=True)
        for n, (a, b) in enumerate(_cuts(wav, _duration(wav))):
            piece = os.path.join(tmp, f"{n}.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", wav, piece],
                           check=True)
            text = model.transcribe(piece)
            text = (text if isinstance(text, str) else getattr(text, "text", "")).strip()
            if text:
                parts.append([round(a, 1), round(b, 1), text])
    return parts


def _load_model():
    import warnings
    warnings.filterwarnings("ignore")
    import gigaam
    import torch
    torch.set_num_threads(THREADS)
    return gigaam.load_model(MODEL)


def worker() -> None:
    model = _load_model()
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        try:
            segments = _transcribe(model, req["path"])
            out = {"id": req["id"], "text": " ".join(t for _, _, t in segments), "segments": segments}
        except Exception as e:  # noqa: BLE001 - report, keep serving
            out = {"id": req["id"], "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)


def cli(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="transcribe", description="Речь из аудио/видео → текст. GigaAM v3, локально, "
                                 "русский язык. ~5-8 минут записи в минуту работы.")
    ap.add_argument("files", nargs="+", help="аудио или видео (ogg, mp3, m4a, wav, mp4, mov...)")
    ap.add_argument("-t", "--timestamps", action="store_true", help="метки времени [мм:сс-мм:сс] у каждого куска")
    ap.add_argument("-o", "--out", help="записать текст в этот файл (иначе - на экран)")
    a = ap.parse_args(argv)
    if a.out and len(a.files) > 1:
        ap.error("-o работает с одним файлом")
    model = _load_model()
    for f in a.files:
        started = time.time()
        text = format_segments(_transcribe(model, f), a.timestamps)
        if a.out:
            Path(a.out).write_text(text + "\n", encoding="utf-8")
        else:
            print((f"== {f}\n" if len(a.files) > 1 else "") + text, flush=True)
        print(f"[{f}: {time.time() - started:.0f} с]", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- shared helpers (stdlib only)
def clock(t: float) -> str:
    h, rest = divmod(int(t), 3600)
    return (f"{h}:" if h else "") + "%02d:%02d" % divmod(rest, 60)


def format_segments(segments: list, timestamps: bool) -> str:
    if timestamps:
        return "\n".join(f"[{clock(a)}-{clock(b)}] {t}" for a, b, t in segments)
    return " ".join(t for _, _, t in segments)


async def media_seconds(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- bot side (stdlib only)
class Transcriber:
    """Starts the worker on demand, one request at a time, stops it after idle_s without use."""

    def __init__(self, python: Path, idle_s: float = 300):
        self.python, self.idle_s = python, idle_s
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.last_used = 0.0
        self._seq = 0

    @property
    def available(self) -> bool:
        return self.python.exists()

    async def _start(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-m", "claude_control.voice", cwd=str(root),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        line = await asyncio.wait_for(self.proc.stdout.readline(), 300)   # first run may download the model
        if not line or not json.loads(line).get("ready"):
            await self.stop()
            raise RuntimeError("speech model did not start")
        log.info("speech model loaded (pid %s)", self.proc.pid)

    async def transcribe(self, path: Path, seconds: float = 0) -> str:
        return (await self._request(path, seconds)).get("text", "").strip()

    async def segments(self, path: Path, seconds: float = 0) -> list:
        return (await self._request(path, seconds)).get("segments", [])

    async def _request(self, path: Path, seconds: float) -> dict:
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None:
                await self._start()
            self._seq += 1
            self.proc.stdin.write((json.dumps({"id": self._seq, "path": str(path)}) + "\n").encode())
            await self.proc.stdin.drain()
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), 60 + seconds)
            except asyncio.TimeoutError:
                await self.stop()
                raise RuntimeError("speech recognition timed out") from None
            self.last_used = time.time()
            if not line:
                await self.stop()
                raise RuntimeError("speech worker exited")
            reply = json.loads(line)
            if reply.get("error"):
                raise RuntimeError(reply["error"])
            return reply

    async def reap_idle(self) -> None:
        if self.proc and self.proc.returncode is None and not self.lock.locked() \
                and time.time() - self.last_used > self.idle_s:
            await self.stop()
            log.info("speech model unloaded after %d s idle", self.idle_s)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


async def transcribe_file(voice: Transcriber, p: Path, timestamps: bool, fallback_dir: Path) -> str:
    """The `transcribe` tool: the whole text goes to a .txt file; Claude gets it back (or its start + the path)."""
    if not p.is_file():
        return f"Error: file not found: {p}"
    seconds = await media_seconds(p)
    if seconds <= 0:
        return f"Error: no audio found in {p.name} (not an audio/video file?)"
    if seconds > MAX_FILE_H * 3600:
        return f"Error: {seconds / 3600:.1f} h is too long (limit {MAX_FILE_H} h); cut it with ffmpeg first."
    started = time.time()
    try:
        segments = await voice.segments(p, seconds)
    except Exception as e:  # noqa: BLE001 - tell Claude, not a traceback
        return f"Error: transcription failed: {e}"
    log.info("file transcribed: %.0fs audio in %.0fs, %d pieces", seconds, time.time() - started, len(segments))
    if not segments:
        return f"No speech recognised in {p.name} ({clock(seconds)})."
    text = format_segments(segments, timestamps) + "\n"
    out = p.with_name(p.stem + ".расшифровка.txt")
    try:
        out.write_text(text, encoding="utf-8")
    except OSError:
        out = fallback_dir / out.name
        out.write_text(text, encoding="utf-8")
    head = f"Transcript of {p.name} ({clock(seconds)}, GigaAM v3; may contain recognition errors) saved to {out}"
    if len(text) <= 15000:
        return f"{head}\n\n{text}"
    return f"{head} - {len(text)} characters, read that file for the rest. Beginning:\n\n{text[:3000]}…"


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]) if len(sys.argv) > 1 else worker())
