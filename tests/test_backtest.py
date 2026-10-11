import json

import pytest

from goldshop.backtest.replay import load_events, run_backtest
from goldshop.backtest.report import summarize, write_report
from goldshop.config import AppConfig, MarketDataConfig, PaperConfig, StrategyConfig
from goldshop.market_data.models import BookTicker, MarkPrice, Trade
from goldshop.market_data.recorder import ParquetRecorder

T0 = 1_791_708_000_000


def synthetic(n_s=120):
    """Mid oscillates ±3 bps around 100k; trades print 5 bps away on both sides every 2 s."""
    ev = []
    for i in range(n_s * 4):  # 4 book updates per second
        ms = T0 + i * 250
        mid = 100_000 * (1 + 3e-4 * ((i // 8) % 2 * 2 - 1) * 0.1)
        ev.append(BookTicker(0, ms, ms, i, round(mid - 0.05, 1), 1.0, round(mid + 0.05, 1), 1.0))
        if i % 8 == 0:
            ev.append(Trade(0, ms + 1, ms + 1, i, mid * (1 - 5e-4), 1.0, True))
            ev.append(Trade(0, ms + 2, ms + 2, i + 1, mid * (1 + 5e-4), 1.0, False))
        if i % 4 == 0:
            ev.append(MarkPrice(0, ms, mid, mid, 0.0001, T0 + 60_000))
    return ev


def cfg():
    return AppConfig(
        strategy=StrategyConfig(vol_warmup_s=5, requote_min_interval_ms=0),
        paper=PaperConfig(latency_ms=100),
        market_data=MarketDataConfig(stale_after_s=5),
    )


async def test_backtest_round_trips_and_report(tmp_path, btc_filters):
    res = await run_backtest(cfg(), btc_filters, synthetic())
    s = summarize(res)
    assert s["fills"] > 4 and s["buys"] > 0 and s["sells"] > 0
    assert s["fees"] == pytest.approx(sum(f.fee for f in res.fills))
    assert s["fees"] > 0
    assert abs(s["final_inventory"]) <= res.params.max_inventory_qty + 1e-12
    assert {"+1s", "+5s", "+30s"} <= set(s["markout_avg_bps"])
    path = write_report(res, tmp_path)
    html = path.read_text()
    assert "Net PnL" in html and "Markout distribution" in html and "__DATA__" not in html
    assert json.loads((tmp_path / "summary.json").read_text())["fills"] == s["fills"]
    assert (tmp_path / "fills.csv").read_text().count("\n") == s["fills"] + 1


async def test_data_gap_is_treated_as_stale(btc_filters):
    ev = synthetic(60)
    gap = 20_000
    shifted = ev[:200] + [type(e)(*[getattr(e, f) + gap if f in ("event_ms", "txn_ms", "trade_ms")
                                    else getattr(e, f) for f in e.__dataclass_fields__])
                          for e in ev[200:]]
    res = await run_backtest(cfg(), btc_filters, shifted)
    assert res.stale_gaps == 1


async def test_load_events_merges_streams_in_time_order(tmp_path):
    rec = ParquetRecorder(tmp_path, "BTCUSDT", 10)
    for e in synthetic(10):
        rec.handle(e)
    await rec.close()
    events = load_events(tmp_path, "BTCUSDT")
    assert len(events) == len(synthetic(10))
    assert [e.event_ms for e in events] == sorted(e.event_ms for e in events)
    assert {type(e) for e in events} == {BookTicker, Trade, MarkPrice}


async def test_load_events_skips_file_being_written(tmp_path):
    rec = ParquetRecorder(tmp_path, "BTCUSDT", 10)
    for e in synthetic(5):
        rec.handle(e)
    await rec.flush()  # writer still open → no parquet footer yet
    assert load_events(tmp_path, "BTCUSDT") == []
    await rec.close()
    assert load_events(tmp_path, "BTCUSDT")
