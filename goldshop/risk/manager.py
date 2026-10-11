"""Risk manager: hard limits with authority over the strategy and Telegram commands.

Pure, time-driven logic (exchange event time in ms) so it behaves identically in
paper, backtest, testnet and live. It decides; the engine acts on its decisions.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from goldshop.config import RiskConfig
from goldshop.execution.orders import Side
from goldshop.notify.telegram import Priority

log = logging.getLogger(__name__)

DAY_MS = 86_400_000

AlertFn = Callable[[str, Priority], None]


@dataclass(frozen=True)
class OrderCheck:
    ok: bool
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, alert: AlertFn | None = None) -> None:
        self.cfg = cfg
        self._alert = alert or (lambda text, prio: None)
        self.daily_halt_until_ms = 0       # > now → halted by daily loss limit
        self.cb_until_ms = 0               # > now → circuit breaker active
        self._cb_active = False
        self.manual_pause = False
        self.funding_reduce_only: Side | None = None  # side we may NOT increase
        self._mids: deque[tuple[int, float]] = deque()

    # ---- state ----------------------------------------------------------------

    def daily_halted(self, now_ms: int) -> bool:
        if self.daily_halt_until_ms and now_ms >= self.daily_halt_until_ms:
            self.daily_halt_until_ms = 0
            self._alert("🟢 New UTC day — daily loss halt lifted, quoting may resume", Priority.HIGH)
        return self.daily_halt_until_ms > now_ms

    def can_quote(self, now_ms: int) -> tuple[bool, str]:
        if self.daily_halted(now_ms):
            return False, "daily loss limit (halted until 00:00 UTC)"
        if self.manual_pause:
            return False, "paused by command"
        self._update_cb(now_ms)
        if self._cb_active:
            return False, "volatility circuit breaker"
        return True, "ok"

    # ---- inputs ---------------------------------------------------------------

    def on_mid(self, now_ms: int, mid: float) -> None:
        """Circuit breaker: price range within the window ≥ X% → stop quoting for cooldown."""
        w = self._mids
        w.append((now_ms, mid))
        cutoff = now_ms - self.cfg.circuit_breaker_window_s * 1000
        while w and w[0][0] < cutoff:
            w.popleft()
        lo = min(m for _, m in w)
        hi = max(m for _, m in w)
        move_pct = (hi - lo) / lo * 100
        if move_pct >= self.cfg.circuit_breaker_pct:
            self.cb_until_ms = now_ms + int(self.cfg.circuit_breaker_cooldown_s * 1000)
            if not self._cb_active:
                self._cb_active = True
                self._alert(
                    f"⛔ Circuit breaker: price moved {move_pct:.2f}% within "
                    f"{self.cfg.circuit_breaker_window_s:g}s — quoting stopped for "
                    f"{self.cfg.circuit_breaker_cooldown_s:g}s", Priority.HIGH)
        self._update_cb(now_ms)

    def _update_cb(self, now_ms: int) -> None:
        if self._cb_active and now_ms >= self.cb_until_ms:
            self._cb_active = False
            self._alert("🟢 Circuit breaker cleared — quoting resumes", Priority.HIGH)

    def on_funding_rate(self, rate: float, position_qty: float) -> None:
        """Positive rate: longs pay. Funding against us beyond the threshold → only reduce."""
        side: Side | None = None
        if position_qty > 0 and rate >= self.cfg.funding_guard_rate:
            side = Side.BUY
        elif position_qty < 0 and rate <= -self.cfg.funding_guard_rate:
            side = Side.SELL
        if side != self.funding_reduce_only:
            self.funding_reduce_only = side
            if side:
                self._alert(f"💸 Funding guard: rate {rate:+.4%} against our position "
                            f"({position_qty:+g}) — only reducing inventory", Priority.HIGH)
            else:
                self._alert("🟢 Funding guard cleared", Priority.HIGH)

    def check_daily_loss(self, now_ms: int, net_today: float) -> bool:
        """True exactly when the limit is newly breached (caller must cancel + flatten)."""
        if self.daily_halt_until_ms > now_ms:
            return False
        if net_today <= -self.cfg.daily_loss_limit_usdt:
            self.daily_halt_until_ms = (now_ms // DAY_MS + 1) * DAY_MS
            self._alert(f"🛑 Daily loss limit hit: net today {net_today:+.2f} USDT ≤ "
                        f"−{self.cfg.daily_loss_limit_usdt:g} — cancelling all, flattening, "
                        "halted until 00:00 UTC", Priority.HIGH)
            return True
        return False

    def check_order(self, side: Side, price: float, qty: float, position_qty: float,
                    working_same_side_qty: float) -> OrderCheck:
        if side is self.funding_reduce_only:
            return OrderCheck(False, "funding guard: side would increase position")
        notional = price * qty
        if notional > self.cfg.max_order_notional_usdt + 1e-9:
            return OrderCheck(False, f"order notional {notional:.2f} > max_order_notional_usdt")
        worst = position_qty + side.sign * (working_same_side_qty + qty)
        reduces = abs(worst) < abs(position_qty)
        if not reduces and abs(worst) * price > self.cfg.max_position_notional_usdt + 1e-9:
            return OrderCheck(False, f"position would reach {abs(worst) * price:.2f} USDT "
                                     "> max_position_notional_usdt")
        return OrderCheck(True)

    # ---- commands -------------------------------------------------------------

    def pause(self) -> str:
        self.manual_pause = True
        return "⏸ Paused: orders cancelled, position kept. /resume to continue."

    def resume(self, now_ms: int) -> str:
        if self.daily_halted(now_ms):
            return "❌ /resume refused: daily loss limit is active until 00:00 UTC."
        self.manual_pause = False
        if self._cb_active:
            return "▶️ Resumed, but the circuit breaker is still active; quoting restarts when it clears."
        return "▶️ Resumed."
