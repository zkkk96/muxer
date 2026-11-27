"""引擎推送给 UI 的事件对象。

全部为冻结数据类，保证跨线程传递时不会被 UI 侧意外修改。
"""

from __future__ import annotations

from dataclasses import dataclass

from muxer.core.models import ProbeResult, TaskSnapshot


@dataclass(frozen=True)
class TasksLoaded:
    """引擎启动时从数据库恢复的全部任务（含已归档）。"""

    tasks: tuple[TaskSnapshot, ...]


@dataclass(frozen=True)
class TaskUpserted:
    """任务被创建或状态 / 进度发生变化。"""

    task: TaskSnapshot


@dataclass(frozen=True)
class TaskRemoved:
    """任务被取消并删除。"""

    task_id: str


@dataclass(frozen=True)
class HistoryCleared:
    """历史记录已被清空。"""


@dataclass(frozen=True)
class ProbeFinished:
    """URL 探测成功。"""

    result: ProbeResult


@dataclass(frozen=True)
class ProbeFailed:
    """URL 探测失败。"""

    request_id: str
    url: str
    message: str


@dataclass(frozen=True)
class EngineLog:
    """引擎内部日志，UI 可用于日志抽屉（M7）。"""

    level: str
    message: str