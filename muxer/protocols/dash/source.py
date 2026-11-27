"""DASH 下载源（点播）。

流程：解析 MPD → 分别下载视频 / 音频表示的分片 →
fMP4 二进制拼接（或 TS 走 ffmpeg concat）→ 合流 → 移动到目标目录。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from muxer.core.files import sanitize_filename, unique_path
from muxer.core.ffmpeg import FFmpeg, FFmpegError
from muxer.core.ffmpeg import concat as ffmpeg_concat
from muxer.core.ffmpeg import mux as ffmpeg_mux
from muxer.core.ffmpeg import remux as ffmpeg_remux
from muxer.core.http import HttpError
from muxer.protocols.base import (
    DownloadCancelled,
    DownloadContext,
    DownloadFailed,
    DownloadPaused,
)
from muxer.protocols.dash.analyzer import analyze
from muxer.protocols.dash.manifest import SegmentPlan

LOGGER = logging.getLogger(__name__)

SEGMENT_RETRIES = 3
VALID_CONTAINERS = frozenset({"mp4", "mkv"})


class _Reporter:
    """视频 + 音频分片统一进度。"""

    def __init__(self, ctx: DownloadContext, total_segments: int):
        self._ctx = ctx
        self._total = total_segments
        self._done = 0
        self._bytes = 0

    def advance(self, segments: int, byte_count: int = 0) -> None:
        self._done += segments
        self._bytes += byte_count
        self._report()

    def add(self, byte_count: int) -> None:
        self._done += 1
        self._bytes += byte_count
        self._report()

    def _report(self) -> None:
        self._ctx.report(self._bytes, 0, self._done, self._total)


class DashSource:
    """MPD 点播下载。"""

    protocol = "dash"

    def __init__(self, ffmpeg: FFmpeg | None, segment_concurrency: int = 16):
        self._ffmpeg = ffmpeg
        self._concurrency = max(1, segment_concurrency)

    async def download(self, ctx: DownloadContext) -> Path:
        task = ctx.task
        preferred = str(task.options.get("variant") or "")
        analysis = await analyze(ctx.http, task.url, task.headers, preferred)

        if analysis.live:
            raise DownloadFailed("检测到直播 DASH，录制功能将在 M5 接入")
        if analysis.drm:
            raise DownloadFailed("检测到 DRM 保护（ContentProtection），暂不支持")

        tracks: list[tuple[str, SegmentPlan]] = []
        if analysis.video_plan is not None:
            tracks.append(("video", analysis.video_plan))
        if analysis.audio_plan is not None:
            tracks.append(("audio", analysis.audio_plan))
        if not tracks:
            raise DownloadFailed("MPD 中没有可下载的轨道")

        total_segments = sum(len(plan.media_urls) for _, plan in tracks)
        reporter = _Reporter(ctx, total_segments)
        downloaded: list[tuple[str, SegmentPlan, Path]] = []
        for kind, plan in tracks:
            directory = ctx.tmp_dir / kind
            await self._download_plan(ctx, plan, directory, reporter)
            downloaded.append((kind, plan, directory))

        ctx.phase("postprocessing")
        destination = self._destination(task)
        await self._merge(ctx, downloaded, destination)
        return destination

    # ---- 分片下载 ----

    async def _download_plan(
        self,
        ctx: DownloadContext,
        plan: SegmentPlan,
        directory: Path,
        reporter: _Reporter,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        suffix = _segment_suffix(plan)
        for index, url in enumerate(plan.init_urls):
            path = directory / f"init{index}.m4s"
            if path.exists() and path.stat().st_size > 0:
                continue
            data = await self._fetch_with_retry(ctx, url, f"初始化段 #{index + 1}")
            _atomic_write(path, data)

        paths = {
            index: directory / f"seg{index:06d}{suffix}"
            for index in range(len(plan.media_urls))
        }
        completed = [
            index
            for index, path in paths.items()
            if path.exists() and path.stat().st_size > 0
        ]
        if completed:
            reporter.advance(
                len(completed), sum(paths[index].stat().st_size for index in completed)
            )
            LOGGER.info("DASH 续传：跳过 %d 个已完成分片", len(completed))

        done_set = set(completed)
        pending = [index for index in paths if index not in done_set]
        if not pending:
            return

        queue: asyncio.Queue[int] = asyncio.Queue()
        for index in pending:
            queue.put_nowait(index)
        halt = asyncio.Event()
        errors: list[BaseException] = []

        async def worker() -> None:
            while not halt.is_set():
                if ctx.is_cancelled():
                    errors.append(DownloadCancelled())
                    return
                if ctx.is_paused():
                    errors.append(DownloadPaused())
                    return
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    data = await self._fetch_with_retry(
                        ctx, plan.media_urls[index], f"分片 #{index + 1}"
                    )
                    _atomic_write(paths[index], data)
                except (DownloadCancelled, DownloadPaused, DownloadFailed) as exc:
                    errors.append(exc)
                    halt.set()
                    return
                except Exception as exc:  # noqa: BLE001 - 兜底成任务失败
                    errors.append(DownloadFailed(f"分片 #{index + 1} 处理失败：{exc}"))
                    halt.set()
                    return
                reporter.add(len(data))
                await ctx.rate_limiter.acquire(len(data))

        workers = [
            asyncio.create_task(worker(), name=f"dash-seg-{index}")
            for index in range(min(self._concurrency, len(pending)))
        ]
        await asyncio.gather(*workers, return_exceptions=True)

        for exc in errors:
            if isinstance(exc, DownloadCancelled):
                raise exc
        for exc in errors:
            if isinstance(exc, DownloadPaused):
                raise exc
        if errors:
            raise errors[0]
        if ctx.is_cancelled():
            raise DownloadCancelled()
        if ctx.is_paused():
            raise DownloadPaused()

    async def _fetch_with_retry(
        self, ctx: DownloadContext, url: str, label: str
    ) -> bytes:
        latest: Exception | None = None
        for attempt in range(SEGMENT_RETRIES):
            if ctx.is_cancelled():
                raise DownloadCancelled()
            if ctx.is_paused():
                raise DownloadPaused()
            try:
                return await ctx.http.fetch_bytes(url, ctx.task.headers)
            except (HttpError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                latest = exc
                if attempt < SEGMENT_RETRIES - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise DownloadFailed(f"{label} 下载失败：{latest}")

    # ---- 合并 ----

    async def _merge(
        self,
        ctx: DownloadContext,
        tracks: list[tuple[str, SegmentPlan, Path]],
        destination: Path,
    ) -> None:
        work = ctx.tmp_dir / "merge"
        work.mkdir(parents=True, exist_ok=True)

        outputs: dict[str, Path] = {}
        for kind, plan, directory in tracks:
            output = work / f"{kind}{'.mp4' if plan.is_fmp4 else '.ts'}"
            await self._build_track(ctx, plan, directory, output)
            outputs[kind] = output

        video = outputs.get("video")
        audio = outputs.get("audio")
        if video is not None and audio is not None:
            if self._ffmpeg is None:
                raise DownloadFailed("未找到 ffmpeg，无法合并音视频（请在设置中指定路径）")
            merged = work / "merged.mp4"
            try:
                await ffmpeg_mux(self._ffmpeg.ffmpeg, video, audio, merged)
            except FFmpegError as exc:
                raise DownloadFailed(str(exc)) from exc
            source = merged
        elif video is not None:
            source = video
        elif audio is not None:
            source = audio
        else:
            raise DownloadFailed("没有可合并的轨道")

        if source.suffix.lower() != destination.suffix.lower():
            if self._ffmpeg is None:
                raise DownloadFailed("未找到 ffmpeg，无法转换容器格式")
            final = work / f"final{destination.suffix}"
            try:
                await ffmpeg_remux(self._ffmpeg.ffmpeg, source, final)
            except FFmpegError as exc:
                raise DownloadFailed(str(exc)) from exc
            source = final

        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            raise DownloadFailed(f"移动文件失败：{exc}") from exc

    async def _build_track(
        self,
        ctx: DownloadContext,
        plan: SegmentPlan,
        directory: Path,
        output: Path,
    ) -> None:
        """fMP4 二进制拼接；其他容器交给 ffmpeg concat。"""
        if plan.is_fmp4:
            with output.open("wb") as handle:
                for index in range(len(plan.init_urls)):
                    init_path = directory / f"init{index}.m4s"
                    if init_path.exists():
                        handle.write(init_path.read_bytes())
                for index in range(len(plan.media_urls)):
                    part = directory / f"seg{index:06d}.m4s"
                    if not part.exists():
                        raise DownloadFailed(f"缺少分片文件：{part.name}")
                    handle.write(part.read_bytes())
            return

        if self._ffmpeg is None:
            raise DownloadFailed("未找到 ffmpeg，无法合并分片（请在设置中指定路径）")
        suffix = _segment_suffix(plan)
        list_file = ctx.tmp_dir / f"{directory.name}_concat.txt"
        lines = [
            _ffconcat_line(directory / f"seg{index:06d}{suffix}")
            for index in range(len(plan.media_urls))
        ]
        list_file.write_text("\n".join(lines), encoding="utf-8")
        try:
            await ffmpeg_concat(
                self._ffmpeg.ffmpeg, list_file, output, plan.total_duration or None
            )
        except FFmpegError as exc:
            raise DownloadFailed(str(exc)) from exc

    # ---- 目标路径 ----

    @staticmethod
    def _destination(task) -> Path:
        container = str(task.options.get("container") or "mp4").lower()
        if container not in VALID_CONTAINERS:
            container = "mp4"
        title = sanitize_filename(task.title or "video")
        suffix = Path(title).suffix.lower()
        if suffix in {".mp4", ".mkv", ".ts", ".m4v", ".mov"}:
            title = title[: -len(suffix)]
        directory = Path(task.output_dir)
        return unique_path(directory, f"{title or 'video'}.{container}")


def _segment_suffix(plan: SegmentPlan) -> str:
    """根据计划类型决定分片落盘扩展名。"""
    if plan.is_fmp4:
        return ".m4s"
    if plan.media_urls:
        suffix = Path(urlparse(plan.media_urls[0]).path).suffix.lower()
        if suffix in (".ts", ".m4s", ".mp4", ".m4a", ".aac"):
            return suffix
    return ".ts"


def _atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".part")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("清理临时分片失败: %s", temp)
        raise DownloadFailed(f"写入分片失败：{exc}") from exc


def _ffconcat_line(path: Path) -> str:
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'"