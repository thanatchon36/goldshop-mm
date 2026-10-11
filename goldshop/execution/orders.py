"""Order model and state machine shared by every executor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderStatus(str, Enum):
    PENDING_NEW = "PENDING_NEW"        # sent, not yet acknowledged by the exchange
    NEW = "NEW"                        # resting on the book
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"  # cancel sent; fills can still arrive
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"              # e.g. post-only (GTX) would have crossed
    EXPIRED = "EXPIRED"


TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED,
                      OrderStatus.EXPIRED})

_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_NEW: frozenset({
        OrderStatus.NEW, OrderStatus.REJECTED, OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED,
        OrderStatus.EXPIRED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
    }),
    OrderStatus.NEW: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.PENDING_CANCEL,
        OrderStatus.CANCELED, OrderStatus.EXPIRED,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.PENDING_CANCEL,
        OrderStatus.CANCELED, OrderStatus.EXPIRED,
    }),
    # A cancel can lose the race against a fill, or be rejected while the order lives on.
    OrderStatus.PENDING_CANCEL: frozenset({
        OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.NEW,
        OrderStatus.PARTIALLY_FILLED, OrderStatus.REJECTED,
    }),
}


class InvalidTransition(Exception):
    pass


@dataclass
class Order:
    client_id: str
    side: Side
    price: float
    qty: float
    filled_qty: float = 0.0
    status: OrderStatus = OrderStatus.PENDING_NEW

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_working(self) -> bool:
        """Live and not being cancelled — i.e. counts as our current quote on its side."""
        return self.status in (OrderStatus.PENDING_NEW, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    def transition(self, new: OrderStatus) -> None:
        if new not in _ALLOWED.get(self.status, frozenset()):
            raise InvalidTransition(f"{self.client_id}: {self.status.value} → {new.value}")
        self.status = new

    def apply_fill(self, qty: float) -> None:
        if self.is_terminal:
            raise InvalidTransition(f"{self.client_id}: fill on terminal order ({self.status.value})")
        if qty <= 0 or qty > self.remaining + 1e-12:
            raise ValueError(f"{self.client_id}: bad fill qty {qty} (remaining {self.remaining})")
        self.filled_qty += qty
        if self.remaining <= 1e-12:
            self.filled_qty = self.qty
            self.transition(OrderStatus.FILLED)
        elif self.status is not OrderStatus.PENDING_CANCEL:
            self.transition(OrderStatus.PARTIALLY_FILLED)


_CLIENT_ID_RE = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")  # Binance's newClientOrderId rule


class ClientIdGenerator:
    """Deterministic client order ids: `{prefix}-{b|s}-{seq}`.

    The same (prefix, seq) always produces the same id, so a retried request
    carries the same id and the exchange rejects the duplicate instead of
    opening a second order.
    """

    def __init__(self, prefix: str, start: int = 0) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", prefix):
            raise ValueError("prefix must be 1–16 chars of [A-Za-z0-9_]")
        self.prefix = prefix
        self.seq = start

    def next(self, side: Side) -> str:
        self.seq += 1
        cid = f"{self.prefix}-{'b' if side is Side.BUY else 's'}-{self.seq}"
        assert _CLIENT_ID_RE.match(cid), cid
        return cid
