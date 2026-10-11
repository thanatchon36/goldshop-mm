"""PaperExecutor: the Executor interface on top of the PaperExchange simulator."""

from __future__ import annotations

from goldshop.execution.base import ExecHandler
from goldshop.execution.orders import ClientIdGenerator, Order, Side
from goldshop.execution.paper.sim import PaperExchange
from goldshop.market_data.models import MarketEvent


class PaperExecutor:
    def __init__(self, sim: PaperExchange, ids: ClientIdGenerator) -> None:
        self.sim = sim
        self.ids = ids
        self._handlers: list[ExecHandler] = []
        self._now_ms = 0

    def on_exec_event(self, handler: ExecHandler) -> None:
        self._handlers.append(handler)

    def on_market_event(self, ev: MarketEvent) -> None:
        self._now_ms = max(self._now_ms, ev.event_ms)
        for e in self.sim.on_market_event(ev):
            for h in self._handlers:
                h(e)
        # Forget finished orders so memory stays flat when running 24/7.
        for cid in [c for c, o in self.sim.orders.items() if o.is_terminal]:
            del self.sim.orders[cid]

    def working_orders(self) -> list[Order]:
        return [o for o in self.sim.orders.values() if not o.is_terminal]

    async def place(self, side: Side, price: float, qty: float) -> Order:
        o = Order(self.ids.next(side), side, price, qty)
        self.sim.submit(o, self._now_ms)
        return o

    async def cancel(self, client_id: str) -> None:
        self.sim.cancel(client_id, self._now_ms)

    async def cancel_all(self) -> None:
        for o in self.working_orders():
            self.sim.cancel(o.client_id, self._now_ms)

    async def flatten(self, position_qty: float) -> None:
        side = Side.SELL if position_qty > 0 else Side.BUY
        for e in self.sim.market_close(position_qty, self.ids.next(side), self._now_ms):
            for h in self._handlers:
                h(e)
