import pytest
from pydantic import SecretStr

from goldshop.config import NotifyConfig, TelegramSettings
from goldshop.notify.commands import CommandPoller, CommandRouter
from goldshop.notify.telegram import TelegramNotifier

TG = TelegramSettings(bot_token=SecretStr("1:x"), chat_id="-500", commands_enabled=True)


def router(log, clock):
    async def h(name, args):
        log.append((name, args))
        return f"ok {name}"

    return CommandRouter(lambda a: h("status", a), lambda a: h("pnl", a), lambda a: h("pause", a),
                         lambda a: h("resume", a), lambda a: h("flatten", a), clock=lambda: clock[0])


async def test_flatten_requires_confirm_within_30s():
    log, clock = [], [0.0]
    r = router(log, clock)
    assert "No pending" in await r.dispatch("/flatten CONFIRM")
    assert "CONFIRM within 30s" in await r.dispatch("/flatten")
    clock[0] = 31
    assert "expired" in await r.dispatch("/flatten CONFIRM")
    await r.dispatch("/flatten")
    clock[0] = 40
    assert await r.dispatch("/flatten CONFIRM") == "ok flatten"
    assert "No pending" in await r.dispatch("/flatten CONFIRM")  # one-shot
    assert [x[0] for x in log] == ["flatten"]


async def test_bot_suffix_and_unknown_command():
    log, clock = [], [0.0]
    r = router(log, clock)
    assert await r.dispatch("/status@goldshop_bot") == "ok status"
    assert "Commands:" in await r.dispatch("/rm -rf")


async def test_poller_only_obeys_configured_chat_and_skips_backlog():
    log, clock = [], [0.0]
    sent = []
    notifier = TelegramNotifier(TG, "[PAPER] BTCUSDT", NotifyConfig(min_interval_s=0))
    notifier.send = lambda text, prio=None: sent.append(text)
    calls = []
    updates = [
        {"update_id": 11, "message": {"chat": {"id": -999}, "from": {"username": "eve"}, "text": "/flatten"}},
        {"update_id": 12, "message": {"chat": {"id": -500}, "from": {"username": "me"}, "text": "/pause"}},
        {"update_id": 13, "message": {"chat": {"id": -500}, "text": "hello"}},
    ]

    async def get(method, params):
        calls.append(params)
        if params.get("offset") == -1:
            return 200, {"ok": True, "result": [{"update_id": 10}]}  # backlog → skip
        if params.get("offset") == 11:
            return 200, {"ok": True, "result": updates}
        raise KeyboardInterrupt  # stop the loop

    p = CommandPoller(TG, router(log, clock), notifier, get=get)
    with pytest.raises(KeyboardInterrupt):
        await p.run()
    assert log == [("pause", [])]
    assert sent == ["ok pause"]
    assert calls[-1]["offset"] == 14
