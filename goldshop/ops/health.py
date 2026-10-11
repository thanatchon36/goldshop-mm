"""Ops HTTP server: GET /health for container healthchecks, GET /metrics for Prometheus.

/health is 200 when healthy and 503 otherwise (stale data, user stream down,
engine loop stalled). If the event loop itself is blocked the request times
out, which the healthcheck also treats as unhealthy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiohttp import web

log = logging.getLogger(__name__)

PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


async def start_health_server(
    port: int,
    check: Callable[[], tuple[bool, str]],
    metrics: Callable[[], bytes] | None = None,
) -> web.AppRunner:
    """`check` returns (healthy, reason); `metrics` returns the Prometheus exposition."""

    async def health(_: web.Request) -> web.Response:
        ok, reason = check()
        return web.json_response(
            {"status": "ok" if ok else "unhealthy", "reason": reason}, status=200 if ok else 503
        )

    async def prom(_: web.Request) -> web.Response:
        assert metrics is not None
        return web.Response(body=metrics(), headers={"Content-Type": PROM_CONTENT_TYPE})

    app = web.Application()
    app.router.add_get("/health", health)
    if metrics is not None:
        app.router.add_get("/metrics", prom)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("ops endpoint listening", extra={"port": port, "metrics": metrics is not None})
    return runner
