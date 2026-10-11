"""The single executor interface. Paper now; Binance (testnet/live) in phase 3."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from goldshop.execution.orders import Order, Side
from goldshop.market_data.models import MarketEvent


@dataclass(frozen=True)
class Fill:
    ts_ms: int
    client_id: str
    side: Side
    price: float
    qty: float
    fee: float  # quote currency, positive = paid


@dataclass(frozen=True)
class FundingPayment:
    ts_ms: int
    rate: float
    mark: float
    position_qty: float
    amount: float  # quote currency, positive = received


@dataclass(frozen=True)
class OrderRejected:
    ts_ms: int
    client_id: str
    reason: str


ExecEvent = Fill | FundingPayment | OrderRejected
ExecHandler = Callable[[ExecEvent], None]


class Executor(Protocol):
    def on_exec_event(self, handler: ExecHandler) -> None: ...

    def on_market_event(self, ev: MarketEvent) -> None:
        """Paper executors simulate the exchange from market data; real ones ignore this."""

    def working_orders(self) -> list[Order]: ...

    async def place(self, side: Side, price: float, qty: float) -> Order: ...

    async def cancel(self, client_id: str) -> None: ...

    async def cancel_all(self) -> None: ...

    async def flatten(self, position_qty: float) -> None:
        """Close `position_qty` (signed) with a reduce-only market order."""
