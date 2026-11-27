"""HLS 下载源（点播）。

流程：解析播放列表 → 并发下载分片（AES-128 自动解密）→
ffmpeg 拼接 / 混流 → 移动到目标目录。暂停时保留已下载分片，继续时跳过。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
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
from muxer.protocols.hls.analyzer import HlsAnalysis, analyze
from muxer.protocols.hls.crypto import DecryptError, decrypt_segment, sequence_iv
from muxer.protocols.hls.playlist import (
    ByteRange,
    MediaPlaylist,
    PlaylistError,
    Segment,
    parse_playlist,
)

LOGGER = logging.getLogger(__name__)

SEGMENT_RETRIES = 3
VALID_CONTAINERS = frozenset({"mp4", "mkv"})
MEDIA_SUFFIXES = frozenset({".mp4", ".mkv", ".ts", ".m4v", ".mov", ".flv", ".webm"})


class _Reporter:
    """把分片进度汇总成一次上下文回调（含初始已完成分片）。"""

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

    def live_add(self, byte_count: int, recorded_seconds: float) -> None:
        """直播：分片数总量未知，额外上报已录制秒数。"""
        self._done += 1
        self._bytes += byte_count
        self._ctx.report(self._bytes, 0, self._done, 0)
        self._ctx.report_live(recorded_seconds)

    def _report(self) -> None:
        self._ctx.report(self._bytes, 0, self._done, self._total)


class HLSSource:
    """m3u8 点播下载。"""

    protocol = "hls"

    def __init__(self, ffmpeg: FFmpeg | None, segment_concurrency: int = 16):
        self._ffmpeg = ffmpeg
        self._concurrency = max(1, segment_concurrency)

    async def download(self, ctx: DownloadContext) -> Path:
        task = ctx.task
        preferred = str(task.options.get("variant") or "")
        analysis = await analyze(ctx.http, task.url, task.headers, preferred)

        if analysis.sample_aes:
            raise DownloadFailed("检测到 SAMPLE-AES / DRM 加密，暂不支持")
        if analysis.live:
            return await self._record_live(ctx, analysis)
        if not analysis.media.segments:
            raise DownloadFailed("播放列表没有分片")

        key_cache: dict[str, bytes] = {}
        total_segments = len(analysis.media.segments)
        if analysis.audio_playlist is not None:
            total_segments += len(analysis.audio_playlist.segments)
        reporter = _Reporter(ctx, total_segments)

        video_dir = ctx.tmp_dir / "video"
        await self._download_track(ctx, analysis.media, video_dir, reporter, key_cache)

        audio_dir: Path | None = None
        if analysis.audio_playlist is not None:
            audio_dir = ctx.tmp_dir / "audio"
            await self._download_track(
                ctx, analysis.audio_playlist, audio_dir, reporter, key_cache
            )

        ctx.phase("postprocessing")
        destination = self._destination(task)
        await self._merge(ctx, analysis, video_dir, audio_dir, destination)
        return destination

    # ---- 分片下载 ----

    async def _download_track(
        self,
        ctx: DownloadContext,
        playlist: MediaPlaylist,
        directory: Path,
        reporter: _Reporter,
        key_cache: dict[str, bytes],
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".m4s" if playlist.has_map else ".ts"
        await self._download_init(ctx, playlist, directory)

        paths = {
            segment.index: directory / f"seg{segment.index:06d}{suffix}"
            for segment in playlist.segments
        }
        completed = [
            index
            for index, path in paths.items()
            if path.exists() and path.stat().st_size > 0
        ]
        if completed:
            reporter.advance(len(completed), sum(paths[index].stat().st_size for index in completed))
            LOGGER.info("续传：跳过 %d 个已完成分片", len(completed))

        done_set = set(completed)
        pending = [segment for segment in playlist.segments if segment.index not in done_set]
        if not pending:
            return

        queue: asyncio.Queue[Segment] = asyncio.Queue()
        for segment in pending:
            queue.put_nowait(segment)
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
                    segment = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    data = await self._fetch_segment(ctx, segment, key_cache)
                    _atomic_write(paths[segment.index], data)
                except (DownloadCancelled, DownloadPaused, DownloadFailed) as exc:
                    errors.append(exc)
                    halt.set()
                    return
                except Exception as exc:  # noqa: BLE001 - 兜底成任务失败
                    errors.append(
                        DownloadFailed(f"分片 #{segment.index + 1} 处理失败：{exc}")
                    )
                    halt.set()
                    return
                reporter.add(len(data))
                await ctx.rate_limiter.acquire(len(data))

        workers = [
            asyncio.create_task(worker(), name=f"hls-seg-{index}")
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

    async def _download_init(
        self, ctx: DownloadContext, playlist: MediaPlaylist, directory: Path
    ) -> None:
        init = next((segment.init for segment in playlist.segments if segment.init), None)
        if init is None:
            return
        init_path = directory / "init.mp4"
        if init_path.exists() and init_path.stat().st_size > 0:
            return
        data = await self._fetch_with_retry(ctx, init.uri, init.byterange, ctx.task.headers)
        _atomic_write(init_path, data)

    async def _fetch_segment(
        self, ctx: DownloadContext, segment: Segment, key_cache: dict[str, bytes]
    ) -> bytes:
        headers = dict(ctx.task.headers)
        if segment.byterange is not None:
            headers["Range"] = (
                f"bytes={segment.byterange.offset}-{segment.byterange.end}"
            )
        data = await self._fetch_with_retry(ctx, segment.uri, None, headers, segment.index)

        if segment.key is not None and segment.key.method.upper() == "AES-128":
            key = await self._load_key(ctx, segment.key.uri, key_cache)
            iv = segment.key.iv or sequence_iv(segment.sequence)
            try:
                data = decrypt_segment(data, key, iv)
            except DecryptError as exc:
                raise DownloadFailed(f"分片 #{segment.index + 1} 解密失败：{exc}") from exc
        return data

    async def _fetch_with_retry(
        self,
        ctx: DownloadContext,
        url: str,
        byterange: ByteRange | None,
        headers: dict[str, str],
        index: int | None = None,
    ) -> bytes:
        merged = dict(headers)
        if byterange is not None:
            merged["Range"] = f"bytes={byterange.offset}-{byterange.end}"
        last_error: Exception | None = None
        for attempt in range(SEGMENT_RETRIES):
            if ctx.is_cancelled():
                raise DownloadCancelled()
            if ctx.is_paused():
                raise DownloadPaused()
            try:
                return await ctx.http.fetch_bytes(url, merged)
            except (HttpError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < SEGMENT_RETRIES - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
        label = f"分片 #{index + 1}" if index is not None else "初始化段"
        raise DownloadFailed(f"{label} 下载失败：{last_error}")

    async def _load_key(
        self, ctx: DownloadContext, uri: str, cache: dict[str, bytes]
    ) -> bytes:
        if not uri:
            raise DownloadFailed("AES-128 密钥地址缺失")
        if uri in cache:
            return cache[uri]
        key = await ctx.http.fetch_bytes(uri, ctx.task.headers)
        if len(key) != 16:
            raise DownloadFailed(f"AES-128 密钥长度异常：{len(key)} 字节")
        cache[uri] = key
        return key

    # ---- 合并 ----

    async def _merge(
        self,
        ctx: DownloadContext,
        analysis: HlsAnalysis,
        video_dir: Path,
        audio_dir: Path | None,
        destination: Path,
    ) -> None:
        work = ctx.tmp_dir / "merge"
        work.mkdir(parents=True, exist_ok=True)
        duration = analysis.media.duration or None

        if audio_dir is not None and analysis.audio_playlist is not None:
            if self._ffmpeg is None:
                raise DownloadFailed("未找到 ffmpeg，无法合并音视频（请在设置中指定路径）")
            video_out = work / "video.mp4"
            audio_out = work / "audio.m4a"
            await self._build_track(ctx, analysis.media, video_dir, video_out)
            await self._build_track(ctx, analysis.audio_playlist, audio_dir, audio_out)
            merged = work / "merged.mp4"
            try:
                await ffmpeg_mux(
                    self._ffmpeg.ffmpeg, video_out, audio_out, merged, duration
                )
            except FFmpegError as exc:
                raise DownloadFailed(str(exc)) from exc
            source = merged
        else:
            source = work / "merged.mp4"
            await self._build_track(ctx, analysis.media, video_dir, source)

        if destination.suffix.lower() == ".mkv" and source.suffix.lower() != ".mkv":
            if self._ffmpeg is None:
                raise DownloadFailed("未找到 ffmpeg，无法转换为 MKV")
            final = work / "final.mkv"
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
        playlist: MediaPlaylist,
        directory: Path,
        output: Path,
    ) -> None:
        """把分片拼成单轨文件：fMP4 直接二进制拼接，TS 走 ffmpeg concat。"""
        if playlist.has_map:
            with output.open("wb") as handle:
                init_path = directory / "init.mp4"
                if init_path.exists():
                    handle.write(init_path.read_bytes())
                for segment in playlist.segments:
                    part = directory / f"seg{segment.index:06d}.m4s"
                    if not part.exists():
                        raise DownloadFailed(f"缺少分片文件：{part.name}")
                    handle.write(part.read_bytes())
            return

        if self._ffmpeg is None:
            raise DownloadFailed("未找到 ffmpeg，无法合并 TS 分片（请在设置中指定路径）")
        list_file = ctx.tmp_dir / f"{directory.name}_concat.txt"
        lines = [
            _ffconcat_line(directory / f"seg{segment.index:06d}.ts")
            for segment in playlist.segments
        ]
        list_file.write_text("\n".join(lines), encoding="utf-8")
        try:
            await ffmpeg_concat(
                self._ffmpeg.ffmpeg, list_file, output, playlist.duration or None
            )
        except FFmpegError as exc:
            raise DownloadFailed(str(exc)) from exc


    # ---- 直播录制 ----

    async def _record_live(self, ctx: DownloadContext, analysis: HlsAnalysis) -> Path:
        """轮询播放列表录制新分片，直到用户完成、到达时长上限或出现 ENDLIST。"""
        reporter = _Reporter(ctx, 0)
        key_cache: dict[str, bytes] = {}
        video_dir = ctx.tmp_dir / "live_video"
        audio_dir = ctx.tmp_dir / "live_audio" if analysis.audio_playlist else None

        tasks = [
            asyncio.create_task(
                self._record_track(ctx, analysis.media, video_dir, reporter, key_cache),
                name="hls-live-video",
            )
        ]
        if analysis.audio_playlist is not None and audio_dir is not None:
            tasks.append(
                asyncio.create_task(
                    self._record_track(
                        ctx, analysis.audio_playlist, audio_dir, reporter, key_cache
                    ),
                    name="hls-live-audio",
                )
            )
        await asyncio.gather(*tasks)

        ctx.phase("postprocessing")
        destination = self._destination(ctx.task)
        await self._merge_live(ctx, analysis, video_dir, audio_dir, destination)
        return destination

    async def _record_track(
        self,
        ctx: DownloadContext,
        playlist: MediaPlaylist,
        directory: Path,
        reporter: _Reporter,
        key_cache: dict[str, bytes],
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".m4s" if playlist.has_map else ".ts"
        captured: set[int] = set()
        recorded = 0.0
        failures = 0
        current = playlist

        while True:
            if ctx.is_cancelled():
                raise DownloadCancelled()
            if ctx.is_paused():
                raise DownloadPaused()
            if ctx.is_finishing():
                break
            if self._limit_reached(ctx, recorded):
                break

            await self._download_init(ctx, current, directory)
            for segment in current.segments:
                if segment.sequence in captured:
                    continue
                if ctx.is_cancelled():
                    raise DownloadCancelled()
                if ctx.is_paused():
                    raise DownloadPaused()
                if ctx.is_finishing() or self._limit_reached(ctx, recorded):
                    break

                path = directory / f"seg_{segment.sequence:08d}{suffix}"
                if path.exists() and path.stat().st_size > 0:
                    captured.add(segment.sequence)
                    continue
                data = await self._fetch_segment(ctx, segment, key_cache)
                _atomic_write(path, data)
                captured.add(segment.sequence)
                recorded += segment.duration
                reporter.live_add(len(data), recorded)

            if ctx.is_finishing() or self._limit_reached(ctx, recorded):
                break
            if current.endlist:
                LOGGER.info("直播播放列表出现 ENDLIST，结束录制")
                break

            await self._sleep(ctx, max(1.0, current.target_duration * 0.7))
            try:
                current = await self._load_playlist(ctx, playlist.url)
                failures = 0
            except (PlaylistError, HttpError, OSError) as exc:
                failures += 1
                LOGGER.warning("直播播放列表获取失败(%d): %s", failures, exc)
                if failures >= 8:
                    raise DownloadFailed(f"直播播放列表连续获取失败：{exc}") from exc
                await self._sleep(ctx, 2.0)

    async def _load_playlist(self, ctx: DownloadContext, url: str) -> MediaPlaylist:
        text = await ctx.http.fetch_text(url, ctx.task.headers)
        parsed = parse_playlist(text, url)
        if not isinstance(parsed, MediaPlaylist):
            raise DownloadFailed("直播地址返回的是主播放列表")
        return parsed

    def _limit_reached(self, ctx: DownloadContext, recorded: float) -> bool:
        return ctx.max_live_seconds > 0 and recorded >= ctx.max_live_seconds

    async def _sleep(self, ctx: DownloadContext, seconds: float) -> None:
        """可被暂停 / 取消 / 完成打断的等待。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if ctx.is_cancelled() or ctx.is_paused() or ctx.is_finishing():
                return
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    async def _merge_live(
        self,
        ctx: DownloadContext,
        analysis: HlsAnalysis,
        video_dir: Path,
        audio_dir: Path | None,
        destination: Path,
    ) -> None:
        work = ctx.tmp_dir / "merge"
        work.mkdir(parents=True, exist_ok=True)
        is_fmp4 = analysis.media.has_map

        video_out = work / ("video.mp4" if is_fmp4 else "video.ts")
        await self._concat_live_track(ctx, video_dir, video_out, is_fmp4)
        source = video_out

        if audio_dir is not None and analysis.audio_playlist is not None:
            audio_fmp4 = analysis.audio_playlist.has_map
            audio_out = work / ("audio.mp4" if audio_fmp4 else "audio.ts")
            await self._concat_live_track(ctx, audio_dir, audio_out, audio_fmp4)
            if self._ffmpeg is None:
                raise DownloadFailed("未找到 ffmpeg，无法合并音视频")
            merged = work / "merged.mp4"
            try:
                await ffmpeg_mux(self._ffmpeg.ffmpeg, video_out, audio_out, merged)
            except FFmpegError as exc:
                raise DownloadFailed(str(exc)) from exc
            source = merged

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

    async def _concat_live_track(
        self, ctx: DownloadContext, directory: Path, output: Path, is_fmp4: bool
    ) -> None:
        suffix = ".m4s" if is_fmp4 else ".ts"
        files = sorted(directory.glob(f"seg_*{suffix}"), key=lambda item: item.name)
        if not files:
            raise DownloadFailed("录制时间太短，没有录到任何分片")
        if is_fmp4:
            with output.open("wb") as handle:
                init_path = directory / "init.mp4"
                if init_path.exists():
                    handle.write(init_path.read_bytes())
                for item in files:
                    handle.write(item.read_bytes())
            return
        if self._ffmpeg is None:
            raise DownloadFailed("未找到 ffmpeg，无法合并直播分片")
        list_file = ctx.tmp_dir / f"{directory.name}_concat.txt"
        list_file.write_text(
            "\n".join(_ffconcat_line(item) for item in files), encoding="utf-8"
        )
        try:
            await ffmpeg_concat(self._ffmpeg.ffmpeg, list_file, output)
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
        stem = title[: -len(suffix)] if suffix in MEDIA_SUFFIXES else title
        directory = Path(task.output_dir)
        return unique_path(directory, f"{stem or 'video'}.{container}")


def _atomic_write(path: Path, data: bytes) -> None:
    """先写 .part 再改名，避免半截分片被当成已完成。"""
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
    """生成 ffconcat 的 file 行，转义单引号。"""
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'"