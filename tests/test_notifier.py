import asyncio

import pytest
from pydantic import SecretStr

from goldshop.config import NotifyConfig, TelegramSettings
from goldshop.notify.telegram import Priority, TelegramNotifier

TG = TelegramSettings(bot_token=SecretStr("123:abc"), chat_id="-100", commands_enabled=False)


def cfg(**kw):
    return NotifyConfig(**{**dict(queue_size=10, min_interval_s=0, max_retries=3), **kw})


class FakeTelegram:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.sent: list[str] = []
        self.calls = 0

    async def __call__(self, payload):
        self.calls += 1
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            if r[0] != 200:
                return r
        self.sent.append(payload["text"])
        return 200, {"ok": True}


async def run_until_idle(n: TelegramNotifier, timeout=2.0):
    task = asyncio.create_task(n.run())
    await n.drain(timeout)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_disabled_without_settings_is_noop():
    n = TelegramNotifier(None, "[PAPER] BTCUSDT", cfg())
    assert not n.enabled
    n.send("hello")
    assert n.pending == 0


async def test_prefix_and_high_priority_first():
    tg = FakeTelegram()
    n = TelegramNotifier(TG, "[PAPER] BTCUSDT", cfg(), post=tg)
    n.send("report 1")
    n.send("report 2")
    n.send("ALERT", Priority.HIGH)
    await run_until_idle(n)
    assert tg.sent == ["[PAPER] BTCUSDT ALERT", "[PAPER] BTCUSDT report 1", "[PAPER] BTCUSDT report 2"]


async def test_send_does_not_block_or_await():
    n = TelegramNotifier(TG, "[X]", cfg(), post=FakeTelegram())
    assert n.send("x") is None  # plain sync call, returns immediately


async def test_queue_full_drops_low_priority_first():
    n = TelegramNotifier(TG, "[P]", cfg(queue_size=3), post=FakeTelegram())
    n.send("low1")
    n.send("low2")
    n.send("high1", Priority.HIGH)
    n.send("high2", Priority.HIGH)  # full → drops low1
    n.send("high3", Priority.HIGH)  # full → drops low2
    assert list(n._high) == ["[P] high1", "[P] high2", "[P] high3"] and not n._low
    n.send("low3")  # full of alerts → low is dropped
    assert not n._low and n.dropped == 3


async def test_respects_429_retry_after(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)

    monkeypatch.setattr("goldshop.notify.telegram.asyncio.sleep", fake_sleep)
    tg = FakeTelegram([(429, {"ok": False, "parameters": {"retry_after": 7}})])
    n = TelegramNotifier(TG, "[P]", cfg(), post=tg)
    await n._deliver("hi")
    assert 7.0 in sleeps and tg.sent == ["hi"]


async def test_retries_network_errors_with_backoff(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("goldshop.notify.telegram.asyncio.sleep", fake_sleep)
    tg = FakeTelegram([OSError("down"), (502, {}), (502, {})])
    n = TelegramNotifier(TG, "[P]", cfg(max_retries=3), post=tg)
    await n._deliver("hi")
    assert tg.sent == ["hi"] and sleeps == [1.0, 2.0, 4.0]


async def test_gives_up_after_max_retries(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("goldshop.notify.telegram.asyncio.sleep", fake_sleep)
    tg = FakeTelegram([(500, {})] * 10)
    n = TelegramNotifier(TG, "[P]", cfg(max_retries=2), post=tg)
    await n._deliver("hi")
    assert tg.calls == 3 and n.dropped == 1


async def test_4xx_is_not_retried():
    tg = FakeTelegram([(400, {"ok": False, "description": "chat not found"})])
    n = TelegramNotifier(TG, "[P]", cfg(), post=tg)
    await n._deliver("hi")
    assert tg.calls == 1 and n.dropped == 1


async def test_rate_limit_between_messages():
    tg = FakeTelegram()
    n = TelegramNotifier(TG, "[P]", cfg(min_interval_s=0.2), post=tg)
    for i in range(3):
        n.send(str(i))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await run_until_idle(n)
    assert len(tg.sent) == 3
    assert loop.time() - t0 >= 0.4


async def test_drain_waits_for_in_flight_message():
    class Slow(FakeTelegram):
        async def __call__(self, payload):
            await asyncio.sleep(0.2)
            return await super().__call__(payload)

    tg = Slow()
    n = TelegramNotifier(TG, "[P]", cfg(), post=tg)
    n.send("bye", Priority.HIGH)
    await run_until_idle(n)
    assert tg.sent == ["[P] bye"]
