สร้างระบบ market-making bot สำหรับ Binance USDⓈ-M Futures ชื่อโปรเจกต์ "goldshop-mm"
แนวคิด: เหมือนร้านทอง ตั้ง limit order ทั้งฝั่ง bid และ ask เพื่อกินสเปรด
โดยคุม inventory ไม่ให้สะสมฝั่งใดฝั่งหนึ่งมากเกินไป รันใน container แบบ 24/7
และส่งรายงานผลประกอบการ + แจ้งเตือนไปที่ Telegram

## ข้อกำหนดหลัก
- Python 3.11+, asyncio ทั้งระบบ, type hints ครบ, config validate ด้วย pydantic
- เชื่อมต่อ Binance ผ่าน REST + WebSocket ทางการ (aiohttp + websockets) ไม่ใช้ polling สำหรับ market data
- ค่าลับรับผ่าน environment variable / .env / Docker secrets เท่านั้น ห้าม hardcode ห้าม log ค่า secret
- ค่าอื่นทั้งหมดอยู่ใน config.yaml พร้อม default ที่ปลอดภัย (ขนาดเล็ก, conservative)
- ห้ามทิ้ง TODO หรือ stub แบบเงียบๆ ถ้าส่วนไหนยังไม่ทำให้บอกชัดเจนในสรุปแต่ละเฟส

## Environment variables
| ตัวแปร | ใช้ในโหมด | หมายเหตุ |
|---|---|---|
| MODE | ทุกโหมด | paper (default) / testnet / live |
| BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_API_SECRET | testnet | คีย์จาก testnet.binancefuture.com |
| BINANCE_API_KEY, BINANCE_API_SECRET | live | คีย์บัญชีจริง |
| LIVE_CONFIRM | live | ต้องเท่ากับ I_UNDERSTAND_THE_RISK |
| TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID | optional | ไม่มีให้ข้ามโดยไม่ error |
| TELEGRAM_COMMANDS | optional | true เพื่อเปิดรับคำสั่ง (default false) |

## โหมดการทำงาน (สำคัญมาก)
- MODE=paper เป็น default
  - รันได้โดยไม่ต้องมี API key ใดๆ ใช้เฉพาะ public market data endpoint ของตลาดจริง
  - ถ้ามีการตั้งคีย์ไว้ในโหมดนี้ ให้ไม่ใช้และเตือนใน log
  - จำลอง fill แบบเข้มงวด (conservative) ตามกฎในหัวข้อ "Paper fill engine"
- MODE=testnet: ส่ง order จริงไปที่ Binance Futures Testnet ใช้เฉพาะ BINANCE_TESTNET_* เท่านั้น
- MODE=live: ใช้ BINANCE_API_* และต้องตั้ง LIVE_CONFIRM=I_UNDERSTAND_THE_RISK ไม่งั้นปฏิเสธและหยุดทันที
- ห้ามสลับคีย์ข้ามโหมด: ถ้า testnet ไม่มีคีย์ testnet ให้หยุด ห้าม fallback ไปใช้คีย์จริง
- ตอนเริ่มโปรแกรม แสดงโหมด, symbol, ขนาด order และ risk limit ทั้งหมดให้ชัดเจนใน log และ Telegram

## โมดูลที่ต้องมี
1. screener/
   - สแกนทุก USDT perpetual: spread_bps, 24h quoteVolume, σ ต่อนาทีจาก kline 1m
   - คำนวณ net_edge_bps = spread_bps − 2×maker_fee_bps และ goldshop_score = net_edge_bps / sigma_1m_bps
   - กรองด้วย volume ขั้นต่ำก่อนดึง kline เพื่อลด request
   - เป็นคำสั่ง CLI แยก (`python -m goldshop.screener`) ใช้ช่วยเลือก symbol ไม่ได้เปลี่ยน symbol อัตโนมัติขณะรัน

2. market_data/
   - WebSocket: bookTicker, depth (partial book), aggTrade, markPrice (funding)
   - reconnect พร้อม exponential backoff
   - ตรวจจับข้อมูลค้าง (stale) ถ้าไม่มี update เกิน N วินาที ให้ยกเลิก order ทั้งหมดและหยุด quote
   - recorder: บันทึก L2 + trades ลง Parquet แบ่งไฟล์รายชั่วโมง เพื่อใช้ทำ backtest

3. strategy/ (Avellaneda–Stoikov)
   - reservation price เลื่อนตาม inventory (inventory skew)
   - optimal spread จาก σ ที่ประเมินแบบ rolling + พารามิเตอร์ γ, κ ใน config
   - spread ขั้นต่ำต้องมากกว่า 2×maker_fee + buffer เสมอ
   - ถ้า inventory ถึง limit ให้ quote เฉพาะฝั่งที่ลด inventory เท่านั้น
   - strategy เป็น pure logic (รับ state คืน quote) เพื่อใช้ร่วมกันได้ทั้ง paper, backtest, testnet, live

4. execution/
   - interface กลางเดียว มี implementation: PaperExecutor, BinanceExecutor (testnet/live ต่างกันแค่ base URL + คีย์)
   - ใช้ order แบบ post-only (timeInForce=GTX) เท่านั้น เพื่อการันตีว่าเป็น maker
   - clientOrderId แบบ deterministic/idempotent
   - อัปเดต quote เฉพาะเมื่อราคาเป้าหมายเปลี่ยนเกิน threshold (ลด cancel/replace ไม่จำเป็น)
   - เคารพ rate limit (อ่าน header X-MBX-USED-WEIGHT / X-MBX-ORDER-COUNT) และ backoff เมื่อใกล้ชน
   - จัดการ GTX reject, insufficient margin, precision/tick size/lot size จาก exchangeInfo
   - User Data Stream สำหรับ fill/position พร้อม keepalive listenKey
   - sync เวลา server เป็นระยะ จัดการ recvWindow

5. Paper fill engine (ใน execution/paper)
   - นับว่า order ถูก fill เมื่อมี trade ที่ราคา *ทะลุ* ราคาเรา เท่านั้น (ไม่ใช่แค่แตะ) เพื่อจำลองว่าเราต่อคิวอยู่ท้าย
   - จำลอง latency ระหว่างตัดสินใจกับ order ถึง exchange (default 100 ms, ตั้งได้)
   - หักค่าธรรมเนียม maker เต็มตาม config
   - จำลอง GTX reject ถ้าราคา quote จะชนฝั่งตรงข้าม ณ เวลาที่ order ไปถึง
   - คิด funding ตามรอบจริงจาก markPrice stream

6. risk/ (มีอำนาจเหนือ strategy และคำสั่งจาก Telegram เสมอ)
   - max position notional, max order notional
   - daily loss limit: ถ้าขาดทุนถึงเกณฑ์ → cancel all + ปิด position ด้วย reduce-only → หยุดจนถึงวันใหม่ (UTC) หรือจนกว่าคนจะสั่ง resume
   - volatility circuit breaker: ถ้าราคาเคลื่อนเกิน X% ใน Y วินาที ให้หยุด quote ชั่วคราว
   - funding guard: ถ้า funding rate สวนทาง position เกินเกณฑ์ ให้ลด inventory
   - (testnet/live) ใช้ endpoint countdownCancelAll ของ Binance เป็น dead-man switch: ถ้าบอทค้าง/หลุด order ทั้งหมดถูกยกเลิกเอง
   - (testnet/live) ตั้ง leverage และ margin type (ISOLATED) ตาม config ตอนเริ่ม

7. state/
   - ตอน start/restart: reconcile กับ exchange (open orders, position) ก่อนเริ่ม quote เสมอ
   - บันทึก fills, quotes, PnL, funding ลง SQLite
   - คำนวณ markout (mid price หลัง fill ที่ +1s, +5s, +30s) เพื่อวัด adverse selection

8. notify/ (Telegram)
   - เปิดใช้เมื่อมี TELEGRAM_BOT_TOKEN และ TELEGRAM_CHAT_ID เท่านั้น
   - ส่งแบบ async ผ่าน asyncio.Queue + background task ห้าม block trading loop
     คิวเต็มให้ทิ้งข้อความ priority ต่ำก่อน, retry พร้อม backoff, เคารพ 429 retry_after, จำกัด ~1 ข้อความ/วินาที
   - ทุกข้อความขึ้นต้นด้วย tag โหมด [PAPER] / [TESTNET] / [LIVE] และชื่อ symbol
   - รายงานตามรอบ (ตั้งได้ใน config, default ทุก 1 ชม. + สรุปรายวันเวลา 00:00 UTC):
     Realized PnL, Unrealized PnL, ค่าธรรมเนียม, Net PnL (วันนี้/สะสม), จำนวน fill, volume,
     สเปรดเฉลี่ยที่จับได้ (bps), inventory (จำนวน + notional), funding รับ/จ่าย,
     markout เฉลี่ย +1s/+5s/+30s (bps), uptime, จำนวน reconnect, max drawdown วันนี้
   - แจ้งเตือนทันที (priority สูง ส่งก่อนรายงานตามรอบ):
     kill switch / daily loss limit, circuit breaker หยุด/กลับมา quote, inventory แตะ limit,
     WebSocket ขาด/ข้อมูลค้าง, reconnect เกิน N ครั้งใน 10 นาที, error ไม่คาดคิด, start/shutdown
   - คำสั่ง (เปิดเมื่อ TELEGRAM_COMMANDS=true เท่านั้น):
     - ใช้ long polling (getUpdates) ไม่ใช้ webhook จึงไม่ต้องเปิด port
     - รับคำสั่งเฉพาะจาก TELEGRAM_CHAT_ID เท่านั้น นอกนั้นเพิกเฉยและ log ไว้
     - /status, /pnl : ดูสถานะ
     - /pause, /resume : หยุด/เริ่ม quote (ไม่ปิด position)
     - /flatten : cancel all + ปิด position แบบ reduce-only ต้องยืนยันด้วย /flatten CONFIRM ภายใน 30 วินาที
     - /resume ไม่มีผลถ้า daily loss limit ทำงานอยู่
     - log ทุกคำสั่งที่ได้รับ

9. ops/
   - graceful shutdown (SIGTERM): cancel all ก่อนออก
   - structured JSON logging
   - Prometheus metrics: PnL (realized/unrealized), inventory, spread, fill rate, markout, latency, reconnects
   - HTTP /health endpoint สำหรับ healthcheck (unhealthy ถ้าข้อมูลค้างหรือ loop ไม่เดิน)

## Container
- Dockerfile แบบ multi-stage, รันเป็น non-root user, read-only filesystem ยกเว้น volume ข้อมูล
- docker-compose.yml: restart: unless-stopped, healthcheck, volume สำหรับ data/logs, อ่านค่าจาก .env
- .env.example ที่มีทุกตัวแปรแต่ค่าว่าง และ .gitignore ที่กัน .env กับ data/
- (optional) service Prometheus + Grafana พร้อม dashboard เบื้องต้น

## การทดสอบ
- unit tests (pytest) สำหรับ strategy, risk, paper fill engine, order state machine และ notifier (mock Telegram)
- test ว่า paper mode รันได้โดยไม่มีคีย์ และ testnet ไม่ fallback ไปใช้คีย์จริง
- backtest ที่ replay ข้อมูลจาก recorder โดยใช้ paper fill engine ตัวเดียวกัน
  (ห้ามใช้ OHLCV backtest สำหรับ strategy นี้)
- backtest report: PnL curve, fee, fill count, inventory over time, markout distribution

## ลำดับการสร้าง (ทำทีละเฟส สรุปผลและวิธีรันให้ฉันดูก่อนไปเฟสถัดไป)
1. โครงโปรเจกต์ + config + screener + market data + recorder
2. strategy + paper fill engine + paper mode รันจริงได้ + backtest report + Telegram reporting/alerts
3. Binance executor + risk + state reconcile บน Testnet + Telegram commands
4. Docker + docker-compose + monitoring
5. README: วิธีตั้ง API key, วิธีตั้ง Telegram bot, วิธีรันแต่ละโหมด, checklist ก่อนเปิด live

## README ต้องมี
- ลำดับที่แนะนำ: paper (ไม่ต้องมีคีย์) → testnet → live ด้วยเงินน้อย
- คีย์จริง: เปิดเฉพาะสิทธิ์ Futures trading, ห้ามเปิด Withdraw, ตั้ง IP whitelist ให้ตรงกับ server,
  สร้างคีย์แยกเฉพาะบอทนี้
- Telegram: สร้างบอทกับ @BotFather, หา chat_id จาก getUpdates, ห้าม commit token
- เกณฑ์ก่อนเปิด live: paper กำไรหลังหัก fee อย่างน้อย 1–2 สัปดาห์ ครอบคลุมหลายสภาพตลาด,
  markout เฉลี่ยไม่ติดลบเกินสเปรดที่ได้, testnet ผ่านการทดสอบ kill switch / restart / reconnect
