"""Conservative paper fill engine (shared by paper mode and backtests).

Rules:
- An order only fills when a trade prints *through* its price (buy: trade < our
  bid, sell: trade > our ask). Merely touching it does not count — we assume we
  sit at the back of the queue.
- New orders and cancels reach the "exchange" after `latency_ms`. Until a cancel
  arrives the order can still be filled.
- On arrival a post-only order is rejected if it would cross the book then (GTX).
- Maker fee is charged on every fill.
- Funding is paid/received at each funding time from the markPrice stream.
Time is the exchange event time of the market data being processed.
"""

from __future__ import annotations

import heapq
import itertools
from typing import Literal

from goldshop.execution.base import ExecEvent, Fill, FundingPayment, OrderRejected
from goldshop.execution.orders import Order, OrderStatus, Side
from goldshop.market_data.models import BookTicker, MarketEvent, MarkPrice, Trade

_Action = tuple[int, int, Literal["new", "cancel"], str]


class PaperExchange:
    def __init__(self, latency_ms: int, maker_fee_bps: float, taker_fee_bps: float = 5.0) -> None:
        self.latency_ms = latency_ms
        self.maker_fee_bps = maker_fee_bps
        self.taker_fee_bps = taker_fee_bps
        self.orders: dict[str, Order] = {}
        self._resting: set[str] = set()  # arrived at the exchange and live
        self._actions: list[_Action] = []
        self._seq = itertools.count()
        self.book: BookTicker | None = None
        self.position_qty = 0.0
        self._next_funding_ms = 0
        self._funding_rate = 0.0

    # ---- requests from the strategy side -----------------------------------

    def submit(self, order: Order, now_ms: int) -> None:
        self.orders[order.client_id] = order
        heapq.heappush(self._actions, (now_ms + self.latency_ms, next(self._seq), "new", order.client_id))

    def cancel(self, client_id: str, now_ms: int) -> None:
        o = self.orders.get(client_id)
        if o is None or o.is_terminal or o.status is OrderStatus.PENDING_CANCEL:
            return
        o.transition(OrderStatus.PENDING_CANCEL)
        heapq.heappush(self._actions, (now_ms + self.latency_ms, next(self._seq), "cancel", client_id))

    def market_close(self, qty_signed: float, client_id: str, now_ms: int) -> list[ExecEvent]:
        """Reduce-only market order: fills at the opposite best price, taker fee.
        (Conservative only to L1 — ignores depth/slippage beyond the touch.)"""
        if self.book is None or not qty_signed:
            return []
        qty = min(abs(qty_signed), abs(self.position_qty))
        if qty <= 0:
            return []
        side = Side.SELL if self.position_qty > 0 else Side.BUY
        px = self.book.bid if side is Side.SELL else self.book.ask
        self.position_qty += side.sign * qty
        return [Fill(now_ms + self.latency_ms, client_id, side, px, qty,
                     px * qty * self.taker_fee_bps / 1e4)]

    # ---- market data -------------------------------------------------------

    def on_market_event(self, ev: MarketEvent) -> list[ExecEvent]:
        out = self._advance(ev.event_ms)
        if isinstance(ev, BookTicker):
            self.book = ev
        elif isinstance(ev, Trade):
            out += self._match(ev)
        elif isinstance(ev, MarkPrice):
            out += self._funding(ev)
        return out

    def _advance(self, now_ms: int) -> list[ExecEvent]:
        """Apply every request that has reached the exchange by `now_ms`, in order."""
        out: list[ExecEvent] = []
        while self._actions and self._actions[0][0] <= now_ms:
            at, _, kind, cid = heapq.heappop(self._actions)
            o = self.orders.get(cid)
            if o is None:  # already finished and forgotten (e.g. filled before the cancel landed)
                continue
            if kind == "new":
                if self._would_cross(o):
                    o.transition(OrderStatus.REJECTED)
                    out.append(OrderRejected(at, cid, "GTX: post-only order would cross"))
                    continue
                self._resting.add(cid)
                if o.status is OrderStatus.PENDING_NEW:
                    o.transition(OrderStatus.NEW)
            elif cid in self._resting:
                self._resting.discard(cid)
                if not o.is_terminal:
                    o.transition(OrderStatus.CANCELED)
        return out

    def _would_cross(self, o: Order) -> bool:
        if self.book is None:
            return True  # no book → cannot prove it is a maker; reject
        if o.side is Side.BUY:
            return o.price >= self.book.ask
        return o.price <= self.book.bid

    def _match(self, t: Trade) -> list[ExecEvent]:
        out: list[ExecEvent] = []
        for cid in list(self._resting):
            o = self.orders[cid]
            through = t.price < o.price if o.side is Side.BUY else t.price > o.price
            if not through:
                continue
            qty = min(o.remaining, t.qty)
            o.apply_fill(qty)
            if o.is_terminal:
                self._resting.discard(cid)
            fee = o.price * qty * self.maker_fee_bps / 1e4
            self.position_qty += o.side.sign * qty
            out.append(Fill(t.event_ms, cid, o.side, o.price, qty, fee))
        return out

    def _funding(self, m: MarkPrice) -> list[ExecEvent]:
        out: list[ExecEvent] = []
        if self._next_funding_ms and m.event_ms >= self._next_funding_ms and self.position_qty:
            # Longs pay shorts when the rate is positive.
            amount = -self.position_qty * m.mark * self._funding_rate
            out.append(FundingPayment(self._next_funding_ms, self._funding_rate, m.mark,
                                      self.position_qty, amount))
        self._next_funding_ms = m.next_funding_ms
        self._funding_rate = m.funding_rate
        return out
