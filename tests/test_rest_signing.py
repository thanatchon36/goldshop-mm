import hashlib
import hmac
import urllib.parse

from aiohttp import web
from pydantic import SecretStr

from goldshop.binance.rest import BinanceHTTPError, BinanceRestClient, OrderRateLimited
from goldshop.config import Credentials

import pytest


async def start(handler, port):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


CREDS = Credentials(api_key=SecretStr("KEY123"), api_secret=SecretStr("SECRET456"))


async def test_signed_request_is_valid_hmac_and_sends_key(unused_tcp_port):
    seen = {}

    async def handler(req):
        if req.path == "/fapi/v1/time":
            return web.json_response({"serverTime": 1_000_000})
        qs = req.query_string
        body, sig = qs.rsplit("&signature=", 1)
        seen["valid"] = hmac.new(b"SECRET456", body.encode(), hashlib.sha256).hexdigest() == sig
        seen["key"] = req.headers.get("X-MBX-APIKEY")
        seen["params"] = dict(urllib.parse.parse_qsl(body))
        return web.json_response({"ok": 1}, headers={"X-MBX-ORDER-COUNT-10S": "3",
                                                     "X-MBX-USED-WEIGHT-1M": "-1"})

    runner = await start(handler, unused_tcp_port)
    try:
        async with BinanceRestClient(f"http://127.0.0.1:{unused_tcp_port}", credentials=CREDS,
                                     recv_window_ms=4000) as c:
            await c.sync_time()
            await c.new_order(symbol="BTCUSDT", side="BUY")
            assert seen["valid"] and seen["key"] == "KEY123"
            assert seen["params"]["recvWindow"] == "4000" and "timestamp" in seen["params"]
            assert c.order_count_10s == 3 and c.used_weight_1m == 0  # testnet's -1 ignored
            assert abs(int(seen["params"]["timestamp"]) - 1_000_000) < 5_000  # offset applied
    finally:
        await runner.cleanup()


async def test_timestamp_error_resyncs_and_retries_once(unused_tcp_port):
    n = {"order": 0, "time": 0}

    async def handler(req):
        if req.path == "/fapi/v1/time":
            n["time"] += 1
            return web.json_response({"serverTime": 5})
        n["order"] += 1
        if n["order"] == 1:
            return web.json_response({"code": -1021, "msg": "Timestamp outside recvWindow"}, status=400)
        return web.json_response({"status": "NEW"})

    runner = await start(handler, unused_tcp_port)
    try:
        async with BinanceRestClient(f"http://127.0.0.1:{unused_tcp_port}", credentials=CREDS) as c:
            assert (await c.new_order(symbol="X"))["status"] == "NEW"
            assert n == {"order": 2, "time": 1}
    finally:
        await runner.cleanup()


async def test_errors_carry_binance_code(unused_tcp_port):
    async def handler(req):
        return web.json_response({"code": -5022, "msg": "post only"}, status=400)

    runner = await start(handler, unused_tcp_port)
    try:
        async with BinanceRestClient(f"http://127.0.0.1:{unused_tcp_port}", credentials=CREDS) as c:
            with pytest.raises(BinanceHTTPError) as ei:
                await c.new_order(symbol="X")
            assert ei.value.code == -5022
    finally:
        await runner.cleanup()


async def test_order_budget_guard_refuses_locally():
    c = BinanceRestClient("http://unused", credentials=CREDS)
    import time
    c.order_count_10s, c._order_count_at = 290, time.monotonic()
    with pytest.raises(OrderRateLimited):
        c._check_order_budget()
    c._order_count_at -= 11  # window passed
    c._check_order_budget()


async def test_signed_call_without_credentials_fails(unused_tcp_port):
    async with BinanceRestClient("http://127.0.0.1:1") as c:
        with pytest.raises(RuntimeError, match="without credentials"):
            await c.position_mode()


def test_endpoints_never_mix_modes():
    from goldshop.config import ExchangeConfig, Mode
    ex = ExchangeConfig()
    t = ex.endpoints(Mode.TESTNET)
    assert all("binancefuture.com" in u for u in (t.rest.replace("demo-fapi.binance.com", "binancefuture.com"),
                                                    t.ws_public, t.ws_market, t.ws_private))
    assert "fapi.binance.com" not in t.rest.replace("demo-fapi", "")
    live = ex.endpoints(Mode.LIVE)
    assert live.rest == "https://fapi.binance.com" and "binancefuture" not in live.ws_private
