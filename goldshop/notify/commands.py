"""Telegram commands via long polling (getUpdates) — no webhook, no open port.

Only messages from TELEGRAM_CHAT_ID are acted on; everything else is ignored
and logged. Every command received is logged. Commands queued while the bot was
down are skipped at startup so an old /flatten CONFIRM can never fire later.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from goldshop.config import TelegramSettings
from goldshop.notify.telegram import Priority, TelegramNotifier

log = logging.getLogger(__name__)

# (method, params) -> (status, body)
GetFn = Callable[[str, dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]
Handler = Callable[[list[str]], Awaitable[str]]

FLATTEN_CONFIRM_S = 30


class CommandRouter:
    """Maps command text to handlers; owns the /flatten two-step confirmation."""

    def __init__(self, status: Handler, pnl: Handler, pause: Handler, resume: Handler,
                 flatten: Handler, clock: Callable[[], float] = time.monotonic) -> None:
        self._handlers = {"/status": status, "/pnl": pnl, "/pause": pause, "/resume": resume}
        self._flatten = flatten
        self._clock = clock
        self._flatten_armed_at: float | None = None

    async def dispatch(self, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return ""
        cmd = parts[0].split("@")[0].lower()  # "/status@my_bot" in groups
        args = parts[1:]
        if cmd == "/flatten":
            if args == ["CONFIRM"]:
                armed = self._flatten_armed_at
                self._flatten_armed_at = None
                if armed is None or self._clock() - armed > FLATTEN_CONFIRM_S:
                    return "❌ No pending /flatten (or it expired). Send /flatten first."
                return await self._flatten(args)
            self._flatten_armed_at = self._clock()
            return (f"⚠️ This cancels all orders and closes the position (reduce-only market). "
                    f"Send /flatten CONFIRM within {FLATTEN_CONFIRM_S}s.")
        h = self._handlers.get(cmd)
        if h is None:
            return "Commands: /status /pnl /pause /resume /flatten"
        return await h(args)


class CommandPoller:
    def __init__(self, settings: TelegramSettings, router: CommandRouter,
                 notifier: TelegramNotifier, get: GetFn | None = None,
                 poll_timeout_s: int = 30) -> None:
        self.settings = settings
        self.router = router
        self.notifier = notifier
        self._get = get
        self.poll_timeout_s = poll_timeout_s
        self._offset: int | None = None
        self._session: aiohttp.ClientSession | None = None

    async def run(self) -> None:
        try:
            await self._skip_backlog()
            delay = 1.0
            while True:
                params: dict[str, Any] = {"timeout": self.poll_timeout_s,
                                          "allowed_updates": '["message"]'}
                if self._offset is not None:
                    params["offset"] = self._offset
                try:
                    status, body = await self._call("getUpdates", params)
                except Exception as e:
                    status, body = 0, {"description": type(e).__name__}
                if status != 200 or not body.get("ok"):
                    wait = float(body.get("parameters", {}).get("retry_after", delay))
                    if status == 409:  # another poller or a webhook is active
                        wait = max(wait, 30.0)
                    log.warning("telegram getUpdates failed",
                                extra={"status": status, "description": body.get("description"),
                                       "retry_s": wait})
                    await asyncio.sleep(wait)
                    delay = min(delay * 2, 60.0)
                    continue
                delay = 1.0
                for upd in body.get("result", []):
                    self._offset = int(upd["update_id"]) + 1
                    await self._handle(upd)
        finally:
            if self._session:
                await self._session.close()

    async def _skip_backlog(self) -> None:
        try:
            status, body = await self._call("getUpdates", {"offset": -1, "timeout": 0})
            if status == 200 and body.get("result"):
                self._offset = int(body["result"][-1]["update_id"]) + 1
                log.info("skipped telegram updates queued before start")
        except Exception as e:
            log.warning("could not skip telegram backlog", extra={"error": type(e).__name__})

    async def _handle(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message") or {}
        text = msg.get("text") or ""
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        user = (msg.get("from") or {}).get("username") or (msg.get("from") or {}).get("id")
        if not text.startswith("/"):
            return
        if chat_id != self.settings.chat_id:
            log.warning("ignored telegram command from unauthorized chat",
                        extra={"chat_id": chat_id, "user": user, "command": text[:40]})
            return
        log.info("telegram command received", extra={"user": user, "command": text[:80]})
        try:
            reply = await self.router.dispatch(text)
        except Exception as e:
            log.exception("telegram command failed")
            reply = f"❗ Command failed: {type(e).__name__}: {e}"
        if reply:
            self.notifier.send(reply, Priority.HIGH)

    async def _call(self, method: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self._get:
            return await self._get(method, params)
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.poll_timeout_s + 15))
        url = f"https://api.telegram.org/bot{self.settings.bot_token.get_secret_value()}/{method}"
        async with self._session.get(url, params=params) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = {}
            return resp.status, body if isinstance(body, dict) else {}
