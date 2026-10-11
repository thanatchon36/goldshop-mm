"""User Data Stream: order/fill/position events over WebSocket, with listenKey keepalive.

A fresh listenKey is created on every (re)connect; it is kept alive every
`keepalive_s` (Binance expires it after 60 min without a keepalive).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from goldshop.binance.rest import BinanceRestClient
from goldshop.market_data.feed import backoff_delay

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], None]
ReconnectHandler = Callable[[], Awaitable[None]]


class UserDataStream:
    def __init__(self, rest: BinanceRestClient, ws_private_url: str, handler: EventHandler,
                 on_connected: ReconnectHandler | None = None, keepalive_s: float = 30 * 60,
                 backoff_initial_s: float = 1.0, backoff_max_s: float = 60.0) -> None:
        self.rest = rest
        self.url = ws_private_url
        self.handler = handler
        self.on_connected = on_connected
        self.keepalive_s = keepalive_s
        self.backoff = (backoff_initial_s, backoff_max_s)
        self.connected = False
        self.disconnected_since = time.monotonic()
        self.reconnects = 0
        self._stop = asyncio.Event()
        self.ready = asyncio.Event()  # set after the first successful connect

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            keepalive: asyncio.Task[None] | None = None
            try:
                key = await self.rest.new_listen_key()
                async with websockets.connect(f"{self.url}?listenKey={key}", open_timeout=10,
                                              close_timeout=2) as ws:
                    self.connected = True
                    attempt = 0
                    log.info("user data stream connected")
                    keepalive = asyncio.create_task(self._keepalive())
                    if self.on_connected:
                        await self.on_connected()  # resync state missed while disconnected
                    self.ready.set()
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("e") == "listenKeyExpired":
                            log.warning("listenKey expired, reconnecting")
                            break
                        try:
                            self.handler(msg)
                        except Exception:
                            log.exception("user stream handler failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("user data stream error", extra={"error": f"{type(e).__name__}: {e}"})
            finally:
                if keepalive:
                    keepalive.cancel()
                if self.connected:
                    self.disconnected_since = time.monotonic()
                self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            delay = backoff_delay(attempt, *self.backoff)
            attempt += 1
            log.info("user data stream reconnecting", extra={"delay_s": round(delay, 2)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive_s)
            try:
                await self.rest.keepalive_listen_key()
                log.debug("listenKey keepalive sent")
            except Exception as e:
                log.warning("listenKey keepalive failed", extra={"error": str(e)})
