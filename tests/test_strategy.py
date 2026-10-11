import math

import pytest

from goldshop.binance.symbol import SymbolFilters, SymbolRuleError
from goldshop.strategy.avellaneda import MarketState, StrategyParams, compute_quote
from goldshop.strategy.volatility import EwmaVolatility


def params(**kw):
    base = dict(gamma=0.05, kappa=0.5, horizon_s=60, maker_fee_bps=2.0, min_spread_buffer_bps=1.0,
                order_qty=0.001, max_inventory_qty=0.005)
    return StrategyParams(**{**base, **kw})


def state(inv=0.0, sigma=1.0, bid=100_000.0, ask=100_000.1):
    return MarketState(bid, ask, sigma, inv)


def test_flat_inventory_is_symmetric_and_respects_min_spread(btc_filters):
    q = compute_quote(state(), params(), btc_filters)
    assert q.reservation_px == pytest.approx(q.mid)
    assert q.spread_bps >= 2 * 2.0 + 1.0
    # actual quoted distance (after tick rounding) is at least the min spread
    assert (q.ask_px - q.bid_px) / q.mid * 1e4 >= 5.0 - 1e-9
    assert q.mid - q.bid_px == pytest.approx(q.ask_px - q.mid, abs=0.11)
    assert q.at_limit is None


def test_min_spread_floor_applies_when_as_spread_is_small(btc_filters):
    q = compute_quote(state(sigma=0.0), params(kappa=100.0), btc_filters)
    assert q.spread_bps == pytest.approx(5.0)


def test_as_spread_used_when_wider_than_floor(btc_filters):
    p = params()
    q = compute_quote(state(sigma=3.0), p, btc_filters)
    expected = p.gamma * 9 * 60 + (2 / p.gamma) * math.log(1 + p.gamma / p.kappa)
    assert q.spread_bps == pytest.approx(expected)


def test_long_inventory_skews_quotes_down(btc_filters):
    flat = compute_quote(state(), params(), btc_filters)
    long = compute_quote(state(inv=0.003), params(), btc_filters)
    short = compute_quote(state(inv=-0.003), params(), btc_filters)
    assert long.reservation_px < flat.reservation_px < short.reservation_px
    assert long.ask_px < flat.ask_px and long.bid_px < flat.bid_px


def test_prices_on_tick_and_never_cross(btc_filters):
    # huge skew would push the ask below best bid → clamped to post-only safe price
    q = compute_quote(state(inv=0.005, sigma=5.0), params(max_inventory_qty=1.0), btc_filters)
    for px in (q.bid_px, q.ask_px):
        assert round(px / 0.1, 6) == round(px / 0.1)
    assert q.ask_px > 100_000.0 and q.bid_px < 100_000.1


def test_inventory_limit_quotes_only_reducing_side(btc_filters):
    q = compute_quote(state(inv=0.005), params(), btc_filters)
    assert q.bid_px is None and q.ask_px is not None and q.at_limit == "long"
    q = compute_quote(state(inv=-0.005), params(), btc_filters)
    assert q.ask_px is None and q.bid_px is not None and q.at_limit == "short"
    q = compute_quote(state(inv=0.004), params(), btc_filters)  # one more lot still fits
    assert q.bid_px is not None and q.at_limit is None


def test_ewma_vol_warmup_and_estimate():
    v = EwmaVolatility(halflife_s=30, warmup_s=10)
    mid = 100.0
    v.update(0, mid)
    assert not v.ready
    r = 2e-4  # 2 bps per second, alternating
    for i in range(1, 200):
        mid *= math.exp(r if i % 2 else -r)
        v.update(i * 1000, mid)
    assert v.ready
    assert v.sigma_bps == pytest.approx(2.0, rel=1e-6)


def test_ewma_vol_ignores_sub_second_updates_and_scales_gaps():
    v = EwmaVolatility(halflife_s=30, warmup_s=0)
    v.update(0, 100.0)
    v.update(500, 200.0)  # < 1s later: ignored
    assert v.sigma_bps == 0.0
    v.update(4000, 100.0 * math.exp(4e-4))  # 4 bps over 4 s → variance 16/4 = 4 bps²/s
    assert v.sigma_bps == pytest.approx(2.0)


def test_order_qty_from_notional(btc_filters):
    assert btc_filters.order_qty_for_notional(100, 83_000) == 0.001
    assert btc_filters.order_qty_for_notional(500, 83_000) == 0.006
    with pytest.raises(SymbolRuleError, match="below BTCUSDT minimums"):
        btc_filters.order_qty_for_notional(60, 83_000)  # rounds to 0 lots


def test_filters_from_exchange_info():
    info = {"symbols": [{"symbol": "ADAUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.00010"},
        {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ]}]}
    f = SymbolFilters.from_exchange_info(info, "ADAUSDT")
    assert f.floor_price(0.25678) == 0.2567 and f.ceil_price(0.25671) == 0.2568
    assert f.floor_qty(12.9) == 12.0
    with pytest.raises(SymbolRuleError):
        SymbolFilters.from_exchange_info(info, "NOPEUSDT")
