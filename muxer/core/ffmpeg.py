"""ffmpeg / ffprobe 封装：定位、拼接、混流、重封装与进度解析。

只做 `-c copy` 级别的容器操作，不重编码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOGGER = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
MERGED_STDERR_LINES = 40


class FFmpegError(RuntimeError):
    """ffmpeg 执行失败。"""


@dataclass(frozen=True)
class FFmpeg:
    """一对可执行文件路径。"""

    ffmpeg: str
    ffprobe: str | None

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)


def _bundled_candidates() -> list[Path]:
    """打包目录内置 ffmpeg（M7 切换为内置 LGPL 构建）。"""
    roots = [Path(sys.executable).resolve().parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    paths: list[Path] = []
    for root in roots:
        paths.extend(
            (
                root / "ffmpeg" / "ffmpeg.exe",
                root / "ffmpeg" / "bin" / "ffmpeg.exe",
                root / "_internal" / "ffmpeg" / "ffmpeg.exe",
            )
        )
    return paths


def locate(configured: str = "") -> FFmpeg | None:
    """按 用户设置 > PATH 的顺序定位 ffmpeg。"""
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(_bundled_candidates())
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if not candidate.exists():
            continue
        probe_name = "ffprobe.exe" if candidate.suffix.lower() == ".exe" else "ffprobe"
        sibling = candidate.with_name(probe_name)
        ffprobe: str | None = None
        if sibling.exists():
            ffprobe = str(sibling)
        else:
            ffprobe = shutil.which("ffprobe")
        return FFmpeg(ffmpeg=str(candidate), ffprobe=ffprobe)
    return None


async def _run(
    args: list[str],
    total_seconds: float | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """执行 ffmpeg 并解析 -progress 输出；失败时抛出带尾部日志的异常。"""
    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    stderr_tail: deque[str] = deque(maxlen=MERGED_STDERR_LINES)

    async def drain_stderr() -> None:
        assert process.stderr is not None
        async for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_tail.append(line)

    async def read_stdout() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not (progress and total_seconds and line.startswith("out_time_ms=")):
                continue
            try:
                milliseconds = int(line.split("=", 1)[1])
            except ValueError:
                continue
            ratio = milliseconds / 1_000_000 / max(total_seconds, 0.001)
            progress(min(1.0, max(0.0, ratio)))

    readers = [asyncio.create_task(drain_stderr()), asyncio.create_task(read_stdout())]
    try:
        await process.wait()
        await asyncio.gather(*readers, return_exceptions=True)
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        for reader in readers:
            reader.cancel()
        raise

    if process.returncode != 0:
        tail = " | ".join(list(stderr_tail)[-3:]) or "无输出"
        raise FFmpegError(f"ffmpeg 执行失败（退出码 {process.returncode}）：{tail}")


def _common_args() -> list[str]:
    return ["-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]


async def concat(
    executable: str,
    list_file: Path,
    output: Path,
    total_seconds: float | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """用 concat 分离器拼接同编码分片。"""
    args = [
        executable,
        *_common_args(),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output),
    ]
    await _run(args, total_seconds, progress)


async def remux(
    executable: str,
    source: Path,
    output: Path,
    total_seconds: float | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """换容器，不重编码。"""
    args = [executable, *_common_args(), "-i", str(source), "-c", "copy", str(output)]
    await _run(args, total_seconds, progress)


async def mux(
    executable: str,
    video: Path,
    audio: Path,
    output: Path,
    total_seconds: float | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """把独立音轨与视频轨合流。"""
    args = [
        executable,
        *_common_args(),
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        str(output),
    ]
    await _run(args, total_seconds, progress)


async def probe_duration(ffprobe: str | None, path: Path) -> float | None:
    """读取媒体时长，失败返回 None。"""
    if not ffprobe:
        return None
    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=creationflags,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
        return float(payload.get("format", {}).get("duration", 0.0)) or None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None