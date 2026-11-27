"""HTTP-FLV 录制源。

点播 FLV：按 Content-Length 下载完整后重新封装为 MP4。
直播 FLV：持续读取连接，用户点击"完成录制"或到达时长上限后封装；
连接中断会重连并继续追加。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

import aiohttp

from muxer.core.files import sanitize_filename, unique_path
from muxer.core.ffmpeg import FFmpeg, FFmpegError
from muxer.core.ffmpeg import remux as ffmpeg_remux
from muxer.core.http import HttpError
from muxer.protocols.base import (
    DownloadCancelled,
    DownloadContext,
    DownloadFailed,
    DownloadPaused,
)

LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
MAX_ATTEMPTS = 6
CONTAINERS = frozenset({"mp4", "mkv", "flv"})
MEDIA_SUFFIXES = frozenset({".flv", ".mp4", ".mkv", ".ts"})


class FlvSource:
    """HTTP-FLV 点播 / 直播。"""

    protocol = "flv"

    def __init__(self, ffmpeg: FFmpeg | None = None):
        self._ffmpeg = ffmpeg

    async def download(self, ctx: DownloadContext) -> Path:
        task = ctx.task
        live = bool(task.options.get("live"))
        part = ctx.tmp_dir / "recording.flv"
        ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        base_seconds = task.live_seconds
        failures = 0
        ticker = (
            asyncio.create_task(self._tick_live(ctx, base_seconds, started))
            if live
            else None
        )

        try:
            while True:
                if ctx.is_cancelled():
                    raise DownloadCancelled()
                if ctx.is_paused():
                    raise DownloadPaused()
                if ctx.is_finishing():
                    break
                if self._limit_reached(ctx, base_seconds, started):
                    break
                try:
                    await self._stream_once(ctx, part, live, base_seconds, started)
                except (HttpError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if ctx.is_cancelled():
                        raise DownloadCancelled() from exc
                    if ctx.is_paused():
                        raise DownloadPaused() from exc
                    failures += 1
                    if not live and failures >= MAX_ATTEMPTS:
                        raise DownloadFailed(f"FLV 下载失败：{exc}") from exc
                    if live and failures >= MAX_ATTEMPTS * 3:
                        raise DownloadFailed(f"直播连接反复中断：{exc}") from exc
                    await self._sleep(ctx, min(10.0, 0.5 * (2**min(failures, 4))))
                    continue

                if ctx.is_finishing():
                    break
                if not live:
                    break
                if self._limit_reached(ctx, base_seconds, started):
                    break
                # 直播流意外结束：等待后重连
                await self._sleep(ctx, 1.0)

            if not part.exists() or part.stat().st_size == 0:
                raise DownloadFailed("没有录制到任何数据")
            return await self._finalize(ctx, part)
        finally:
            if ticker is not None:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)

    # ---- 读取 ----

    async def _stream_once(
        self,
        ctx: DownloadContext,
        part: Path,
        live: bool,
        base_seconds: float,
        started: float,
    ) -> None:
        offset = part.stat().st_size if part.exists() else 0
        headers = dict(ctx.task.headers)
        if offset > 0 and not live:
            headers["Range"] = f"bytes={offset}-"

        async with ctx.http.stream(ctx.task.url, headers, offset) as response:
            if not live and offset > 0 and response.status == 200:
                LOGGER.warning("服务器忽略 Range，FLV 重新从头下载")
                offset = 0
                part.write_bytes(b"")
            total = _total_size(response, offset)
            mode = "ab" if offset > 0 else "wb"
            with part.open(mode) as handle:
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    if ctx.is_cancelled():
                        raise DownloadCancelled()
                    if ctx.is_paused():
                        raise DownloadPaused()
                    if ctx.is_finishing():
                        break
                    handle.write(chunk)
                    handle.flush()
                    offset += len(chunk)
                    ctx.report(offset, total or 0, 0, 0)
                    if live:
                        elapsed = time.monotonic() - started
                        ctx.report_live(base_seconds + elapsed)
                        if ctx.max_live_seconds > 0 and base_seconds + elapsed >= ctx.max_live_seconds:
                            break
                    await ctx.rate_limiter.acquire(len(chunk))
            if not live and total and offset < total:
                raise HttpError(f"数据不完整：{offset}/{total}")

    # ---- 收尾 ----

    async def _finalize(self, ctx: DownloadContext, part: Path) -> Path:
        task = ctx.task
        container = str(task.options.get("container") or "mp4").lower()
        if container not in CONTAINERS:
            container = "mp4"
        destination = self._destination(task, container)

        if container == "flv":
            try:
                shutil.move(str(part), str(destination))
            except OSError as exc:
                raise DownloadFailed(f"移动文件失败：{exc}") from exc
            return destination

        if self._ffmpeg is None:
            raise DownloadFailed("未找到 ffmpeg，无法把 FLV 封装为 " + container.upper())

        temp = ctx.tmp_dir / f"remuxed.{container}"
        try:
            await ffmpeg_remux(self._ffmpeg.ffmpeg, part, temp)
        except FFmpegError as exc:
            raise DownloadFailed(str(exc)) from exc
        try:
            shutil.move(str(temp), str(destination))
        except OSError as exc:
            raise DownloadFailed(f"移动文件失败：{exc}") from exc
        return destination

    @staticmethod
    def _destination(task, container: str) -> Path:
        title = sanitize_filename(task.title or "recording")
        suffix = Path(title).suffix.lower()
        if suffix in MEDIA_SUFFIXES:
            title = title[: -len(suffix)]
        return unique_path(Path(task.output_dir), f"{title or 'recording'}.{container}")

    def _limit_reached(
        self, ctx: DownloadContext, base_seconds: float, started: float
    ) -> bool:
        if ctx.max_live_seconds <= 0:
            return False
        return base_seconds + (time.monotonic() - started) >= ctx.max_live_seconds

    async def _tick_live(
        self, ctx: DownloadContext, base_seconds: float, started: float
    ) -> None:
        """无数据时也每秒刷新已录时长，避免界面进度停滞。"""
        while True:
            await asyncio.sleep(1.0)
            if ctx.is_cancelled() or ctx.is_paused() or ctx.is_finishing():
                return
            ctx.report_live(base_seconds + (time.monotonic() - started))

    async def _sleep(self, ctx: DownloadContext, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if ctx.is_cancelled() or ctx.is_paused() or ctx.is_finishing():
                return
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _total_size(response: aiohttp.ClientResponse, offset: int) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        try:
            return int(content_range.rsplit("/", 1)[1])
        except ValueError:
            pass
    length = response.headers.get("Content-Length")
    if length is None:
        return None
    try:
        value = int(length)
    except ValueError:
        return None
    return offset + value if response.status == 206 else value
