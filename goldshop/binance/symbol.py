"""Symbol trading rules (tick size, lot size, min notional) from exchangeInfo."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any


class SymbolRuleError(Exception):
    pass


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, info: dict[str, Any], symbol: str) -> SymbolFilters:
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                f = {x["filterType"]: x for x in s["filters"]}
                return cls(
                    symbol=symbol,
                    tick_size=Decimal(f["PRICE_FILTER"]["tickSize"]),
                    step_size=Decimal(f["LOT_SIZE"]["stepSize"]),
                    min_qty=Decimal(f["LOT_SIZE"]["minQty"]),
                    min_notional=Decimal(f["MIN_NOTIONAL"]["notional"]),
                )
        raise SymbolRuleError(f"symbol {symbol} not found in exchangeInfo")

    @property
    def tick(self) -> float:
        return float(self.tick_size)

    def _round(self, x: float, unit: Decimal, mode: str) -> float:
        return float((Decimal(repr(x)) / unit).to_integral_value(rounding=mode) * unit)

    def floor_price(self, p: float) -> float:
        return self._round(p, self.tick_size, ROUND_FLOOR)

    def ceil_price(self, p: float) -> float:
        return self._round(p, self.tick_size, ROUND_CEILING)

    def floor_qty(self, q: float) -> float:
        return self._round(q, self.step_size, ROUND_FLOOR)

    def format_price(self, p: float) -> str:
        """Exact decimal string on the tick grid (what the order API expects)."""
        return _fmt((Decimal(repr(p)) / self.tick_size).to_integral_value() * self.tick_size)

    def format_qty(self, q: float) -> str:
        return _fmt((Decimal(repr(q)) / self.step_size).to_integral_value(ROUND_FLOOR) * self.step_size)

    def order_qty_for_notional(self, notional: float, price: float) -> float:
        """Order qty for a target notional, rounded down; raises if below exchange minimums."""
        qty = self.floor_qty(notional / price)
        if Decimal(repr(qty)) < self.min_qty or Decimal(repr(qty * price)) < self.min_notional:
            min_needed = max(
                float(self.min_notional), float(self.min_qty) * price
            ) + float(self.step_size) * price
            raise SymbolRuleError(
                f"order_notional_usdt={notional} is below {self.symbol} minimums "
                f"(minQty={self.min_qty}, minNotional={self.min_notional}, step={self.step_size}); "
                f"use at least ~{min_needed:.0f} USDT"
            )
        return qty


def _fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s
