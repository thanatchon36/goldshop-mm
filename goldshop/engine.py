"""Trading engine: market events → σ estimate → strategy quote → risk gate → executor.

Mode-agnostic: the same engine runs paper, backtest, testnet and live; only the
executor differs. Time is the exchange event time of the market data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from goldshop.accounting import Portfolio
from goldshop.binance.symbol import SymbolFilters
from goldshop.config import RiskConfig, StrategyConfig
from goldshop.execution.base import ExecEvent, Executor, Fill, FundingPayment, OrderRejected
from goldshop.execution.orders import Side
from goldshop.market_data.models import BookTicker, MarketEvent, MarkPrice
from goldshop.notify.telegram import Priority
from goldshop.risk.manager import RiskManager
from goldshop.state.markout import MarkoutSample, MarkoutTracker
from goldshop.state.store import StateStore
from goldshop.stats import DAY_MS, Stats
from goldshop.strategy.avellaneda import MarketState, QuoteTarget, StrategyParams, compute_quote
from goldshop.strategy.volatility import EwmaVolatility

log = logging.getLogger(__name__)

AlertFn = Callable[[str, Priority], None]


class Engine:
    def __init__(
        self,
        filters: SymbolFilters,
        params: StrategyParams,
        cfg: StrategyConfig,
        executor: Executor,
        alert: AlertFn | None = None,
        stale: bool = True,
        risk: RiskManager | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.symbol = filters.symbol
        self.filters = filters
        self.params = params
        self.cfg = cfg
        self.executor = executor
        self._alert = alert or (lambda text, prio: None)
        # Without an explicit risk manager use effectively-unbounded limits (backtests of
        # the pure strategy); the runner always passes the configured one.
        self.risk = risk or RiskManager(RiskConfig(
            max_position_notional_usdt=1e18, max_order_notional_usdt=1e18,
            daily_loss_limit_usdt=1e18, circuit_breaker_pct=1e9, funding_guard_rate=1e9))
        self.store = store
        self.portfolio = Portfolio()
        self.markout = MarkoutTracker()
        self.stats = Stats(self.portfolio, self.markout)
        self.vol = EwmaVolatility(cfg.vol_halflife_s, cfg.vol_warmup_s)
        self.book: BookTicker | None = None
        self.mark_px: float | None = None
        self.stale = stale
        self.now_ms = 0
        self.last_event_mono = time.monotonic()
        self.last_quote: QuoteTarget | None = None
        self.quote_status = "starting"
        self.rejects = 0
        self._last_quote_ms = -(10**18)
        self._last_equity_ms = 0
        self._at_limit: str | None = None
        self.on_day_end: Callable[[], None] | None = None  # called before "today" resets
        self.on_markout: Callable[[MarkoutSample], None] | None = None
        self._day_started = False
        executor.on_exec_event(self._on_exec)

    @property
    def mark(self) -> float:
        if self.mark_px is not None:
            return self.mark_px
        return self.book.mid if self.book else 0.0

    # ---- state persistence ------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {"portfolio": self.portfolio.to_dict(), "stats": self.stats.to_dict(),
                "risk": {"daily_halt_until_ms": self.risk.daily_halt_until_ms,
                         "manual_pause": self.risk.manual_pause}}

    def restore_state(self, d: dict[str, Any]) -> None:
        for k, v in d["portfolio"].items():
            setattr(self.portfolio, k, v)
        self.stats.restore(d["stats"])
        self._day_started = True  # a new UTC day since then is detected on the first book
        self.risk.daily_halt_until_ms = int(d["risk"]["daily_halt_until_ms"])
        self.risk.manual_pause = bool(d["risk"]["manual_pause"])

    # ---- market data -------------------------------------------------------------

    async def handle(self, ev: MarketEvent, quote: bool = True) -> None:
        """Process one event. `quote=False` updates state only (used to skip ahead
        through a backlog so we never quote on a stale book)."""
        self.last_event_mono = time.monotonic()
        self.now_ms = max(self.now_ms, ev.event_ms)
        self.executor.on_market_event(ev)  # paper: may produce fills before we requote
        if isinstance(ev, BookTicker):
            if not self._day_started:
                self._day_started = True
                self.stats.start_day(ev.event_ms, ev.mid)
            elif ev.event_ms // DAY_MS != self.stats.day_start_ms // DAY_MS:
                if self.on_day_end:
                    self.on_day_end()
                self.stats.start_day(ev.event_ms, self.mark)
            self.book = ev
            self.vol.update(ev.event_ms, ev.mid)
            self.risk.on_mid(ev.event_ms, ev.mid)
            for s in self.markout.on_mid(ev.event_ms, ev.mid):
                if self.on_markout:
                    self.on_markout(s)
                if self.store:
                    self.store.add("markouts", s.fill_ts_ms, self.symbol, s.horizon_ms, s.bps)
            if ev.event_ms - self._last_equity_ms >= 1000:
                self._last_equity_ms = ev.event_ms
                self.stats.update_equity(self.mark)
                if self.risk.check_daily_loss(ev.event_ms, self.stats.net_today(self.mark)):
                    await self.flatten("daily loss limit")
            if quote:
                await self._maybe_requote()
        elif isinstance(ev, MarkPrice):
            self.mark_px = ev.mark
            self.risk.on_funding_rate(ev.funding_rate, self.portfolio.position_qty)

    async def set_stale(self, stale: bool) -> None:
        self.stale = stale
        if stale:
            self.quote_status = "stopped: market data stale"
            await self.executor.cancel_all()

    # ---- commands ----------------------------------------------------------------

    async def pause(self) -> str:
        msg = self.risk.pause()
        await self.executor.cancel_all()
        return msg

    async def flatten(self, reason: str) -> None:
        """Cancel everything, then close the position reduce-only. Quoting stays off
        afterwards (daily halt or manual pause) so we don't immediately re-open."""
        log.warning("flatten", extra={"reason": reason, "position": self.portfolio.position_qty})
        if reason != "daily loss limit":
            self.risk.manual_pause = True
        await self.executor.cancel_all()
        if abs(self.portfolio.position_qty) > 0:
            await self.executor.flatten(self.portfolio.position_qty)

    # ---- quoting -----------------------------------------------------------------

    async def _maybe_requote(self) -> None:
        if self.stale:
            return
        ok, why = self.risk.can_quote(self.now_ms)
        if not ok or not self.vol.ready or self.book is None:
            self.quote_status = f"stopped: {why}" if not ok else "warming up σ"
            if any(o.is_working for o in self.executor.working_orders()):
                await self.executor.cancel_all()
            return
        if self.now_ms - self._last_quote_ms < self.cfg.requote_min_interval_ms:
            return
        self._last_quote_ms = self.now_ms

        b = self.book
        pos = self.portfolio.position_qty
        q = compute_quote(MarketState(b.bid, b.ask, self.vol.sigma_bps, pos), self.params, self.filters)
        self.last_quote = q
        self.quote_status = "quoting"
        if q.at_limit != self._at_limit:
            self._at_limit = q.at_limit
            if q.at_limit:
                self._alert(f"⚠️ Inventory limit hit ({q.at_limit}): position {pos:+g}, "
                            "quoting only the reducing side", Priority.HIGH)

        working = self.executor.working_orders()
        cancels: list[str] = []
        places: list[tuple[Side, float, float]] = []
        for side, px, qty in ((Side.BUY, q.bid_px, q.bid_qty), (Side.SELL, q.ask_px, q.ask_qty)):
            mine = [o for o in working if o.side is side and o.is_working]
            if px is None:
                cancels += [o.client_id for o in mine]
                continue
            if mine:
                keep, extra = mine[0], mine[1:]
                cancels += [o.client_id for o in extra]
                moved_bps = abs(keep.price - px) / q.mid * 1e4
                if moved_bps <= self.cfg.requote_threshold_bps and keep.remaining == qty:
                    continue
                cancels.append(keep.client_id)
            # Risk counts every live order on this side (incl. ones being cancelled):
            # until a cancel is confirmed it can still fill.
            same_side = sum(o.remaining for o in working if o.side is side
                            and o.client_id not in cancels)
            chk = self.risk.check_order(side, px, qty, pos, same_side)
            if not chk.ok:
                log.debug("order blocked by risk", extra={"side": side.value, "reason": chk.reason})
                continue
            places.append((side, px, qty))

        jobs: list[Awaitable[Any]] = [self.executor.cancel(c) for c in cancels]
        jobs += [self.executor.place(s, p, qty) for s, p, qty in places]
        if jobs:
            await asyncio.gather(*jobs)
        if places and self.store:
            self.store.add("quotes", self.now_ms, self.symbol, q.bid_px, q.ask_px, q.mid,
                           q.reservation_px, q.spread_bps, self.vol.sigma_bps, pos)

    # ---- executor events -----------------------------------------------------------

    def _on_exec(self, e: ExecEvent) -> None:
        if isinstance(e, Fill):
            self.portfolio.apply_fill(e.side, e.price, e.qty, e.fee)
            self.markout.add_fill(e.ts_ms, e.side.sign, e.price)
            mid = self.book.mid if self.book else e.price
            self.stats.record_fill_edge(e.side.sign, e.price, mid)
            if self.store:
                self.store.add("fills", e.ts_ms, self.symbol, e.client_id, e.side.value, e.price,
                               e.qty, e.fee, mid, e.side.sign * (mid - e.price) / mid * 1e4,
                               self.portfolio.position_qty)
            log.info("fill", extra={"side": e.side.value, "price": e.price, "qty": e.qty,
                                    "fee": round(e.fee, 6), "position": self.portfolio.position_qty,
                                    "client_id": e.client_id})
        elif isinstance(e, FundingPayment):
            self.portfolio.apply_funding(e.amount)
            if self.store:
                self.store.add("funding", e.ts_ms, self.symbol, e.rate, e.mark, e.position_qty, e.amount)
            log.info("funding", extra={"rate": e.rate, "position": e.position_qty,
                                       "amount": round(e.amount, 6)})
        elif isinstance(e, OrderRejected):
            self.rejects += 1
            log.debug("order rejected", extra={"client_id": e.client_id, "reason": e.reason})
