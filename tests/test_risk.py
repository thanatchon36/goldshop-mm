
from goldshop.config import RiskConfig
from goldshop.execution.orders import Side
from goldshop.risk.manager import DAY_MS, RiskManager


def rm(**kw):
    alerts = []
    r = RiskManager(RiskConfig(**kw), alert=lambda t, p: alerts.append(t))
    return r, alerts


def test_order_and_position_limits():
    r, _ = rm(max_order_notional_usdt=200, max_position_notional_usdt=600)
    assert r.check_order(Side.BUY, 100_000, 0.001, 0, 0).ok
    assert not r.check_order(Side.BUY, 100_000, 0.003, 0, 0).ok  # 300 > 200 per order
    # long 0.005 (500) + working 0.001 + new 0.001 → 700 > 600
    assert not r.check_order(Side.BUY, 100_000, 0.001, 0.005, 0.001).ok
    # reducing side is always allowed even when over the position limit
    assert r.check_order(Side.SELL, 100_000, 0.001, 0.007, 0).ok


def test_daily_loss_limit_halts_until_next_utc_day_and_resume_refused():
    r, alerts = rm(daily_loss_limit_usdt=20)
    now = 10 * DAY_MS + 3_600_000
    assert not r.check_daily_loss(now, -19.99)
    assert r.check_daily_loss(now, -20.0)
    assert not r.check_daily_loss(now + 1, -25.0)  # only fires once
    assert any("Daily loss limit" in a for a in alerts)
    assert r.can_quote(now)[0] is False
    assert "refused" in r.resume(now)
    assert r.can_quote(11 * DAY_MS - 1)[0] is False
    assert r.can_quote(11 * DAY_MS)[0] is True
    assert any("New UTC day" in a for a in alerts)


def test_circuit_breaker_trips_and_clears_after_cooldown():
    r, alerts = rm(circuit_breaker_pct=0.5, circuit_breaker_window_s=10, circuit_breaker_cooldown_s=60)
    r.on_mid(0, 100.0)
    r.on_mid(5_000, 100.4)
    assert r.can_quote(5_000)[0]
    r.on_mid(9_000, 100.6)  # 0.6% within 10 s
    assert r.can_quote(9_000) == (False, "volatility circuit breaker")
    r.on_mid(30_000, 100.6)
    assert not r.can_quote(30_000)[0]
    r.on_mid(69_001, 100.6)
    assert r.can_quote(69_001)[0]
    assert any("Circuit breaker:" in a for a in alerts) and any("cleared" in a for a in alerts)


def test_slow_move_outside_window_does_not_trip():
    r, _ = rm(circuit_breaker_pct=0.5, circuit_breaker_window_s=10)
    for i in range(30):
        r.on_mid(i * 5_000, 100.0 * (1 + 0.001 * i))  # 0.1% per 5 s
    assert r.can_quote(150_000)[0]


def test_funding_guard_blocks_increasing_side():
    r, alerts = rm(funding_guard_rate=0.0005)
    r.on_funding_rate(0.001, position_qty=0.003)  # long and longs pay
    assert r.funding_reduce_only is Side.BUY
    assert not r.check_order(Side.BUY, 100, 0.001, 0.003, 0).ok
    assert r.check_order(Side.SELL, 100, 0.001, 0.003, 0).ok
    r.on_funding_rate(0.001, position_qty=-0.003)  # short and longs pay → fine
    assert r.funding_reduce_only is None
    r.on_funding_rate(-0.001, position_qty=-0.003)
    assert r.funding_reduce_only is Side.SELL
    assert len(alerts) == 3


def test_pause_resume():
    r, _ = rm()
    r.pause()
    assert r.can_quote(0) == (False, "paused by command")
    assert r.resume(0).startswith("▶️")
    assert r.can_quote(0)[0]
