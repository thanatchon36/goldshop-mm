import asyncio
import json

import websockets

from goldshop.binance.user_stream import UserDataStream


class FakeRest:
    def __init__(self):
        self.keys = 0
        self.keepalives = 0

    async def new_listen_key(self):
        self.keys += 1
        return f"k{self.keys}"

    async def keepalive_listen_key(self):
        self.keepalives += 1


async def test_reconnects_with_new_listen_key_and_resyncs():
    paths = []

    async def handler(ws):
        paths.append(ws.request.path)
        await ws.send(json.dumps({"e": "ORDER_TRADE_UPDATE", "o": {}}))
        if len(paths) == 1:
            await ws.send(json.dumps({"e": "listenKeyExpired"}))
        await asyncio.sleep(10)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/private/ws"
    got, resyncs = [], []

    async def on_connected():
        resyncs.append(1)

    rest = FakeRest()
    uds = UserDataStream(rest, url, got.append, on_connected=on_connected, keepalive_s=0.05,
                         backoff_initial_s=0.01, backoff_max_s=0.02)
    task = asyncio.create_task(uds.run())
    try:
        await asyncio.sleep(0.5)
        assert paths[:2] == ["/private/ws?listenKey=k1", "/private/ws?listenKey=k2"]
        assert uds.connected and uds.reconnects == 1 and len(resyncs) == 2
        assert [m["e"] for m in got] == ["ORDER_TRADE_UPDATE", "ORDER_TRADE_UPDATE"]
        assert rest.keepalives >= 2
    finally:
        uds.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
