"""Market data events and parsers for Binance USDⓈ-M futures stream payloads."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BookTicker:
    recv_ns: int
    event_ms: int
    txn_ms: int
    update_id: int
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class Depth:
    """Partial book snapshot (top N levels), best level first."""

    recv_ns: int
    event_ms: int
    txn_ms: int
    final_update_id: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class Trade:
    recv_ns: int
    event_ms: int
    trade_ms: int
    agg_id: int
    price: float
    qty: float
    is_buyer_maker: bool  # True => aggressor sold (hit the bid)


@dataclass(frozen=True, slots=True)
class MarkPrice:
    recv_ns: int
    event_ms: int
    mark: float
    index: float
    funding_rate: float
    next_funding_ms: int


MarketEvent = BookTicker | Depth | Trade | MarkPrice


def _levels(raw: list[list[str]]) -> tuple[tuple[float, float], ...]:
    return tuple((float(p), float(q)) for p, q in raw)


def parse_event(data: dict[str, Any], recv_ns: int | None = None) -> MarketEvent | None:
    """Parse the `data` object of a combined-stream message. Unknown events → None."""
    r = recv_ns if recv_ns is not None else time.time_ns()
    e = data.get("e")
    if e == "bookTicker":
        return BookTicker(
            r, int(data["E"]), int(data["T"]), int(data["u"]),
            float(data["b"]), float(data["B"]), float(data["a"]), float(data["A"]),
        )
    if e == "depthUpdate":
        return Depth(
            r, int(data["E"]), int(data["T"]), int(data["u"]),
            _levels(data["b"]), _levels(data["a"]),
        )
    if e == "aggTrade":
        return Trade(
            r, int(data["E"]), int(data["T"]), int(data["a"]),
            float(data["p"]), float(data["q"]), bool(data["m"]),
        )
    if e == "markPriceUpdate":
        return MarkPrice(
            r, int(data["E"]), float(data["p"]), float(data["i"]),
            float(data["r"]) if data.get("r") not in (None, "") else 0.0,
            int(data.get("T") or 0),
        )
    return None
