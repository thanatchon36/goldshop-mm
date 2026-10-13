# goldshop-mm

[![CI](https://github.com/thanatchon36/goldshop-mm/actions/workflows/ci.yml/badge.svg)](https://github.com/thanatchon36/goldshop-mm/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

*Single-symbol Avellaneda–Stoikov market maker for Binance USDⓈ-M futures: post-only quotes, inventory control,
a strict paper fill engine, Telegram reports and Prometheus/Grafana monitoring.*

A market-making bot for **Binance USDⓈ-M Futures**, built like a gold shop: it keeps a limit order on both the bid and
the ask to earn the spread, while keeping inventory from piling up on one side. It runs 24/7 in a container and sends
performance reports and alerts to Telegram.

> ⚠️ **Warning.** Market making on futures is high risk. The profit per round trip is tiny (a few bps) and adverse
> selection can easily outweigh it. At VIP0 the maker fee (0.02 % per side) often eats the whole spread on liquid
> pairs. This software comes with no warranty. Always start in paper mode and only trade money you can afford to lose.

---

## Contents

1. [Recommended order: paper → testnet → live](#1-recommended-order-paper--testnet--live)
2. [Installation](#2-installation)
3. [Configuration (.env and config.yaml)](#3-configuration)
4. [Telegram bot setup](#4-telegram-bot-setup)
5. [Binance API keys (testnet / live)](#5-binance-api-keys)
6. [Running each mode](#6-running-each-mode)
7. [Tools: screener, recorder, backtest](#7-tools-screener-recorder-backtest)
8. [Monitoring: health, Prometheus, Grafana](#8-monitoring)
9. [Risk protections](#9-risk-protections)
10. [✅ Go-live checklist](#10--go-live-checklist)
11. [Project layout](#11-project-layout)
12. [Known limitations](#12-known-limitations)
13. [Roadmap](#13-roadmap)
14. [License and disclaimer](#14-license-and-disclaimer)

---

## 1. Recommended order: paper → testnet → live

| Step | Mode | Keys needed | Purpose |
|---|---|---|---|
| 1 | `paper` (default) | ❌ none | Real market prices with a strict fill simulation → **does the strategy make money after fees?** |
| 2 | `testnet` | testnet keys | Real orders on Binance Futures Testnet → **tests the mechanics** (place/cancel, reconcile, kill switch, restart, reconnect) |
| 3 | `live` | real keys + `LIVE_CONFIRM` | **Small size first**, scale up only once results match paper |

Good to know:
- **Paper** is where profit is measured. A fill counts only when a trade goes *through* our price (we assume we are at
  the back of the queue), plus latency, GTX rejects and full fees.
- **Testnet** cannot measure profit. Its order book is thin, prices barely move and fills are odd. Use it only to check
  that the system *behaves correctly*.
- The bot **never swaps keys across modes**. `MODE=testnet` uses only `BINANCE_TESTNET_*` and testnet URLs. Without
  testnet keys it stops immediately; it never falls back to real keys.

---

## 2. Installation

### Docker (recommended for 24/7 running)

Requires Docker and Docker Compose v2.

```bash
git clone https://github.com/thanatchon36/goldshop-mm.git && cd goldshop-mm
cp .env.example .env          # edit as described in section 3
mkdir -p data
docker compose up -d --build  # MODE=paper (default) needs no keys
docker compose ps             # should show (healthy) within ~30 s
docker compose logs -f
```

> **Linux server:** the container runs as uid `10001` (not root), so give it write access to the data folder first:
> `sudo chown -R 10001:10001 data` (not needed with Docker Desktop on macOS/Windows).

Container hardening: multi-stage build, non-root user, read-only filesystem (writable only `./data` and `/tmp`),
`cap_drop: ALL`, `no-new-privileges`, `restart: unless-stopped`, a healthcheck on `/health`, log rotation 20 MB × 5.

### Python (for development and experiments)

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                     # all unit tests
python -m goldshop            # paper mode
```

---

## 3. Configuration

### `.env`: secrets only (never commit it; `.gitignore` already excludes it)

| Variable | Mode | Notes |
|---|---|---|
| `MODE` | all | `paper` (default) / `testnet` / `live` |
| `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET` | testnet | Testnet keys (section 5.1) |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | live | Real account keys (section 5.2) |
| `LIVE_CONFIRM` | live | Must equal `I_UNDERSTAND_THE_RISK`, otherwise the bot refuses to start |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional | Unset = no Telegram messages (not an error) |
| `TELEGRAM_COMMANDS` | optional | `true` to accept commands from Telegram (default `false`) |
| `GRAFANA_ADMIN_PASSWORD` | optional | Grafana password (monitoring profile) |

Secrets can come from environment variables, the `.env` file or Docker secrets (`/run/secrets/<lowercase variable
name>`, for example `/run/secrets/binance_api_secret`). Secrets are never written to logs: they are stored as
`SecretStr`, and a log filter strips them as a second layer.

If keys are set while in paper mode, the log warns *"API keys are set but will NOT be used"*. That is expected.

### `config.yaml`: everything else (small, conservative defaults)

Every value is documented in the file. The important ones:

| Section | Key | Default | Meaning |
|---|---|---|---|
| — | `symbol` | `BTCUSDT` | Pick it with the screener (section 7); never changes while running |
| `exchange` | `maker_fee_bps` | `2.0` | **Set this to your account's real fee tier** |
| `strategy` | `order_notional_usdt` | `100` | Order size per side (must pass the symbol's minQty/minNotional, otherwise the bot refuses to start and prints the minimum) |
| `strategy` | `max_inventory_notional_usdt` | `500` | At full inventory, quote only the side that reduces it |
| `strategy` | `gamma`, `kappa`, `horizon_s` | `0.05`, `0.5`, `60` | Avellaneda–Stoikov parameters (in bps) |
| `strategy` | `min_spread_buffer_bps` | `1.0` | Quoted spread is always ≥ 2 × maker fee + buffer |
| `risk` | `max_position_notional_usdt` | `600` | Hard position cap |
| `risk` | `max_order_notional_usdt` | `200` | Hard cap per order |
| `risk` | `daily_loss_limit_usdt` | `20` | Net loss today reaches this → cancel + flatten + halt until 00:00 UTC |
| `risk` | `circuit_breaker_pct` / `_window_s` | `0.5` / `10` | Price moves ≥ 0.5 % within 10 s → stop quoting for 60 s |
| `risk` | `leverage`, `margin_type` | `2`, `ISOLATED` | Applied automatically at startup (testnet/live) |
| `report` | `interval_s` | `3600` | Hourly Telegram report plus a daily summary at 00:00 UTC |

After editing `config.yaml` (Docker mounts it read-only): `docker compose restart`.

---

## 4. Telegram bot setup

1. In Telegram, talk to **[@BotFather](https://t.me/BotFather)** → send `/newbot` → choose a name → you get a
   **token** like `123456789:AA...`.
2. Create a group (or use a private chat with the bot), **add the bot to the group** and send any message there
   (for example `/start`).
3. Find the **chat_id** by opening this in a browser:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look at `"chat":{"id": ... }`. Groups have negative ids (for example `-4012345678`; supergroups start with `-100`).
   > If the bot is already running with `TELEGRAM_COMMANDS=true`, stop it before calling getUpdates, or the two will
   > compete for messages.
4. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AA...
   TELEGRAM_CHAT_ID=-4012345678
   TELEGRAM_COMMANDS=false     # true to control the bot from Telegram
   ```
5. Start the bot → the group should receive `🟢 goldshop-mm started` with the mode, symbol, order size and every
   risk limit.

**🔒 Security:**
- **Never commit the token** or paste it into chats, issues or screenshots. If it leaks, send `/revoke` to @BotFather
  and update `.env`.
- The bot accepts commands **only from `TELEGRAM_CHAT_ID`**. Messages from anywhere else are ignored and logged.
- Everyone in that group can command the bot, so use a group with only you (or people you trust).

### Messages you will receive

Every message starts with `[PAPER]` / `[TESTNET]` / `[LIVE]` and the symbol.

- **Periodic reports** (hourly, plus a daily summary at 00:00 UTC): net PnL today / cumulative, realized / unrealized,
  fees, funding, fill count, volume, average edge vs mid (bps), inventory, markout +1s/+5s/+30s, today's max
  drawdown, uptime, reconnects.
- **Immediate alerts** (high priority, sent ahead of reports): start/shutdown, daily loss limit, circuit breaker
  stop/resume, funding guard, inventory at its limit, stale data / user stream lost / restored, more than 5
  reconnects in 10 minutes, position mismatch with the exchange, insufficient margin, unexpected errors.

### Commands (with `TELEGRAM_COMMANDS=true`)

Uses long polling, so no port needs to be open. Commands sent before the bot started are skipped.

| Command | Effect |
|---|---|
| `/status` | Quote status, the reason it stopped (if any), position, open orders, σ, risk flags, uptime |
| `/pnl` | Current PnL report |
| `/pause` | Stop quoting and cancel all orders (**does not close the position**) |
| `/resume` | Resume quoting. **Has no effect while the daily loss limit is active** (wait for 00:00 UTC) |
| `/flatten` | Cancel everything and close the position with a reduce-only market order. **Must be confirmed with `/flatten CONFIRM` within 30 s**; the bot stays paused afterwards (use `/resume` to restart) |

The risk manager always overrides Telegram commands (for example, `/resume` cannot lift a daily loss halt or a
circuit breaker).

---

## 5. Binance API keys

### 5.1 Testnet

1. Go to **https://testnet.binancefuture.com** (it may redirect to Binance Demo Trading) → log in → **API Key**.
2. Create an HMAC key → copy the API Key and Secret Key.
3. Add to `.env`:
   ```
   MODE=testnet
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_API_SECRET=...
   ```
4. The testnet account must be in **One-way Mode** (not Hedge Mode). In Hedge Mode the bot refuses to start and says
   why.

Testnet URLs live in `config.yaml` (`testnet_rest_base_url: https://demo-fapi.binance.com` and
`wss://fstream.binancefuture.com/...`). Testnet mode never touches live URLs.

### 5.2 Live (real account)

On Binance → **Account → API Management → Create API** → *System generated* (HMAC).

**Grant the minimum permissions:**

- [x] **Enable Reading**
- [x] **Enable Futures**: the only permission trading needs
- [ ] **Enable Withdrawals: never turn this on**
- [ ] Enable Spot & Margin Trading: not needed, leave off
- [ ] Universal Transfer and everything else: off

**Restrict by IP:** choose *Restrict access to trusted IPs only* and enter **only the public IP of the server running
the bot** (`curl -s https://api.ipify.org` on that server). A leaked key without an IP restriction works from
anywhere.

**Create a key just for this bot.** Don't share it with other bots or apps, so you can revoke it at once without
breaking anything else, and audit it easily.

Prepare the Futures account:
- Transfer only as much USDT into the USDⓈ-M Futures wallet as you can afford to lose (the bot uses ISOLATED margin
  at 2× leverage per the config).
- Set **One-way Mode**.
- If fees are paid in BNB, the bot estimates them in USDT from the maker rate in the config (with a one-time log
  warning).

Add to `.env` (type it on the server directly; **never paste keys into chats, emails or tickets**):
```
MODE=live
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
```
Run `chmod 600 .env` so only the owner can read it, or use Docker secrets instead of `.env`.

---

## 6. Running each mode

The mode comes only from `MODE` in `.env`. The run commands are the same for every mode:

```bash
docker compose up -d --force-recreate     # a changed .env needs a recreate
docker compose logs -f
```

Or with Python: `python -m goldshop [--duration 600] [--no-record]`

### paper (default)

```bash
MODE=paper
```
- Uses only public market data from the real exchange; no keys needed.
- Strict fill engine: fills only when a trade goes through our price, 100 ms latency (`paper.latency_ms`), GTX rejects
  simulated when the order arrives, full maker fee, funding at the real intervals from the markPrice stream.
- State is kept in `data/state-paper.sqlite` (PnL carries over restarts).

### testnet

```bash
MODE=testnet
BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_API_SECRET=...
```
At startup the bot: syncs server time → checks One-way Mode → sets ISOLATED + leverage → **cancels any open
orders** → reads the position from the exchange (if it differs from the saved state, the exchange wins and an alert
is sent) → opens the User Data Stream → arms the dead-man switch → starts quoting once both market data and the user
stream are ready.

### live

```bash
MODE=live
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
```
Same steps as testnet; only the URLs and keys differ. **Pass the checklist in section 10 first**, and start small
(`order_notional_usdt` at the symbol minimum, a low `daily_loss_limit_usdt`).

### Stopping the bot

```bash
docker compose stop        # SIGTERM → cancel all orders → disarm dead-man → save state → Telegram notice
```
A normal stop **cancels orders but does not close the position**. To close it, use `/flatten` first or close it on
Binance yourself.

---

## 7. Tools: screener, recorder, backtest

### Screener: choosing a symbol

```bash
python -m goldshop.screener                         # top 20
python -m goldshop.screener --top 30 --min-volume 20000000 --maker-fee-bps 1.8
python -m goldshop.screener --csv > screen.csv
```
Scans every USDT perpetual (filtered by volume before fetching klines) and reports `spread_bps`, 24 h volume,
σ per minute, `net_edge_bps = spread − 2×maker_fee` and `goldshop_score = net_edge / σ` (higher is better).
> The spread is a single-moment snapshot. Run it several times at different hours before deciding.

### Recorder: data for backtests

The bot records automatically (`recorder.enabled: true`) to hourly Parquet files:
`data/recordings/<SYMBOL>/<book_ticker|depth|trades|mark_price>/<YYYY-MM-DD>/<HH>.parquet`

Record without trading: `python -m goldshop.market_data [--symbol ETHUSDT]`

### Backtest: replay through the same paper fill engine used live

```bash
python -m goldshop.backtest                                   # all data for config.symbol
python -m goldshop.backtest --start 2026-10-11T00:00 --end 2026-10-12T00:00 --config alt.yaml
```
Output in `reports/backtest-<SYMBOL>-<time>/`: `report.html` (PnL curve, inventory, markout distribution),
`summary.json`, `timeseries.csv`, `fills.csv`.

- Uses only tick data from the recorder (not OHLCV, which cannot tell whether a quote would have been filled).
- The current hour's file, still being written, is skipped with a warning.
- Gaps longer than `stale_after_s` are simulated as stale data, exactly as in a live run.

---

## 8. Monitoring

| Endpoint | Purpose |
|---|---|
| `GET /health` (port 8080 in the container) | 200 = OK, 503 = stale data / user stream lost / engine loop stalled. Used as the Docker healthcheck |
| `GET /metrics` | Prometheus: PnL, inventory, spread, σ, fill rate, markout, latency, reconnects, quote/risk status |

Start Prometheus + Grafana (optional):

```bash
docker compose --profile monitoring up -d --build
```
- Grafana: http://localhost:3000 (user `admin`, password from `GRAFANA_ADMIN_PASSWORD`) → dashboard **goldshop-mm**
- Prometheus: http://localhost:9090 (30 days retention)
- Both listen on `127.0.0.1` only. On a server, use an SSH tunnel: `ssh -L 3000:localhost:3000 user@server`

Logs are JSON, one event per line, on stdout (`docker compose logs`). Every alert, fill and Telegram command is
logged. History lives in SQLite `data/state-<mode>.sqlite` (tables `fills`, `quotes`, `pnl`, `funding`,
`markouts`).

---

## 9. Risk protections

The risk manager always overrides both the strategy and Telegram commands.

| Mechanism | Triggers when | Effect |
|---|---|---|
| Post-only (GTX) | every order | Always maker; an order that would cross is rejected by the exchange (`-5022`) |
| Position / order limits | a new order would exceed a cap | The side that increases the position is not sent |
| Inventory limit (strategy) | inventory reaches `max_inventory_notional_usdt` | Quote only the side that reduces inventory, and alert |
| **Daily loss limit** | today's net PnL ≤ −limit | Cancel all → close the position reduce-only → halt until 00:00 UTC (`/resume` has no effect) |
| Circuit breaker | price moves ≥ X % within Y s | Stop quoting for the cooldown |
| Funding guard | funding against the position ≥ threshold | Quote only the side that reduces inventory |
| Stale data | no update for `stale_after_s`, or the user stream is lost | Cancel all and stop quoting until data returns |
| **Dead-man switch** (testnet/live) | the bot hangs, the network drops or the process dies | `countdownCancelAll`: the exchange cancels every order by itself within 60 s |
| Reconcile | at startup, every 30 s and after a user-stream reconnect | Sync orders/position with the exchange, recover missed fills, alert on mismatch |
| Rate-limit guard | weight / order count near 80 % of the limit | Wait / hold orders |
| Graceful shutdown | SIGTERM | Cancel all → disarm dead-man → save state |

> The dead-man switch **cancels orders but does not close the position**. If an order fills while the bot is hung,
> that position stays open until the bot comes back and reconciles (or you close it yourself).

---

## 10. ✅ Go-live checklist

Don't skip anything. If an item fails, go back and fix it in paper/testnet.

### A. Paper results (profitability)
- [ ] Paper ran continuously for **at least 1–2 weeks** on the same symbol and config you will use live
- [ ] That window **covered several market regimes** (sideways, strong trend, news / high volatility, weekend)
- [ ] **Net PnL after fees and funding is positive** (daily Telegram report / Grafana / `pnl` in SQLite)
- [ ] **Average markout is not worse than the captured spread**: compare "Avg captured edge vs mid" with markout
      +5s/+30s. If edge − |negative markout| ≤ fee, adverse selection is eating everything.
- [ ] No day hit the daily loss limit without a cause you understand
- [ ] `maker_fee_bps` in the config matches the account's real fee tier
- [ ] (Recommended) a backtest of the recorded data agrees with paper

### B. Testnet (mechanics)
- [ ] **Kill switch**: with orders open, run `docker kill goldshop-mm` (SIGKILL) → within ~60 s Binance shows no open
      orders
- [ ] **Restart**: `docker compose restart` → log/Telegram shows a reconcile, no duplicate orders, position matches
      the exchange
- [ ] **Reconnect**: cut the container's network briefly
      ```bash
      docker network disconnect goldshop-mm_default goldshop-mm      # cut for ~10 s
      docker network connect --alias goldshop goldshop-mm_default goldshop-mm
      ```
      → alerts "🔴 Quoting stopped, orders cancelled" then "🟢 Data streams healthy again" (within ~10 s), and no
      orders left resting during the outage (find the network name with `docker network ls`)
- [ ] **Graceful stop**: `docker compose stop` → every order cancelled
- [ ] **Telegram commands**: `/status`, `/pause` (orders gone), `/resume`, `/flatten` + `/flatten CONFIRM`
      (position 0)
- [ ] **Daily loss limit**: temporarily set `daily_loss_limit_usdt` very low → flatten + halt, and `/resume` is
      refused
- [ ] Healthcheck shows `(healthy)` and Grafana is fully populated

### C. Live account and keys
- [ ] A new key just for this bot with only Reading + Futures, **Withdrawals off**, **IP restricted to the server**
- [ ] `.env` exists only on the server with mode `600`, and was never committed or sent over chat
- [ ] The Futures account is in One-way Mode and the Futures wallet holds only what you can afford to lose
- [ ] The Telegram token never leaked (if it was ever pasted elsewhere → `/revoke` and use a new token)

### D. First-week live settings
- [ ] `order_notional_usdt` at the symbol minimum
- [ ] A low `daily_loss_limit_usdt` (for example 1–2 % of the futures wallet)
- [ ] `max_position_notional_usdt` no more than 3–5× the order size
- [ ] Watch closely for the first 24–48 h and compare fills/markout with paper. If they differ a lot, stop and find
      out why.

---

## 11. Project layout

```
goldshop/
  __main__.py           bot runner (paper/testnet/live): account setup, reconcile, tasks, commands, shutdown
  config.py             config.yaml (pydantic) + secrets from env/.env/Docker secrets + per-mode key rules
  engine.py             market data → σ → quote → risk → executor (shared by every mode and the backtest)
  accounting.py         position / PnL with average cost
  stats.py              daily statistics + report text
  screener/             python -m goldshop.screener
  market_data/          WebSocket feed (reconnect, stale detection), models, Parquet recorder
  strategy/             Avellaneda–Stoikov (pure logic) + EWMA σ
  execution/            common interface, order state machine, BinanceExecutor, paper/ (fill engine)
  binance/              REST (signing, rate limits, time sync), User Data Stream, symbol filters
  risk/                 risk manager
  state/                SQLite store, markout tracker
  notify/               Telegram notifier (queue + rate limit) and commands (long polling)
  ops/                  /health, /metrics
  backtest/             replay + report
monitoring/             Prometheus config, Grafana provisioning + dashboard
tests/                  pytest (strategy, risk, fill engine, order state machine, notifier, executor, …)
```

Run the tests: `pytest -q`

---

## 12. Known limitations

- **The paper fill engine only sees L1.** A paper flatten fills at the best bid/ask plus taker fee, with no slippage
  beyond that.
- **Testnet does not reflect real profit** (thin book, little price movement). Use it for mechanics only.
- **While a cancel is unconfirmed**, two orders on the same side can briefly coexist. The risk manager already counts
  them against the position cap, but the strategy's inventory limit can be exceeded by one lot for a moment.
- **A daily loss halt lifts only at 00:00 UTC.** `/resume` cannot lift it (deliberately conservative).
- One symbol per process, One-way Mode only.
- Fees paid in BNB are estimated in USDT from the maker rate in the config.
- The current hour's Parquet file can't be read until it is closed (the backtest skips it automatically).

---

## 13. Roadmap

Open work is tracked in [issues](https://github.com/thanatchon36/goldshop-mm/issues) and the
[Go-live](https://github.com/thanatchon36/goldshop-mm/milestones) milestone:

- Prove net profit after fees over 1–2 weeks of paper (checklist A) and test the mechanics on testnet (checklist B)
- Prepare the live account, keys and first-week settings (checklists C and D)
- Close the [known limitations](#12-known-limitations): deeper-than-L1 paper flattens, BNB fees, several symbols
  per process

## 14. License and disclaimer

[MIT](LICENSE). Provided as is, with no warranty. This project is not financial advice, and the authors are not
responsible for any loss from using it. Report anything that could leak keys or place unintended orders privately,
as described in [SECURITY.md](SECURITY.md).
