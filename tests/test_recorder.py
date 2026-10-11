import pyarrow.parquet as pq

from goldshop.market_data.models import BookTicker, Depth, MarkPrice, Trade
from goldshop.market_data.recorder import ParquetRecorder, hour_key

T0 = 1_791_708_000_000  # some ms timestamp
HOUR_MS = 3_600_000


def _book(ms, bid=100.0):
    return BookTicker(1, ms, ms, 1, bid, 1.0, bid + 0.1, 1.0)


def _files(root, stream):
    return sorted((root / "BTCUSDT" / stream).rglob("*.parquet"))


async def test_writes_all_streams(tmp_path):
    rec = ParquetRecorder(tmp_path, "BTCUSDT", 10)
    rec.handle(_book(T0))
    rec.handle(Depth(1, T0, T0, 9, ((100.0, 1.0), (99.9, 2.0)), ((100.1, 3.0),)))
    rec.handle(Trade(1, T0, T0, 7, 100.05, 0.5, True))
    rec.handle(MarkPrice(1, T0, 100.02, 100.01, 0.0001, T0 + HOUR_MS))
    await rec.close()
    assert rec.rows_written == 4
    d = pq.read_table(_files(tmp_path, "depth")[0]).to_pylist()[0]
    assert d["bid_px"] == [100.0, 99.9] and d["ask_qty"] == [3.0]
    t = pq.read_table(_files(tmp_path, "trades")[0]).to_pylist()[0]
    assert t["is_buyer_maker"] is True
    assert len(_files(tmp_path, "mark_price")) == 1


async def test_multiple_flushes_append_to_same_hour_file(tmp_path):
    rec = ParquetRecorder(tmp_path, "BTCUSDT", 10)
    rec.handle(_book(T0))
    await rec.flush()
    rec.handle(_book(T0 + 1000))
    await rec.flush()
    await rec.close()
    files = _files(tmp_path, "book_ticker")
    assert len(files) == 1
    assert pq.read_table(files[0]).num_rows == 2


async def test_rotates_on_hour_boundary(tmp_path):
    rec = ParquetRecorder(tmp_path, "BTCUSDT", 10)
    rec.handle(_book(T0))
    rec.handle(_book(T0 + HOUR_MS))
    await rec.close()
    files = _files(tmp_path, "book_ticker")
    assert len(files) == 2
    assert hour_key(T0) != hour_key(T0 + HOUR_MS)


async def test_restart_does_not_overwrite_existing_file(tmp_path):
    for _ in range(2):
        rec = ParquetRecorder(tmp_path, "BTCUSDT", 10)
        rec.handle(_book(T0))
        await rec.close()
    files = _files(tmp_path, "book_ticker")
    assert [f.name for f in files] == [f"{hour_key(T0)[-2:]}.1.parquet", f"{hour_key(T0)[-2:]}.parquet"]
    assert all(pq.read_table(f).num_rows == 1 for f in files)


def test_hour_key_is_utc():
    assert hour_key(0) == "1970-01-01/00"
