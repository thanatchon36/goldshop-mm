"""Rolling σ estimate: EWMA of 1-second mid log-return variance, in bps/√s."""

from __future__ import annotations

import math


class EwmaVolatility:
    def __init__(self, halflife_s: float, warmup_s: float) -> None:
        self.halflife_s = halflife_s
        self.warmup_s = warmup_s
        self._var: float | None = None  # bps² per second
        self._last_ms: int | None = None
        self._last_mid = 0.0
        self._observed_s = 0.0

    def update(self, ts_ms: int, mid: float) -> None:
        """Feed mid prices in time order; samples at most once per second."""
        if mid <= 0:
            return
        if self._last_ms is None:
            self._last_ms, self._last_mid = ts_ms, mid
            return
        dt = (ts_ms - self._last_ms) / 1000
        if dt < 1.0:
            return
        r_bps = math.log(mid / self._last_mid) * 1e4
        obs = r_bps * r_bps / dt  # normalise to a 1-second variance
        alpha = 1 - 0.5 ** (dt / self.halflife_s)
        self._var = obs if self._var is None else (1 - alpha) * self._var + alpha * obs
        self._observed_s += dt
        self._last_ms, self._last_mid = ts_ms, mid

    @property
    def ready(self) -> bool:
        return self._var is not None and self._observed_s >= self.warmup_s

    @property
    def sigma_bps(self) -> float:
        """σ of 1-second returns in bps (0 until the first sample)."""
        return math.sqrt(self._var) if self._var is not None else 0.0
