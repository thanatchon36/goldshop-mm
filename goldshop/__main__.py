"""`python -m goldshop` — run the market-making bot in MODE=paper / testnet / live.

paper:   live public market data, simulated fills, no keys.
testnet: Binance Futures testnet only (testnet keys + testnet URLs).
live:    real account, requires LIVE_CONFIRM=I_UNDERSTAND_THE_RISK.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import signal
import sys
import time

from goldshop.binance.rest import BinanceRestClient
from goldshop.binance.symbol import SymbolFilters, SymbolRuleError
from goldshop.binance.user_stream import UserDataStream
from goldshop.config import (
    AppConfig,
    ConfigError,
    Mode,
    Runtime,
    load_config,
    load_runtime,
    secret_values,
)
from goldshop.engine import Engine
from goldshop.execution.binance import BinanceExecutor
from goldshop.execution.orders import ClientIdGenerator
from goldshop.execution.paper.executor import PaperExecutor
from goldshop.execution.paper.sim import PaperExchange
from goldshop.logging_setup import redactor, setup_logging
from goldshop.market_data.feed import MarketDataFeed
from goldshop.market_data.models import BookTicker, MarketEvent
from goldshop.market_data.recorder import ParquetRecorder
from goldshop.notify.commands import CommandPoller, CommandRouter
from goldshop.notify.telegram import Priority, TelegramNotifier
from goldshop.ops.health import start_health_server
from goldshop.ops.metrics import Metrics, Snapshot
from goldshop.risk.manager import RiskManager
from goldshop.state.store import StateStore
from goldshop.strategy.avellaneda import StrategyParams

log = logging.getLogger("goldshop")

RECONNECT_WINDOW_S = 600


class StartupError(Exception):
    pass


def _startup_text(cfg: AppConfig, rt: Runtime, p: StrategyParams, f: SymbolFilters,
                  ref_px: float, rest_url: str) -> str:
    s, r = cfg.strategy, cfg.risk
    lines = [
        "🟢 goldshop-mm started",
        f"Mode: {rt.mode.value} | Symbol: {cfg.symbol} | REST {rest_url}",
        f"Order size: {p.order_qty:g} (~{p.order_qty * ref_px:,.2f} USDT per side), post-only GTX",
        f"Strategy max inventory: {p.max_inventory_qty:g} (~{s.max_inventory_notional_usdt:,.0f} USDT)",
        f"Min spread: {p.min_spread_bps:.2f} bps (2×maker {cfg.exchange.maker_fee_bps} + buffer "
        f"{s.min_spread_buffer_bps}) | A-S γ={s.gamma} κ={s.kappa} τ={s.horizon_s:g}s",
        "Risk limits:",
        f"• max position {r.max_position_notional_usdt:,.0f} USDT, max order {r.max_order_notional_usdt:,.0f} USDT",
        f"• daily loss limit {r.daily_loss_limit_usdt:,.2f} USDT → cancel all + reduce-only flatten + halt to 00:00 UTC",
        f"• circuit breaker {r.circuit_breaker_pct}% in {r.circuit_breaker_window_s:g}s → pause "
        f"{r.circuit_breaker_cooldown_s:g}s",
        f"• funding guard |rate| ≥ {r.funding_guard_rate:.4%} against position → reduce only",
        f"• stale data > {cfg.market_data.stale_after_s:g}s → cancel all, stop quoting",
    ]
    if rt.mode is Mode.PAPER:
        lines.append(f"Paper latency {cfg.paper.latency_ms} ms (fills only on trade-through)")
    else:
        lines.append(f"• dead-man switch: countdownCancelAll {r.dead_man_countdown_s}s | "
                     f"{r.margin_type} margin, leverage {r.leverage}x")
    lines.append(f"Reports every {cfg.report.interval_s // 60} min"
                 + (" + daily 00:00 UTC" if cfg.report.daily_summary else "")
                 + (" | Telegram commands ON" if rt.telegram and rt.telegram.commands_enabled else ""))
    return "\n".join(lines)


async def _prepare_account(rest: BinanceRestClient, cfg: AppConfig, ex: BinanceExecutor) -> None:
    """Exchange-side setup + reconcile, before any quote is placed."""
    mode = await rest.position_mode()
    if mode.get("dualSidePosition"):
        raise StartupError("account is in Hedge Mode; switch Binance Futures to One-way Mode")
    await rest.set_margin_type(cfg.symbol, cfg.risk.margin_type)
    await rest.set_leverage(cfg.symbol, cfg.risk.leverage)
    leftovers = await rest.open_orders(cfg.symbol)
    if leftovers:
        log.warning("cancelling open orders found at startup",
                    extra={"count": len(leftovers),
                           "client_ids": [o.get("clientOrderId") for o in leftovers]})
    await rest.cancel_all(cfg.symbol)


async def run(cfg: AppConfig, rt: Runtime, args: argparse.Namespace) -> int:
    ex_cfg, s = cfg.exchange, cfg.strategy
    ep = ex_cfg.endpoints(rt.mode)
    paper = rt.mode is Mode.PAPER
    notifier = TelegramNotifier(rt.telegram, f"{rt.mode.tag} {cfg.symbol}", cfg.notify)

    def alert(text: str, priority: Priority = Priority.LOW) -> None:
        if priority is Priority.HIGH:  # every alert is also in the log, Telegram or not
            log.warning("alert", extra={"text": text})
        notifier.send(text, priority)

    notifier_task = asyncio.create_task(notifier.run(), name="telegram")

    rest = BinanceRestClient(ep.rest, credentials=None if paper else rt.credentials,
                             timeout_s=ex_cfg.request_timeout_s,
                             weight_backoff_ratio=ex_cfg.weight_backoff_ratio,
                             recv_window_ms=ex_cfg.recv_window_ms)
    try:
        async with rest:
            await rest.sync_time()
            info = await rest.exchange_info()
            filters = SymbolFilters.from_exchange_info(info, cfg.symbol)
            ref = next(b for b in await rest.book_ticker_all() if b["symbol"] == cfg.symbol)
            ref_px = (float(ref["bidPrice"]) + float(ref["askPrice"])) / 2
            order_qty = filters.order_qty_for_notional(s.order_notional_usdt, ref_px)
            max_inv_qty = filters.floor_qty(s.max_inventory_notional_usdt / ref_px)
            if max_inv_qty < order_qty:
                raise SymbolRuleError(f"max_inventory_notional_usdt={s.max_inventory_notional_usdt} is "
                                      f"smaller than one order ({order_qty} ≈ {order_qty * ref_px:.0f} USDT)")
            params = StrategyParams(s.gamma, s.kappa, s.horizon_s, ex_cfg.maker_fee_bps,
                                    s.min_spread_buffer_bps, order_qty, max_inv_qty)

            ids = ClientIdGenerator(f"gs{secrets.token_hex(3)}")
            executor: PaperExecutor | BinanceExecutor
            if paper:
                sim = PaperExchange(cfg.paper.latency_ms, ex_cfg.maker_fee_bps, ex_cfg.taker_fee_bps)
                executor = PaperExecutor(sim, ids)
            else:
                executor = BinanceExecutor(rest, filters, ids, ex_cfg.maker_fee_bps, alert)
                await _prepare_account(rest, cfg, executor)

            store = StateStore(cfg.state.db_dir, rt.mode.value)
            risk = RiskManager(cfg.risk, alert)
            engine = Engine(filters, params, s, executor, alert=alert, risk=risk, store=store)
            state_key = f"engine:{cfg.symbol}"
            saved = store.get(state_key)
            if saved:
                engine.restore_state(saved)
                log.info("restored state", extra={"db": str(store.path),
                                                  "position": engine.portfolio.position_qty,
                                                  "fills": engine.portfolio.fill_count})

            # Reconcile position with the exchange: the exchange is the truth.
            if isinstance(executor, BinanceExecutor):
                pos, entry = await executor.fetch_position()
                if abs(pos - engine.portfolio.position_qty) > float(filters.step_size) / 2:
                    alert(f"⚠️ Startup reconcile: stored position {engine.portfolio.position_qty:+g} ≠ "
                          f"exchange {pos:+g}; using exchange", Priority.HIGH)
                engine.portfolio.position_qty, engine.portfolio.avg_price = pos, entry if pos else 0.0
            else:
                executor.sim.position_qty = engine.portfolio.position_qty

            def on_day_end() -> None:
                if cfg.report.daily_summary:
                    notifier.send(engine.stats.report("Daily summary", engine.mark,
                                                      feed.state.reconnects))

            engine.on_day_end = on_day_end
            started = time.monotonic()

            def snapshot() -> Snapshot:
                p, m, b, q = engine.portfolio, engine.mark, engine.book, engine.last_quote
                return Snapshot(
                    mode=rt.mode.value, symbol=cfg.symbol, net=p.net(m), realized=p.realized,
                    unrealized=p.unrealized(m), fees=p.fees, funding=p.funding,
                    net_today=engine.stats.net_today(m), inventory_qty=p.position_qty, mark=m,
                    market_spread_bps=(b.ask - b.bid) / b.mid * 1e4 if b else None,
                    quote_spread_bps=q.spread_bps if q else None, sigma_bps=engine.vol.sigma_bps,
                    quoting=engine.quote_status == "quoting" and not engine.stale,
                    stale=engine.stale, paused=risk.manual_pause,
                    daily_halted=risk.daily_halt_until_ms > engine.now_ms,
                    working_orders=sum(1 for o in executor.working_orders() if o.is_working),
                    ws_reconnects=feed.state.reconnects,
                    user_stream_reconnects=uds.reconnects if uds else 0,
                    uptime_s=time.monotonic() - started, telegram_dropped=notifier.dropped,
                )

            metrics = Metrics(snapshot)
            executor.on_exec_event(metrics.on_exec)
            engine.on_markout = metrics.on_markout
            if isinstance(executor, BinanceExecutor):
                executor.on_latency = metrics.on_latency

            feed = MarketDataFeed(cfg.symbol, ep.ws_public, ep.ws_market, cfg.market_data)
            queue: asyncio.Queue[MarketEvent] = asyncio.Queue()
            feed.on_event(queue.put_nowait)
            feed.on_event(lambda ev: metrics.md_latency.observe(
                max(0.0, (ev.recv_ns / 1e6 + rest.time_offset_ms - ev.event_ms) / 1000)))
            rec: ParquetRecorder | None = None
            if cfg.recorder.enabled and not args.no_record:
                rec = ParquetRecorder(cfg.recorder.data_dir, cfg.symbol, cfg.recorder.flush_interval_s)
                feed.on_event(rec.handle)
            uds: UserDataStream | None = None
            if isinstance(executor, BinanceExecutor):
                uds = UserDataStream(rest, ep.ws_private, executor.handle_user_event,
                                     on_connected=executor.resync,
                                     backoff_initial_s=cfg.market_data.reconnect_backoff_initial_s,
                                     backoff_max_s=cfg.market_data.reconnect_backoff_max_s)

            def not_ready_reason() -> str | None:
                stale, reason = feed.check_stale()
                if stale:
                    return f"market data: {reason}"
                if uds is not None and not uds.connected:
                    return "user data stream disconnected"
                return None

            # ---- background tasks -------------------------------------------------------

            async def stale_monitor() -> None:
                """Quote only while market data AND (testnet/live) the user stream are healthy."""
                prev_ok, ever_ok = False, False
                while True:
                    await asyncio.sleep(0.5)
                    reason = not_ready_reason()
                    ok = reason is None
                    if ok == prev_ok:
                        continue
                    prev_ok = ok
                    await engine.set_stale(not ok)
                    if not ok:
                        alert(f"🔴 Quoting stopped, orders cancelled: {reason}", Priority.HIGH)
                    elif ever_ok:
                        alert("🟢 Data streams healthy again — quoting resumes", Priority.HIGH)
                    ever_ok = ever_ok or ok

            last_err_alert = 0.0

            async def consume() -> None:
                nonlocal last_err_alert
                while True:
                    batch = [await queue.get()]
                    while not queue.empty():
                        batch.append(queue.get_nowait())
                    # Quote only on the newest book so a backlog never produces stale quotes.
                    last_book = max((i for i, e in enumerate(batch) if isinstance(e, BookTicker)),
                                    default=-1)
                    metrics.engine_lag.observe(max(0.0, (time.time_ns() - batch[-1].recv_ns) / 1e9))
                    for i, ev in enumerate(batch):
                        try:
                            await engine.handle(ev, quote=i == last_book)
                        except Exception as e:
                            log.exception("engine error")
                            if time.monotonic() - last_err_alert > 60:
                                last_err_alert = time.monotonic()
                                alert(f"❗ Unexpected error: {type(e).__name__}: {e}", Priority.HIGH)

            async def reporter() -> None:
                interval = cfg.report.interval_s
                while True:
                    await asyncio.sleep(interval - time.time() % interval + 0.5)  # aligned to clock
                    notifier.send(engine.stats.report("Report", engine.mark, feed.state.reconnects))

            async def reconnect_watch() -> None:
                alerted_at = 0.0
                while True:
                    await asyncio.sleep(10)
                    n = feed.reconnects_within(RECONNECT_WINDOW_S)
                    if (n > cfg.notify.reconnect_alert_count
                            and time.monotonic() - alerted_at > RECONNECT_WINDOW_S):
                        alerted_at = time.monotonic()
                        alert(f"🔌 {n} WebSocket reconnects in the last 10 min", Priority.HIGH)

            async def snapshot_loop() -> None:
                while True:
                    await asyncio.sleep(cfg.state.pnl_snapshot_interval_s)
                    p, m = engine.portfolio, engine.mark
                    store.add("pnl", int(time.time() * 1000), cfg.symbol, p.net(m), p.realized,
                              p.unrealized(m), p.fees, p.funding, p.position_qty, m)
                    store.put(state_key, engine.to_state())

            async def dead_man() -> None:
                ms = cfg.risk.dead_man_countdown_s * 1000
                failing_since: float | None = None
                while True:
                    try:
                        await rest.countdown_cancel_all(cfg.symbol, ms)
                        failing_since = None
                    except Exception as e:
                        log.warning("dead-man heartbeat failed", extra={"error": str(e)})
                        failing_since = failing_since or time.monotonic()
                        if time.monotonic() - failing_since > cfg.risk.dead_man_countdown_s / 2:
                            alert("⚠️ Dead-man heartbeat failing — exchange may auto-cancel orders",
                                  Priority.HIGH)
                            failing_since = time.monotonic()
                    await asyncio.sleep(cfg.risk.dead_man_refresh_s)

            async def position_check() -> None:
                assert isinstance(executor, BinanceExecutor)
                step = float(filters.step_size)
                mismatches = 0
                while True:
                    await asyncio.sleep(cfg.risk.position_check_interval_s)
                    try:
                        await executor.resync()
                        pos, entry = await executor.fetch_position()
                    except Exception as e:
                        log.warning("position check failed", extra={"error": str(e)})
                        continue
                    local = engine.portfolio.position_qty
                    if abs(pos - local) > step / 2:
                        mismatches += 1
                        if mismatches >= 2:  # two checks in a row → not just a fill in flight
                            alert(f"⚠️ Position mismatch: local {local:+g} vs exchange {pos:+g} — "
                                  "adopting exchange position", Priority.HIGH)
                            engine.portfolio.position_qty = pos
                            engine.portfolio.avg_price = entry if pos else 0.0
                            mismatches = 0
                    else:
                        mismatches = 0

            async def time_sync() -> None:
                while True:
                    await asyncio.sleep(ex_cfg.time_sync_interval_s)
                    try:
                        await rest.sync_time()
                    except Exception as e:
                        log.warning("time sync failed", extra={"error": str(e)})

            # ---- Telegram commands ---------------------------------------------------------

            async def cmd_status(_: list[str]) -> str:
                p, m = engine.portfolio, engine.mark
                orders = ", ".join(f"{o.side.value} {o.remaining:g}@{o.price:g}"
                                   for o in executor.working_orders()) or "none"
                up = int(time.monotonic() - started)
                q = engine.last_quote
                return "\n".join([
                    f"ℹ️ Status: {engine.quote_status}"
                    + (f" ({not_ready_reason()})" if not_ready_reason() else ""),
                    f"Mark {m:,.2f} | σ {engine.vol.sigma_bps:.2f} bps/√s",
                    f"Position {p.position_qty:+g} @ {p.avg_price:,.2f} ({p.position_qty * m:+,.2f} USDT)",
                    f"Orders: {orders}",
                    f"Last quote: {q.bid_px} / {q.ask_px} (spread {q.spread_bps:.2f} bps)" if q else "Last quote: none",
                    f"Risk: paused={risk.manual_pause} daily_halt={risk.daily_halted(engine.now_ms)} "
                    f"funding_guard={risk.funding_reduce_only.value if risk.funding_reduce_only else 'off'}",
                    f"Uptime {up // 3600}h{(up % 3600) // 60:02d}m | WS reconnects {feed.state.reconnects}"
                    + (f" | user-stream reconnects {uds.reconnects}" if uds else ""),
                ])

            async def cmd_pnl(_: list[str]) -> str:
                return engine.stats.report("PnL", engine.mark, feed.state.reconnects)

            async def cmd_pause(_: list[str]) -> str:
                return await engine.pause()

            async def cmd_resume(_: list[str]) -> str:
                return risk.resume(engine.now_ms)

            async def cmd_flatten(_: list[str]) -> str:
                await engine.flatten("telegram /flatten")
                return "🧹 Flatten sent: orders cancelled, position closing (reduce-only). Quoting paused — /resume to restart."

            # ---- start ----------------------------------------------------------------------

            startup = _startup_text(cfg, rt, params, filters, ref_px, ep.rest)
            log.info("startup", extra={"mode": rt.mode.value, "symbol": cfg.symbol, "order_qty": order_qty,
                                       "max_inventory_qty": max_inv_qty, "risk": cfg.risk.model_dump(),
                                       "summary": startup})
            alert(startup, Priority.HIGH)

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            if args.duration:
                loop.call_later(args.duration, stop.set)

            tasks = [
                asyncio.create_task(feed.run(), name="feed"),
                asyncio.create_task(consume(), name="engine"),
                asyncio.create_task(stale_monitor(), name="stale-monitor"),
                asyncio.create_task(reporter(), name="reporter"),
                asyncio.create_task(reconnect_watch(), name="reconnect-watch"),
                asyncio.create_task(store.run(cfg.state.flush_interval_s), name="state-store"),
                asyncio.create_task(snapshot_loop(), name="snapshots"),
            ]
            if rec:
                tasks.append(asyncio.create_task(rec.run(), name="recorder"))
            if uds:
                tasks += [asyncio.create_task(uds.run(), name="user-stream"),
                          asyncio.create_task(dead_man(), name="dead-man"),
                          asyncio.create_task(position_check(), name="position-check"),
                          asyncio.create_task(time_sync(), name="time-sync")]
            if rt.telegram and rt.telegram.commands_enabled:
                router = CommandRouter(cmd_status, cmd_pnl, cmd_pause, cmd_resume, cmd_flatten)
                tasks.append(asyncio.create_task(CommandPoller(rt.telegram, router, notifier).run(),
                                                 name="telegram-commands"))

            def healthy() -> tuple[bool, str]:
                reason = not_ready_reason()
                if reason:
                    return False, reason
                age = time.monotonic() - engine.last_event_mono
                if age > cfg.market_data.stale_after_s * 2:
                    return False, f"engine loop stalled ({age:.1f}s since last event)"
                return True, "ok"

            health = (await start_health_server(cfg.ops.health_port, healthy, metrics.render)
                      if cfg.ops.health_enabled else None)

            # A crashed background task must not leave a zombie bot: treat it as fatal.
            waiter = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
            crashed = [t for t in done if t is not waiter and t.exception() is not None]
            for t in crashed:
                log.error("task crashed", extra={"task": t.get_name()}, exc_info=t.exception())
                alert(f"❗ Task {t.get_name()} crashed: {t.exception()!r} — shutting down", Priority.HIGH)

            # ---- graceful shutdown: stop quoting, cancel everything, persist ----------------
            log.info("shutting down")
            engine.stale = True
            for t in tasks:
                if t.get_name() in ("engine", "stale-monitor", "feed"):
                    t.cancel()
            try:
                await executor.cancel_all()
                if uds:
                    await rest.countdown_cancel_all(cfg.symbol, 0)  # disarm dead-man
            except Exception:
                log.exception("cancel on shutdown failed (dead-man switch will cancel)")
            feed.stop()
            if uds:
                uds.stop()
            store.put(state_key, engine.to_state())
            alert("🛑 goldshop-mm shutting down — all orders cancelled\n"
                  + engine.stats.report("Final", engine.mark, feed.state.reconnects), Priority.HIGH)
            await notifier.drain(timeout_s=10)
            for t in tasks:
                t.cancel()
            waiter.cancel()
            notifier_task.cancel()
            await asyncio.gather(*tasks, waiter, notifier_task, return_exceptions=True)
            await store.close()
            if rec:
                await rec.close()
            if health:
                await health.cleanup()
            log.info("stopped", extra={"fills": engine.portfolio.fill_count,
                                       "net_pnl": round(engine.portfolio.net(engine.mark), 4)})
            return 1 if crashed else 0
    except Exception as e:
        log.exception("fatal error")
        alert(f"❗ goldshop-mm stopped with error: {type(e).__name__}: {e}", Priority.HIGH)
        await notifier.drain(timeout_s=5)
        raise
    finally:
        notifier_task.cancel()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m goldshop", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--no-record", action="store_true", help="don't record market data")
    p.add_argument("--duration", type=float, help="stop after N seconds")
    args = p.parse_args()
    try:
        cfg = load_config(args.config)
        setup_logging(cfg.logging.level)
        rt = load_runtime()
        redactor.add(*secret_values(rt))
        sys.exit(asyncio.run(run(cfg, rt, args)))
    except (ConfigError, SymbolRuleError, StartupError) as e:
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
