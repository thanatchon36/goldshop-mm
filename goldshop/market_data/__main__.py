"""`python -m goldshop.market_data` — stream market data for config.symbol and record it.

Phase-1 entrypoint (no trading). Stops cleanly on SIGINT/SIGTERM, flushing Parquet files.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from goldshop.config import ConfigError, load_config, load_runtime, secret_values
from goldshop.logging_setup import redactor, setup_logging
from goldshop.market_data.feed import MarketDataFeed
from goldshop.market_data.recorder import ParquetRecorder
from goldshop.ops.health import start_health_server

log = logging.getLogger("goldshop.market_data")


async def _status_loop(feed: MarketDataFeed, rec: ParquetRecorder | None, every_s: float) -> None:
    while True:
        await asyncio.sleep(every_s)
        st = feed.state
        log.info(
            "market data status",
            extra={
                "symbol": feed.symbol,
                "stale": feed.stale,
                "bid": st.book.bid if st.book else None,
                "ask": st.book.ask if st.book else None,
                "mark": st.mark.mark if st.mark else None,
                "funding_rate": st.mark.funding_rate if st.mark else None,
                "last_trade": st.last_trade.price if st.last_trade else None,
                "reconnects": st.reconnects,
                "rows_recorded": rec.rows_written if rec else None,
            },
        )


async def _main(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    setup_logging(cfg.logging.level)
    runtime = load_runtime()
    redactor.add(*secret_values(runtime))
    symbol = (args.symbol or cfg.symbol).upper()
    record = cfg.recorder.enabled and not args.no_record
    log.info(
        "starting market data",
        extra={"mode": runtime.mode.value, "symbol": symbol, "record": record,
               "data_dir": str(cfg.recorder.data_dir)},
    )

    feed = MarketDataFeed(symbol, cfg.exchange.ws_public_url, cfg.exchange.ws_market_url, cfg.market_data)
    rec: ParquetRecorder | None = None
    if record:
        rec = ParquetRecorder(cfg.recorder.data_dir, symbol, cfg.recorder.flush_interval_s)
        feed.on_event(rec.handle)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, feed.stop)

    aux = [asyncio.create_task(_status_loop(feed, rec, args.status_every))]
    if rec:
        aux.append(asyncio.create_task(rec.run()))
    def healthy() -> tuple[bool, str]:
        stale, reason = feed.check_stale()
        return not stale, reason

    health = None
    if cfg.ops.health_enabled:
        health = await start_health_server(cfg.ops.health_port, healthy)
    if args.duration:
        loop.call_later(args.duration, feed.stop)
    try:
        await feed.run()
    finally:
        for t in aux:
            t.cancel()
        await asyncio.gather(*aux, return_exceptions=True)
        if rec:
            await rec.close()
        if health:
            await health.cleanup()
        log.info("stopped", extra={"rows_recorded": rec.rows_written if rec else 0,
                                   "reconnects": feed.state.reconnects})


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m goldshop.market_data", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--symbol", help="override config.symbol")
    p.add_argument("--no-record", action="store_true", help="stream only, don't write Parquet")
    p.add_argument("--duration", type=float, help="stop after N seconds")
    p.add_argument("--status-every", type=float, default=30.0, help="status log interval (s)")
    args = p.parse_args()
    try:
        asyncio.run(_main(args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
