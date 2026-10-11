"""Pure screener math and the async scan pipeline."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

from goldshop.binance.rest import BinanceRestClient
from goldshop.config import ScreenerConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenRow:
    symbol: str
    quote_volume_24h: float
    bid: float
    ask: float
    spread_bps: float
    sigma_1m_bps: float
    net_edge_bps: float
    goldshop_score: float


def spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 1e4


def sigma_1m_bps(closes: list[float]) -> float:
    """Sample std-dev of 1-minute log returns, in bps."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return math.nan
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * 1e4


def net_edge_bps(spread: float, maker_fee_bps: float) -> float:
    return spread - 2 * maker_fee_bps


def goldshop_score(net_edge: float, sigma: float) -> float:
    if not sigma > 0:  # also catches NaN
        return math.nan
    return net_edge / sigma


def usdt_perpetuals(exchange_info: dict[str, Any]) -> set[str]:
    return {
        s["symbol"]
        for s in exchange_info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    }


async def scan(
    client: BinanceRestClient, cfg: ScreenerConfig, maker_fee_bps: float
) -> list[ScreenRow]:
    info = await client.exchange_info()
    perps = usdt_perpetuals(info)
    tickers, books = await asyncio.gather(client.ticker_24h_all(), client.book_ticker_all())

    # Volume filter first so we only fetch klines for the survivors.
    volume = {
        t["symbol"]: float(t["quoteVolume"])
        for t in tickers
        if t["symbol"] in perps and float(t["quoteVolume"]) >= cfg.min_quote_volume_usdt
    }
    book = {
        b["symbol"]: (float(b["bidPrice"]), float(b["askPrice"]))
        for b in books
        if b["symbol"] in volume and float(b["bidPrice"]) > 0 and float(b["askPrice"]) > 0
    }
    log.info(
        "screener universe",
        extra={"usdt_perps": len(perps), "after_volume_filter": len(book)},
    )

    sem = asyncio.Semaphore(cfg.max_concurrency)

    async def one(sym: str) -> ScreenRow | None:
        async with sem:
            try:
                kl = await client.klines(sym, "1m", cfg.kline_minutes + 1)
            except Exception as e:  # one bad symbol must not kill the scan
                log.warning("kline fetch failed", extra={"symbol": sym, "error": str(e)})
                return None
        # Drop the still-open last candle.
        closes = [float(k[4]) for k in kl[:-1]]
        bid, ask = book[sym]
        sp = spread_bps(bid, ask)
        sig = sigma_1m_bps(closes)
        edge = net_edge_bps(sp, maker_fee_bps)
        return ScreenRow(sym, volume[sym], bid, ask, sp, sig, edge, goldshop_score(edge, sig))

    rows = [r for r in await asyncio.gather(*(one(s) for s in book)) if r is not None]
    rows.sort(key=lambda r: _nan_to(r.goldshop_score), reverse=True)
    return rows


def _nan_to(x: float, default: float = -math.inf) -> float:
    return default if math.isnan(x) else x
