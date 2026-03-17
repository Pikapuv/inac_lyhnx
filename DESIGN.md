# agent_eth DESIGN.md

High-level design for `agent_eth` (Binance spot bot) with V1.5 signals, V2 risk management, and V3-light UI/UX.

---

## 0. Scope

- **Exchange:** Binance Spot
- **Primary pair:** initially `ETH/USDT`, later `SOL/USDT`
- **Bot name:** `agent_eth`
- **Runtime:** Python on VPS (A = dev, B = runtime), Telegram bot for UI.

Goals:

1. V1.5: Signal bot (dump → BUY, TP/SL alerts, time-stop).
2. V2: Account-aware risk (balance, P&L per day, limits based on initial capital).
3. V3-light: Semi-auto trading with Telegram buttons (ENTER/SKIP, TP/SL/TIME-STOP confirmations), no manual commands.

This file documents the **target design**, not necessarily current implementation.

---

## 1. Core Concepts & Terminology

- **Initial Capital (C0):**
  - Fixed reference capital for risk limits per day.
  - Example: `C0 = 20 USDT`.

- **Daily P&L (P&L_day):**
  - Sum of realized profit/loss of all closed trades during one **trading day**.
  - Reset to 0 at the beginning of each trading day.

- **Trading Day:**
  - Configurable; initially use calendar day (UTC) or 09:00–08:59 VN.
  - Affects when P&L_day and per-day counters reset.

- **Per-trade risk:**
  - Fixed fraction of initial capital per trade.
  - Example: `stake_pct = 25%` of C0 → 5 USDT per trade.

- **Per-trade targets:**
  - Take-profit (TP): +1–2% from entry.
  - Stop-loss (SL): -2% from entry.

- **Per-day limits (based on C0):**
  - `daily_limit_pct = 3%` of C0.
  - For C0=20 USDT, `daily_limit_usdt = 0.6`.
  - Constraints:
    - If `P&L_day >= +daily_limit_usdt` → stop new trades for the day.
    - If `P&L_day <= -daily_limit_usdt` → stop new trades for the day.

- **Max trades per day:** e.g. `max_trades_per_day = 3`.

---

## 2. Settings & State Files

We separate **user settings** from **daily runtime state**.

### 2.1. `settings.json` (persistent user configuration)

Example structure:

```json
{
  "initial_capital_usdt": 20.0,
  "daily_limit_pct": 3.0,
  "max_trades_per_day": 3,

  "tp_pct_min": 1.0,
  "tp_pct_max": 2.0,
  "sl_pct": 2.0,

  "dump_threshold_pct": -0.3,
  "pump_threshold_pct": 1.5,

  "symbol": "ETH/USDT",

  "trading_sessions": [
    { "start_hour": 0, "end_hour": 23 }   // simplified: always on
  ],

  "auto_trade_day": false,
  "auto_trade_night": false
}
```

- Adjusted via `/settings` UI (Telegram buttons), not by editing files.
- `initial_capital_usdt` defines daily limit in USDT:
  - `daily_limit_usdt = initial_capital_usdt * daily_limit_pct / 100`.

### 2.2. `state.json` (runtime per-day state)

Example structure:

```json
{
  "trading_day": "2026-03-17",          // e.g. UTC date / custom trading day id
  "initial_capital_usdt": 20.0,
  "daily_limit_usdt": 0.6,

  "pnl_day_usdt": 0.15,
  "trades_opened": 2,
  "trades_closed": 1,

  "has_position": true,
  "entry_price": 2320.69,
  "position_open_time": 1773712800.0,
  "tp_alert_sent": false,
  "sl_alert_sent": false,

  "auto_trade_enabled": false
}
```

- Reset logic when `current_trading_day != state.trading_day`.
- `pnl_day_usdt` accumulates realized P&L of closed trades.

---

## 3. Price & Market Data Tracking

### 3.1. Primary & reference pairs

For **SOL/USDT** strategy:

- Primary pair: `SOL/USDT` (or `ETH/USDT` initially).
- Reference pairs:
  - `BTC/USDT` – market trend reference.
  - `SOL/BTC` – SOL strength vs BTC.

Bot should fetch and maintain histories:

```python
prices = {
  "SOLUSDT": sol_price_now,
  "BTCUSDT": btc_price_now,
  "SOLBTC": solbtc_price_now,
}

price_history = {
  "SOLUSDT": [(ts, price), ...],  # last 30–60 minutes
  "BTCUSDT": [...],
  "SOLBTC": [...],
}
```

### 3.2. Derived metrics

At each poll:

- `% change` over 5 and 15 minutes for each relevant pair:

  ```python
  change_5m_SOLUSDT
  change_15m_SOLUSDT
  change_5m_BTCUSDT
  change_5m_SOLBTC
  ```

- These feed into entry conditions and filters.

### 3.3. Volume & volatility

- Track volume for SOL/USDT from Binance (via ticker or kline):

  ```python
  volume_SOL_5m
  volume_SOL_15m
  volume_SOL_avg_1h
  ```

- Track simple volatility (ATR or stddev) for SOL/USDT:

  ```python
  atr_SOL_5m
  atr_SOL_15m
  ```

- Use these to avoid trading when:
  - Volume too low (dead market).
  - Volatility too high (chaotic regime).

---

## 4. Strategy V1.5+ (Entry & Exit Logic)

### 4.1. Entry (BUY) conditions

Base idea: **buy dumps with context**.

Conditions (for SOL/USDT example):

1. **Session filter**:
   - Current time within `trading_sessions`.

2. **Risk/day filter**:
   - `abs(pnl_day_usdt) < daily_limit_usdt`.
   - `trades_opened < max_trades_per_day`.

3. **No existing position**:
   - `has_position == False` (for V1.5 entry-level system).

4. **Trend filter** (optional, future):
   - Price above medium-term MA (e.g. MA50 on 1h) ⇒ uptrend / sideway.

5. **Dump conditions** (multi-level):

   - Compute `change_5m_SOLUSDT`.

   - Example thresholds:

     ```text
     -0.2% >= change_5m_SOLUSDT > -0.5%   → MUA THỬ  (size_usdt = 0.15 * C0)
     -0.5% >= change_5m_SOLUSDT > -1.0%   → MUA CHUẨN (size_usdt = 0.25 * C0)
     change_5m_SOLUSDT <= -1.0%          → MUA MẠNH  (size_usdt = 0.30 * C0)
     ```

6. **Relative dump filter** vs BTC/SOLBTC:

   - Only BUY if SOL is dumping more than BTC:

     ```python
     change_5m_SOLUSDT < change_5m_BTCUSDT
     ```

   - and/or SOLBTC also weakening:

     ```python
     change_5m_SOLBTC <= 0  # SOL losing vs BTC
     ```

7. **Volume filter**:

   - Volume on the last 5 or 15 minutes greater than some multiple of the average:

     ```python
     volume_SOL_5m > vol_factor * volume_SOL_avg_1h
     ```

8. **Volatility filter**:

   - ATR within reasonable bounds to avoid dead/insane volatility regimes.

If all relevant conditions pass, create a **BUY proposal** with:

```python
proposal = {
  "id": ...,            # unique id
  "symbol": "SOL/USDT",
  "price": price_now,
  "ts": now,
  "size_usdt": stake_pct * C0,
  "size_sol": size_usdt / price_now,
  "tp1": price_now * (1 + tp_pct_min/100),
  "tp2": price_now * (1 + tp_pct_max/100),
  "sl":  price_now * (1 - sl_pct/100),
}
```

And send a signal message to Telegram with **inline buttons** (planned for V3-light):

- `[✅ Vào lệnh]` (ENTER)
- `[❌ Bỏ qua]` (SKIP)

### 4.2. Position state (V1.5)

When user **accepts** a BUY proposal (`ENTER`):

- Set:

```python
has_position = True
current_entry_price = proposal["price"]
position_open_time = proposal["ts"]
size_usdt = proposal["size_usdt"]
size_coin = proposal["size_sol"]

tp_alert_sent = False
sl_alert_sent = False
```

- Optionally, in V3, also send the actual LIMIT BUY order to Binance.

### 4.3. Take profit (TP) signal

Each poll, if `has_position`:

```python
pnl_pct = (price_now - current_entry_price) / current_entry_price * 100

if (not tp_alert_sent) and pnl_pct >= tp_pct_min:
    # Send "CÂN NHẮC CHỐT LỜI" once
    tp_alert_sent = True
    # In V3-light, this comes with a button: [Chốt] / [Giữ tiếp]
```

- Only send TP alert **once per position**.
- Optional: a second TP level `tp_pct_max` could trigger another alert if desired.

### 4.4. Stop-loss (SL) signal

Similarly:

```python
if (not sl_alert_sent) and pnl_pct <= -sl_pct:
    # Send "CÂN NHẮC CẮT LỖ" once
    sl_alert_sent = True
```

- Only send SL alert **once per position**.
- In V3-light, attach buttons for quick decision.

### 4.5. Time-stop signal (break-even opportunity)

To avoid endless holding of meh trades:

- Define `time_stop_minutes` (e.g. 60–120 minutes).

Each poll, if `has_position`:

```python
age_minutes = (now - position_open_time) / 60

if age_minutes >= time_stop_minutes and abs(pnl_pct) <= 0.5:
    # Send TIME STOP alert: position has stagnated near breakeven
```

- Suggest the user to close the position at small profit/loss and free the slot.

---

## 5. Daily Risk Management (V2, based on initial capital)

- `initial_capital_usdt = C0` (configurable via settings UI).
- `daily_limit_usdt = C0 * daily_limit_pct / 100` (e.g. 0.6 USDT for C0=20, 3%).

### 5.1. Tracking daily P&L

At each **trade close** (user exits position, or bot auto-closes in V3):

1. Compute realized P&L for that trade (after fees, approximated or fetched from Binance via `fetch_my_trades`).
2. Update:

```python
pnl_day_usdt += trade_pnl_usdt
trades_closed += 1
```

3. Write to log and `state.json`.

### 5.2. Enforcing per-day limits

Before generating new BUY proposals, check:

```python
if pnl_day_usdt >= daily_limit_usdt:
    # HIT daily profit target → no more BUY proposals

if pnl_day_usdt <= -daily_limit_usdt:
    # HIT daily loss limit → no more BUY proposals

if trades_opened >= max_trades_per_day:
    # Reached trade count limit → no more BUY proposals
```

In V3-light, also **disable auto-trade** when these limits are hit.

---

## 6. UI / UX (V3-light)

Not implemented yet; this section defines target behaviors.

### 6.1. Settings UI (`/settings`)

- User trigger: `/settings` in Telegram.
- Bot responds with inline keyboard to adjust:
  - Initial capital (C0)
  - Daily limit %
  - Max trades per day
  - TP/SL ranges
  - Dump threshold
  - Sessions / auto-trade toggles

Example layout:

> [agent_eth – SETTINGS]\n
> Vốn ban đầu: 20.00 USDT\n
> Giới hạn ngày: ±3% (= ±0.60 USDT)\n
> Max lệnh/ngày: 3\n
> TP: 1.0–2.0%\n
> SL: 2.0%\n
> Dump threshold: -0.3%\n
>\n
> [⚙ Vốn ban đầu] [⚙ Giới hạn ngày]\n
> [⚙ TP/SL]       [⚙ Dump threshold]\n
> [⚙ Max lệnh/ngày]

### 6.2. BUY proposals with buttons

When entry conditions are met, bot sends:

- Text: full details (symbol, price, size, TP/SL, context).
- Inline buttons:
  - `[✅ Vào lệnh]` (callback_data: `ENTER:<proposal_id>`)
  - `[❌ Bỏ qua]` (callback_data: `SKIP:<proposal_id>`)

Callback handling:

- On `ENTER:<id>`:
  - Look up `proposal` from `pending_proposals`.
  - Update `has_position`, `entry_price`, `position_open_time`, `tp_alert_sent = False`, `sl_alert_sent = False`.
  - In V3 (full), also place LIMIT BUY via `binance_trade` API.

- On `SKIP:<id>`:
  - Mark proposal as skipped, log it.

### 6.3. TP/SL/TIME-STOP alerts with buttons

- TP alert:
  - `[✅ Chốt lời] [❌ Giữ tiếp]`
- SL alert:
  - `[✅ Cắt lỗ] [❌ Giữ thêm]`
- TIME-STOP alert:
  - `[✅ Đóng lệnh] [❌ Bỏ qua]`

Callbacks for these will:

- In V3-light:
  - Mark the intention (user wants to close), log it.
  - User still executes on Binance manually.
- In V3 (full):
  - Place SELL order via `binance_trade` API.
  - Update `pnl_day_usdt` based on realized result.

---

## 7. Logging

All important events should be logged to a file, e.g. `logs/agent_eth.log`:

- Startup / shutdown.
- BUY signals (proposals).
- ENTER / SKIP.
- TP / SL alerts and user decisions.
- TIME-STOP alerts.
- Daily summary:
  - P&L_day, trades_opened, trades_closed.

Example log lines:

```text
2026-03-17T10:30:12Z [BUY_SIGNAL] symbol=ETH/USDT price=2320.69 change_5m=-0.35 change_15m=-0.17 size_usdt=5.00 size_eth=0.002155
2026-03-17T10:31:00Z [ENTER] id=12345 price=2320.69 size_eth=0.002155
2026-03-17T11:05:48Z [TP_ALERT] id=12345 price=2344.99 pnl_pct=+1.05
2026-03-17T11:06:10Z [USER_TP_OK] id=12345
2026-03-17T11:06:11Z [TRADE_CLOSED] id=12345 pnl_usdt=0.09 pnl_day_usdt=0.09
```

This makes it possible to evaluate strategy performance and debug behavior over time.

---

## 8. Summary

This design describes how `agent_eth` should evolve from:

- **V1.5** – simple dump-based signal bot (BUY + TP/SL alerts, estimated position),
- to **V2** – account-aware risk system (balance, daily P&L based on initial capital, per-day limit, max trades/day),
- and **V3-light** – semi-automated trading with Telegram buttons (ENTER/SKIP, TP/SL/TIME-STOP), and configurable settings via UI instead of editing config files.

Implementation should proceed incrementally:

1. Solidify V1.5 (fix TP spam, add SL alert, add TIME-STOP).
2. Integrate balance + P&L_day tracking based on initial capital (V2).
3. Add settings/state JSON + /settings UI.
4. Add inline buttons + callback handling for V3-light.

Push changes via Git (dev branch → main), and only roll out to VPS B after testing on VPS A.
