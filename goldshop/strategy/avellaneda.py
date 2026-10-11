"""Avellaneda–Stoikov quoting as pure logic: state in, quote out.

Everything is in bps relative to mid so the parameters carry across symbols:
  reservation offset = −q · γ · σ² · τ
  optimal spread     =  γ · σ² · τ + (2/γ) · ln(1 + γ/κ)
with q = inventory in lots (inventory / order size), σ in bps/√s, τ in seconds,
κ per bps. The quoted spread is floored at 2×maker_fee + buffer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from goldshop.binance.symbol import SymbolFilters

_EPS = 1e-12


@dataclass(frozen=True)
class StrategyParams:
    gamma: float
    kappa: float
    horizon_s: float
    maker_fee_bps: float
    min_spread_buffer_bps: float
    order_qty: float
    max_inventory_qty: float

    @property
    def min_spread_bps(self) -> float:
        return 2 * self.maker_fee_bps + self.min_spread_buffer_bps


@dataclass(frozen=True)
class MarketState:
    best_bid: float
    best_ask: float
    sigma_bps: float  # per √second
    inventory_qty: float  # signed: + long, − short


@dataclass(frozen=True)
class QuoteTarget:
    bid_px: float | None
    bid_qty: float
    ask_px: float | None
    ask_qty: float
    mid: float
    reservation_px: float
    spread_bps: float
    at_limit: Literal["long", "short"] | None  # side whose inventory limit suppresses a quote


def compute_quote(m: MarketState, p: StrategyParams, f: SymbolFilters) -> QuoteTarget:
    mid = (m.best_bid + m.best_ask) / 2
    q_lots = m.inventory_qty / p.order_qty
    var_tau = m.sigma_bps**2 * p.horizon_s

    res_offset_bps = -q_lots * p.gamma * var_tau
    as_spread_bps = p.gamma * var_tau + (2 / p.gamma) * math.log(1 + p.gamma / p.kappa)
    spread_bps = max(as_spread_bps, p.min_spread_bps)
    half = spread_bps / 2

    reservation = mid * (1 + res_offset_bps / 1e4)
    # Rounding only ever widens: bid down, ask up.
    bid = f.floor_price(reservation * (1 - half / 1e4))
    ask = f.ceil_price(reservation * (1 + half / 1e4))
    # Post-only: never cross the opposite best (that would be a GTX reject).
    bid = min(bid, f.floor_price(m.best_ask - f.tick))
    ask = max(ask, f.ceil_price(m.best_bid + f.tick))

    at_limit: Literal["long", "short"] | None = None
    bid_px: float | None = bid
    ask_px: float | None = ask
    if m.inventory_qty + p.order_qty > p.max_inventory_qty + _EPS:
        bid_px, at_limit = None, "long"
    if m.inventory_qty - p.order_qty < -p.max_inventory_qty - _EPS:
        ask_px, at_limit = None, "short"

    return QuoteTarget(
        bid_px=bid_px,
        bid_qty=p.order_qty,
        ask_px=ask_px,
        ask_qty=p.order_qty,
        mid=mid,
        reservation_px=reservation,
        spread_bps=spread_bps,
        at_limit=at_limit,
    )
