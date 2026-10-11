import asyncio
import json

import pytest
import websockets

from goldshop.config import MarketDataConfig
from goldshop.market_data.feed import MarketDataFeed, backoff_delay
from goldshop.market_data.models import BookTicker, Depth, MarkPrice, Trade, parse_event

BOOK = {"e": "bookTicker", "u": 5, "s": "BTCUSDT", "b": "100.0", "B": "1.5", "a": "100.1",
        "A": "2.0", "T": 1000, "E": 1001}
DEPTH = {"e": "depthUpdate", "E": 1001, "T": 1000, "s": "BTCUSDT", "U": 1, "u": 9, "pu": 0,
         "b": [["100.0", "1"], ["99.9", "2"]], "a": [["100.1", "3"]]}
TRADE = {"e": "aggTrade", "E": 1001, "a": 7, "s": "BTCUSDT", "p": "100.05", "q": "0.5",
         "f": 1, "l": 2, "T": 1000, "m": True}
MARK = {"e": "markPriceUpdate", "E": 1001, "s": "BTCUSDT", "p": "100.02", "i": "100.01",
        "P": "100.0", "r": "0.0001", "T": 1700000000000}


def test_parse_events():
    b = parse_event(BOOK, recv_ns=1)
    assert isinstance(b, BookTicker) and b.bid == 100.0 and b.ask_qty == 2.0 and b.mid == 100.05
    d = parse_event(DEPTH, recv_ns=1)
    assert isinstance(d, Depth) and d.bids[1] == (99.9, 2.0) and d.final_update_id == 9
    t = parse_event(TRADE, recv_ns=1)
    assert isinstance(t, Trade) and t.is_buyer_maker and t.price == 100.05
    m = parse_event(MARK, recv_ns=1)
    assert isinstance(m, MarkPrice) and m.funding_rate == 0.0001
    assert parse_event({"e": "somethingElse"}) is None


def test_backoff_grows_and_caps():
    for attempt, cap in [(0, 1), (1, 2), (3, 8), (10, 60)]:
        d = backoff_delay(attempt, 1.0, 60.0)
        assert cap / 2 <= d <= cap


def _cfg(**kw):
    base = dict(stale_after_s=0.5, reconnect_backoff_initial_s=0.05, reconnect_backoff_max_s=0.1)
    return MarketDataConfig(**{**base, **kw})


def test_stale_check_before_any_data_and_after_silence():
    f = MarketDataFeed("BTCUSDT", "ws://x", "ws://y", _cfg())
    stale, reason = f.check_stale(now_mono=100.0)
    assert stale and "no data yet" in reason
    for c in f._conns:
        c.last_msg_mono = 100.0
    assert f.check_stale(now_mono=100.2) == (False, "ok")
    stale, reason = f.check_stale(now_mono=101.0)
    assert stale and "no update" in reason


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


async def test_feed_reconnects_dispatches_and_flags_stale():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        # public and market both get the same payloads; parse_event ignores nothing relevant
        for data in (BOOK, DEPTH, TRADE, MARK):
            await ws.send(json.dumps({"stream": "x", "data": data}))
        await ws.close()  # force a reconnect

    server, url = await _serve(handler)
    events = []
    stale_changes = []
    feed = MarketDataFeed("BTCUSDT", url, url, _cfg())
    feed.on_event(events.append)
    feed.on_stale_change(lambda s, r: stale_changes.append(s))
    task = asyncio.create_task(feed.run())
    try:
        await asyncio.sleep(0.6)
        assert feed.state.reconnects >= 2
        assert connections >= 3
        assert {type(e) for e in events} == {BookTicker, Depth, Trade, MarkPrice}
        assert feed.state.book is not None and feed.state.mark is not None
        assert feed.reconnects_within(600) == feed.state.reconnects
    finally:
        feed.stop()
        await asyncio.wait_for(task, 5)
        server.close()
        await server.wait_closed()


async def test_feed_goes_stale_when_server_silent():
    sent = False

    async def handler(ws):
        nonlocal sent
        if not sent:  # only the first connection ever delivers data
            sent = True
            await ws.send(json.dumps({"stream": "x", "data": BOOK}))
        await asyncio.sleep(10)  # connected but silent

    server, url = await _serve(handler)
    changes = []
    # Same URL for both routes: one connection gets the message, the other never does,
    # so use a dedicated silent server for the second route.
    async def silent(ws):
        await ws.send(json.dumps({"stream": "x", "data": MARK}))
        await asyncio.sleep(10)

    server2, url2 = await _serve(silent)
    feed = MarketDataFeed("BTCUSDT", url, url2, _cfg(stale_after_s=0.3))
    feed.on_stale_change(lambda s, r: changes.append(s))
    task = asyncio.create_task(feed.run())
    try:
        await asyncio.sleep(0.2)
        assert feed.stale is False and changes == [False]
        await asyncio.sleep(0.5)
        assert feed.stale is True and changes == [False, True]
        server2.close()
    finally:
        feed.stop()
        await asyncio.wait_for(task, 5)
        server.close()
        await server.wait_closed()


def test_handler_exception_does_not_break_dispatch():
    f = MarketDataFeed("BTCUSDT", "ws://x", "ws://y", _cfg())
    seen = []
    f.on_event(lambda e: 1 / 0)
    f.on_event(seen.append)
    f._dispatch(json.dumps({"stream": "x", "data": BOOK}))
    assert len(seen) == 1


@pytest.mark.parametrize("raw", ['{"result": null, "id": 1}', '{"stream": "x", "data": {"e": "?"}}'])
def test_dispatch_ignores_non_events(raw):
    f = MarketDataFeed("BTCUSDT", "ws://x", "ws://y", _cfg())
    seen = []
    f.on_event(seen.append)
    f._dispatch(raw)
    assert seen == []
