import aiohttp

from goldshop.ops.health import start_health_server


async def test_health_reports_ok_and_unhealthy(unused_tcp_port):
    state = {"ok": True}
    runner = await start_health_server(unused_tcp_port, lambda: (state["ok"], "why"))
    try:
        async with aiohttp.ClientSession() as s:
            url = f"http://127.0.0.1:{unused_tcp_port}/health"
            async with s.get(url) as r:
                assert r.status == 200 and (await r.json())["status"] == "ok"
            state["ok"] = False
            async with s.get(url) as r:
                assert r.status == 503 and (await r.json())["reason"] == "why"
    finally:
        await runner.cleanup()
