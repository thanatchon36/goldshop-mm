"""Running performance stats (cumulative + today, UTC) and report text."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from goldshop.accounting import Portfolio
from goldshop.state.markout import MarkoutTracker

DAY_MS = 86_400_000


@dataclass
class _Snapshot:
    """Portfolio totals at the start of the UTC day, so 'today' = now − snapshot."""

    realized: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    net: float = 0.0
    fill_count: int = 0
    volume: float = 0.0
    edge_sum: float = 0.0


@dataclass
class Stats:
    portfolio: Portfolio
    markout: MarkoutTracker
    started_mono: float = field(default_factory=time.monotonic)
    day_start_ms: int = 0
    edge_bps_sum: float = 0.0  # Σ side·(mid − fill)/mid, i.e. captured half-spread per fill
    _day: _Snapshot = field(default_factory=_Snapshot)
    peak_net_today: float = 0.0
    max_drawdown_today: float = 0.0

    def start_day(self, now_ms: int, mark: float) -> None:
        p = self.portfolio
        self.day_start_ms = now_ms - now_ms % DAY_MS
        net = p.net(mark)
        self._day = _Snapshot(p.realized, p.fees, p.funding, net, p.fill_count,
                              p.volume_quote, self.edge_bps_sum)
        self.peak_net_today = net
        self.max_drawdown_today = 0.0

    def net_today(self, mark: float) -> float:
        return self.portfolio.net(mark) - self._day.net

    def to_dict(self) -> dict[str, object]:
        return {"day_start_ms": self.day_start_ms, "edge_bps_sum": self.edge_bps_sum,
                "day": asdict(self._day), "peak_net_today": self.peak_net_today,
                "max_drawdown_today": self.max_drawdown_today}

    def restore(self, d: dict[str, Any]) -> None:
        self.day_start_ms = int(d["day_start_ms"])
        self.edge_bps_sum = float(d["edge_bps_sum"])
        self._day = _Snapshot(**d["day"])
        self.peak_net_today = float(d["peak_net_today"])
        self.max_drawdown_today = float(d["max_drawdown_today"])

    def record_fill_edge(self, side_sign: int, price: float, mid: float) -> None:
        self.edge_bps_sum += side_sign * (mid - price) / mid * 1e4

    def update_equity(self, mark: float) -> None:
        net = self.portfolio.net(mark)
        self.peak_net_today = max(self.peak_net_today, net)
        self.max_drawdown_today = max(self.max_drawdown_today, self.peak_net_today - net)

    def report(self, title: str, mark: float, reconnects: int) -> str:
        p, d = self.portfolio, self._day
        net = p.net(mark)
        fills_today = p.fill_count - d.fill_count
        edge_today = (self.edge_bps_sum - d.edge_sum) / fills_today if fills_today else None
        mo = self.markout.averages(since_ms=self.day_start_ms)
        up_s = int(time.monotonic() - self.started_mono)
        day = datetime.fromtimestamp(self.day_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        def f(x: float | None, nd: int = 2) -> str:
            return "n/a" if x is None else f"{x:+,.{nd}f}"

        lines = [
            f"📊 {title} (UTC day {day})",
            f"Net PnL: today {f(net - d.net)} | total {f(net)} USDT",
            f"Realized {f(p.realized - d.realized)} | Unrealized {f(p.unrealized(mark))}",
            f"Fees {f(-(p.fees - d.fees))} | Funding {f(p.funding - d.funding)}",
            f"Fills {fills_today} | Volume {(p.volume_quote - d.volume):,.0f} USDT",
            f"Avg captured edge vs mid: {f(edge_today)} bps",
            f"Inventory {p.position_qty:+g} ({p.position_qty * mark:+,.2f} USDT)",
            "Markout +1s/+5s/+30s: "
            + " / ".join(f(mo.get(h)) for h in (1000, 5000, 30000)) + " bps",
            f"Max drawdown today {self.max_drawdown_today:,.2f} USDT",
            f"Uptime {up_s // 3600}h{(up_s % 3600) // 60:02d}m | Reconnects {reconnects}",
        ]
        return "\n".join(lines)
