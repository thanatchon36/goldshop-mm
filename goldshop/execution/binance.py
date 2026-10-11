"""BinanceExecutor: the Executor interface against Binance USDⓈ-M Futures.

Testnet and live differ only in base URL and keys (chosen by the runner).
- Every quote is LIMIT + timeInForce=GTX (post-only): it can only ever be a maker.
- clientOrderIds are deterministic, so a retried request cannot open a second order.
- Order state comes from the User Data Stream; REST responses only move
  PENDING_NEW → NEW. Fills are the source of truth for the portfolio and are
  emitted even if the local order record is already terminal.
- `resync()` repairs anything missed while the stream was down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from goldshop.binance.rest import (
    ERR_GTX_WOULD_TAKE,
    ERR_MARGIN_INSUFFICIENT,
    ERR_NO_SUCH_ORDER,
    ERR_UNKNOWN_ORDER,
    BinanceHTTPError,
    BinanceRestClient,
    OrderRateLimited,
)
from goldshop.binance.symbol import SymbolFilters
from goldshop.execution.base import ExecEvent, ExecHandler, Fill, FundingPayment, OrderRejected
from goldshop.execution.orders import TERMINAL, ClientIdGenerator, InvalidTransition, Order, OrderStatus, Side
from goldshop.market_data.models import MarketEvent, MarkPrice
from goldshop.notify.telegram import Priority

log = logging.getLogger(__name__)

_STATUS = {s.value: s for s in OrderStatus}


class BinanceExecutor:
    def __init__(self, rest: BinanceRestClient, filters: SymbolFilters, ids: ClientIdGenerator,
                 maker_fee_bps: float, alert: Any = None) -> None:
        self.rest = rest
        self.symbol = filters.symbol
        self.filters = filters
        self.ids = ids
        self.maker_fee_bps = maker_fee_bps
        self._alert = alert or (lambda text, prio: None)
        self.orders: dict[str, Order] = {}
        self._sent_mono: dict[str, float] = {}
        self._handlers: list[ExecHandler] = []
        self._funding_rate = 0.0
        self._mark = 0.0
        self._last_margin_alert = 0.0
        self._warned_fee_asset = False
        self.position_qty = 0.0  # last value seen on the exchange (ACCOUNT_UPDATE / positionRisk)
        self.on_latency: Any = None  # (op, seconds) → metrics

    # ---- Executor interface ---------------------------------------------------

    def on_exec_event(self, handler: ExecHandler) -> None:
        self._handlers.append(handler)

    def on_market_event(self, ev: MarketEvent) -> None:
        if isinstance(ev, MarkPrice):
            self._funding_rate, self._mark = ev.funding_rate, ev.mark

    def working_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.is_terminal]

    async def place(self, side: Side, price: float, qty: float) -> Order:
        o = Order(self.ids.next(side), side, price, qty)
        self.orders[o.client_id] = o
        self._sent_mono[o.client_id] = time.monotonic()
        try:
            async with self._timed("place"):
                await self.rest.new_order(
                    symbol=self.symbol, side=side.value, type="LIMIT", timeInForce="GTX",
                    quantity=self.filters.format_qty(qty), price=self.filters.format_price(price),
                    newClientOrderId=o.client_id, newOrderRespType="ACK",
                )
            if o.status is OrderStatus.PENDING_NEW:
                o.transition(OrderStatus.NEW)
        except OrderRateLimited as e:
            self._reject(o, str(e))
            log.warning("order skipped: local order-rate guard", extra={"reason": str(e)})
        except BinanceHTTPError as e:
            if e.code == ERR_GTX_WOULD_TAKE:
                self._reject(o, "GTX: post-only order would cross")
            elif e.code == ERR_MARGIN_INSUFFICIENT:
                self._reject(o, "insufficient margin")
                if time.monotonic() - self._last_margin_alert > 300:
                    self._last_margin_alert = time.monotonic()
                    self._alert("⚠️ Order rejected: insufficient margin", Priority.HIGH)
            elif e.status >= 500 or e.code is None:
                log.warning("order outcome unknown, will query", extra={"error": str(e)})
                asyncio.get_running_loop().create_task(self._resolve(o.client_id))
            else:  # precision / filter / other request errors: it never reached the book
                self._reject(o, f"{e.code}: {e.msg}")
                log.warning("order rejected", extra={"client_id": o.client_id, "error": str(e)})
        except (asyncio.TimeoutError, OSError) as e:
            log.warning("order send failed, outcome unknown, will query",
                        extra={"client_id": o.client_id, "error": type(e).__name__})
            asyncio.get_running_loop().create_task(self._resolve(o.client_id))
        return o

    async def cancel(self, client_id: str) -> None:
        o = self.orders.get(client_id)
        if o is None or o.is_terminal or o.status is OrderStatus.PENDING_CANCEL:
            return
        o.transition(OrderStatus.PENDING_CANCEL)
        try:
            async with self._timed("cancel"):
                await self.rest.cancel_order(self.symbol, client_id)
        except BinanceHTTPError as e:
            if e.code == ERR_UNKNOWN_ORDER:  # already filled/cancelled — find out which
                await self._resolve(client_id)
            else:
                log.warning("cancel failed", extra={"client_id": client_id, "error": str(e)})
                asyncio.get_running_loop().create_task(self._resolve(client_id))
        except (asyncio.TimeoutError, OSError) as e:
            log.warning("cancel send failed", extra={"client_id": client_id, "error": type(e).__name__})
            asyncio.get_running_loop().create_task(self._resolve(client_id))

    async def cancel_all(self) -> None:
        for o in self.working_orders():
            if o.status is not OrderStatus.PENDING_CANCEL:
                o.transition(OrderStatus.PENDING_CANCEL)
        async with self._timed("cancel_all"):
            await self.rest.cancel_all(self.symbol)
        # The exchange confirmed nothing is open any more; fills that raced the cancel
        # still arrive through the user stream and are applied regardless of local state.
        for o in self.working_orders():
            o.status = OrderStatus.CANCELED

    async def flatten(self, position_qty: float) -> None:
        """Close the position with a reduce-only MARKET order (taker)."""
        qty = abs(position_qty)
        if self.filters.floor_qty(qty) <= 0:
            return
        side = Side.SELL if position_qty > 0 else Side.BUY
        cid = self.ids.next(side)
        log.warning("flattening position", extra={"qty": qty, "side": side.value})
        await self.rest.new_order(symbol=self.symbol, side=side.value, type="MARKET",
                                  quantity=self.filters.format_qty(qty), reduceOnly="true",
                                  newClientOrderId=cid, newOrderRespType="ACK")

    # ---- exchange state -------------------------------------------------------

    async def fetch_position(self) -> tuple[float, float]:
        """(signed qty, entry price) from positionRisk (one-way mode)."""
        rows = await self.rest.position_risk(self.symbol)
        row = next((r for r in rows if r.get("positionSide", "BOTH") == "BOTH"), rows[0] if rows else None)
        if row is None:
            return 0.0, 0.0
        self.position_qty = float(row["positionAmt"])
        return self.position_qty, float(row["entryPrice"])

    async def resync(self) -> None:
        """Reconcile local orders with the exchange (after reconnects and periodically)."""
        open_ids = {o["clientOrderId"] for o in await self.rest.open_orders(self.symbol)}
        for o in self.working_orders():
            if o.client_id in open_ids:
                continue
            if time.monotonic() - self._sent_mono.get(o.client_id, 0) < 3:
                continue  # just sent; may not be visible yet
            await self._resolve(o.client_id)
        strays = open_ids - set(self.orders)
        if strays:
            log.warning("open orders not created by this session", extra={"client_ids": sorted(strays)})
        for cid in [c for c, o in self.orders.items() if o.is_terminal]:
            if time.monotonic() - self._sent_mono.get(cid, 0) > 300:
                self.orders.pop(cid, None)
                self._sent_mono.pop(cid, None)

    async def _resolve(self, client_id: str) -> None:
        """Ask the exchange for the truth about one order and apply it."""
        o = self.orders.get(client_id)
        if o is None:
            return
        try:
            r = await self.rest.query_order(self.symbol, client_id)
        except BinanceHTTPError as e:
            if e.code == ERR_NO_SUCH_ORDER:  # never reached the book (or GTX-expired, purged)
                if not o.is_terminal:
                    o.status = OrderStatus.CANCELED if o.status is OrderStatus.PENDING_CANCEL else OrderStatus.REJECTED
                return
            log.warning("order query failed", extra={"client_id": client_id, "error": str(e)})
            return
        executed = float(r.get("executedQty", 0))
        if executed > o.filled_qty + 1e-12:
            # Fill(s) we never saw on the stream: book them at the average price.
            px = float(r.get("avgPrice") or o.price)
            missing = executed - o.filled_qty
            log.warning("recovered missed fill", extra={"client_id": client_id, "qty": missing})
            self._emit(Fill(int(r.get("updateTime", 0)), client_id, o.side, px, missing,
                            px * missing * self.maker_fee_bps / 1e4))
            o.filled_qty = executed
        self._apply_status(o, r.get("status", ""))

    # ---- user data stream --------------------------------------------------------

    def handle_user_event(self, msg: dict[str, Any]) -> None:
        e = msg.get("e")
        if e == "ORDER_TRADE_UPDATE":
            self._on_order_update(msg["o"], int(msg.get("E", 0)))
        elif e == "ACCOUNT_UPDATE":
            self._on_account_update(msg["a"], int(msg.get("E", 0)))

    def _on_order_update(self, o: dict[str, Any], event_ms: int) -> None:
        if o.get("s") != self.symbol:
            return
        cid, exec_type, status = o["c"], o["x"], o["X"]
        order = self.orders.get(cid)
        if exec_type == "TRADE":
            qty, px = float(o["l"]), float(o["L"])
            fee_asset, fee = o.get("N"), float(o.get("n") or 0)
            if fee_asset not in (None, "USDT"):
                if not self._warned_fee_asset:
                    self._warned_fee_asset = True
                    log.warning("commission paid in non-USDT asset; estimating fee in USDT",
                                extra={"asset": fee_asset})
                fee = px * qty * self.maker_fee_bps / 1e4
            side = Side(o["S"])
            self._emit(Fill(int(o.get("T", event_ms)), cid, side, px, qty, fee))
            if order is not None:
                order.filled_qty = min(float(o.get("z", order.filled_qty + qty)), order.qty)
        if order is None:
            return
        if exec_type == "EXPIRED" and status == "EXPIRED":
            self._emit(OrderRejected(event_ms, cid, "expired (post-only)"))
        self._apply_status(order, status)

    def _apply_status(self, o: Order, status: str) -> None:
        new = _STATUS.get(status)
        if new is None or new is o.status:
            return
        if new in TERMINAL:
            o.status = new  # exchange is authoritative for final states
            return
        if o.status is OrderStatus.PENDING_CANCEL:
            return  # an ack/partial fill racing our cancel doesn't make it "working" again
        try:
            o.transition(new)
        except InvalidTransition:
            log.debug("ignored status update", extra={"client_id": o.client_id, "status": status})

    def _on_account_update(self, a: dict[str, Any], event_ms: int) -> None:
        for p in a.get("P", []):
            if p.get("s") == self.symbol and p.get("ps", "BOTH") == "BOTH":
                self.position_qty = float(p["pa"])
        if a.get("m") == "FUNDING_FEE":
            for b in a.get("B", []):
                if b.get("a") == "USDT":
                    self._emit(FundingPayment(event_ms, self._funding_rate, self._mark,
                                              self.position_qty, float(b["bc"])))

    @contextlib.asynccontextmanager
    async def _timed(self, op: str) -> AsyncIterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            if self.on_latency:
                self.on_latency(op, time.perf_counter() - t0)

    def _reject(self, o: Order, reason: str) -> None:
        if not o.is_terminal:
            o.status = OrderStatus.REJECTED
        self._emit(OrderRejected(int(time.time() * 1000), o.client_id, reason))

    def _emit(self, ev: ExecEvent) -> None:
        for h in self._handlers:
            try:
                h(ev)
            except Exception:
                log.exception("exec handler failed")
