"""协议源抽象：调度器只关心 download() 的返回值与三种控制异常。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from muxer.core.http import HttpClient
from muxer.core.models import TaskRecord
from muxer.core.ratelimit import RateLimiter


class DownloadPaused(Exception):
    """用户暂停；临时文件保留，可继续。"""


class DownloadCancelled(Exception):
    """用户取消；调度器负责清理临时文件与记录。"""


class DownloadFailed(Exception):
    """不可恢复的失败。"""


@dataclass
class DownloadContext:
    """一次下载所需的全部依赖与回调。"""

    task: TaskRecord
    http: HttpClient
    tmp_dir: Path
    rate_limiter: RateLimiter
    is_paused: Callable[[], bool]
    is_cancelled: Callable[[], bool]
    # report(已下载字节, 总字节, 已完成分片, 总分片)
    report: Callable[[int, int, int, int], None]
    # phase("postprocessing") 让调度器把状态切到"合并中"
    phase: Callable[[str], None]
    # 直播已录制秒数
    report_live: Callable[[float], None]
    # 用户点击"完成录制"
    is_finishing: Callable[[], bool]
    # 直播最长录制秒数，0 表示不限
    max_live_seconds: float = 0.0


class MediaSource(Protocol):
    """协议源接口，HLS / DASH 在 M3、M4 按同一接口实现。"""

    protocol: str

    async def download(self, ctx: DownloadContext) -> Path:
        """执行下载，返回最终文件路径。"""
        ...