"""Speech to text for Telegram voice messages (GigaAM v3, CPU).

The model (~1.6 GB RAM) never lives inside the bot: it runs in a separate worker process from
its own venv (.venv-voice), started on the first voice message and stopped after a few idle
minutes. Protocol: one JSON object per line on stdin/stdout.

    python -m claude_control.voice      # worker (run with .venv-voice/bin/python)
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


def _transcribe(model, path: str) -> str:
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "all.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-ar", "16000", "-ac", "1", wav], check=True)
        for n, (a, b) in enumerate(_cuts(wav, _duration(wav))):
            piece = os.path.join(tmp, f"{n}.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", wav, piece],
                           check=True)
            text = model.transcribe(piece)
            parts.append((text if isinstance(text, str) else getattr(text, "text", "")).strip())
    return " ".join(p for p in parts if p)


def worker() -> None:
    import warnings
    warnings.filterwarnings("ignore")
    import gigaam
    import torch
    torch.set_num_threads(THREADS)
    model = gigaam.load_model(MODEL)
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        try:
            out = {"id": req["id"], "text": _transcribe(model, req["path"])}
        except Exception as e:  # noqa: BLE001 - report, keep serving
            out = {"id": req["id"], "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)


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
            return reply.get("text", "").strip()

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


if __name__ == "__main__":
    worker()
