import pytest

from goldshop.execution.base import Fill, FundingPayment, OrderRejected
from goldshop.execution.orders import ClientIdGenerator, OrderStatus, Side
from goldshop.execution.paper.executor import PaperExecutor
from goldshop.execution.paper.sim import PaperExchange
from goldshop.market_data.models import BookTicker, MarkPrice, Trade


def book(ms, bid=100.0, ask=100.1):
    return BookTicker(0, ms, ms, 0, bid, 1.0, ask, 1.0)


def trade(ms, px, qty=1.0):
    return Trade(0, ms, ms, 0, px, qty, True)


def mark(ms, rate=0.0001, nxt=0, px=100.0):
    return MarkPrice(0, ms, px, px, rate, nxt)


@pytest.fixture
def ex():
    sim = PaperExchange(latency_ms=100, maker_fee_bps=2.0)
    e = PaperExecutor(sim, ClientIdGenerator("t"))
    e.events = []
    e.on_exec_event(e.events.append)
    e.on_market_event(book(0))
    return e


async def test_order_not_live_until_latency_elapsed(ex):
    o = await ex.place(Side.BUY, 100.0, 1.0)
    ex.on_market_event(trade(50, 99.0))  # before arrival → no fill
    assert o.status is OrderStatus.PENDING_NEW and ex.events == []
    ex.on_market_event(book(100))
    assert o.status is OrderStatus.NEW


async def test_touch_does_not_fill_but_trade_through_does(ex):
    o = await ex.place(Side.BUY, 100.0, 1.0)
    ex.on_market_event(book(100))
    ex.on_market_event(trade(200, 100.0))  # touch
    assert ex.events == []
    ex.on_market_event(trade(300, 99.9, qty=0.4))  # through, partial
    ex.on_market_event(trade(400, 99.8, qty=5.0))
    fills = [e for e in ex.events if isinstance(e, Fill)]
    assert [f.qty for f in fills] == pytest.approx([0.4, 0.6])
    assert all(f.price == 100.0 for f in fills)  # filled at our price
    assert fills[0].fee == pytest.approx(100.0 * 0.4 * 2e-4)
    assert o.status is OrderStatus.FILLED
    assert ex.sim.position_qty == pytest.approx(1.0)
    assert ex.working_orders() == []


async def test_sell_fills_only_above_price(ex):
    await ex.place(Side.SELL, 100.2, 1.0)
    ex.on_market_event(book(100))
    ex.on_market_event(trade(200, 100.2))
    ex.on_market_event(trade(300, 100.1))
    assert ex.events == []
    ex.on_market_event(trade(400, 100.3))
    assert isinstance(ex.events[0], Fill) and ex.events[0].side is Side.SELL


async def test_gtx_reject_when_crossing_at_arrival(ex):
    # Valid when sent, but by arrival the ask has dropped to our bid.
    o = await ex.place(Side.BUY, 100.0, 1.0)
    ex.on_market_event(book(50, bid=99.9, ask=100.0))
    ex.on_market_event(book(150, bid=99.9, ask=100.0))
    assert o.status is OrderStatus.REJECTED
    assert isinstance(ex.events[0], OrderRejected) and "GTX" in ex.events[0].reason


async def test_order_can_fill_while_cancel_in_flight(ex):
    o = await ex.place(Side.BUY, 100.0, 1.0)
    ex.on_market_event(book(100))
    await ex.cancel(o.client_id)  # sent at t=100, arrives t=200
    ex.on_market_event(trade(150, 99.0))
    assert o.status is OrderStatus.FILLED
    ex.on_market_event(book(250))  # late cancel for a forgotten order is harmless
    assert [type(e) for e in ex.events] == [Fill]


async def test_cancel_takes_effect_after_latency(ex):
    o = await ex.place(Side.BUY, 100.0, 1.0)
    ex.on_market_event(book(100))
    await ex.cancel(o.client_id)
    ex.on_market_event(book(200))
    assert o.status is OrderStatus.CANCELED
    ex.on_market_event(trade(300, 90.0))
    assert ex.events == []


async def test_cancel_before_arrival(ex):
    o = await ex.place(Side.SELL, 100.5, 1.0)
    await ex.cancel(o.client_id)
    ex.on_market_event(book(150))
    assert o.status is OrderStatus.CANCELED


async def test_funding_paid_by_long_when_rate_positive(ex):
    ex.on_market_event(mark(0, rate=0.0001, nxt=1000, px=100.0))
    await ex.place(Side.BUY, 100.0, 2.0)
    ex.on_market_event(book(100))
    ex.on_market_event(trade(200, 99.0, qty=2.0))
    ex.on_market_event(mark(999, rate=0.0001, nxt=1000))
    assert not any(isinstance(e, FundingPayment) for e in ex.events)
    ex.on_market_event(mark(1001, rate=0.0002, nxt=2000, px=101.0))
    [fp] = [e for e in ex.events if isinstance(e, FundingPayment)]
    assert fp.amount == pytest.approx(-2.0 * 101.0 * 0.0001)  # long pays, rate at funding time
    assert fp.ts_ms == 1000


async def test_no_book_means_reject():
    sim = PaperExchange(latency_ms=0, maker_fee_bps=2.0)
    e = PaperExecutor(sim, ClientIdGenerator("t"))
    o = await e.place(Side.BUY, 100.0, 1.0)
    e.on_market_event(trade(10, 50.0))
    assert o.status is OrderStatus.REJECTED
