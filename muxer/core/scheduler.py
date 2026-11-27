"""任务调度器：队列、并发、状态机与持久化。

纯 asyncio 实现，不依赖 Qt；引擎线程只是它的宿主与事件转发层。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from muxer.core.events import (
    HistoryCleared,
    ProbeFailed,
    ProbeFinished,
    TaskRemoved,
    TasksLoaded,
    TaskUpserted,
)
from muxer.core.ffmpeg import FFmpeg, locate as locate_ffmpeg
from muxer.core.http import HttpClient, filename_from_url
from muxer.core.models import (
    ACTIVE_STATUSES,
    RESUMABLE_STATUSES,
    ProbeResult,
    TaskRecord,
    TaskStatus,
    now_iso,
)
from muxer.core.progress import SpeedMeter
from muxer.core.ratelimit import RateLimiter
from muxer.core.settings import EngineSettings
from muxer.core.storage import Storage
from muxer.protocols.base import (
    DownloadCancelled,
    DownloadContext,
    DownloadFailed,
    DownloadPaused,
)
from muxer.protocols.dash.analyzer import analyze as analyze_dash
from muxer.protocols.dash.analyzer import estimate_bytes as estimate_dash_bytes
from muxer.protocols.dash.source import DashSource
from muxer.protocols.flv.source import FlvSource
from muxer.protocols.direct import DirectSource
from muxer.protocols.hls.analyzer import analyze as analyze_hls
from muxer.protocols.hls.analyzer import estimate_bytes
from muxer.protocols.hls.source import HLSSource

LOGGER = logging.getLogger(__name__)

SUPPORTED_PROTOCOLS = frozenset({"direct", "flv", "hls", "dash"})
@dataclass
class _Runtime:
    """单个任务在内存中的运行期状态。"""

    record: TaskRecord
    meter: SpeedMeter = field(default_factory=SpeedMeter)
    pause_requested: bool = False
    cancel_requested: bool = False
    finish_requested: bool = False
    queued: bool = False
    last_emit: float = 0.0
    last_save: float = 0.0


class Scheduler:
    """负责任务排队、状态迁移与事件推送。"""

    def __init__(
        self,
        settings: EngineSettings,
        http: HttpClient,
        storage: Storage,
        emit: Callable[[object], None],
    ):
        self._settings = settings
        self._http = http
        self._storage = storage
        self._emit = emit
        self._tasks: dict[str, _Runtime] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._rate = RateLimiter(settings.rate_limit_kbps * 1024)
        self._ffmpeg: FFmpeg | None = None
        self._sources = {
            "direct": DirectSource(),
            "flv": FlvSource(None),
            "hls": HLSSource(None, settings.segment_concurrency),
            "dash": DashSource(None, settings.segment_concurrency),
        }
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 生命周期 ----

    async def start(self) -> None:
        """恢复历史任务、标记上次未完成的任务为暂停，然后拉起工作协程。"""
        self._loop = asyncio.get_running_loop()
        snapshots = []
        for record in self._storage.load_all():
            if record.status in ACTIVE_STATUSES:
                record.status = TaskStatus.PAUSED
                record.error = "上次退出时任务未完成，可继续"
                record.updated_at = now_iso()
                self._storage.save(record)
            snapshots.append(record.to_snapshot())
            if record.status is not TaskStatus.CANCELED and not record.archived:
                self._tasks[record.id] = _Runtime(record)

        self._ffmpeg = locate_ffmpeg(self._settings.ffmpeg_path)
        if self._ffmpeg is None:
            LOGGER.warning("未找到 ffmpeg，HLS 合并功能不可用")
        else:
            LOGGER.info("ffmpeg: %s", self._ffmpeg.ffmpeg)
        self._sources["hls"] = HLSSource(self._ffmpeg, self._settings.segment_concurrency)
        self._sources["dash"] = DashSource(self._ffmpeg, self._settings.segment_concurrency)
        self._sources["flv"] = FlvSource(self._ffmpeg)

        self._emit(TasksLoaded(tuple(snapshots)))
        worker_count = max(1, self._settings.task_concurrency)
        self._workers = [asyncio.create_task(self._worker(), name=f"muxer-worker-{i}") for i in range(worker_count)]
        LOGGER.info("调度器启动：恢复 %d 个任务，工作协程 %d 个", len(snapshots), worker_count)

    async def shutdown(self) -> None:
        """暂停进行中的任务并结束工作协程，保证下次启动可续传。"""
        for runtime in self._tasks.values():
            if runtime.record.status in ACTIVE_STATUSES:
                runtime.pause_requested = True
        if self._workers:
            await asyncio.sleep(0.4)
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
        for runtime in self._tasks.values():
            if runtime.record.status in ACTIVE_STATUSES:
                runtime.record.status = TaskStatus.PAUSED
                runtime.record.updated_at = now_iso()
                self._storage.save(runtime.record)
        LOGGER.info("调度器已停止")

    # ---- 命令入口（均在事件循环线程内调用） ----

    def submit(self, payload: dict) -> None:
        """创建任务并入队。"""
        url = str(payload.get("url", "")).strip()
        if not url:
            LOGGER.warning("忽略空 URL 的下载请求")
            return
        record = TaskRecord(
            id=uuid.uuid4().hex,
            url=url,
            title=str(payload.get("title") or filename_from_url(url) or "未命名媒体"),
            protocol=str(payload.get("protocol") or "direct"),
            status=TaskStatus.QUEUED,
            output_dir=str(payload.get("output_dir") or "."),
            bytes_total=int(payload.get("size") or 0),
            headers=dict(payload.get("headers") or {}),
            options={
                "quality": str(payload.get("quality") or ""),
                "container": str(payload.get("container") or ""),
                "variant": str(payload.get("variant") or ""),
                "live": bool(payload.get("live")),
            },
        )
        runtime = _Runtime(record)
        self._tasks[record.id] = runtime
        self._storage.save(record)
        self._emit_snapshot(runtime)
        self._enqueue(runtime)
        LOGGER.info("任务入队 %s: %s", record.id, record.title)

    def pause(self, task_id: str) -> None:
        runtime = self._tasks.get(task_id)
        if runtime is None:
            return
        if runtime.record.status is TaskStatus.QUEUED:
            self._set_status(runtime, TaskStatus.PAUSED)
        elif runtime.record.status is TaskStatus.DOWNLOADING:
            runtime.pause_requested = True

    def resume(self, task_id: str) -> None:
        runtime = self._tasks.get(task_id)
        if runtime is None or runtime.record.status not in RESUMABLE_STATUSES:
            return
        runtime.pause_requested = False
        runtime.cancel_requested = False
        runtime.record.error = ""
        self._set_status(runtime, TaskStatus.QUEUED)
        self._enqueue(runtime)

    def stop(self, task_id: str) -> None:
        """完成直播录制：源会在下一个分片边界退出并进入合并。"""
        runtime = self._tasks.get(task_id)
        if runtime is None:
            return
        if runtime.record.status is TaskStatus.DOWNLOADING:
            runtime.finish_requested = True

    def cancel(self, task_id: str) -> None:
        runtime = self._tasks.get(task_id)
        if runtime is None:
            return
        if runtime.record.status in (TaskStatus.DOWNLOADING, TaskStatus.POSTPROCESSING):
            # 下载中在分片边界生效；合并中需要等 ffmpeg 结束后由 _run 统一收尾
            runtime.cancel_requested = True
        else:
            self._remove(runtime)

    def archive_completed(self) -> None:
        """归档已完成任务：任务页移除，历史页保留。"""
        self._storage.archive_completed()
        for runtime in list(self._tasks.values()):
            if runtime.record.status is TaskStatus.COMPLETED:
                self._tasks.pop(runtime.record.id, None)
                self._emit(TaskRemoved(runtime.record.id))

    def clear_history(self) -> None:
        """清空完成 / 失败记录（任务页与历史页同步移除）。"""
        removed = [
            runtime.record.id
            for runtime in self._tasks.values()
            if runtime.record.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
        for task_id in removed:
            self._tasks.pop(task_id, None)
            self._emit(TaskRemoved(task_id))
        deleted = self._storage.clear_finished()
        self._emit(HistoryCleared())
        LOGGER.info("已清空历史记录：%d 条", deleted)

    def set_rate_limit(self, kbps: int) -> None:
        """运行期调整全局限速。"""
        self._rate.set_rate(max(0, kbps) * 1024)

    def probe_and_start(self, payload: dict) -> None:
        """嗅探批量下载：先探测协议与直播状态，再以最佳参数入队。"""
        if self._loop is None:
            return
        self._loop.create_task(self._probe_and_start(payload))

    async def _probe_and_start(self, payload: dict) -> None:
        request_id = str(payload.get("request_id", ""))
        url = str(payload.get("url", "")).strip()
        headers = dict(payload.get("headers") or {})
        output_dir = str(payload.get("output_dir") or ".")
        if not url:
            self._emit(ProbeFailed(request_id=request_id, url=url, message="缺少链接"))
            return

        try:
            info = await self._http.probe(url, headers)
            protocol = info.protocol
            title = info.filename or filename_from_url(info.final_url) or "未命名媒体"
            live = False
            size = info.content_length
            variant = ""
            quality = ""
            container = ""
            variants = ()
            duration = None
            encrypted = False

            if protocol == "hls":
                analysis = await analyze_hls(self._http, url, headers)
                if analysis.sample_aes:
                    raise ValueError("SAMPLE-AES / DRM 加密暂不支持")
                title = analysis.title
                live = analysis.live
                size = estimate_bytes(analysis) or size
                variants = analysis.variants
                variant, quality = _best_variant(analysis.variants)
                container = "mp4"
                duration = analysis.duration or None
                encrypted = analysis.encrypted
            elif protocol == "dash":
                analysis = await analyze_dash(self._http, url, headers)
                if analysis.live:
                    raise ValueError("直播 DASH 暂不支持")
                if analysis.drm:
                    raise ValueError("DRM 保护内容暂不支持")
                title = analysis.title
                size = estimate_dash_bytes(analysis) or size
                variants = analysis.variants
                variant, quality = _best_variant(analysis.variants)
                container = "mp4"
                duration = analysis.duration or None
                encrypted = analysis.drm
            elif protocol == "flv":
                live = info.content_length is None

            self.submit(
                {
                    "url": url,
                    "title": title,
                    "protocol": protocol,
                    "output_dir": output_dir,
                    "size": size or 0,
                    "quality": quality,
                    "container": container,
                    "variant": variant,
                    "live": live,
                    "headers": headers,
                }
            )
            self._emit(
                ProbeFinished(
                    result=ProbeResult(
                        request_id=request_id,
                        url=url,
                        title=title,
                        protocol=protocol,
                        supported=True,
                        size_bytes=size,
                        content_type=info.content_type,
                        message="已加入下载队列",
                        variants=variants,
                        duration_seconds=duration,
                        encrypted=encrypted,
                        live=live,
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - 反馈给 UI
            LOGGER.warning("探测并入队失败 %s: %s", url, exc)
            self._emit(
                ProbeFailed(
                    request_id=request_id,
                    url=url,
                    message=f"加入队列失败：{exc}",
                )
            )

    def probe(self, request_id: str, url: str, headers: dict[str, str] | None = None) -> None:
        """异步探测 URL；结果通过事件返回。"""
        if self._loop is None:
            return
        self._loop.create_task(self._probe(request_id, url, headers or {}))

    def active_count(self) -> int:
        return sum(
            1
            for runtime in self._tasks.values()
            if runtime.record.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING)
        )

    # ---- 内部：探测 ----

    async def _probe(self, request_id: str, url: str, headers: dict[str, str]) -> None:
        try:
            info = await self._http.probe(url, headers)
        except Exception as exc:  # noqa: BLE001 - 任何探测失败都要反馈给 UI
            LOGGER.warning("探测失败 %s: %s", url, exc)
            self._emit(ProbeFailed(request_id=request_id, url=url, message=str(exc)))
            return

        if info.protocol == "hls":
            await self._probe_hls(request_id, url, headers, info.content_type)
            return
        if info.protocol == "dash":
            await self._probe_dash(request_id, url, headers, info.content_type)
            return

        protocol = info.protocol
        supported = protocol in SUPPORTED_PROTOCOLS
        title = info.filename or filename_from_url(info.final_url) or "未命名媒体"
        live = protocol == "flv" and info.content_length is None
        message = "直播 FLV：持续录制，点击任务卡的停止按钮完成并封装" if live else ""
        result = ProbeResult(
            request_id=request_id,
            url=url,
            title=title,
            protocol=protocol,
            supported=supported,
            size_bytes=info.content_length,
            content_type=info.content_type,
            message=message,
            live=live,
        )
        self._emit(ProbeFinished(result))

    async def _probe_dash(
        self, request_id: str, url: str, headers: dict[str, str], content_type: str
    ) -> None:
        """解析 MPD：变体、时长、DRM 与直播判定。"""
        try:
            analysis = await analyze_dash(self._http, url, headers)
        except Exception as exc:  # noqa: BLE001 - 解析失败要反馈给 UI
            LOGGER.warning("DASH 解析失败 %s: %s", url, exc)
            self._emit(
                ProbeFailed(request_id=request_id, url=url, message=f"DASH 解析失败：{exc}")
            )
            return

        if analysis.live:
            supported = False
            message = "检测到直播 DASH，录制功能将在 M5 接入"
        elif analysis.drm:
            supported = False
            message = "检测到 DRM 保护（ContentProtection），暂不支持"
        else:
            supported = True
            message = ""

        result = ProbeResult(
            request_id=request_id,
            url=url,
            title=analysis.title,
            protocol="dash",
            supported=supported,
            size_bytes=estimate_dash_bytes(analysis),
            content_type=content_type,
            message=message,
            variants=analysis.variants,
            duration_seconds=analysis.duration or None,
            encrypted=analysis.drm,
            live=analysis.live,
        )
        self._emit(ProbeFinished(result))

    async def _probe_hls(
        self, request_id: str, url: str, headers: dict[str, str], content_type: str
    ) -> None:
        """解析 m3u8：变体、时长、加密状态与直播判定。"""
        try:
            analysis = await analyze_hls(self._http, url, headers)
        except Exception as exc:  # noqa: BLE001 - 解析失败要反馈给 UI
            LOGGER.warning("HLS 解析失败 %s: %s", url, exc)
            self._emit(
                ProbeFailed(request_id=request_id, url=url, message=f"HLS 解析失败：{exc}")
            )
            return

        if analysis.sample_aes:
            supported = False
            message = "检测到 SAMPLE-AES / DRM 加密，暂不支持"
        elif analysis.live:
            supported = True
            message = "直播流：持续录制，点击任务卡的停止按钮完成并合并"
        elif analysis.encrypted:
            supported = True
            message = "AES-128 加密，将自动解密"
        else:
            supported = True
            message = ""

        result = ProbeResult(
            request_id=request_id,
            url=url,
            title=analysis.title,
            protocol="hls",
            supported=supported,
            size_bytes=estimate_bytes(analysis),
            content_type=content_type,
            message=message,
            variants=analysis.variants,
            duration_seconds=analysis.duration or None,
            encrypted=analysis.encrypted,
            live=analysis.live,
        )
        self._emit(ProbeFinished(result))

    # ---- 内部：调度 ----

    def _enqueue(self, runtime: _Runtime) -> None:
        if runtime.queued:
            return
        runtime.queued = True
        self._queue.put_nowait(runtime.record.id)

    async def _worker(self) -> None:
        while True:
            task_id = await self._queue.get()
            runtime = self._tasks.get(task_id)
            if runtime is None:
                continue
            runtime.queued = False
            if runtime.record.status is not TaskStatus.QUEUED or runtime.cancel_requested:
                continue
            await self._run(runtime)

    async def _run(self, runtime: _Runtime) -> None:
        record = runtime.record
        if runtime.cancel_requested:
            self._remove(runtime)
            return
        if runtime.pause_requested:
            runtime.pause_requested = False
            self._set_status(runtime, TaskStatus.PAUSED)
            return

        record.status = TaskStatus.DOWNLOADING
        record.error = ""
        record.updated_at = now_iso()
        self._storage.save(record)
        runtime.meter.reset(record.bytes_done)
        runtime.last_emit = 0.0
        runtime.last_save = time.monotonic()
        self._emit_snapshot(runtime)

        tmp_dir = Path(self._settings.tasks_root) / record.id
        source = self._sources.get(record.protocol, self._sources["direct"])
        ctx = DownloadContext(
            task=record,
            http=self._http,
            tmp_dir=tmp_dir,
            rate_limiter=self._rate,
            is_paused=lambda: runtime.pause_requested,
            is_cancelled=lambda: runtime.cancel_requested,
            report=lambda done, total, seg_done, seg_total: self._on_progress(
                runtime, done, total, seg_done, seg_total
            ),
            phase=lambda phase: self._set_phase(runtime, phase),
            report_live=lambda seconds: self._on_live(runtime, seconds),
            is_finishing=lambda: runtime.finish_requested,
            max_live_seconds=self._settings.live_max_seconds,
        )

        try:
            output = await source.download(ctx)
        except DownloadPaused:
            runtime.pause_requested = False
            self._set_status(runtime, TaskStatus.PAUSED)
        except DownloadCancelled:
            self._remove(runtime)
        except DownloadFailed as exc:
            self._set_status(runtime, TaskStatus.FAILED, str(exc))
        except asyncio.CancelledError:
            self._set_status(runtime, TaskStatus.PAUSED)
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，避免工作协程被异常杀死
            LOGGER.exception("任务异常 %s", record.id)
            self._set_status(runtime, TaskStatus.FAILED, f"内部错误：{exc}")
        else:
            if runtime.cancel_requested:
                # 合并期间点取消：删掉刚生成的文件并移除任务
                try:
                    Path(output).unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("取消后清理输出文件失败: %s", output)
                self._remove(runtime)
                return
            record.output_path = str(output)
            record.bytes_done = record.bytes_total or record.bytes_done
            self._set_status(runtime, TaskStatus.COMPLETED)
            _remove_dir(tmp_dir)

    # ---- 内部：状态与进度 ----

    def _on_progress(
        self, runtime: _Runtime, done: int, total: int, seg_done: int, seg_total: int
    ) -> None:
        record = runtime.record
        record.bytes_done = done
        if total > 0:
            record.bytes_total = total
        if seg_total > 0:
            record.segments_done = seg_done
            record.segments_total = seg_total
        runtime.meter.update(done)

        now = time.monotonic()
        if record.segments_total > 0:
            finished = record.segments_done >= record.segments_total
        else:
            finished = record.bytes_total > 0 and done >= record.bytes_total
        if now - runtime.last_emit < self._settings.progress_interval and not finished:
            return
        runtime.last_emit = now
        self._emit_snapshot(runtime)

        if now - runtime.last_save >= self._settings.progress_save_interval:
            runtime.last_save = now
            record.updated_at = now_iso()
            self._storage.save(record)

    def _on_live(self, runtime: _Runtime, seconds: float) -> None:
        """直播录制时长：单调递增并节流推送。"""
        if seconds <= runtime.record.live_seconds:
            return
        runtime.record.live_seconds = seconds
        now = time.monotonic()
        if now - runtime.last_emit < self._settings.progress_interval:
            return
        runtime.last_emit = now
        self._emit_snapshot(runtime)

    def _set_phase(self, runtime: _Runtime, phase: str) -> None:
        """源在开始合并时调用，把状态切到 postprocessing。"""
        if phase == "postprocessing" and runtime.record.status is TaskStatus.DOWNLOADING:
            self._set_status(runtime, TaskStatus.POSTPROCESSING)

    def _set_status(self, runtime: _Runtime, status: TaskStatus, error: str | None = None) -> None:
        runtime.record.status = status
        if error is not None:
            runtime.record.error = error
        runtime.record.updated_at = now_iso()
        self._storage.save(runtime.record)
        self._emit_snapshot(runtime)

    def _emit_snapshot(self, runtime: _Runtime) -> None:
        record = runtime.record
        speed = 0.0
        eta: int | None = None
        if record.status is TaskStatus.DOWNLOADING:
            speed = runtime.meter.speed()
            if record.segments_total > 0:
                remaining_segments = max(0, record.segments_total - record.segments_done)
                average = record.bytes_done / record.segments_done if record.segments_done else 0
                remaining = int(remaining_segments * average) if average else None
            else:
                remaining = record.bytes_total - record.bytes_done if record.bytes_total else None
            eta = runtime.meter.eta(remaining)
        self._emit(TaskUpserted(record.to_snapshot(speed_bps=speed, eta_seconds=eta)))

    def _remove(self, runtime: _Runtime) -> None:
        record = runtime.record
        self._tasks.pop(record.id, None)
        self._storage.delete(record.id)
        _remove_dir(Path(self._settings.tasks_root) / record.id)
        self._emit(TaskRemoved(record.id))
        LOGGER.info("任务已移除 %s", record.id)


def _remove_dir(path: Path) -> None:
    """忽略清理失败，避免影响状态机。"""
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - 极端情况
        LOGGER.warning("清理临时目录失败 %s: %s", path, exc)


def _best_variant(variants) -> tuple[str, str]:
    """取最高码率变体；媒体列表（无码率）不写入质量标签。"""
    if not variants:
        return "", ""
    best = max(variants, key=lambda item: item.bandwidth)
    if not best.bandwidth and best.label == "源播放列表":
        return best.uri, ""
    quality = best.label.split(" · ")[0]
    return best.uri, quality
