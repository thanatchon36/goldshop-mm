# goldshop-mm

Market-making bot สำหรับ **Binance USDⓈ-M Futures** — แนวคิดแบบ "ร้านทอง": ตั้ง limit order ทั้งฝั่ง bid และ ask
เพื่อกินสเปรด โดยคุม inventory ไม่ให้สะสมฝั่งใดฝั่งหนึ่งมากเกินไป รันใน container แบบ 24/7
และส่งรายงานผลประกอบการ + แจ้งเตือนไปที่ Telegram

> ⚠️ **คำเตือน** market making บน futures มีความเสี่ยงสูง กำไรต่อรอบเล็กมาก (ระดับ bps) และแพ้ adverse selection
> ได้ง่าย ค่าธรรมเนียม maker ระดับ VIP0 (0.02% ต่อฝั่ง) มักกินกำไรหมดสำหรับคู่ที่สภาพคล่องสูง
> ซอฟต์แวร์นี้ไม่มีการรับประกันใดๆ — เริ่มที่ paper เสมอ และใช้เงินที่เสียได้เท่านั้น

---

## สารบัญ

1. [ลำดับที่แนะนำ: paper → testnet → live](#1-ลำดับที่แนะนำ-paper--testnet--live)
2. [ติดตั้ง](#2-ติดตั้ง)
3. [ตั้งค่า (.env และ config.yaml)](#3-ตั้งค่า)
4. [ตั้ง Telegram bot](#4-ตั้ง-telegram-bot)
5. [ตั้ง Binance API key (testnet / live)](#5-ตั้ง-binance-api-key)
6. [วิธีรันแต่ละโหมด](#6-วิธีรันแต่ละโหมด)
7. [เครื่องมือ: screener, recorder, backtest](#7-เครื่องมือ-screener-recorder-backtest)
8. [Monitoring: health, Prometheus, Grafana](#8-monitoring)
9. [ระบบป้องกันความเสี่ยง](#9-ระบบป้องกันความเสี่ยง)
10. [✅ Checklist ก่อนเปิด live](#10--checklist-ก่อนเปิด-live)
11. [โครงสร้างโปรเจกต์](#11-โครงสร้างโปรเจกต์)
12. [ข้อจำกัดที่รู้อยู่](#12-ข้อจำกัดที่รู้อยู่)

---

## 1. ลำดับที่แนะนำ: paper → testnet → live

| ขั้น | โหมด | ต้องมีคีย์ไหม | ใช้ทำอะไร |
|---|---|---|---|
| 1 | `paper` (default) | ❌ ไม่ต้อง | ใช้ราคาตลาดจริง จำลอง fill แบบเข้มงวด → **วัดว่ากลยุทธ์ทำกำไรหลังหัก fee ได้จริงไหม** |
| 2 | `testnet` | คีย์ testnet | ส่ง order จริงไป Binance Futures Testnet → **ทดสอบกลไก** (ส่ง/ยกเลิก order, reconcile, kill switch, restart, reconnect) |
| 3 | `live` | คีย์จริง + `LIVE_CONFIRM` | **เงินน้อยๆ ก่อน** แล้วค่อยขยายเมื่อผลสอดคล้องกับ paper |

ข้อควรรู้:
- **paper** คือที่วัดผลกำไร: fill นับเฉพาะเมื่อมี trade ราคา *ทะลุ* ราคาเรา (ถือว่าเราต่อคิวท้ายสุด) + latency + GTX reject + fee เต็ม
- **testnet** ใช้วัดกำไรไม่ได้ — order book ของ testnet บาง ราคาขยับน้อย fill แปลก ใช้ทดสอบว่าระบบ *ทำงานถูก* เท่านั้น
- ระบบ **ไม่สลับคีย์ข้ามโหมด**: `MODE=testnet` ใช้เฉพาะ `BINANCE_TESTNET_*` และ URL testnet — ถ้าไม่มีคีย์ testnet จะหยุดทันที ไม่หยิบคีย์จริงมาใช้

---

## 2. ติดตั้ง

### แบบ Docker (แนะนำสำหรับรันจริง 24/7)

ต้องมี Docker + Docker Compose v2

```bash
git clone <repo> goldshop-mm && cd goldshop-mm
cp .env.example .env          # แก้ค่าตามหัวข้อ 3
mkdir -p data
docker compose up -d --build  # MODE=paper (default) ไม่ต้องมีคีย์
docker compose ps             # ต้องเห็น (healthy) ภายใน ~30 วินาที
docker compose logs -f
```

> **Linux server:** container รันเป็น user uid `10001` (ไม่ใช่ root) ต้องให้สิทธิ์เขียนโฟลเดอร์ข้อมูลก่อน:
> `sudo chown -R 10001:10001 data` (บน Docker Desktop ของ macOS/Windows ไม่ต้อง)

คุณสมบัติด้านความปลอดภัยของ container: multi-stage build, non-root, filesystem read-only (เขียนได้เฉพาะ `./data` และ `/tmp`),
`cap_drop: ALL`, `no-new-privileges`, `restart: unless-stopped`, healthcheck ที่ `/health`, log rotate 20MB × 5

### แบบ Python (สำหรับพัฒนา / ทดลอง)

ต้องมี Python 3.11+

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                     # unit tests ทั้งหมด
python -m goldshop            # paper mode
```

---

## 3. ตั้งค่า

### `.env` — ค่าลับเท่านั้น (ห้าม commit — `.gitignore` กันไว้แล้ว)

| ตัวแปร | ใช้ในโหมด | หมายเหตุ |
|---|---|---|
| `MODE` | ทุกโหมด | `paper` (default) / `testnet` / `live` |
| `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET` | testnet | คีย์จาก testnet (หัวข้อ 5.1) |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | live | คีย์บัญชีจริง (หัวข้อ 5.2) |
| `LIVE_CONFIRM` | live | ต้องเท่ากับ `I_UNDERSTAND_THE_RISK` ไม่งั้นปฏิเสธและหยุดทันที |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional | ไม่ตั้ง = ไม่ส่ง Telegram (ไม่ error) |
| `TELEGRAM_COMMANDS` | optional | `true` เพื่อรับคำสั่งจาก Telegram (default `false`) |
| `GRAFANA_ADMIN_PASSWORD` | optional | รหัสผ่าน Grafana (profile monitoring) |

ค่าลับรับได้ 3 ทาง: environment variable, ไฟล์ `.env`, หรือ Docker secrets (`/run/secrets/<ชื่อตัวแปรตัวเล็ก>`
เช่น `/run/secrets/binance_api_secret`) ค่าลับไม่ถูกเขียนลง log (เก็บเป็น `SecretStr` + มีตัวกรองลบออกจาก log อีกชั้น)

ถ้าอยู่ในโหมด paper แต่มีคีย์ตั้งไว้ ระบบจะเตือนใน log ว่า *"API keys are set but will NOT be used"* — เป็นเรื่องปกติ

### `config.yaml` — ค่าอื่นทั้งหมด (default ขนาดเล็กและ conservative)

ทุกค่ามีคำอธิบายในไฟล์ ค่าสำคัญ:

| หมวด | ค่า | default | ความหมาย |
|---|---|---|---|
| — | `symbol` | `BTCUSDT` | เลือกด้วย screener (หัวข้อ 7) — ไม่เปลี่ยนเองขณะรัน |
| `exchange` | `maker_fee_bps` | `2.0` | **ตั้งให้ตรงกับ fee tier จริงของบัญชีคุณ** |
| `strategy` | `order_notional_usdt` | `100` | ขนาด order ต่อฝั่ง (ต้องผ่าน minQty/minNotional ของ symbol ไม่งั้นโปรแกรมไม่ยอมเริ่มและบอกค่าขั้นต่ำ) |
| `strategy` | `max_inventory_notional_usdt` | `500` | inventory เต็ม → quote เฉพาะฝั่งที่ลด inventory |
| `strategy` | `gamma`, `kappa`, `horizon_s` | `0.05`, `0.5`, `60` | พารามิเตอร์ Avellaneda–Stoikov (หน่วย bps) |
| `strategy` | `min_spread_buffer_bps` | `1.0` | สเปรดที่ quote ≥ 2×maker_fee + buffer เสมอ |
| `risk` | `max_position_notional_usdt` | `600` | เพดานแข็งของ position |
| `risk` | `max_order_notional_usdt` | `200` | เพดานแข็งต่อ order |
| `risk` | `daily_loss_limit_usdt` | `20` | ขาดทุนสุทธิวันนี้ถึงเกณฑ์ → cancel + flatten + หยุดถึง 00:00 UTC |
| `risk` | `circuit_breaker_pct` / `_window_s` | `0.5` / `10` | ราคาขยับ ≥ 0.5% ใน 10 วินาที → หยุด quote 60 วินาที |
| `risk` | `leverage`, `margin_type` | `2`, `ISOLATED` | ตั้งให้อัตโนมัติตอนเริ่ม (testnet/live) |
| `report` | `interval_s` | `3600` | รายงาน Telegram ทุกชั่วโมง + สรุปรายวัน 00:00 UTC |

หลังแก้ `config.yaml` (Docker mount ไว้แบบ read-only): `docker compose restart`

---

## 4. ตั้ง Telegram bot

1. เปิด Telegram คุยกับ **[@BotFather](https://t.me/BotFather)** → ส่ง `/newbot` → ตั้งชื่อ → จะได้ **token** รูปแบบ `123456789:AA...`
2. สร้างกลุ่ม (หรือใช้แชทส่วนตัวกับบอท) แล้ว **เพิ่มบอทเข้ากลุ่ม** และพิมพ์ข้อความอะไรก็ได้ในกลุ่ม 1 ข้อความ
   (เช่น `/start`)
3. หา **chat_id**: เปิดในเบราว์เซอร์
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   ดูที่ `"chat":{"id": ... }` — กลุ่มจะเป็นเลขติดลบ (เช่น `-4012345678`, supergroup ขึ้นต้นด้วย `-100`)
   > ถ้าบอทรันอยู่พร้อม `TELEGRAM_COMMANDS=true` ให้หยุดบอทก่อนเปิด getUpdates (ไม่งั้นแย่งข้อความกัน)
4. ใส่ใน `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AA...
   TELEGRAM_CHAT_ID=-4012345678
   TELEGRAM_COMMANDS=false     # true ถ้าต้องการสั่งงานผ่าน Telegram
   ```
5. รันบอท → ต้องได้ข้อความ `🟢 goldshop-mm started` ในกลุ่ม พร้อมโหมด, symbol, ขนาด order และ risk limit ทั้งหมด

**🔒 ความปลอดภัย:**
- **ห้าม commit token** ห้ามวางในแชท/issue/screenshot — ถ้าหลุด ให้ส่ง `/revoke` ที่ @BotFather แล้วอัปเดต `.env`
- บอทรับคำสั่ง **เฉพาะจาก `TELEGRAM_CHAT_ID`** เท่านั้น ข้อความจากที่อื่นถูกเพิกเฉยและบันทึกลง log
- ทุกคนที่อยู่ในกลุ่มนั้นสั่งบอทได้ — ใช้กลุ่มที่มีแต่คุณ (หรือคนที่ไว้ใจ)

### ข้อความที่จะได้รับ

ทุกข้อความขึ้นต้นด้วย `[PAPER]` / `[TESTNET]` / `[LIVE]` + ชื่อ symbol

- **รายงานตามรอบ** (ทุกชั่วโมง + สรุปรายวัน 00:00 UTC): Net PnL วันนี้/สะสม, realized/unrealized, fee, funding,
  จำนวน fill, volume, edge เฉลี่ยเทียบ mid (bps), inventory, markout +1s/+5s/+30s, max drawdown วันนี้, uptime, reconnect
- **แจ้งเตือนทันที** (priority สูง ส่งก่อนรายงาน): start/shutdown, daily loss limit, circuit breaker หยุด/กลับมา,
  funding guard, inventory แตะ limit, ข้อมูลค้าง/user stream หลุด/กลับมา, reconnect เกิน 5 ครั้งใน 10 นาที,
  position ไม่ตรงกับ exchange, margin ไม่พอ, error ที่ไม่คาดคิด

### คำสั่ง (เมื่อ `TELEGRAM_COMMANDS=true`)

ใช้ long polling (ไม่ต้องเปิด port) — คำสั่งที่ค้างอยู่ก่อนบอทเริ่มจะถูกข้าม

| คำสั่ง | ผล |
|---|---|
| `/status` | สถานะ quote, เหตุผลที่หยุด (ถ้ามี), position, order ที่เปิดอยู่, σ, risk flags, uptime |
| `/pnl` | รายงาน PnL ปัจจุบัน |
| `/pause` | หยุด quote + cancel order ทั้งหมด (**ไม่ปิด position**) |
| `/resume` | กลับมา quote — **ไม่มีผลถ้า daily loss limit ทำงานอยู่** (รอ 00:00 UTC) |
| `/flatten` | cancel ทั้งหมด + ปิด position ด้วย market reduce-only — **ต้องยืนยันด้วย `/flatten CONFIRM` ภายใน 30 วินาที** แล้วจะ pause ต่อ (ใช้ `/resume` เพื่อเริ่มใหม่) |

Risk manager มีอำนาจเหนือคำสั่ง Telegram เสมอ (เช่น `/resume` ไม่สามารถปลด daily loss halt หรือ circuit breaker ได้)

---

## 5. ตั้ง Binance API key

### 5.1 Testnet

1. ไปที่ **https://testnet.binancefuture.com** (อาจ redirect ไปหน้า Binance Demo Trading) → login → เมนู **API Key**
2. สร้างคีย์ (HMAC) → คัดลอก API Key และ Secret Key
3. ใส่ใน `.env`:
   ```
   MODE=testnet
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_API_SECRET=...
   ```
4. บัญชี testnet ต้องเป็น **One-way Mode** (ไม่ใช่ Hedge Mode) — ถ้าเป็น Hedge Mode บอทจะไม่ยอมเริ่มและบอกสาเหตุ

URL ของ testnet อยู่ใน `config.yaml` (`testnet_rest_base_url: https://demo-fapi.binance.com` และ
`wss://fstream.binancefuture.com/...`) — โหมด testnet ไม่แตะ URL ของ live เลย

### 5.2 Live (บัญชีจริง)

ที่ Binance → **Account → API Management → Create API** → *System generated* (HMAC)

**ตั้งสิทธิ์ให้น้อยที่สุด:**

- [x] **Enable Reading**
- [x] **Enable Futures** — สิทธิ์เดียวที่ต้องใช้สำหรับเทรด
- [ ] **Enable Withdrawals — ห้ามเปิดเด็ดขาด**
- [ ] Enable Spot & Margin Trading — ไม่ต้องใช้ ปิดไว้
- [ ] Universal Transfer / อื่นๆ — ปิดทั้งหมด

**จำกัด IP:** เลือก *Restrict access to trusted IPs only* แล้วใส่ **IP สาธารณะของ server ที่รันบอท** เท่านั้น
(ดูได้จาก `curl -s https://api.ipify.org` บน server นั้น) — คีย์ที่ไม่จำกัด IP ถ้าหลุดก็ใช้ได้จากทุกที่

**สร้างคีย์แยกเฉพาะบอทนี้** — ไม่ใช้ร่วมกับบอท/แอปอื่น จะได้ revoke ได้ทันทีโดยไม่กระทบอย่างอื่น
และตรวจสอบย้อนหลังได้ง่าย

เตรียมบัญชี Futures:
- โอน USDT เข้า USDⓈ-M Futures wallet เท่าที่ยอมเสียได้ (บอทใช้ ISOLATED margin, leverage 2x ตาม config)
- ตั้ง **One-way Mode**
- ถ้าใช้ BNB จ่าย fee บอทจะประเมิน fee เป็น USDT จากอัตรา maker ใน config (เตือนใน log หนึ่งครั้ง)

ใส่ใน `.env` (พิมพ์บน server โดยตรง — **อย่าวางคีย์ในแชท อีเมล หรือ ticket**):
```
MODE=live
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
```
`chmod 600 .env` เพื่อให้อ่านได้เฉพาะเจ้าของ หรือใช้ Docker secrets แทน `.env`

---

## 6. วิธีรันแต่ละโหมด

โหมดเลือกจาก `MODE` ใน `.env` อย่างเดียว คำสั่งรันเหมือนกันทุกโหมด:

```bash
docker compose up -d --force-recreate     # หลังเปลี่ยน .env ต้อง recreate
docker compose logs -f
```

หรือแบบ Python: `python -m goldshop [--duration 600] [--no-record]`

### paper (default)

```bash
MODE=paper
```
- ใช้เฉพาะ public market data ของตลาดจริง ไม่ต้องมีคีย์
- fill engine แบบเข้มงวด: fill เมื่อ trade ทะลุราคาเราเท่านั้น, latency 100 ms (ตั้งได้ใน `paper.latency_ms`),
  GTX reject จำลองตอน order ไปถึง, หัก maker fee เต็ม, funding ตามรอบจริงจาก markPrice stream
- state เก็บที่ `data/state-paper.sqlite` (PnL ต่อเนื่องข้าม restart)

### testnet

```bash
MODE=testnet
BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_API_SECRET=...
```
ตอนเริ่ม บอทจะ: sync เวลา server → ตรวจ One-way Mode → ตั้ง ISOLATED + leverage → **cancel order ที่ค้างอยู่**
→ อ่าน position จาก exchange (ถ้าไม่ตรงกับที่บันทึกไว้จะใช้ค่าจาก exchange และแจ้งเตือน) → เปิด User Data Stream
→ เริ่ม dead-man switch → quote เมื่อทั้ง market data และ user stream พร้อม

### live

```bash
MODE=live
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
```
ขั้นตอนเหมือน testnet ทุกอย่าง ต่างแค่ URL และคีย์ — **ผ่าน checklist หัวข้อ 10 ก่อน** และเริ่มด้วยขนาดเล็ก
(`order_notional_usdt` ต่ำสุดที่ symbol ยอม, `daily_loss_limit_usdt` ต่ำ)

### หยุดบอท

```bash
docker compose stop        # SIGTERM → cancel order ทั้งหมด → ปิด dead-man → บันทึก state → แจ้ง Telegram
```
การหยุดแบบปกติ **cancel order แต่ไม่ปิด position** — ถ้าต้องการปิด position ใช้ `/flatten` ก่อน หรือปิดเองที่ Binance

---

## 7. เครื่องมือ: screener, recorder, backtest

### Screener — เลือก symbol

```bash
python -m goldshop.screener                         # top 20
python -m goldshop.screener --top 30 --min-volume 20000000 --maker-fee-bps 1.8
python -m goldshop.screener --csv > screen.csv
```
สแกน USDT perpetual ทั้งหมด (กรองด้วย volume ก่อนดึง kline): `spread_bps`, volume 24h, σ ต่อนาที,
`net_edge_bps = spread − 2×maker_fee`, `goldshop_score = net_edge / σ` (ยิ่งสูงยิ่งดี)
> spread มาจาก snapshot ณ วินาทีเดียว — รันซ้ำหลายรอบหลายช่วงเวลาก่อนตัดสินใจ

### Recorder — บันทึกข้อมูลสำหรับ backtest

บอทบันทึกให้อัตโนมัติ (`recorder.enabled: true`) เป็น Parquet แยกรายชั่วโมง:
`data/recordings/<SYMBOL>/<book_ticker|depth|trades|mark_price>/<YYYY-MM-DD>/<HH>.parquet`

บันทึกอย่างเดียวโดยไม่เทรด: `python -m goldshop.market_data [--symbol ETHUSDT]`

### Backtest — replay ข้อมูลผ่าน paper fill engine ตัวเดียวกับที่รันจริง

```bash
python -m goldshop.backtest                                   # ข้อมูลทั้งหมดของ config.symbol
python -m goldshop.backtest --start 2026-10-11T00:00 --end 2026-10-12T00:00 --config alt.yaml
```
ผลลัพธ์ใน `reports/backtest-<SYMBOL>-<เวลา>/`: `report.html` (PnL curve, inventory, markout distribution),
`summary.json`, `timeseries.csv`, `fills.csv`

- ใช้ข้อมูล tick จาก recorder เท่านั้น (ไม่ใช้ OHLCV — บอกไม่ได้ว่า quote จะถูก fill หรือไม่)
- ไฟล์ของชั่วโมงปัจจุบันที่ยังเขียนอยู่จะถูกข้าม (มีคำเตือน)
- ช่วงที่ข้อมูลขาดเกิน `stale_after_s` ถูกจำลองเป็น "ข้อมูลค้าง" เหมือนตอนรันจริง

---

## 8. Monitoring

| endpoint | ใช้ทำอะไร |
|---|---|
| `GET /health` (port 8080 ใน container) | 200 = ปกติ, 503 = ข้อมูลค้าง / user stream หลุด / engine loop ไม่เดิน — ใช้เป็น Docker healthcheck |
| `GET /metrics` | Prometheus: PnL, inventory, spread, σ, fill rate, markout, latency, reconnects, สถานะ quote/risk |

เปิด Prometheus + Grafana (optional):

```bash
docker compose --profile monitoring up -d --build
```
- Grafana: http://localhost:3000 (user `admin`, รหัสจาก `GRAFANA_ADMIN_PASSWORD`) → dashboard **goldshop-mm**
- Prometheus: http://localhost:9090 (เก็บข้อมูล 30 วัน)
- ทั้งสองเปิดที่ `127.0.0.1` เท่านั้น — บน server ให้ดูผ่าน SSH tunnel: `ssh -L 3000:localhost:3000 user@server`

Log เป็น JSON บรรทัดละ event ทาง stdout (`docker compose logs`) ทุก alert, fill, คำสั่ง Telegram ถูกบันทึกลง log
ข้อมูลย้อนหลังอยู่ใน SQLite `data/state-<mode>.sqlite` (ตาราง `fills`, `quotes`, `pnl`, `funding`, `markouts`)

---

## 9. ระบบป้องกันความเสี่ยง

Risk manager มีอำนาจเหนือ strategy และคำสั่ง Telegram เสมอ

| กลไก | ทำงานเมื่อ | ผล |
|---|---|---|
| Post-only (GTX) | ทุก order | เป็น maker เสมอ ถ้าจะชนฝั่งตรงข้าม exchange ปฏิเสธ (`-5022`) |
| Position / order limit | order ใหม่จะทำให้เกินเพดาน | ไม่ส่ง order ฝั่งที่เพิ่ม position |
| Inventory limit (strategy) | inventory ถึง `max_inventory_notional_usdt` | quote เฉพาะฝั่งที่ลด inventory + แจ้งเตือน |
| **Daily loss limit** | Net PnL วันนี้ ≤ −limit | cancel ทั้งหมด → ปิด position ด้วย reduce-only → หยุดถึง 00:00 UTC (`/resume` ไม่มีผล) |
| Circuit breaker | ราคาขยับ ≥ X% ใน Y วินาที | หยุด quote ช่วง cooldown |
| Funding guard | funding rate สวนทาง position ≥ เกณฑ์ | quote เฉพาะฝั่งที่ลด inventory |
| Stale data | ไม่มี update เกิน `stale_after_s` หรือ user stream หลุด | cancel ทั้งหมด หยุด quote จนกว่าจะกลับมา |
| **Dead-man switch** (testnet/live) | บอทค้าง/เน็ตหลุด/process ตาย | `countdownCancelAll` — exchange ยกเลิก order ทั้งหมดเองภายใน 60 วินาที |
| Reconcile | ตอนเริ่ม + ทุก 30 วินาที + หลัง user stream reconnect | ซิงก์ order/position กับ exchange, กู้ fill ที่หลุด, แจ้งเตือนถ้าไม่ตรง |
| Rate limit guard | weight / order count ใกล้ 80% ของ limit | รอ / ไม่ส่ง order |
| Graceful shutdown | SIGTERM | cancel ทั้งหมด → ปิด dead-man → บันทึก state |

> Dead-man switch **ยกเลิก order แต่ไม่ปิด position** — ถ้า order ถูก fill ในช่วงที่บอทค้าง position นั้นจะยังอยู่
> จนบอทกลับมา reconcile (หรือคุณปิดเอง)

---

## 10. ✅ Checklist ก่อนเปิด live

ห้ามข้ามข้อใด — ถ้าข้อไหนไม่ผ่าน ให้กลับไปแก้ที่ paper/testnet

### A. ผลจาก paper (วัดกำไร)
- [ ] รัน paper ต่อเนื่อง **อย่างน้อย 1–2 สัปดาห์** บน symbol และ config เดียวกับที่จะใช้ live
- [ ] ช่วงนั้น **ครอบคลุมหลายสภาพตลาด** (sideway, เทรนด์แรง, ช่วงข่าว/ผันผวนสูง, วันหยุดสุดสัปดาห์)
- [ ] **Net PnL หลังหัก fee และ funding เป็นบวก** (ดูรายงานรายวันใน Telegram / Grafana / `pnl` ใน SQLite)
- [ ] **markout เฉลี่ยไม่ติดลบเกินสเปรดที่ได้** — เทียบ "Avg captured edge vs mid" กับ markout +5s/+30s
      ถ้า edge − |markout ติดลบ| ≤ fee แปลว่าโดน adverse selection กินหมด
- [ ] ไม่มีวันไหนแตะ daily loss limit โดยไม่เข้าใจสาเหตุ
- [ ] `maker_fee_bps` ใน config ตรงกับ fee tier จริงของบัญชี
- [ ] (แนะนำ) backtest ข้อมูลที่บันทึกไว้ให้ผลสอดคล้องกับ paper

### B. Testnet (ทดสอบกลไก)
- [ ] **Kill switch**: ระหว่างมี order เปิด สั่ง `docker kill goldshop-mm` (SIGKILL) → ภายใน ~60 วินาทีที่ Binance ต้องไม่มี open order
- [ ] **Restart**: `docker compose restart` → log/Telegram แสดง reconcile, ไม่มี order ซ้อน, position ตรงกับ exchange
- [ ] **Reconnect**: ตัดเน็ตของ container ชั่วคราว
      ```bash
      docker network disconnect goldshop-mm_default goldshop-mm      # ตัด ~10 วินาที
      docker network connect --alias goldshop goldshop-mm_default goldshop-mm
      ```
      → ได้แจ้งเตือน "🔴 Quoting stopped, orders cancelled" แล้ว "🟢 Data streams healthy again" (ภายใน ~10 วินาที)
      และไม่มี order ค้างระหว่างหลุด (ชื่อ network ดูได้จาก `docker network ls`)
- [ ] **Graceful stop**: `docker compose stop` → order ถูก cancel ทั้งหมด
- [ ] **คำสั่ง Telegram**: `/status`, `/pause` (order หาย), `/resume`, `/flatten` + `/flatten CONFIRM` (position เป็น 0)
- [ ] **Daily loss limit**: ตั้ง `daily_loss_limit_usdt` ต่ำมากชั่วคราว → เห็น flatten + halt + `/resume` ถูกปฏิเสธ
- [ ] Healthcheck เป็น `(healthy)` และ Grafana แสดงข้อมูลครบ

### C. บัญชีและคีย์ live
- [ ] คีย์ใหม่เฉพาะบอทนี้ เปิดแค่ Reading + Futures, **Withdraw ปิด**, **จำกัด IP = IP ของ server**
- [ ] `.env` อยู่บน server เท่านั้น สิทธิ์ `600` ไม่เคยถูก commit หรือส่งผ่านแชท
- [ ] บัญชี Futures เป็น One-way Mode และมีเงินใน Futures wallet เท่าที่ยอมเสียได้
- [ ] Telegram token ไม่เคยหลุด (ถ้าเคยวางในที่อื่น → `/revoke` และใช้ token ใหม่)

### D. ค่าเริ่มต้นสำหรับ live สัปดาห์แรก
- [ ] `order_notional_usdt` ต่ำสุดที่ symbol ยอม
- [ ] `daily_loss_limit_usdt` ต่ำ (เช่น 1–2% ของเงินในบัญชี futures)
- [ ] `max_position_notional_usdt` ไม่เกิน 3–5 เท่าของขนาด order
- [ ] เฝ้าดูใกล้ชิด 24–48 ชั่วโมงแรก และเทียบ fill/markout กับ paper — ถ้าต่างกันมาก หยุดและหาสาเหตุ

---

## 11. โครงสร้างโปรเจกต์

```
goldshop/
  __main__.py           ตัวรันบอท (paper/testnet/live): เตรียมบัญชี, reconcile, tasks, คำสั่ง, shutdown
  config.py             config.yaml (pydantic) + ค่าลับจาก env/.env/Docker secrets + กฎเลือกคีย์ตามโหมด
  engine.py             market data → σ → quote → risk → executor (ใช้ร่วมกันทุกโหมดและ backtest)
  accounting.py         position / PnL แบบ average cost
  stats.py              สถิติรายวัน + ข้อความรายงาน
  screener/             python -m goldshop.screener
  market_data/          WebSocket feed (reconnect, stale), models, Parquet recorder
  strategy/             Avellaneda–Stoikov (pure logic) + EWMA σ
  execution/            interface กลาง, order state machine, BinanceExecutor, paper/ (fill engine)
  binance/              REST (signed, rate limit, time sync), User Data Stream, symbol filters
  risk/                 risk manager
  state/                SQLite store, markout tracker
  notify/               Telegram notifier (คิว + rate limit) และคำสั่ง (long polling)
  ops/                  /health, /metrics
  backtest/             replay + report
monitoring/             Prometheus config, Grafana provisioning + dashboard
tests/                  pytest (strategy, risk, fill engine, order state machine, notifier, executor, …)
```

รันเทส: `pytest -q`

---

## 12. ข้อจำกัดที่รู้อยู่

- **paper fill engine มองแค่ L1** — flatten ใน paper fill ที่ best bid/ask + taker fee ไม่คิด slippage ลึกกว่านั้น
- **testnet ไม่สะท้อนกำไรจริง** (book บาง, ราคาขยับน้อย) — ใช้ทดสอบกลไกเท่านั้น
- **ช่วงที่ cancel ยังไม่ยืนยัน** อาจมี order ฝั่งเดียวกันซ้อน 2 ตัวชั่วคราว — risk manager นับ order เหล่านี้ไว้แล้ว
  ในการตรวจเพดาน position แต่ inventory limit ของ strategy อาจเกินได้ 1 lot ชั่วขณะ
- **daily loss halt ปลดได้แค่ตอน 00:00 UTC** — `/resume` ปลดไม่ได้ (ตั้งใจให้ conservative)
- รองรับ symbol เดียวต่อ process และเฉพาะ One-way Mode
- fee ที่จ่ายเป็น BNB ถูกประเมินเป็น USDT จากอัตรา maker ใน config
- ไฟล์ Parquet ของชั่วโมงปัจจุบันอ่านไม่ได้จนกว่าจะปิดไฟล์ (backtest ข้ามให้อัตโนมัติ)
