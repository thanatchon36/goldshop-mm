import pytest

from goldshop.config import StrategyConfig
from goldshop.engine import Engine
from goldshop.execution.orders import ClientIdGenerator, Side
from goldshop.execution.paper.executor import PaperExecutor
from goldshop.execution.paper.sim import PaperExchange
from goldshop.market_data.models import BookTicker, Trade
from goldshop.strategy.avellaneda import StrategyParams


def book(ms, bid=100_000.0, ask=100_000.1):
    return BookTicker(0, ms, ms, 0, bid, 1.0, ask, 1.0)


def make(btc_filters, **cfg_kw):
    cfg = StrategyConfig(**{**dict(vol_warmup_s=0, requote_min_interval_ms=0), **cfg_kw})
    params = StrategyParams(cfg.gamma, cfg.kappa, cfg.horizon_s, 2.0, cfg.min_spread_buffer_bps,
                            order_qty=0.001, max_inventory_qty=0.002)
    alerts = []
    ex = PaperExecutor(PaperExchange(latency_ms=0, maker_fee_bps=2.0), ClientIdGenerator("t"))
    eng = Engine(btc_filters, params, cfg, ex, alert=lambda t, p: alerts.append(t), stale=False)
    return eng, ex, alerts


async def warm(eng, start=0, n=3):
    for i in range(n):
        await eng.handle(book(start + i * 1000))


async def test_quotes_both_sides_after_warmup(btc_filters):
    eng, ex, _ = make(btc_filters)
    await eng.handle(book(0))
    assert ex.working_orders() == []  # σ not ready yet (needs ≥1 sample)
    await warm(eng, 1000)
    sides = sorted(o.side.value for o in ex.working_orders())
    assert sides == ["BUY", "SELL"]


async def test_no_replace_when_target_unchanged(btc_filters):
    eng, ex, _ = make(btc_filters)
    await warm(eng)
    ids = {o.client_id for o in ex.working_orders()}
    await eng.handle(book(3000))
    await eng.handle(book(3100))
    assert {o.client_id for o in ex.working_orders()} == ids


async def test_replace_when_target_moves_beyond_threshold(btc_filters):
    eng, ex, _ = make(btc_filters)
    await warm(eng)
    ids = {o.client_id for o in ex.working_orders()}
    await eng.handle(book(3000, bid=100_100.0, ask=100_100.1))  # +10 bps
    await eng.handle(book(3001, bid=100_100.0, ask=100_100.1))  # cancels land (latency 0)
    new = ex.working_orders()
    assert len(new) == 2 and not ids & {o.client_id for o in new}


async def test_stale_cancels_and_stops_quoting(btc_filters):
    eng, ex, _ = make(btc_filters)
    await warm(eng)
    await eng.set_stale(True)
    await eng.handle(book(5000))
    assert ex.working_orders() == []
    await eng.handle(book(6000))
    assert ex.working_orders() == []
    await eng.set_stale(False)
    await eng.handle(book(7000))
    assert len(ex.working_orders()) == 2


async def test_fill_updates_portfolio_and_inventory_limit_alert(btc_filters):
    eng, ex, alerts = make(btc_filters)
    await warm(eng)
    bid = next(o for o in ex.working_orders() if o.side is Side.BUY)
    for i in range(2):  # two buy fills → at max inventory 0.002
        await eng.handle(Trade(0, 3000 + i * 10, 3000 + i * 10, i, bid.price - 1, 1.0, True))
        await eng.handle(book(3001 + i * 10))
        bid = next((o for o in ex.working_orders() if o.side is Side.BUY), bid)
    assert eng.portfolio.position_qty == pytest.approx(0.002)
    assert eng.portfolio.fees > 0 and eng.portfolio.fill_count == 2
    await eng.handle(book(4000))
    assert [o.side for o in ex.working_orders()] == [Side.SELL]
    assert any("Inventory limit hit (long)" in a for a in alerts)
    assert eng.markout._pending  # fills tracked for markout
