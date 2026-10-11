import pytest

from goldshop.accounting import Portfolio
from goldshop.execution.orders import Side
from goldshop.state.markout import MarkoutTracker


def test_round_trip_realizes_spread_minus_fees():
    p = Portfolio()
    p.apply_fill(Side.BUY, 100.0, 1.0, fee=0.02)
    p.apply_fill(Side.SELL, 100.1, 1.0, fee=0.02)
    assert p.position_qty == 0
    assert p.realized == pytest.approx(0.1)
    assert p.net(100.0) == pytest.approx(0.06)
    assert p.fill_count == 2 and p.volume_quote == pytest.approx(200.1)


def test_average_cost_and_unrealized():
    p = Portfolio()
    p.apply_fill(Side.BUY, 100.0, 1.0, 0)
    p.apply_fill(Side.BUY, 102.0, 1.0, 0)
    assert p.avg_price == pytest.approx(101.0)
    assert p.unrealized(103.0) == pytest.approx(4.0)
    p.apply_fill(Side.SELL, 104.0, 0.5, 0)
    assert p.realized == pytest.approx(1.5) and p.position_qty == pytest.approx(1.5)
    assert p.avg_price == pytest.approx(101.0)


def test_flip_position():
    p = Portfolio()
    p.apply_fill(Side.SELL, 100.0, 1.0, 0)
    p.apply_fill(Side.BUY, 99.0, 3.0, 0)
    assert p.realized == pytest.approx(1.0)
    assert p.position_qty == pytest.approx(2.0) and p.avg_price == pytest.approx(99.0)


def test_funding_in_net():
    p = Portfolio()
    p.apply_funding(-0.5)
    assert p.net(100.0) == pytest.approx(-0.5)


def test_markout_horizons_and_sign():
    m = MarkoutTracker((1000, 5000))
    m.add_fill(0, +1, 100.0)   # bought at 100
    m.add_fill(0, -1, 100.2)   # sold at 100.2
    assert m.on_mid(999, 101.0) == []
    s1 = m.on_mid(1000, 100.1)
    assert [round(s.bps, 6) for s in s1] == [10.0, round(-(100.1 - 100.2) / 100.2 * 1e4, 6)]
    m.on_mid(6000, 99.0)
    avg = m.averages()
    assert avg[5000] == pytest.approx((-100.0 + (100.2 - 99.0) / 100.2 * 1e4) / 2)
    assert m.averages(since_ms=1)[1000] is None
