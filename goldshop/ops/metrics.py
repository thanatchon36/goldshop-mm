"""Prometheus metrics.

Gauges that mirror state (PnL, inventory, spreads, flags, reconnects) are read
at scrape time, so the trading loop pays nothing for them. Counters and
histograms (fills, markouts, latencies) are updated from events.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from goldshop.execution.base import ExecEvent, Fill, OrderRejected
from goldshop.state.markout import MarkoutSample

_MARKOUT_BUCKETS = (-20, -10, -5, -2, -1, -0.5, 0, 0.5, 1, 2, 5, 10, 20)
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)


@dataclass(frozen=True)
class Snapshot:
    """Point-in-time state, produced by the runner on each scrape."""

    mode: str
    symbol: str
    net: float
    realized: float
    unrealized: float
    fees: float
    funding: float
    net_today: float
    inventory_qty: float
    mark: float
    market_spread_bps: float | None
    quote_spread_bps: float | None
    sigma_bps: float
    quoting: bool
    stale: bool
    paused: bool
    daily_halted: bool
    working_orders: int
    ws_reconnects: int
    user_stream_reconnects: int
    uptime_s: float
    telegram_dropped: int


class _StateCollector(Collector):
    def __init__(self, snapshot: Callable[[], Snapshot]) -> None:
        self._snapshot = snapshot

    def collect(self) -> Iterator[GaugeMetricFamily]:
        s = self._snapshot()
        lbl = ["mode", "symbol"]
        base = [s.mode, s.symbol]

        def g(name: str, doc: str, value: float | None) -> GaugeMetricFamily:
            m = GaugeMetricFamily(name, doc, labels=lbl)
            if value is not None:
                m.add_metric(base, float(value))
            return m

        pnl = GaugeMetricFamily("goldshop_pnl_usdt", "PnL components since first start (USDT)",
                                labels=[*lbl, "kind"])
        for kind, v in (("net", s.net), ("realized", s.realized), ("unrealized", s.unrealized),
                        ("fees", -s.fees), ("funding", s.funding), ("net_today", s.net_today)):
            pnl.add_metric([*base, kind], v)
        yield pnl
        yield g("goldshop_inventory_qty", "Signed position in base asset", s.inventory_qty)
        yield g("goldshop_inventory_notional_usdt", "Signed position notional at mark",
                s.inventory_qty * s.mark)
        yield g("goldshop_mark_price", "Mark price", s.mark)
        yield g("goldshop_market_spread_bps", "Best bid/ask spread of the market", s.market_spread_bps)
        yield g("goldshop_quote_spread_bps", "Spread of our last quote", s.quote_spread_bps)
        yield g("goldshop_sigma_bps", "Rolling σ of 1s mid returns (bps/√s)", s.sigma_bps)
        yield g("goldshop_quoting", "1 if quoting, else 0", s.quoting)
        yield g("goldshop_stale", "1 if market data / user stream not healthy", s.stale)
        yield g("goldshop_paused", "1 if paused by command or flatten", s.paused)
        yield g("goldshop_daily_halted", "1 if the daily loss limit halted trading", s.daily_halted)
        yield g("goldshop_working_orders", "Open orders", s.working_orders)
        yield g("goldshop_ws_reconnects", "Market data WebSocket reconnects since start", s.ws_reconnects)
        yield g("goldshop_user_stream_reconnects", "User data stream reconnects since start",
                s.user_stream_reconnects)
        yield g("goldshop_uptime_seconds", "Process uptime", s.uptime_s)
        yield g("goldshop_telegram_dropped", "Telegram messages dropped", s.telegram_dropped)


class Metrics:
    def __init__(self, snapshot: Callable[[], Snapshot]) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.fills = Counter("goldshop_fills", "Fills", ["side"], registry=r)
        self.fill_volume = Counter("goldshop_fill_volume_usdt", "Filled notional (USDT)", registry=r)
        self.rejects = Counter("goldshop_order_rejects", "Rejected orders (GTX, margin, rate guard…)",
                               registry=r)
        self.markout = Histogram("goldshop_markout_bps", "Markout after fill (bps, + = favourable)",
                                 ["horizon"], buckets=_MARKOUT_BUCKETS, registry=r)
        self.order_latency = Histogram("goldshop_order_latency_seconds",
                                       "REST round-trip for order actions", ["op"],
                                       buckets=_LATENCY_BUCKETS, registry=r)
        self.md_latency = Histogram("goldshop_market_data_latency_seconds",
                                    "Local receive time − exchange event time (includes clock offset)",
                                    buckets=_LATENCY_BUCKETS, registry=r)
        self.engine_lag = Histogram("goldshop_engine_lag_seconds",
                                    "Local receive → processed by the engine (queue backlog)",
                                    buckets=_LATENCY_BUCKETS, registry=r)
        r.register(_StateCollector(snapshot))

    def on_exec(self, e: ExecEvent) -> None:
        if isinstance(e, Fill):
            self.fills.labels(e.side.value).inc()
            self.fill_volume.inc(e.price * e.qty)
        elif isinstance(e, OrderRejected):
            self.rejects.inc()

    def on_markout(self, s: MarkoutSample) -> None:
        self.markout.labels(f"{s.horizon_ms // 1000}s").observe(s.bps)

    def on_latency(self, op: str, seconds: float) -> None:
        self.order_latency.labels(op).observe(seconds)

    def render(self) -> bytes:
        return generate_latest(self.registry)
