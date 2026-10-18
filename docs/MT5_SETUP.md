# MetaTrader 5 setup

Getting from zero to a bot paper-trading gold on an MT5 demo account.
The `MetaTrader5` Python package only works on **Windows**, with the MT5
terminal running on the same machine.

## 1. Broker and demo account

1. Pick a broker that offers MT5 and gold (XAUUSD). Install *their* MT5 build
   so the right servers come pre-loaded.
2. In MT5: **File > Open an Account**, choose a **demo** account. If the
   broker offers both, pick a **hedging** account type and keep this account
   for the bot only: manual trades on the same symbol in a netting account
   would merge with the bot's position.
3. In **Market Watch**, find gold and note its exact name (`XAUUSD`, `GOLD`,
   `XAUUSD.m`, ...). Right-click it > **Specification** and note the contract
   size (usually 100 oz), the minimum volume, and the swap values.

## 2. Terminal settings

* **Tools > Options > Expert Advisors**: tick **Allow algorithmic trading**.
* Make sure the **Algo Trading** toolbar button is green.
* **Tools > Options > Charts**: set **Max bars in chart** to Unlimited.
* Open a gold chart on the **D1** timeframe and press **Home** a few times so
  the terminal downloads years of daily history.

## 3. Python and the code

1. Install 64-bit **Python 3.12** from python.org (tick "Add python.exe to PATH").
2. In a terminal (PowerShell):
   ```powershell
   git clone https://github.com/<you>/gold-trading-bot.git
   cd gold-trading-bot
   py -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[mt5,plot]"
   copy live.example.toml live.toml
   ```
3. Edit `live.toml`: set `symbol` to the exact name from step 1. Keep the
   `[strategy]` and `[risk]` values identical to the ones you validated.

## 4. Check the connection (read-only)

```powershell
goldbot live check
```

This never trades. It prints the account (demo or real), the symbol's
contract specs, the current spread, how many daily bars are available,
today's signals, the position size a new trade would get, and the bot's
saved state. Fix anything it complains about before going further.

By default the bot uses the account the terminal is logged in to. To pin a
specific account instead, set environment variables (the password then
lives in your Windows user profile, never in the repo):

```powershell
setx MT5_LOGIN "12345678"
setx MT5_PASSWORD "your-password"
setx MT5_SERVER "YourBroker-Demo"
```

Open a new terminal afterwards so the variables take effect.

## 5. Backtest on your broker's data

```powershell
goldbot live export-history --bars 5000 --out data/broker_d1.csv
goldbot backtest data/broker_d1.csv --spread 0.35 --lot-step 1 --swap-long 0.05 --swap-short 0.02
goldbot walkforward data/broker_d1.csv
```

Use the spread and lot step `goldbot live check` printed. To turn the
broker's swap into the annual rate the backtester wants, when the
specification shows swap **in points**:

```
cost per oz per night = |swap points| x point size
annual rate          ~= cost per oz per night x 365 / gold price
```

Example: swap long -60 points, point size 0.01, gold at 4,000:
0.60 x 365 / 4000 = 0.055, so `--swap-long 0.055`. A positive swap means
the broker pays you; pass it as a negative rate.

Broker daily bars usually close at 17:00 New York, the same moment the live
bot works from, so backtest and live trading see identical candles.

## 6. Alerts (recommended)

**Telegram** (free):
1. Message **@BotFather** on Telegram, send `/newbot`, copy the token.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
3. `setx TELEGRAM_BOT_TOKEN "<token>"` and `setx TELEGRAM_CHAT_ID "<id>"`.

You'll get a message for every order, every error, kill-switch trips and a
summary after each daily check.

**Dead man's switch**: create a free check at healthchecks.io with a 5-minute
period and 15-minute grace, and put its ping URL in `heartbeat_url`. If the
bot, the terminal or the VPS dies, healthchecks.io emails you. Silence is
the failure you'd otherwise miss.

## 7. Dry run, then paper trade

```powershell
goldbot live run --once --dry-run     # one pass, prints what it would do
goldbot live run --dry-run            # keeps running; logs orders without sending
goldbot live run                      # paper trades the demo account
```

The bot checks every minute but acts **once per daily bar**, starting
`wait_after_open_minutes` after the new day opens, so most of the time the
log just says `up to date`. Everything it does goes to `state/goldbot.log`
and `state/journal.csv`.

Paper trade for 1-3 months. Each week, export history and run a backtest over
the same dates: the trades should match. If they don't, find out why before
risking money.

## 8. Keeping it running on a VPS

MetaQuotes' built-in "MQL5 VPS" can only run MQL5 Expert Advisors, not
Python, so use an ordinary **Windows VPS** (ideally in the same city as your
broker's server, about $15-40/month):

1. Install MT5, Python and the bot as above. Log the terminal in and leave it open.
2. Put a shortcut to MT5 in `shell:startup` so it opens when you log in, and
   configure Windows to log in automatically after a reboot.
3. **Task Scheduler > Create Task**:
   * Trigger: *At log on*.
   * Action: start `C:\path\to\gold-trading-bot\.venv\Scripts\goldbot.exe`
     with arguments `live run --config live.toml`, *Start in*
     `C:\path\to\gold-trading-bot`.
   * Settings: *If the task fails, restart every 1 minute*, up to 999 times.
     Untick *Stop the task if it runs longer than...*.
4. Reboot once to prove everything comes back by itself.

## 9. Going live

Only after the README's go-live checklist passes:

1. Open a real account with the same broker and account type.
2. Set `allow_live = true` in `live.toml`. The bot refuses real-money
   accounts without it.
3. Start with the smallest size that still trades, e.g. `risk_per_trade = 0.0025`.
4. Delete `state/state.json` when switching accounts, so the drawdown peak starts fresh.

## Troubleshooting

| Message | Fix |
|---|---|
| `MetaTrader5 package is not installed` | You are not on Windows, or skipped `pip install -e ".[mt5]"`. |
| `Algo Trading is switched off` | Click the **Algo Trading** toolbar button so it turns green. |
| `Symbol 'XAUUSD' not found` | Use the exact Market Watch name in `live.toml`. |
| `Only N daily bars available` | Scroll the D1 chart back; set Max bars in chart to Unlimited. |
| `retcode 10027` | Algo trading disabled in the terminal (see above). |
| `retcode 10018` | Market closed (weekend or holiday); it will retry. |
| `retcode 10019` | Not enough margin: lower `risk_per_trade` or `max_leverage`. |
| `retcode 10016` | Stop too close to price for this broker; check the min stop distance in `live check`. |
| `HALTED: found position ... it did not open` | A position carries the bot's magic number but isn't in `state/state.json`. Close or adopt it manually, fix the state file, then `goldbot live reset-halt`. |
| `KILL SWITCH: drawdown exceeded 20%` | Working as designed. Review the journal before `goldbot live reset-halt`. |
