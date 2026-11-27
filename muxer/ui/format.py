"""UI 展示用的格式化工具。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

PROTOCOL_LABELS = {
    "hls": "HLS",
    "dash": "DASH",
    "flv": "FLV",
    "direct": "直链",
}


def protocol_label(protocol: str) -> str:
    """协议的可读名称。"""
    return PROTOCOL_LABELS.get(protocol, protocol.upper())


def format_bytes(value: int | None) -> str:
    """字节数转人类可读大小。"""
    if value is None or value < 0:
        return "--"
    if value < 1024:
        return f"{value} B"
    size = float(value)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def format_speed(bytes_per_second: float) -> str:
    """速度展示；低于 1 B/s 视为空闲。"""
    if bytes_per_second < 1:
        return "--"
    return f"{format_bytes(int(bytes_per_second))}/s"


def format_eta(seconds: int | None) -> str:
    """剩余时间展示。"""
    if seconds is None or seconds <= 0:
        return "--"
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def format_duration(seconds: float | None) -> str:
    """时长展示：6 秒 -> 00:06，3725 秒 -> 1:02:05。"""
    if seconds is None or seconds <= 0:
        return "--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def format_size_pair(done: int, total: int) -> str:
    """进度大小：已知总量时显示 已完成 / 总量。"""
    if total > 0:
        return f"{format_bytes(done)} / {format_bytes(total)}"
    return format_bytes(done)


def parse_timestamp(value: str) -> datetime | None:
    """解析 ISO 时间戳，失败返回 None。"""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def time_group(value: str) -> str:
    """历史页分组：今天 / 昨天 / 具体日期。"""
    moment = parse_timestamp(value)
    if moment is None:
        return "更早"
    today = date.today()
    if moment.date() == today:
        return "今天"
    if moment.date() == today - timedelta(days=1):
        return "昨天"
    return moment.strftime("%Y-%m-%d")


def time_clock(value: str) -> str:
    """只显示时:分。"""
    moment = parse_timestamp(value)
    return moment.strftime("%H:%M") if moment else ""