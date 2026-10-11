"""Markout: how the mid moved after each fill, to measure adverse selection.

markout_bps = side × (mid(t_fill + h) − fill_price) / fill_price × 1e4
Positive means the fill was good for us (price moved our way afterwards).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

DEFAULT_HORIZONS_MS: tuple[int, ...] = (1_000, 5_000, 30_000)


@dataclass(frozen=True)
class MarkoutSample:
    fill_ts_ms: int
    horizon_ms: int
    bps: float


class MarkoutTracker:
    def __init__(self, horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS,
                 keep_ms: int = 48 * 3_600_000) -> None:
        self.horizons_ms = tuple(sorted(horizons_ms))
        self.keep_ms = keep_ms
        # (fill_ts, sign, price, index of next horizon) — fills arrive in time order
        self._pending: deque[list[float]] = deque()
        self.samples: deque[MarkoutSample] = deque()

    def add_fill(self, ts_ms: int, side_sign: int, price: float) -> None:
        self._pending.append([ts_ms, side_sign, price, 0])

    def on_mid(self, ts_ms: int, mid: float) -> list[MarkoutSample]:
        new: list[MarkoutSample] = []
        for p in self._pending:
            fill_ts, sign, px, i = int(p[0]), p[1], p[2], int(p[3])
            while i < len(self.horizons_ms) and ts_ms >= fill_ts + self.horizons_ms[i]:
                s = MarkoutSample(fill_ts, self.horizons_ms[i], sign * (mid - px) / px * 1e4)
                new.append(s)
                i += 1
            p[3] = i
        while self._pending and self._pending[0][3] >= len(self.horizons_ms):
            self._pending.popleft()
        self.samples.extend(new)
        while self.samples and self.samples[0].fill_ts_ms < ts_ms - self.keep_ms:
            self.samples.popleft()
        return new

    def averages(self, since_ms: int = 0) -> dict[int, float | None]:
        out: dict[int, float | None] = {}
        for h in self.horizons_ms:
            xs = [s.bps for s in self.samples if s.horizon_ms == h and s.fill_ts_ms >= since_ms]
            out[h] = sum(xs) / len(xs) if xs else None
        return out
