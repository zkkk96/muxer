"""文件命名、路径与磁盘空间工具。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TRAILING_DOTS = re.compile(r"[. ]+$")
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def sanitize_filename(name: str, fallback: str = "download", max_length: int = 120) -> str:
    """清掉 Windows 非法字符与保留名，并限制长度。"""
    cleaned = INVALID_CHARS.sub("_", name or "").strip()
    cleaned = TRAILING_DOTS.sub("", cleaned)
    if not cleaned:
        return fallback
    stem = Path(cleaned).stem
    if stem.upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) <= max_length:
        return cleaned
    suffix = Path(cleaned).suffix
    keep = max(1, max_length - len(suffix))
    return f"{Path(cleaned).stem[:keep]}{suffix}"


def unique_path(directory: Path, filename: str) -> Path:
    """目标文件已存在时追加 (1)、(2) 后缀。"""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while True:
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def ensure_space(directory: Path, needed_bytes: int) -> None:
    """磁盘剩余空间不足时抛出 OSError，供调度器转成任务失败。"""
    if needed_bytes <= 0:
        return
    usage = shutil.disk_usage(str(directory))
    if usage.free < needed_bytes:
        raise OSError(
            f"磁盘空间不足：需要 {needed_bytes / 1024 / 1024:.0f} MB，"
            f"剩余 {usage.free / 1024 / 1024:.0f} MB"
        )