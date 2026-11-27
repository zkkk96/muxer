"""引擎运行参数，由 UI 层的 AppConfig 转换而来。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Muxer/0.1"
)


@dataclass(frozen=True)
class EngineSettings:
    """引擎启动时冻结的一份配置，运行期修改走命令接口。"""

    db_path: Path
    tasks_root: Path
    task_concurrency: int = 2
    segment_concurrency: int = 16
    rate_limit_kbps: int = 0
    ffmpeg_path: str = ""
    live_max_seconds: float = 0.0
    user_agent: str = DEFAULT_USER_AGENT
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    retries: int = 5
    progress_interval: float = 0.2
    progress_save_interval: float = 5.0