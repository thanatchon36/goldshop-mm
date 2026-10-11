import aiohttp

from goldshop.execution.base import Fill, OrderRejected
from goldshop.execution.orders import Side
from goldshop.ops.health import start_health_server
from goldshop.ops.metrics import Metrics, Snapshot
from goldshop.state.markout import MarkoutSample


def snap(**kw):
    base = dict(mode="paper", symbol="BTCUSDT", net=1.5, realized=2.0, unrealized=-0.1, fees=0.4,
                funding=0.0, net_today=0.7, inventory_qty=0.002, mark=100_000.0,
                market_spread_bps=0.01, quote_spread_bps=5.0, sigma_bps=0.2, quoting=True,
                stale=False, paused=False, daily_halted=False, working_orders=2, ws_reconnects=3,
                user_stream_reconnects=0, uptime_s=60.0, telegram_dropped=0)
    return Snapshot(**{**base, **kw})


def test_state_gauges_and_event_metrics():
    m = Metrics(lambda: snap())
    m.on_exec(Fill(1, "x", Side.BUY, 100_000.0, 0.001, 0.02))
    m.on_exec(OrderRejected(1, "y", "GTX"))
    m.on_markout(MarkoutSample(1, 5000, -1.5))
    m.on_latency("place", 0.08)
    text = m.render().decode()
    assert 'goldshop_pnl_usdt{kind="net",mode="paper",symbol="BTCUSDT"} 1.5' in text
    assert 'goldshop_pnl_usdt{kind="fees",mode="paper",symbol="BTCUSDT"} -0.4' in text
    assert 'goldshop_inventory_notional_usdt{mode="paper",symbol="BTCUSDT"} 200.0' in text
    assert 'goldshop_fills_total{side="BUY"} 1.0' in text
    assert "goldshop_fill_volume_usdt_total 100.0" in text
    assert "goldshop_order_rejects_total 1.0" in text
    assert 'goldshop_markout_bps_count{horizon="5s"} 1.0' in text
    assert 'goldshop_order_latency_seconds_count{op="place"} 1.0' in text
    assert 'goldshop_ws_reconnects{mode="paper",symbol="BTCUSDT"} 3.0' in text


def test_missing_spreads_are_omitted_not_zero():
    text = Metrics(lambda: snap(market_spread_bps=None, quote_spread_bps=None)).render().decode()
    assert "goldshop_quote_spread_bps{" not in text


async def test_metrics_endpoint(unused_tcp_port):
    m = Metrics(lambda: snap())
    runner = await start_health_server(unused_tcp_port, lambda: (True, "ok"), m.render)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{unused_tcp_port}/metrics") as r:
                assert r.status == 200 and r.headers["Content-Type"].startswith("text/plain")
                assert "goldshop_quoting" in await r.text()
    finally:
        await runner.cleanup()
