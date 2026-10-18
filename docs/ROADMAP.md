# Roadmap: from backtest to a running bot

## Phase 1: research (this release)

- [x] Data loader for Dukascopy, MT5 and broker CSVs
- [x] Donchian breakout + 200-day trend filter + ATR stops
- [x] Backtester with spread, slippage, commission, overnight swap and gap-through stops
- [x] Position sizing and kill switches shared with the live bot
- [x] Parameter sweep, walk-forward and Monte Carlo validation
- [ ] Run the go-live checklist in the README on real broker data

## Phase 2: paper trading on MetaTrader 5

- [x] `broker.py` interface and `mt5_broker.py` implementation (official `MetaTrader5` package)
- [x] `live.py`: once per completed daily bar, run `compute_signals` + `decide`, size with
      `position_size`, and send market orders with a **stop-loss held on the broker's server**
- [x] Reconcile with the broker on every pass; halt on anything unexplained; restore missing stops
- [x] Restart-safe state file and trade journal
- [x] Demo-only unless `allow_live = true`; dry-run mode
- [x] Telegram alerts and heartbeat URL
- [ ] Run `goldbot live check` against your broker and fix anything it flags
- [ ] Backtest on `goldbot live export-history` data with your broker's spread and swap
- [ ] Paper trade on a demo account for 1-3 months
- [ ] Every week, compare the bot's trades with a backtest over the same dates

## Phase 3: live trading and upkeep

Hosting and monitoring:

- [ ] Run on a Windows VPS near the broker's servers (~$15-40/month), started by Task Scheduler at boot (see docs/MT5_SETUP.md)
- [x] Telegram alerts: every order and fill, errors, kill-switch trips, daily summary
- [x] Heartbeat URL support; set up a healthchecks.io check so silence alerts you
- [x] Log file and trade journal
- [ ] Tag each version that goes live in git

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
