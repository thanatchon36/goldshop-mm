"""Replay recorded market data through the same engine + paper fill engine used live.

Input is the recorder's Parquet output (tick-level L1 + trades + mark price) —
never OHLCV, which cannot tell whether a resting quote would have been filled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from goldshop.binance.symbol import SymbolFilters
from goldshop.config import AppConfig
from goldshop.engine import Engine
from goldshop.execution.base import ExecEvent, Fill
from goldshop.execution.orders import ClientIdGenerator
from goldshop.execution.paper.executor import PaperExecutor
from goldshop.execution.paper.sim import PaperExchange
from goldshop.market_data.models import BookTicker, MarketEvent, MarkPrice, Trade
from goldshop.state.markout import MarkoutSample
from goldshop.strategy.avellaneda import StrategyParams

log = logging.getLogger(__name__)

# Same-millisecond ordering: book first, then mark, then trades.
_PRIORITY = {BookTicker: 0, MarkPrice: 1, Trade: 2}


def _hour_of(path: Path) -> datetime:
    day = path.parent.name
    hh = path.name.split(".")[0]
    return datetime.strptime(f"{day} {hh}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc)


def load_events(data_dir: Path, symbol: str, start: datetime | None = None,
                end: datetime | None = None) -> list[MarketEvent]:
    root = Path(data_dir) / symbol
    events: list[MarketEvent] = []
    for stream in ("book_ticker", "trades", "mark_price"):
        for f in sorted((root / stream).glob("*/*.parquet")):
            h = _hour_of(f)
            if (start and h < start.replace(minute=0, second=0, microsecond=0)) or (end and h >= end):
                continue
            try:
                rows = pq.read_table(f).to_pylist()
            except Exception as e:  # e.g. the current hour, still being written by the recorder
                log.warning("skipping unreadable parquet file", extra={"file": str(f), "error": str(e)})
                continue
            for r in rows:
                if stream == "book_ticker":
                    events.append(BookTicker(r["recv_ns"], r["event_ms"], r["txn_ms"], r["update_id"],
                                             r["bid"], r["bid_qty"], r["ask"], r["ask_qty"]))
                elif stream == "trades":
                    events.append(Trade(r["recv_ns"], r["event_ms"], r["trade_ms"], r["agg_id"],
                                        r["price"], r["qty"], r["is_buyer_maker"]))
                else:
                    events.append(MarkPrice(r["recv_ns"], r["event_ms"], r["mark"], r["index"],
                                            r["funding_rate"], r["next_funding_ms"]))
    lo = int(start.timestamp() * 1000) if start else None
    hi = int(end.timestamp() * 1000) if end else None
    events = [e for e in events if (lo is None or e.event_ms >= lo) and (hi is None or e.event_ms < hi)]
    events.sort(key=lambda e: (e.event_ms, _PRIORITY[type(e)]))
    return events


@dataclass
class Point:
    ts_ms: int
    mid: float
    net: float
    realized: float
    unrealized: float
    fees: float
    funding: float
    inventory: float


@dataclass
class BacktestResult:
    symbol: str
    params: StrategyParams
    latency_ms: int
    series: list[Point] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    markouts: list[MarkoutSample] = field(default_factory=list)
    rejects: int = 0
    stale_gaps: int = 0
    edge_bps_sum: float = 0.0


async def run_backtest(cfg: AppConfig, filters: SymbolFilters, events: list[MarketEvent],
                       sample_ms: int = 1000) -> BacktestResult:
    first_book = next((e for e in events if isinstance(e, BookTicker)), None)
    if first_book is None:
        raise ValueError("no book_ticker data in the selected range")
    s, ex = cfg.strategy, cfg.exchange
    ref = first_book.mid
    params = StrategyParams(
        s.gamma, s.kappa, s.horizon_s, ex.maker_fee_bps, s.min_spread_buffer_bps,
        filters.order_qty_for_notional(s.order_notional_usdt, ref),
        filters.floor_qty(s.max_inventory_notional_usdt / ref),
    )
    executor = PaperExecutor(PaperExchange(cfg.paper.latency_ms, ex.maker_fee_bps),
                             ClientIdGenerator("bt"))
    engine = Engine(filters, params, s, executor, stale=False)
    engine.markout.keep_ms = 10**15  # keep every sample for the distribution
    res = BacktestResult(cfg.symbol, params, cfg.paper.latency_ms)

    def on_exec(e: ExecEvent) -> None:
        if isinstance(e, Fill):
            res.fills.append(e)

    executor.on_exec_event(on_exec)
    gap_ms = cfg.market_data.stale_after_s * 1000
    last_ms = events[0].event_ms
    next_sample = events[0].event_ms
    p = engine.portfolio
    for ev in events:
        # Recorder downtime looks like stale data: behave exactly as live would.
        if ev.event_ms - last_ms > gap_ms:
            res.stale_gaps += 1
            await engine.set_stale(True)
            await engine.handle(ev)
            await engine.set_stale(False)
        else:
            await engine.handle(ev)
        last_ms = ev.event_ms
        if ev.event_ms >= next_sample and engine.book is not None:
            next_sample = ev.event_ms + sample_ms
            m = engine.mark
            res.series.append(Point(ev.event_ms, engine.book.mid, p.net(m), p.realized,
                                    p.unrealized(m), p.fees, p.funding, p.position_qty))
    if engine.book is not None:
        m = engine.mark
        res.series.append(Point(last_ms, engine.book.mid, p.net(m), p.realized, p.unrealized(m),
                                p.fees, p.funding, p.position_qty))
    # Fills too close to the end never reach the later horizons; they are left out.
    res.markouts = list(engine.markout.samples)
    res.rejects = engine.rejects
    res.edge_bps_sum = engine.stats.edge_bps_sum
    return res
