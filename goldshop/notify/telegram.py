"""Non-blocking Telegram notifier.

`send()` only enqueues; a background task delivers. High-priority alerts jump
ahead of reports; when the queue is full, low-priority messages are dropped
first. Delivery is rate-limited (~1 msg/s), retried with backoff, and honours
Telegram's 429 `retry_after`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import aiohttp

from goldshop.config import NotifyConfig, TelegramSettings

log = logging.getLogger(__name__)

# (payload) -> (http_status, json_body)
PostFn = Callable[[dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]

MAX_LEN = 4000  # Telegram's limit is 4096 chars


class Priority(Enum):
    HIGH = "high"
    LOW = "low"


class TelegramNotifier:
    def __init__(
        self,
        settings: TelegramSettings | None,
        prefix: str,
        cfg: NotifyConfig,
        post: PostFn | None = None,
    ) -> None:
        self.enabled = settings is not None
        self.prefix = prefix
        self.cfg = cfg
        self._settings = settings
        self._post = post
        self._high: deque[str] = deque()
        self._low: deque[str] = deque()
        self._wake = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self._inflight = False
        self.sent = 0
        self.dropped = 0
        if not self.enabled:
            log.info("telegram disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")

    # ---- producer side (never blocks) ---------------------------------------

    def send(self, text: str, priority: Priority = Priority.LOW) -> None:
        if not self.enabled:
            return
        msg = f"{self.prefix} {text}"[:MAX_LEN]
        if len(self._high) + len(self._low) >= self.cfg.queue_size:
            if self._low:
                self._low.popleft()
            elif priority is Priority.LOW:
                self._drop("queue full of alerts, dropping report")
                return
            else:
                self._high.popleft()
            self._drop("queue full, dropped oldest queued message")
        (self._high if priority is Priority.HIGH else self._low).append(msg)
        self._wake.set()

    def _drop(self, why: str) -> None:
        self.dropped += 1
        log.warning("telegram message dropped", extra={"reason": why, "dropped_total": self.dropped})

    @property
    def pending(self) -> int:
        return len(self._high) + len(self._low)

    # ---- consumer side ------------------------------------------------------

    async def run(self) -> None:
        if not self.enabled:
            return
        try:
            while True:
                if not self.pending:
                    self._wake.clear()
                    await self._wake.wait()
                msg = self._high.popleft() if self._high else self._low.popleft()
                self._inflight = True
                try:
                    await self._deliver(msg)
                finally:
                    self._inflight = False
                await asyncio.sleep(self.cfg.min_interval_s)
        finally:
            if self._session:
                await self._session.close()

    async def drain(self, timeout_s: float) -> None:
        """Wait (bounded) for the queue to empty — used at shutdown."""
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while (self.pending or self._inflight) and loop.time() < end:
            await asyncio.sleep(0.1)

    async def _deliver(self, text: str) -> None:
        assert self._settings is not None
        payload = {"chat_id": self._settings.chat_id, "text": text,
                   "disable_web_page_preview": True}
        delay = 1.0
        for attempt in range(self.cfg.max_retries + 1):
            try:
                status, body = await (self._post or self._http_post)(payload)
            except Exception as e:  # network error
                status, body = 0, {"description": f"{type(e).__name__}"}
            if status == 200 and body.get("ok", True):
                self.sent += 1
                return
            if status == 429:
                wait = float(body.get("parameters", {}).get("retry_after", delay))
                log.warning("telegram rate limited", extra={"retry_after_s": wait})
                await asyncio.sleep(wait)
                continue
            if 400 <= status < 500:  # bad request / forbidden: retrying won't help
                log.error("telegram rejected message",
                          extra={"status": status, "description": body.get("description")})
                self.dropped += 1
                return
            if attempt < self.cfg.max_retries:
                log.warning("telegram send failed, retrying",
                            extra={"status": status, "attempt": attempt + 1, "delay_s": delay})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        log.error("telegram send gave up", extra={"retries": self.cfg.max_retries})
        self.dropped += 1

    async def _http_post(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert self._settings is not None
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        url = f"https://api.telegram.org/bot{self._settings.bot_token.get_secret_value()}/sendMessage"
        async with self._session.post(url, json=payload) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = {}
            return resp.status, body if isinstance(body, dict) else {}
