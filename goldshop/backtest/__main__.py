"""`python -m goldshop.backtest` — replay recorded data through the paper fill engine.

Uses config.yaml for strategy/paper/fee settings (override the file with --config
to compare parameter sets). Symbol rules (tick/lot size) come from Binance's
public exchangeInfo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from goldshop.backtest.replay import load_events, run_backtest
from goldshop.backtest.report import summarize, write_report
from goldshop.binance.rest import BinanceRestClient
from goldshop.binance.symbol import SymbolFilters, SymbolRuleError
from goldshop.config import ConfigError, load_config
from goldshop.logging_setup import setup_logging


def _ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging("WARNING")  # skipped files are reported as warnings
    symbol = (args.symbol or cfg.symbol).upper()
    cfg = cfg.model_copy(update={"symbol": symbol})
    events = load_events(Path(args.data or cfg.recorder.data_dir), symbol,
                         _ts(args.start) if args.start else None, _ts(args.end) if args.end else None)
    if not events:
        print(f"no recorded data for {symbol} in {args.data or cfg.recorder.data_dir}", file=sys.stderr)
        return 1
    ex = cfg.exchange
    async with BinanceRestClient(ex.rest_base_url, timeout_s=ex.request_timeout_s) as rest:
        filters = SymbolFilters.from_exchange_info(await rest.exchange_info(), symbol)
    print(f"replaying {len(events):,} events for {symbol}…", file=sys.stderr)
    result = await run_backtest(cfg, filters, events)
    out = Path(args.out or f"reports/backtest-{symbol}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    path = write_report(result, out)
    print(json.dumps(summarize(result), indent=2))
    print(f"\nreport: {path.resolve()}", file=sys.stderr)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m goldshop.backtest", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--symbol", help="default: config.symbol")
    p.add_argument("--data", help="recordings dir (default: recorder.data_dir)")
    p.add_argument("--start", help="ISO time, UTC if no offset (e.g. 2026-10-11T08:00)")
    p.add_argument("--end", help="ISO time, exclusive")
    p.add_argument("--out", help="output dir (default: reports/backtest-<symbol>-<now>)")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(_main(args)))
    except (ConfigError, SymbolRuleError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
