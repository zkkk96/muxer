"""应用配置与路径。

配置文件落在 %LOCALAPPDATA%/Muxer/config.json，与任务数据分开存放。
所有读取异常都回落到默认配置，保证程序总能启动。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from muxer import APP_NAME

LOGGER = logging.getLogger(__name__)


def _app_data_dir() -> Path:
    """返回应用数据目录，Windows 用 LOCALAPPDATA，其余平台用 ~/.muxer。"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / ".muxer"


APP_DATA_DIR = _app_data_dir()
APP_CONFIG_PATH = APP_DATA_DIR / "config.json"
PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
UI_ICONS_DIR = ICONS_DIR / "ui"


class AppConfig(BaseModel):
    """用户可改的全部设置，字段名同时作为配置文件键名。"""

    theme: str = Field(default="system", description="light | dark | system")
    download_dir: str = Field(
        default_factory=lambda: str(Path.home() / "Downloads" / "Muxer"),
        description="默认下载目录",
    )
    task_concurrency: int = Field(default=2, ge=1, le=5, description="同时下载的任务数")
    segment_concurrency: int = Field(default=16, ge=1, le=32, description="单任务分片并发")
    rate_limit_kbps: int = Field(default=0, ge=0, description="全局限速，0 表示不限速")
    live_max_minutes: int = Field(
        default=0, ge=0, le=720, description="直播最长录制分钟数，0 表示不限"
    )
    ffmpeg_path: str = Field(default="", description="用户指定的 ffmpeg 路径，空表示自动探测")
    minimize_to_tray: bool = Field(default=True, description="关闭窗口时最小化到托盘")
    first_run_completed: bool = Field(default=False, description="是否已完成首次启动引导")


def load_config() -> AppConfig:
    """读取配置；文件缺失或损坏时返回默认值并记录一条警告。"""
    if not APP_CONFIG_PATH.exists():
        return AppConfig()
    try:
        payload = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
        return AppConfig.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        LOGGER.warning("配置文件读取失败，使用默认配置: %s", exc)
        return AppConfig()


def save_config(config: AppConfig) -> None:
    """原子写入配置文件，避免中途崩溃留下半截 JSON。"""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = APP_CONFIG_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, APP_CONFIG_PATH)