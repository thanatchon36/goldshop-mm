"""SQLite persistence: fills, quotes, PnL snapshots, funding, markouts, and the
portfolio state needed to continue after a restart. One database per mode
(state-paper.sqlite, state-testnet.sqlite, state-live.sqlite) so they never mix.

Writes are buffered and flushed in a worker thread so the trading loop never
waits on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
  ts_ms INTEGER, symbol TEXT, client_id TEXT, side TEXT, price REAL, qty REAL, fee REAL,
  mid REAL, edge_bps REAL, position_after REAL);
CREATE TABLE IF NOT EXISTS funding (
  ts_ms INTEGER, symbol TEXT, rate REAL, mark REAL, position REAL, amount REAL);
CREATE TABLE IF NOT EXISTS quotes (
  ts_ms INTEGER, symbol TEXT, bid REAL, ask REAL, mid REAL, reservation REAL,
  spread_bps REAL, sigma_bps REAL, inventory REAL);
CREATE TABLE IF NOT EXISTS pnl (
  ts_ms INTEGER, symbol TEXT, net REAL, realized REAL, unrealized REAL, fees REAL,
  funding REAL, position REAL, mark REAL);
CREATE TABLE IF NOT EXISTS markouts (
  fill_ts_ms INTEGER, symbol TEXT, horizon_ms INTEGER, bps REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_ms INTEGER);
CREATE INDEX IF NOT EXISTS fills_ts ON fills(ts_ms);
CREATE INDEX IF NOT EXISTS pnl_ts ON pnl(ts_ms);
"""

_COLUMNS = {
    "fills": 10, "funding": 6, "quotes": 9, "pnl": 9, "markouts": 4,
}


class StateStore:
    def __init__(self, db_dir: Path, mode: str) -> None:
        self.path = Path(db_dir) / f"state-{mode}.sqlite"
        self._buf: dict[str, list[tuple[Any, ...]]] = {t: [] for t in _COLUMNS}
        self._kv: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- buffered writes (sync, cheap) ---------------------------------------

    def add(self, table: str, *row: Any) -> None:
        assert len(row) == _COLUMNS[table], (table, row)
        self._buf[table].append(row)

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._kv[key] = json.dumps(value)

    def get(self, key: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    # ---- flushing --------------------------------------------------------------

    async def flush(self) -> None:
        async with self._lock:
            buf, self._buf = self._buf, {t: [] for t in _COLUMNS}
            kv, self._kv = self._kv, {}
            if any(buf.values()) or kv:
                await asyncio.to_thread(self._write, buf, kv)

    async def run(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.flush()
            except Exception:
                log.exception("state flush failed")

    def _write(self, buf: dict[str, list[tuple[Any, ...]]], kv: dict[str, str]) -> None:
        now = int(time.time() * 1000)
        with self._conn:
            for table, rows in buf.items():
                if rows:
                    ph = ",".join("?" * _COLUMNS[table])
                    self._conn.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)
            for k, v in kv.items():
                self._conn.execute(
                    "INSERT INTO kv VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, updated_ms = excluded.updated_ms", (k, v, now))

    async def close(self) -> None:
        await self.flush()
        self._conn.close()
