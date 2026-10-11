"""Async REST client for Binance USDⓈ-M Futures (public + signed endpoints).

- Signs with HMAC-SHA256; timestamps use an offset synced from /fapi/v1/time.
- Tracks X-MBX-USED-WEIGHT-1M and X-MBX-ORDER-COUNT-10S/-1M and backs off
  before hitting the limits (weight: waits; order count: refuses to send).
- Secrets never appear in logs: the signature/key are not logged and the
  log redaction filter also masks them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Any

import aiohttp

from goldshop.config import Credentials

log = logging.getLogger(__name__)

DEFAULT_WEIGHT_LIMIT_1M = 2400
DEFAULT_ORDER_LIMIT_10S = 300
DEFAULT_ORDER_LIMIT_1M = 1200

# Binance error codes we act on
ERR_TIMESTAMP = -1021
ERR_UNKNOWN_ORDER = -2011
ERR_NO_SUCH_ORDER = -2013
ERR_MARGIN_INSUFFICIENT = -2019
ERR_REDUCE_ONLY_REJECTED = -2022
ERR_NO_NEED_MARGIN_TYPE = -4046
ERR_DUPLICATE_CLIENT_ID = -4116
ERR_GTX_WOULD_TAKE = -5022


class BinanceHTTPError(Exception):
    def __init__(self, status: int, code: int | None, msg: str) -> None:
        super().__init__(f"HTTP {status} code={code} {msg}")
        self.status, self.code, self.msg = status, code, msg


class OrderRateLimited(Exception):
    """Raised locally (nothing sent) when the order-count limit is close."""


class BinanceRestClient:
    def __init__(
        self,
        base_url: str,
        *,
        credentials: Credentials | None = None,
        timeout_s: float = 10.0,
        weight_backoff_ratio: float = 0.8,
        recv_window_ms: int = 5000,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._creds = credentials
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._ratio = weight_backoff_ratio
        self.recv_window_ms = recv_window_ms
        self.weight_limit_1m = DEFAULT_WEIGHT_LIMIT_1M
        self.order_limit_10s = DEFAULT_ORDER_LIMIT_10S
        self.order_limit_1m = DEFAULT_ORDER_LIMIT_1M
        self.used_weight_1m = 0
        self.order_count_10s = 0
        self.order_count_1m = 0
        self._order_count_at = 0.0
        self.time_offset_ms = 0
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()  # serialises the "near weight limit" wait

    async def __aenter__(self) -> BinanceRestClient:
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session:
            await self._session.close()

    # ---- core ---------------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params)

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                      *, signed: bool = False, api_key: bool = False) -> Any:
        """Public GETs retry on 429/418. Signed/mutating calls never auto-retry except
        once after a timestamp (-1021) error, which is safe because nothing was accepted."""
        assert self._session is not None, "use `async with BinanceRestClient(...)`"
        is_order = method == "POST" and path == "/fapi/v1/order"
        for attempt in range(5):
            await self._respect_weight()
            if is_order:
                self._check_order_budget()
            headers = {}
            query = dict(params or {})
            if signed or api_key:
                if self._creds is None:
                    raise RuntimeError("signed endpoint called without credentials")
                headers["X-MBX-APIKEY"] = self._creds.api_key.get_secret_value()
            if signed:
                query["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
                query["recvWindow"] = self.recv_window_ms
                qs = urllib.parse.urlencode(query)
                sig = hmac.new(self._creds.api_secret.get_secret_value().encode(),  # type: ignore[union-attr]
                               qs.encode(), hashlib.sha256).hexdigest()
                url = f"{self._base}{path}?{qs}&signature={sig}"
            else:
                url = f"{self._base}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
            async with self._session.request(method, url, headers=headers) as resp:
                self._read_limits(resp)
                if resp.status in (418, 429):
                    retry = float(resp.headers.get("Retry-After", 2**attempt * 5))
                    log.warning("rate limited by Binance",
                                extra={"status": resp.status, "retry_after_s": retry, "path": path})
                    if method != "GET" or signed:
                        raise BinanceHTTPError(resp.status, None, f"rate limited, retry after {retry}s")
                    await asyncio.sleep(retry)
                    continue
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    code = body.get("code") if isinstance(body, dict) else None
                    msg = body.get("msg", "") if isinstance(body, dict) else str(body)
                    if code == ERR_TIMESTAMP and attempt == 0:
                        log.warning("timestamp outside recvWindow, resyncing server time")
                        await self.sync_time()
                        continue
                    raise BinanceHTTPError(resp.status, code, msg)
                return body
        raise BinanceHTTPError(429, None, f"gave up after repeated rate limiting on {path}")

    def _read_limits(self, resp: aiohttp.ClientResponse) -> None:
        h = resp.headers
        w = h.get("X-MBX-USED-WEIGHT-1M") or h.get("X-MBX-USED-WEIGHT")
        if w is not None and int(w) >= 0:  # testnet reports -1
            self.used_weight_1m = int(w)
        c10, c1m = h.get("X-MBX-ORDER-COUNT-10S"), h.get("X-MBX-ORDER-COUNT-1M")
        if c10 is not None:
            self.order_count_10s = int(c10)
            self._order_count_at = time.monotonic()
        if c1m is not None:
            self.order_count_1m = int(c1m)

    def _check_order_budget(self) -> None:
        age = time.monotonic() - self._order_count_at
        near_10s = age < 10 and self.order_count_10s >= self.order_limit_10s * self._ratio
        near_1m = age < 60 and self.order_count_1m >= self.order_limit_1m * self._ratio
        if near_10s or near_1m:
            raise OrderRateLimited(
                f"order count near limit (10s={self.order_count_10s}/{self.order_limit_10s}, "
                f"1m={self.order_count_1m}/{self.order_limit_1m})"
            )

    async def _respect_weight(self) -> None:
        if self.used_weight_1m < self.weight_limit_1m * self._ratio:
            return
        async with self._lock:
            if self.used_weight_1m < self.weight_limit_1m * self._ratio:
                return
            wait = 60 - (time.time() % 60) + 0.5  # weight resets on the minute boundary
            log.warning("near request-weight limit, pausing until next minute",
                        extra={"used_weight_1m": self.used_weight_1m, "wait_s": round(wait, 1)})
            await asyncio.sleep(wait)
            self.used_weight_1m = 0

    # ---- public endpoints -------------------------------------------------

    async def sync_time(self) -> int:
        t0 = time.time() * 1000
        body = await self.get("/fapi/v1/time")
        t1 = time.time() * 1000
        self.time_offset_ms = int(body["serverTime"] - (t0 + t1) / 2)
        log.debug("server time synced", extra={"offset_ms": self.time_offset_ms})
        return self.time_offset_ms

    async def exchange_info(self) -> dict[str, Any]:
        info: dict[str, Any] = await self.get("/fapi/v1/exchangeInfo")
        for rl in info.get("rateLimits", []):
            n = max(int(rl.get("intervalNum", 1)), 1)
            kind, interval, limit = rl.get("rateLimitType"), rl.get("interval"), int(rl["limit"])
            if kind == "REQUEST_WEIGHT" and interval == "MINUTE":
                self.weight_limit_1m = limit // n
            elif kind == "ORDERS" and interval == "SECOND":
                self.order_limit_10s = limit if n == 10 else limit * 10 // n
            elif kind == "ORDERS" and interval == "MINUTE":
                self.order_limit_1m = limit // n
        return info

    async def ticker_24h_all(self) -> list[dict[str, Any]]:
        return await self.get("/fapi/v1/ticker/24hr")  # type: ignore[no-any-return]

    async def book_ticker_all(self) -> list[dict[str, Any]]:
        return await self.get("/fapi/v1/ticker/bookTicker")  # type: ignore[no-any-return]

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        return await self.get(  # type: ignore[no-any-return]
            "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )

    # ---- signed endpoints -------------------------------------------------

    async def new_order(self, **params: Any) -> dict[str, Any]:
        return await self.request("POST", "/fapi/v1/order", params, signed=True)  # type: ignore[no-any-return]

    async def cancel_order(self, symbol: str, client_id: str) -> dict[str, Any]:
        return await self.request("DELETE", "/fapi/v1/order",  # type: ignore[no-any-return]
                                  {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    async def cancel_all(self, symbol: str) -> dict[str, Any]:
        return await self.request("DELETE", "/fapi/v1/allOpenOrders",  # type: ignore[no-any-return]
                                  {"symbol": symbol}, signed=True)

    async def query_order(self, symbol: str, client_id: str) -> dict[str, Any]:
        return await self.request("GET", "/fapi/v1/order",  # type: ignore[no-any-return]
                                  {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    async def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return await self.request("GET", "/fapi/v1/openOrders",  # type: ignore[no-any-return]
                                  {"symbol": symbol}, signed=True)

    async def position_risk(self, symbol: str) -> list[dict[str, Any]]:
        return await self.request("GET", "/fapi/v2/positionRisk",  # type: ignore[no-any-return]
                                  {"symbol": symbol}, signed=True)

    async def position_mode(self) -> dict[str, Any]:
        return await self.request("GET", "/fapi/v1/positionSide/dual", signed=True)  # type: ignore[no-any-return]

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return await self.request("POST", "/fapi/v1/leverage",  # type: ignore[no-any-return]
                                  {"symbol": symbol, "leverage": leverage}, signed=True)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            await self.request("POST", "/fapi/v1/marginType",
                               {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceHTTPError as e:
            if e.code != ERR_NO_NEED_MARGIN_TYPE:  # already set
                raise

    async def countdown_cancel_all(self, symbol: str, countdown_ms: int) -> dict[str, Any]:
        return await self.request("POST", "/fapi/v1/countdownCancelAll",  # type: ignore[no-any-return]
                                  {"symbol": symbol, "countdownTime": countdown_ms}, signed=True)

    async def new_listen_key(self) -> str:
        body = await self.request("POST", "/fapi/v1/listenKey", api_key=True)
        return str(body["listenKey"])

    async def keepalive_listen_key(self) -> None:
        await self.request("PUT", "/fapi/v1/listenKey", api_key=True)

    async def close_listen_key(self) -> None:
        await self.request("DELETE", "/fapi/v1/listenKey", api_key=True)
