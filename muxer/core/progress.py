"""速度与剩余时间估算。

用滑动窗口采样，避免瞬时抖动导致速度数字乱跳。
"""

from __future__ import annotations

import time
from collections import deque


class SpeedMeter:
    """基于时间窗口的下载速度计。"""

    def __init__(self, window_seconds: float = 3.0):
        self._window = window_seconds
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self, initial_bytes: int = 0) -> None:
        """开始新一段计量，可带入已有字节数（断点续传时）。"""
        self._samples.clear()
        self._samples.append((time.monotonic(), initial_bytes))

    def update(self, total_bytes: int) -> None:
        """记录一次累计字节数。"""
        now = time.monotonic()
        self._samples.append((now, total_bytes))
        cutoff = now - self._window
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def speed(self) -> float:
        """字节/秒；样本不足时返回 0。"""
        if len(self._samples) < 2:
            return 0.0
        first_time, first_bytes = self._samples[0]
        last_time, last_bytes = self._samples[-1]
        elapsed = last_time - first_time
        if elapsed <= 0.05:
            return 0.0
        return max(0.0, (last_bytes - first_bytes) / elapsed)

    def eta(self, remaining_bytes: int | None) -> int | None:
        """剩余秒数；未知总量或速度为 0 时返回 None。"""
        if remaining_bytes is None or remaining_bytes <= 0:
            return None
        speed = self.speed()
        if speed <= 1:
            return None
        return int(remaining_bytes / speed)