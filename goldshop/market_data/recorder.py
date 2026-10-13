"""Records market data to Parquet, one file per stream per UTC hour (for replay backtests).

Layout: {data_dir}/{SYMBOL}/{stream}/{YYYY-MM-DD}/{HH}.parquet
If a file for that hour already exists (e.g. after a restart) the new one is
written as {HH}.1.parquet, {HH}.2.parquet, ... — files are never overwritten.
Hours are bucketed by the exchange event time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from goldshop.market_data.models import BookTicker, Depth, MarketEvent, Trade

log = logging.getLogger(__name__)

_LEVELS = pa.list_(pa.float64())

SCHEMAS: dict[str, pa.Schema] = {
    "book_ticker": pa.schema([
        ("recv_ns", pa.int64()), ("event_ms", pa.int64()), ("txn_ms", pa.int64()),
        ("update_id", pa.int64()), ("bid", pa.float64()), ("bid_qty", pa.float64()),
        ("ask", pa.float64()), ("ask_qty", pa.float64()),
    ]),
    "depth": pa.schema([
        ("recv_ns", pa.int64()), ("event_ms", pa.int64()), ("txn_ms", pa.int64()),
        ("final_update_id", pa.int64()),
        ("bid_px", _LEVELS), ("bid_qty", _LEVELS), ("ask_px", _LEVELS), ("ask_qty", _LEVELS),
    ]),
    "trades": pa.schema([
        ("recv_ns", pa.int64()), ("event_ms", pa.int64()), ("trade_ms", pa.int64()),
        ("agg_id", pa.int64()), ("price", pa.float64()), ("qty", pa.float64()),
        ("is_buyer_maker", pa.bool_()),
    ]),
    "mark_price": pa.schema([
        ("recv_ns", pa.int64()), ("event_ms", pa.int64()), ("mark", pa.float64()),
        ("index", pa.float64()), ("funding_rate", pa.float64()), ("next_funding_ms", pa.int64()),
    ]),
}


def to_row(ev: MarketEvent) -> tuple[str, dict[str, Any]]:
    if isinstance(ev, BookTicker):
        return "book_ticker", {
            "recv_ns": ev.recv_ns, "event_ms": ev.event_ms, "txn_ms": ev.txn_ms,
            "update_id": ev.update_id, "bid": ev.bid, "bid_qty": ev.bid_qty,
            "ask": ev.ask, "ask_qty": ev.ask_qty,
        }
    if isinstance(ev, Depth):
        return "depth", {
            "recv_ns": ev.recv_ns, "event_ms": ev.event_ms, "txn_ms": ev.txn_ms,
            "final_update_id": ev.final_update_id,
            "bid_px": [p for p, _ in ev.bids], "bid_qty": [q for _, q in ev.bids],
            "ask_px": [p for p, _ in ev.asks], "ask_qty": [q for _, q in ev.asks],
        }
    if isinstance(ev, Trade):
        return "trades", {
            "recv_ns": ev.recv_ns, "event_ms": ev.event_ms, "trade_ms": ev.trade_ms,
            "agg_id": ev.agg_id, "price": ev.price, "qty": ev.qty,
            "is_buyer_maker": ev.is_buyer_maker,
        }
    return "mark_price", {
        "recv_ns": ev.recv_ns, "event_ms": ev.event_ms, "mark": ev.mark, "index": ev.index,
        "funding_rate": ev.funding_rate, "next_funding_ms": ev.next_funding_ms,
    }


def hour_key(event_ms: int) -> str:
    return datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d/%H")


class ParquetRecorder:
    def __init__(self, data_dir: Path, symbol: str, flush_interval_s: float) -> None:
        self.root = Path(data_dir) / symbol
        self.flush_interval_s = flush_interval_s
        # (stream, hour) -> buffered rows
        self._buf: dict[tuple[str, str], list[dict[str, Any]]] = {}
        # stream -> (hour, open writer)
        self._writers: dict[str, tuple[str, pq.ParquetWriter]] = {}
        self._flush_lock = asyncio.Lock()
        self.rows_written = 0

    def handle(self, ev: MarketEvent) -> None:
        stream, row = to_row(ev)
        self._buf.setdefault((stream, hour_key(row["event_ms"])), []).append(row)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_s)
            await self.flush()

    async def flush(self) -> None:
        async with self._flush_lock:
            buf, self._buf = self._buf, {}
            if buf:
                await asyncio.to_thread(self._write, buf)

    async def close(self) -> None:
        await self.flush()
        async with self._flush_lock:
            await asyncio.to_thread(self._close_all)

    # ---- blocking, run in a worker thread ----------------------------------

    def _write(self, buf: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
        for (stream, hour), rows in sorted(buf.items(), key=lambda kv: kv[0][1]):
            try:
                w = self._writer(stream, hour)
                w.write_table(pa.Table.from_pylist(rows, schema=SCHEMAS[stream]))
                self.rows_written += len(rows)
            except Exception:
                log.exception("parquet write failed", extra={"stream": stream, "hour": hour})

    def _writer(self, stream: str, hour: str) -> pq.ParquetWriter:
        cur = self._writers.get(stream)
        if cur and cur[0] == hour:
            return cur[1]
        if cur and hour < cur[0]:
            # Late event from an already-closed hour: keep it in the current file
            # rather than reopening (and overwriting) the old one.
            return cur[1]
        if cur:
            cur[1].close()
            log.info("parquet file rotated", extra={"stream": stream, "hour": cur[0]})
        path = self._free_path(stream, hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        w = pq.ParquetWriter(path, SCHEMAS[stream], compression="zstd")
        self._writers[stream] = (hour, w)
        return w

    def _free_path(self, stream: str, hour: str) -> Path:
        day, hh = hour.split("/")
        d = self.root / stream / day
        p, n = d / f"{hh}.parquet", 0
        while p.exists():
            n += 1
            p = d / f"{hh}.{n}.parquet"
        return p

    def _close_all(self) -> None:
        for _, w in self._writers.values():
            w.close()
        self._writers.clear()
