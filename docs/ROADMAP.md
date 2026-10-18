# Roadmap: from backtest to a running bot

## Phase 1: research (this release)

- [x] Data loader for Dukascopy, MT5 and broker CSVs
- [x] Donchian breakout + 200-day trend filter + ATR stops
- [x] Backtester with spread, slippage, commission, overnight swap and gap-through stops
- [x] Position sizing and kill switches shared with the live bot
- [x] Parameter sweep, walk-forward and Monte Carlo validation
- [ ] Run the go-live checklist in the README on real broker data

## Phase 2: paper trading

Pick one route:

| Route | Instrument | Notes |
|---|---|---|
| OANDA v20 REST API | XAU_USD CFD | Simple Python API, free practice account. Not available in every country. |
| Interactive Brokers (`ib_async`) | MGC micro gold futures (10 oz) | Exchange pricing, no CFD swap, but roll the contract every ~2 months. |
| MetaTrader 5 (`MetaTrader5` Python package) | XAUUSD CFD | Widest broker choice; Python package is Windows-only. |

Build:

- [ ] `broker.py`: a small interface (`get_daily_bars`, `get_position`, `get_equity`,
      `market_order`, `place_stop`, `cancel_stop`) with one implementation for the chosen broker
- [ ] `live.py`: once a day, shortly after the 22:00 UTC daily close: fetch bars, run
      `compute_signals`, size with `position_size`, place the order at the open and a
      **real stop order on the broker's server** (never rely on the bot being online to stop out)
- [ ] Reconcile on every start-up: compare the bot's state with the broker's positions
      and orders; refuse to trade and alert if they differ
- [ ] Persist state (open trade, stop, equity peak) in SQLite so restarts are safe
- [ ] Compare paper signals with the backtest run on the same dates every week

## Phase 3: live trading and upkeep

Hosting and monitoring:

- [ ] Run on a VPS near the broker's servers (~$10-30/month), as a systemd service that restarts on failure
- [ ] Telegram or Discord alerts: every order and fill, errors, disconnects, kill-switch trips, daily P&L
- [ ] Heartbeat: an external check that alerts when the bot has NOT reported in (silence is the dangerous failure)
- [ ] Logs of every signal, order and fill; code in git with a tag for each version that goes live

Regular upkeep:

| How often | Task |
|---|---|
| Daily (automated) | P&L summary, position reconciliation, heartbeat |
| Weekly | Skim the logs; check that fills match expected prices |
| Monthly | Compare live win rate, average win/loss and drawdown with the backtest; check live drawdown against the Monte Carlo `max_dd_p95` (beyond it, pause and review) |
| Quarterly | Re-check broker spreads, swap rates, margin and contract specs; re-run the walk-forward with new data |
| Yearly | Full review: keep, adjust or retire the strategy |

Rules for changes:

- Don't re-optimise parameters after every losing streak; trend following has long flat spells by design.
- Every change goes through backtest, walk-forward and paper trading again before going live.

## Phase 4: diversification (optional)

- [ ] Add an uncorrelated second strategy, e.g. a London/New York session-open breakout on intraday bars
- [ ] Run several entry lookbacks at once (e.g. 20, 55 and 100 days) with a third of the risk each
- [ ] Add a macro filter (US real yields, dollar index) and test whether it improves out-of-sample results
