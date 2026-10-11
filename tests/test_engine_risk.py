import pytest

from goldshop.config import RiskConfig, StrategyConfig
from goldshop.engine import Engine
from goldshop.execution.orders import ClientIdGenerator, Side
from goldshop.execution.paper.executor import PaperExecutor
from goldshop.execution.paper.sim import PaperExchange
from goldshop.market_data.models import BookTicker, Trade
from goldshop.risk.manager import RiskManager
from goldshop.state.store import StateStore
from goldshop.strategy.avellaneda import StrategyParams


def book(ms, bid=100_000.0, ask=100_000.1):
    return BookTicker(0, ms, ms, 0, bid, 1.0, ask, 1.0)


def make(btc_filters, tmp_path=None, **risk_kw):
    cfg = StrategyConfig(vol_warmup_s=0, requote_min_interval_ms=0)
    params = StrategyParams(cfg.gamma, cfg.kappa, cfg.horizon_s, 2.0, cfg.min_spread_buffer_bps,
                            order_qty=0.001, max_inventory_qty=0.01)
    alerts = []
    ex = PaperExecutor(PaperExchange(latency_ms=0, maker_fee_bps=2.0, taker_fee_bps=5.0),
                       ClientIdGenerator("t"))
    risk = RiskManager(RiskConfig(**risk_kw), alert=lambda t, p: alerts.append(t))
    store = StateStore(tmp_path, "paper") if tmp_path else None
    eng = Engine(btc_filters, params, cfg, ex, alert=lambda t, p: alerts.append(t), stale=False,
                 risk=risk, store=store)
    return eng, ex, alerts


async def warm(eng, n=3):
    for i in range(n):
        await eng.handle(book(i * 1000))


async def buy_fill(eng, ex, ms):
    bid = next(o for o in ex.working_orders() if o.side is Side.BUY)
    await eng.handle(Trade(0, ms, ms, ms, bid.price - 10, 1.0, True))


async def test_daily_loss_limit_flattens_and_halts(btc_filters):
    eng, ex, alerts = make(btc_filters, daily_loss_limit_usdt=0.5)
    await warm(eng)
    await buy_fill(eng, ex, 3000)
    assert eng.portfolio.position_qty == pytest.approx(0.001)
    # price drops 1% → unrealized −1 USDT on 0.001 BTC → beyond the 0.5 limit
    await eng.handle(book(4500, bid=99_000.0, ask=99_000.1))
    assert eng.portfolio.position_qty == pytest.approx(0.0)  # flattened at the bid (taker)
    assert any("Daily loss limit" in a for a in alerts)
    await eng.handle(book(6000, bid=99_000.0, ask=99_000.1))
    assert [o for o in ex.working_orders() if o.is_working] == []
    assert eng.quote_status.startswith("stopped: daily loss")


async def test_pause_cancels_and_resume_restarts(btc_filters):
    eng, ex, _ = make(btc_filters)
    await warm(eng)
    assert len(ex.working_orders()) == 2
    await eng.pause()
    await eng.handle(book(3000))
    assert not [o for o in ex.working_orders() if o.is_working]
    eng.risk.resume(3000)
    await eng.handle(book(4000))
    assert len([o for o in ex.working_orders() if o.is_working]) == 2


async def test_flatten_command_closes_and_pauses(btc_filters):
    eng, ex, _ = make(btc_filters)
    await warm(eng)
    await buy_fill(eng, ex, 3000)
    await eng.flatten("telegram /flatten")
    assert eng.portfolio.position_qty == pytest.approx(0.0)
    assert eng.portfolio.fees > 0.02  # maker + taker fee
    assert eng.risk.manual_pause


async def test_position_limit_blocks_increasing_side(btc_filters):
    eng, ex, _ = make(btc_filters, max_position_notional_usdt=150, max_order_notional_usdt=200)
    await warm(eng)
    await buy_fill(eng, ex, 3000)  # long 0.001 (~100 USDT); another buy would be ~200 > 150
    await eng.handle(book(3500))
    sides = sorted(o.side.value for o in ex.working_orders() if o.is_working)
    assert sides == ["SELL"]


async def test_circuit_breaker_stops_quoting(btc_filters):
    eng, ex, _ = make(btc_filters, circuit_breaker_pct=0.5, circuit_breaker_window_s=10)
    await warm(eng)
    await eng.handle(book(3500, bid=100_600.0, ask=100_600.1))
    await eng.handle(book(3600, bid=100_600.0, ask=100_600.1))
    assert not [o for o in ex.working_orders() if o.is_working]
    assert "circuit breaker" in eng.quote_status


async def test_state_persists_and_restores(btc_filters, tmp_path):
    eng, ex, _ = make(btc_filters, tmp_path)
    await warm(eng)
    await buy_fill(eng, ex, 3000)
    eng.store.put("engine:BTCUSDT", eng.to_state())
    await eng.store.close()
    eng2, _, _ = make(btc_filters, tmp_path)
    eng2.restore_state(eng2.store.get("engine:BTCUSDT"))
    assert eng2.portfolio.position_qty == pytest.approx(0.001)
    assert eng2.portfolio.fill_count == 1
    assert eng2.stats.day_start_ms == eng.stats.day_start_ms
    import sqlite3
    con = sqlite3.connect(tmp_path / "state-paper.sqlite")
    assert con.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM quotes").fetchone()[0] >= 1
