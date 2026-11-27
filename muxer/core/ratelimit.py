"""全局令牌桶限速。

所有任务共享同一个实例，因此设置页的"全局限速"是真正的全局上限。
限速为 0 时 acquire() 直接返回，不产生任何开销。
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """按字节计量的令牌桶。"""

    def __init__(self, bytes_per_second: int):
        self._rate = max(0, bytes_per_second)
        self._burst = float(self._rate) if self._rate else 0.0
        self._tokens = self._burst
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    def set_rate(self, bytes_per_second: int) -> None:
        """运行期调整速率，任务进行中立即生效。"""
        self._rate = max(0, bytes_per_second)
        self._burst = float(self._rate) if self._rate else 0.0
        self._tokens = min(self._tokens, self._burst) if self._rate else 0.0

    async def acquire(self, amount: int) -> None:
        """申请 amount 字节的配额，不足时等待。"""
        if not self.enabled or amount <= 0:
            return

        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
            self._last = now
            deficit = amount - self._tokens
            if deficit > 0:
                # 持有锁等待，保证多任务并发时总速率不超过上限
                await asyncio.sleep(deficit / self._rate)
                self._tokens = 0.0
            else:
                self._tokens -= amount