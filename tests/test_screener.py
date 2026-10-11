import math
import statistics

from goldshop.config import ScreenerConfig
from goldshop.screener.core import (
    goldshop_score,
    net_edge_bps,
    scan,
    sigma_1m_bps,
    spread_bps,
    usdt_perpetuals,
)


def test_spread_bps():
    assert math.isclose(spread_bps(99.95, 100.05), 10.0)


def test_sigma_constant_prices_is_zero():
    assert sigma_1m_bps([100.0] * 10) == 0.0


def test_sigma_matches_sample_stdev_of_log_returns():
    closes = [100.0, 101.0, 100.5, 102.0, 101.0]
    rets = [math.log(b / a) * 1e4 for a, b in zip(closes, closes[1:])]
    assert math.isclose(sigma_1m_bps(closes), statistics.stdev(rets), rel_tol=1e-9)


def test_sigma_too_few_points_is_nan():
    assert math.isnan(sigma_1m_bps([100.0, 101.0]))


def test_net_edge_and_score():
    assert net_edge_bps(10.0, 2.0) == 6.0
    assert goldshop_score(6.0, 3.0) == 2.0
    assert math.isnan(goldshop_score(6.0, 0.0))
    assert math.isnan(goldshop_score(6.0, math.nan))


def test_usdt_perpetuals_filter():
    info = {"symbols": [
        {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "BTCUSDT_250926", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "ETHUSDC", "contractType": "PERPETUAL", "quoteAsset": "USDC", "status": "TRADING"},
        {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "SETTLING"},
    ]}
    assert usdt_perpetuals(info) == {"BTCUSDT"}


class FakeClient:
    def __init__(self):
        self.kline_calls: list[str] = []

    async def exchange_info(self):
        return {"symbols": [
            {"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}
            for s in ("AUSDT", "BUSDT", "LOWUSDT")
        ]}

    async def ticker_24h_all(self):
        return [{"symbol": "AUSDT", "quoteVolume": "1e9"}, {"symbol": "BUSDT", "quoteVolume": "1e9"},
                {"symbol": "LOWUSDT", "quoteVolume": "10"}]

    async def book_ticker_all(self):
        return [{"symbol": "AUSDT", "bidPrice": "99.9", "askPrice": "100.1"},   # 20 bps
                {"symbol": "BUSDT", "bidPrice": "99.99", "askPrice": "100.01"},  # 2 bps
                {"symbol": "LOWUSDT", "bidPrice": "1", "askPrice": "2"}]

    async def klines(self, symbol, interval, limit):
        self.kline_calls.append(symbol)
        closes = [100.0, 100.1] * (limit // 2 + 1)
        return [[0, 0, 0, 0, str(c)] for c in closes[:limit]]


async def test_scan_filters_volume_before_klines_and_ranks():
    c = FakeClient()
    rows = await scan(c, ScreenerConfig(min_quote_volume_usdt=1e6, kline_minutes=20), maker_fee_bps=2.0)
    assert "LOWUSDT" not in c.kline_calls
    assert [r.symbol for r in rows] == ["AUSDT", "BUSDT"]
    assert rows[0].net_edge_bps > 0 > rows[1].net_edge_bps
