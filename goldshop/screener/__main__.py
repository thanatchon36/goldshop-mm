"""`python -m goldshop.screener` — rank USDT perpetuals for market making.

Read-only helper: it never changes the symbol of a running bot.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import asdict

from goldshop.binance.rest import BinanceRestClient
from goldshop.config import ConfigError, load_config
from goldshop.logging_setup import setup_logging
from goldshop.screener.core import ScreenRow, scan


def _print_table(rows: list[ScreenRow]) -> None:
    hdr = f"{'#':>3} {'symbol':<16} {'vol24h(M$)':>11} {'spread':>8} {'σ1m':>8} {'net_edge':>9} {'score':>8}"
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(rows, 1):
        print(
            f"{i:>3} {r.symbol:<16} {r.quote_volume_24h / 1e6:>11.1f} {r.spread_bps:>8.2f} "
            f"{r.sigma_1m_bps:>8.2f} {r.net_edge_bps:>9.2f} {r.goldshop_score:>8.3f}"
        )
    print("\nunits: bps. net_edge = spread − 2×maker_fee; score = net_edge / σ1m (higher is better)")


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging("WARNING" if not args.verbose else cfg.logging.level)
    sc = cfg.screener
    if args.min_volume is not None:
        sc = sc.model_copy(update={"min_quote_volume_usdt": args.min_volume})
    fee = args.maker_fee_bps if args.maker_fee_bps is not None else cfg.exchange.maker_fee_bps
    ex = cfg.exchange
    async with BinanceRestClient(
        ex.rest_base_url, timeout_s=ex.request_timeout_s, weight_backoff_ratio=ex.weight_backoff_ratio
    ) as client:
        rows = await scan(client, sc, fee)
    top = rows[: args.top or sc.top_n]
    if args.csv:
        w = csv.DictWriter(sys.stdout, fieldnames=list(ScreenRow.__dataclass_fields__))
        w.writeheader()
        for r in top:
            w.writerow(asdict(r))
    else:
        print(f"maker fee {fee} bps, min 24h volume ${sc.min_quote_volume_usdt:,.0f}, "
              f"σ from last {sc.kline_minutes} 1m klines — {len(rows)} symbols scanned\n")
        _print_table(top)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m goldshop.screener", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--top", type=int, help="rows to show (default from config)")
    p.add_argument("--min-volume", type=float, help="override min 24h quote volume (USDT)")
    p.add_argument("--maker-fee-bps", type=float, help="override maker fee (bps)")
    p.add_argument("--csv", action="store_true", help="CSV output")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(_main(args)))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
