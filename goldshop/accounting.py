"""Position and PnL accounting (average-cost), in quote currency (USDT)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from goldshop.execution.orders import Side


@dataclass
class Portfolio:
    position_qty: float = 0.0
    avg_price: float = 0.0
    realized: float = 0.0  # gross trading PnL, before fees
    fees: float = 0.0
    funding: float = 0.0  # positive = received
    fill_count: int = 0
    volume_quote: float = 0.0

    def apply_fill(self, side: Side, price: float, qty: float, fee: float) -> None:
        signed = side.sign * qty
        pos = self.position_qty
        if pos == 0 or (pos > 0) == (signed > 0):
            new = pos + signed
            self.avg_price = (abs(pos) * self.avg_price + qty * price) / abs(new)
            self.position_qty = new
        else:
            closing = min(abs(signed), abs(pos))
            direction = 1 if pos > 0 else -1
            self.realized += closing * (price - self.avg_price) * direction
            new = pos + signed
            if abs(new) < 1e-12:
                self.position_qty, self.avg_price = 0.0, 0.0
            elif (new > 0) != (pos > 0):  # flipped through zero
                self.position_qty, self.avg_price = new, price
            else:
                self.position_qty = new
        self.fees += fee
        self.fill_count += 1
        self.volume_quote += price * qty

    def apply_funding(self, amount: float) -> None:
        self.funding += amount

    def unrealized(self, mark: float) -> float:
        return self.position_qty * (mark - self.avg_price) if self.position_qty else 0.0

    def net(self, mark: float) -> float:
        return self.realized + self.unrealized(mark) - self.fees + self.funding

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
