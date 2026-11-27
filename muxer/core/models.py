"""内核数据模型：任务状态、任务记录、任务快照、探针结果。

本模块不依赖 Qt，便于在纯 asyncio 环境下单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """任务状态机的取值。"""

    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELED = "canceled"


ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.QUEUED, TaskStatus.PROBING, TaskStatus.DOWNLOADING, TaskStatus.POSTPROCESSING}
)
RESUMABLE_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.PAUSED, TaskStatus.FAILED})
def now_iso() -> str:
    """统一的本地时间戳格式（毫秒级，保证同秒创建的排序稳定）。"""
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass
class TaskRecord:
    """持久化的任务记录，字段与 SQLite 表一一对应。"""

    id: str
    url: str
    title: str
    protocol: str
    status: TaskStatus
    output_dir: str
    bytes_done: int = 0
    bytes_total: int = 0
    segments_done: int = 0
    segments_total: int = 0
    live_seconds: float = 0.0
    output_path: str = ""
    error: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    archived: bool = False

    @property
    def progress(self) -> float:
        """0.0 - 1.0；总长度未知时按已完成量估算为 0。"""
        if self.status is TaskStatus.COMPLETED:
            return 1.0
        if self.segments_total > 0:
            return min(1.0, self.segments_done / self.segments_total)
        if self.bytes_total <= 0:
            return 0.0
        return min(1.0, self.bytes_done / self.bytes_total)

    @property
    def live(self) -> bool:
        return bool(self.options.get("live"))

    @property
    def quality(self) -> str:
        return str(self.options.get("quality", ""))

    @property
    def container(self) -> str:
        return str(self.options.get("container", ""))

    def to_snapshot(self, speed_bps: float = 0.0, eta_seconds: int | None = None) -> TaskSnapshot:
        """生成不可变快照，供 UI 线程安全消费。"""
        return TaskSnapshot(
            id=self.id,
            url=self.url,
            title=self.title,
            protocol=self.protocol,
            status=self.status,
            progress=self.progress,
            bytes_done=self.bytes_done,
            bytes_total=self.bytes_total,
            segments_done=self.segments_done,
            segments_total=self.segments_total,
            speed_bps=speed_bps,
            eta_seconds=eta_seconds,
            output_dir=self.output_dir,
            output_path=self.output_path,
            error=self.error,
            live=self.live,
            live_seconds=self.live_seconds,
            headers=dict(self.headers),
            quality=self.quality,
            container=self.container,
            created_at=self.created_at,
            updated_at=self.updated_at,
            archived=self.archived,
        )


@dataclass(frozen=True)
class TaskSnapshot:
    """跨线程传递的任务状态快照。"""

    id: str
    url: str
    title: str
    protocol: str
    status: TaskStatus
    progress: float
    bytes_done: int
    bytes_total: int
    segments_done: int
    segments_total: int
    speed_bps: float
    eta_seconds: int | None
    output_dir: str
    output_path: str
    error: str
    quality: str
    container: str
    created_at: str
    updated_at: str
    archived: bool = False
    live: bool = False
    live_seconds: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VariantOption:
    """HLS 码率变体，供界面选择。"""

    uri: str
    label: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""


@dataclass(frozen=True)
class ProbeResult:
    """URL 探测结果。"""

    request_id: str
    url: str
    title: str
    protocol: str
    supported: bool
    size_bytes: int | None = None
    content_type: str = ""
    message: str = ""
    variants: tuple[VariantOption, ...] = ()
    duration_seconds: float | None = None
    encrypted: bool = False
    live: bool = False