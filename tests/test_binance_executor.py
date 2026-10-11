import pytest

from goldshop.binance.rest import BinanceHTTPError, OrderRateLimited
from goldshop.execution.base import Fill, FundingPayment, OrderRejected
from goldshop.execution.binance import BinanceExecutor
from goldshop.execution.orders import ClientIdGenerator, OrderStatus, Side
from goldshop.market_data.models import MarkPrice


class FakeRest:
    def __init__(self):
        self.calls = []
        self.fail_next: Exception | None = None
        self.open: list[dict] = []
        self.query: dict[str, dict] = {}

    def _maybe_fail(self):
        if self.fail_next:
            e, self.fail_next = self.fail_next, None
            raise e

    async def new_order(self, **p):
        self.calls.append(("new", p))
        self._maybe_fail()
        return {"status": "NEW"}

    async def cancel_order(self, symbol, cid):
        self.calls.append(("cancel", cid))
        self._maybe_fail()
        return {"status": "CANCELED"}

    async def cancel_all(self, symbol):
        self.calls.append(("cancel_all", symbol))

    async def query_order(self, symbol, cid):
        self.calls.append(("query", cid))
        if cid not in self.query:
            raise BinanceHTTPError(400, -2013, "Order does not exist.")
        return self.query[cid]

    async def open_orders(self, symbol):
        return self.open

    async def position_risk(self, symbol):
        return [{"positionAmt": "0.002", "entryPrice": "100000.0", "positionSide": "BOTH"}]


@pytest.fixture
def ex(btc_filters):
    rest = FakeRest()
    e = BinanceExecutor(rest, btc_filters, ClientIdGenerator("t"), maker_fee_bps=2.0)
    e.events = []
    e.on_exec_event(e.events.append)
    return e


def otu(cid, x, X, side="BUY", l="0", L="0", z="0", n="0", N="USDT", T=1):
    return {"e": "ORDER_TRADE_UPDATE", "E": T, "o": {"s": "BTCUSDT", "c": cid, "S": side, "x": x,
            "X": X, "l": l, "L": L, "z": z, "n": n, "N": N, "T": T}}


async def test_place_sends_post_only_gtx_with_exact_strings(ex):
    o = await ex.place(Side.BUY, 100000.10000000001, 0.0019999)
    kind, p = ex.rest.calls[0]
    assert p["type"] == "LIMIT" and p["timeInForce"] == "GTX"
    assert p["price"] == "100000.1" and p["quantity"] == "0.001"
    assert p["newClientOrderId"] == o.client_id == "t-b-1"
    assert o.status is OrderStatus.NEW


async def test_gtx_reject_marks_rejected(ex):
    ex.rest.fail_next = BinanceHTTPError(400, -5022, "would be maker")
    o = await ex.place(Side.SELL, 100000.0, 0.001)
    assert o.status is OrderStatus.REJECTED
    assert isinstance(ex.events[0], OrderRejected) and "GTX" in ex.events[0].reason
    assert ex.working_orders() == []


async def test_insufficient_margin_alerts(btc_filters):
    alerts = []
    e = BinanceExecutor(FakeRest(), btc_filters, ClientIdGenerator("t"), 2.0,
                        alert=lambda t, p: alerts.append(t))
    e.rest.fail_next = BinanceHTTPError(400, -2019, "Margin is insufficient.")
    o = await e.place(Side.BUY, 100000.0, 0.001)
    assert o.status is OrderStatus.REJECTED and "insufficient margin" in alerts[0]


async def test_local_order_rate_guard(ex):
    ex.rest.fail_next = OrderRateLimited("near limit")
    o = await ex.place(Side.BUY, 100000.0, 0.001)
    assert o.status is OrderStatus.REJECTED


async def test_fills_from_user_stream(ex):
    o = await ex.place(Side.BUY, 100000.0, 0.002)
    ex.handle_user_event(otu(o.client_id, "TRADE", "PARTIALLY_FILLED", l="0.001", L="100000", z="0.001", n="0.02"))
    ex.handle_user_event(otu(o.client_id, "TRADE", "FILLED", l="0.001", L="100000", z="0.002", n="0.02"))
    fills = [e for e in ex.events if isinstance(e, Fill)]
    assert [f.qty for f in fills] == [0.001, 0.001] and fills[0].fee == 0.02
    assert o.status is OrderStatus.FILLED and o.filled_qty == 0.002


async def test_fill_after_local_cancel_still_counts(ex):
    o = await ex.place(Side.BUY, 100000.0, 0.001)
    await ex.cancel_all()
    assert o.status is OrderStatus.CANCELED
    ex.handle_user_event(otu(o.client_id, "TRADE", "FILLED", l="0.001", L="100000", z="0.001"))
    assert any(isinstance(e, Fill) for e in ex.events)


async def test_ack_racing_cancel_does_not_resurrect_order(ex):
    o = await ex.place(Side.BUY, 100000.0, 0.001)
    await ex.cancel(o.client_id)
    assert o.status is OrderStatus.PENDING_CANCEL
    ex.handle_user_event(otu(o.client_id, "NEW", "NEW"))
    assert o.status is OrderStatus.PENDING_CANCEL
    ex.handle_user_event(otu(o.client_id, "CANCELED", "CANCELED"))
    assert o.status is OrderStatus.CANCELED


async def test_cancel_unknown_order_resolves_via_query(ex):
    o = await ex.place(Side.BUY, 100000.0, 0.001)
    ex.rest.fail_next = BinanceHTTPError(400, -2011, "Unknown order sent.")
    ex.rest.query[o.client_id] = {"status": "FILLED", "executedQty": "0.001", "avgPrice": "100000",
                                  "updateTime": 5}
    await ex.cancel(o.client_id)
    assert o.status is OrderStatus.FILLED
    # the fill was never seen on the stream → recovered from the query
    assert [e.qty for e in ex.events if isinstance(e, Fill)] == [0.001]


async def test_unknown_outcome_on_server_error_is_queried(ex):
    import asyncio
    ex.rest.fail_next = BinanceHTTPError(503, None, "busy")
    o = await ex.place(Side.BUY, 100000.0, 0.001)
    await asyncio.sleep(0)  # let the resolve task run
    assert ("query", o.client_id) in ex.rest.calls
    assert o.status is OrderStatus.REJECTED  # -2013: never reached the book


async def test_resync_resolves_orders_missing_on_exchange(ex, monkeypatch):
    o = await ex.place(Side.SELL, 100000.0, 0.001)
    ex._sent_mono[o.client_id] -= 10  # old enough
    ex.rest.query[o.client_id] = {"status": "CANCELED", "executedQty": "0"}
    await ex.resync()
    assert o.status is OrderStatus.CANCELED


async def test_funding_fee_from_account_update(ex):
    ex.on_market_event(MarkPrice(0, 1, 100000.0, 100000.0, 0.0001, 2))
    ex.handle_user_event({"e": "ACCOUNT_UPDATE", "E": 7, "a": {"m": "FUNDING_FEE",
        "B": [{"a": "USDT", "wb": "1", "cw": "1", "bc": "-0.0123"}],
        "P": [{"s": "BTCUSDT", "pa": "0.002", "ps": "BOTH"}]}})
    [fp] = [e for e in ex.events if isinstance(e, FundingPayment)]
    assert fp.amount == -0.0123 and fp.position_qty == 0.002 and fp.rate == 0.0001


async def test_flatten_is_reduce_only_market(ex):
    await ex.flatten(-0.003)
    kind, p = ex.rest.calls[-1]
    assert p["type"] == "MARKET" and p["side"] == "BUY" and p["reduceOnly"] == "true"
    assert p["quantity"] == "0.003"


async def test_fetch_position(ex):
    assert await ex.fetch_position() == (0.002, 100000.0)


async def test_other_symbol_events_ignored(ex):
    ev = otu("x", "TRADE", "FILLED", l="1", L="1")
    ev["o"]["s"] = "ETHUSDT"
    ex.handle_user_event(ev)
    assert ex.events == []
