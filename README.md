# goldbot

A research toolkit for a gold (XAUUSD) trend-following bot: a daily
**Donchian breakout with a 200-day trend filter and ATR position sizing**,
a backtester with realistic gold costs, and the validation tools you need
before you put real money on it.

> Not financial advice. Most retail algorithmic traders lose money, mostly
> through overfitting and underestimated costs. This project is built to
> catch both before they cost you anything.

## The strategy

Evaluated on each daily close, executed at the next day's open:

| Rule | Default |
|---|---|
| **Long entry**: close above the highest high of the last N days, and above its 200-day SMA | N = 55 |
| **Short entry** (optional): close below the lowest low of the last N days, and below its 200-day SMA | N = 55 |
| **Exit**: close below the lowest low of the last M days (mirror for shorts) | M = 20 |
| **Stop-loss**: fixed at 2 x ATR(20) from the fill, checked intraday; a gap through it fills at the open | 2 ATR |
| **Size**: risk 1% of equity between entry and stop, capped at 5x leverage | 1% |

Account-level kill switches (in `goldbot/risk.py`, shared with the future live bot):

* **Daily loss limit**: no new entries after losing 3% in a day.
* **Max drawdown**: flatten everything and stop trading at a 20% drawdown, until a person reviews it.

Why this strategy: trend following on gold has decades of public evidence
behind it (it is the core of most managed-futures funds), it has few
parameters to overfit, and it only needs to act once a day, so running it is
cheap and robust.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[plot,dev]"
pytest                                  # 42 tests, about 2 seconds

goldbot backtest synthetic              # demo on SYNTHETIC random prices
goldbot backtest data/xauusd_d1.csv     # your real data
```

Each command writes CSVs, a `summary.json` and an equity chart to `results/`.

## Paper and live trading with MetaTrader 5

`goldbot live` runs the same signal, decision and sizing code as the
backtester against an MT5 account. Full setup steps are in
[docs/MT5_SETUP.md](docs/MT5_SETUP.md); in short (on Windows, with the MT5
terminal logged in to a **demo** account and Algo Trading switched on):

```bash
pip install -e ".[mt5,plot]"
copy live.example.toml live.toml        # then edit symbol name etc.
goldbot live check                      # read-only: account, symbol, sizing, signals
goldbot live export-history             # broker's own daily bars -> data/broker_d1.csv
goldbot live run --dry-run              # logs orders without sending them
goldbot live run                        # paper trade on the demo account
```

Safety rails built in:

* Refuses to trade a real-money account unless `allow_live = true`.
* Every position has its stop-loss on the broker's server, so it is protected if the bot or VPS dies; a missing stop is put back.
* Acts once per daily bar, waits out the rollover spread spike, and postpones trading while the spread is too wide.
* Checks its memory against the broker's positions on every pass, and halts on anything it can't explain. It ignores your manual trades (they don't carry its magic number).
* The same daily-loss and max-drawdown kill switches as the backtest; a drawdown halt closes the position and waits for `goldbot live reset-halt`.
* State is saved atomically, so restarts are safe; every action is written to `state/journal.csv`.
* Optional Telegram alerts and a dead-man's-switch heartbeat URL.

## Getting real gold data

The loader accepts Dukascopy, `dukascopy-node`, MetaTrader 5 and most broker
CSV exports, daily or intraday (intraday is converted to daily bars that roll
at 22:00 UTC, gold's 5pm New York close).

* **Dukascopy (free, back to 2003)**, via Node.js:
  ```bash
  npx dukascopy-node -i xauusd -from 2010-01-01 -to 2026-10-01 -t d1 -f csv -dir data
  ```
* **MetaTrader 5**: `goldbot live export-history` saves your broker's daily
  bars directly (see below), or export from View > Symbols > Bars.
* **Your broker's API** (OANDA, IBKR): download daily candles to CSV.

Backtest on data from **the broker you will trade with** where possible.
Spreads, swaps and even daily highs and lows differ between brokers.

## Commands

```bash
# One backtest plus a Monte Carlo of the trade sequence
goldbot backtest data/xauusd_d1.csv --start 2012-01-01

# Set costs from your broker's contract specs (USD per oz, annual swap rates)
goldbot backtest data/xauusd_d1.csv --spread 0.35 --slippage 0.10 --swap-long 0.05 --swap-short 0.02

# Long-only, smaller risk
goldbot backtest data/xauusd_d1.csv --long-only --risk 0.005

# Parameter grid: you want a broad plateau of good results, not one sharp peak
goldbot sweep data/xauusd_d1.csv --entries 20,40,55,80,100 --exits 10,20,30,40

# Walk-forward: optimise on 4 years, trade the next unseen year, roll forward
goldbot walkforward data/xauusd_d1.csv --train-years 4 --test-years 1

# Convert intraday data to daily bars
goldbot resample data/xauusd_m1.csv data/xauusd_d1.csv
```

Run `goldbot <command> --help` for every option.

## Position size and account size

Gold moves a lot per ounce. If 1% of your equity is smaller than the stop
distance on one minimum lot, the trade is **skipped** and the backtest warns
you (`skipped_signals`). With a 1 oz minimum and a 2-ATR stop, that can mean
$100 of risk against a $120+ stop. Your options: more capital, a lower
`--lot-step` (some brokers allow 0.1 oz), or accept a higher `--risk`.
The tool will not quietly size trades you couldn't actually place.

## Go-live checklist

Don't trade real money until all of these hold:

1. [ ] Backtest on 10+ years of your broker's data, with your broker's real spread and swap.
2. [ ] The `sweep` shows a broad region of profitable settings, not one lucky cell.
3. [ ] `walkforward` out-of-sample results are positive, with a drawdown you could live through.
4. [ ] You can accept the Monte Carlo **`max_dd_p95`**, not just the backtest's single drawdown.
5. [ ] 1-3 months of paper trading match the backtest's trade signals.
6. [ ] 1-3 months live at minimum size before scaling up.

## Project layout

```
goldbot/
  data.py        load and clean CSVs, resample intraday to daily
  indicators.py  SMA, ATR, Donchian channels (backward-looking only)
  strategy.py    entry/exit signal rules
  risk.py        position sizing and kill switches
  backtest.py    daily simulator: next-open fills, spread, slippage, swap, gap-through stops
  metrics.py     CAGR, Sharpe, Sortino, drawdown, MAR, win rate, expectancy
  validate.py    parameter sweep, walk-forward, Monte Carlo
  cli.py         command line
  broker.py      what the live bot needs from a broker
  mt5_broker.py  MetaTrader 5 implementation
  live.py        live loop: reconcile, kill switches, once-per-bar decisions, orders
  live_cli.py    goldbot live check / run / reset-halt / export-history
  state.py       restart-safe state file and trade journal
  notify.py      Telegram alerts and heartbeat pings
tests/           42 tests, including a no-lookahead check and the live loop against a fake broker
live.example.toml  live bot settings
docs/MT5_SETUP.md  step-by-step MetaTrader 5 setup
docs/ROADMAP.md    what's done, what's next, and the upkeep schedule
```

## Second opinion from TradingAgents (advice only)

[TradingAgents](https://github.com/TauricResearch/TradingAgents) (v0.7.0) runs a team of AI analysts (market, news, social), a bull/bear debate and a risk review, then rates gold Buy / Overweight / Hold / Underweight / Sell. It never places or changes orders. Use it as a check before manual trades or to sanity-check what the bots hold.

It lives in its own environment, `.venv-agents`, so it cannot affect the live bots.

1. Save an API key once (Claude by default), then open a new terminal:
   `setx ANTHROPIC_API_KEY "your-key"`
2. Run it:
   `.venv-agents\Scripts\goldbot.exe agents` (prints the view and the bots' positions)
   `.venv-agents\Scripts\goldbot.exe agents --notify` (also sends it to Telegram, silently)

Options: `--ticker GLD`, `--date 2026-10-20`, `--provider openai --quick-model ... --deep-model ...`, `--debate-rounds 2`, `--verbose`. Full reports are saved under `results/agents/`. Each run makes dozens of LLM calls, so it costs money and takes several minutes; LLM output varies from run to run.
