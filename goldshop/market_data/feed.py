"""WebSocket market data feed: two connections, reconnect with backoff, staleness detection."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import websockets

from goldshop.config import MarketDataConfig
from goldshop.market_data.models import (
    BookTicker,
    Depth,
    MarketEvent,
    MarkPrice,
    Trade,
    parse_event,
)

log = logging.getLogger(__name__)

EventHandler = Callable[[MarketEvent], None]
StaleHandler = Callable[[bool, str], None]  # (is_stale, reason)


def backoff_delay(attempt: int, initial: float, maximum: float) -> float:
    """Exponential backoff with full jitter in [base/2, base]."""
    base = min(maximum, initial * (2 ** max(attempt, 0)))
    return random.uniform(base / 2, base)


@dataclass
class _Conn:
    name: str
    url: str
    last_msg_mono: float = 0.0
    connected: bool = False
    attempt: int = 0


@dataclass
class FeedState:
    book: BookTicker | None = None
    depth: Depth | None = None
    last_trade: Trade | None = None
    mark: MarkPrice | None = None
    reconnects: int = 0
    reconnect_times: list[float] = field(default_factory=list)  # monotonic


class MarketDataFeed:
    """Streams bookTicker+depth (public route) and aggTrade+markPrice (market route).

    Handlers run synchronously on the event loop and must not block.
    `stale` becomes True when any connection has been silent for `stale_after_s`;
    stale handlers are called on every transition so callers can cancel/stop quoting.
    """

    def __init__(self, symbol: str, public_url: str, market_url: str, cfg: MarketDataConfig) -> None:
        s = symbol.lower()
        self.symbol = symbol
        self.cfg = cfg
        public_streams = f"{s}@bookTicker/{s}@depth{cfg.depth_levels}@{cfg.depth_speed_ms}ms"
        market_streams = f"{s}@aggTrade/{s}@markPrice@1s"
        self._conns = [
            _Conn("public", f"{public_url}?streams={public_streams}"),
            _Conn("market", f"{market_url}?streams={market_streams}"),
        ]
        self.state = FeedState()
        self.stale = True  # until both connections deliver data
        self._handlers: list[EventHandler] = []
        self._stale_handlers: list[StaleHandler] = []
        self._stop = asyncio.Event()

    def on_event(self, h: EventHandler) -> None:
        self._handlers.append(h)

    def on_stale_change(self, h: StaleHandler) -> None:
        self._stale_handlers.append(h)

    def stop(self) -> None:
        self._stop.set()

    def reconnects_within(self, seconds: float) -> int:
        cutoff = time.monotonic() - seconds
        return sum(1 for t in self.state.reconnect_times if t >= cutoff)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._conn_loop(c), name=f"ws-{c.name}") for c in self._conns]
        tasks.append(asyncio.create_task(self._stale_monitor(), name="ws-stale"))
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ internals

    async def _conn_loop(self, c: _Conn) -> None:
        recv_timeout = self.cfg.stale_after_s * 2
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    c.url, open_timeout=10, close_timeout=2, max_queue=1024
                ) as ws:
                    c.connected = True
                    log.info("ws connected", extra={"conn": c.name, "symbol": self.symbol})
                    while not self._stop.is_set():
                        # A silently dead socket gets recycled instead of hanging forever.
                        raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                        c.last_msg_mono = time.monotonic()
                        c.attempt = 0  # healthy again → reset backoff
                        self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(
                    "ws disconnected",
                    extra={"conn": c.name, "error": f"{type(e).__name__}: {e}"},
                )
            finally:
                c.connected = False
            if self._stop.is_set():
                break
            self.state.reconnects += 1
            now = time.monotonic()
            self.state.reconnect_times = [t for t in self.state.reconnect_times if t > now - 3600]
            self.state.reconnect_times.append(now)
            delay = backoff_delay(
                c.attempt, self.cfg.reconnect_backoff_initial_s, self.cfg.reconnect_backoff_max_s
            )
            c.attempt += 1
            log.info("ws reconnecting", extra={"conn": c.name, "delay_s": round(delay, 2)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _dispatch(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        ev = parse_event(data)
        if ev is None:
            return
        st = self.state
        if isinstance(ev, BookTicker):
            st.book = ev
        elif isinstance(ev, Depth):
            st.depth = ev
        elif isinstance(ev, Trade):
            st.last_trade = ev
        elif isinstance(ev, MarkPrice):
            st.mark = ev
        for h in self._handlers:
            try:
                h(ev)
            except Exception:
                log.exception("market data handler failed")

    def check_stale(self, now_mono: float | None = None) -> tuple[bool, str]:
        now = now_mono if now_mono is not None else time.monotonic()
        for c in self._conns:
            if c.last_msg_mono == 0.0:
                return True, f"{c.name}: no data yet"
            age = now - c.last_msg_mono
            if age > self.cfg.stale_after_s:
                return True, f"{c.name}: no update for {age:.1f}s"
        return False, "ok"

    async def _stale_monitor(self) -> None:
        interval = min(1.0, self.cfg.stale_after_s / 4)
        while True:
            await asyncio.sleep(interval)
            stale, reason = self.check_stale()
            if stale != self.stale:
                self.stale = stale
                (log.warning if stale else log.info)(
                    "market data stale" if stale else "market data fresh",
                    extra={"symbol": self.symbol, "reason": reason},
                )
                for h in self._stale_handlers:
                    try:
                        h(stale, reason)
                    except Exception:
                        log.exception("stale handler failed")
