import math

import numpy as np
import pandas as pd
import pytest

from goldbot.backtest import CostConfig, run_backtest
from goldbot.data import load_daily, load_ohlc, synthetic_daily, to_daily
from goldbot.indicators import atr, channel_high
from goldbot.risk import RiskConfig, RiskGuard, position_size
from goldbot.strategy import StrategyConfig, compute_signals
from goldbot.validate import monte_carlo, walk_forward

SMALL = StrategyConfig(entry_lookback=3, exit_lookback=2, trend_lookback=3, atr_lookback=3, stop_atr=2.0, allow_short=False)
NO_COSTS = CostConfig(spread=0, slippage=0, commission_per_oz=0, swap_long_annual=0, swap_short_annual=0)
BIG_ACCOUNT = RiskConfig(lot_step=0.01, max_leverage=100)


def bars(opens, closes, wick=0.5):
    opens, closes = np.asarray(opens, float), np.asarray(closes, float)
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + wick,
            "low": np.minimum(opens, closes) - wick,
            "close": closes,
        },
        index=pd.bdate_range("2024-01-01", periods=len(closes), name="time"),
    )


def from_closes(closes, wick=0.5):
    closes = np.asarray(closes, float)
    return bars(np.concatenate([[closes[0]], closes[:-1]]), closes, wick)


# A flat base, a breakout on bar 6, a rally, then a drop that triggers the channel exit.
BREAKOUT = [100, 100, 100, 100, 100, 100, 103, 105, 107, 109, 111, 106, 104, 103]


# --- indicators -----------------------------------------------------------


def test_channel_excludes_current_bar():
    df = from_closes([1, 2, 3, 10])
    assert channel_high(df, 3).iloc[-1] == pytest.approx(3.5)  # max high of bars 0-2


def test_atr_constant_range():
    df = bars([100] * 20, [100] * 20, wick=1.0)  # every bar ranges 99..101
    assert atr(df, 5).iloc[-1] == pytest.approx(2.0)


# --- risk -----------------------------------------------------------------


def test_position_size_risks_the_budget():
    cfg = RiskConfig(risk_per_trade=0.01, lot_step=0.01, max_leverage=100)
    assert position_size(10_000, 2000, 50, cfg) == pytest.approx(2.0)  # $100 risk / $50 per oz


def test_position_size_rounds_down_to_lot_step():
    assert position_size(10_000, 2000, 60, RiskConfig(lot_step=1.0)) == 1.0  # 1.67 oz -> 1


def test_position_size_leverage_cap():
    cfg = RiskConfig(risk_per_trade=0.5, max_leverage=2, lot_step=0.01)
    assert position_size(10_000, 2000, 1, cfg) == pytest.approx(10.0)  # 2x of $10k at $2000


def test_risk_guard_halts_on_drawdown():
    guard = RiskGuard(RiskConfig(max_drawdown=0.2), 10_000)
    guard.update(12_000)
    guard.update(9_700)
    assert not guard.halted
    guard.update(9_600)
    assert guard.halted


def test_risk_guard_daily_loss():
    guard = RiskGuard(RiskConfig(max_daily_loss=0.03), 10_000)
    guard.start_day(10_000)
    assert guard.can_open(9_800)
    assert not guard.can_open(9_700)


# --- strategy and backtest ------------------------------------------------


def test_trend_filter_blocks_entry():
    # The last bar breaks out of a 3-bar range, but price is still far below its 8-bar average.
    df = from_closes([200, 190, 180, 170, 100, 100, 100, 100, 104])
    assert compute_signals(df, StrategyConfig(3, 2, 3, 3, 2.0, False))["long_entry"].iloc[-1]
    assert not compute_signals(df, StrategyConfig(3, 2, 8, 3, 2.0, False))["long_entry"].iloc[-1]


def test_entry_fills_next_open_and_exit_on_channel():
    df = from_closes(BREAKOUT)
    result = run_backtest(df, SMALL, NO_COSTS, BIG_ACCOUNT)
    t = result.trades.iloc[0]
    assert t.entry_date == df.index[7]  # signal on bar 6's close, fill at bar 7's open
    assert t.entry_price == pytest.approx(df["open"].iloc[7])
    assert t.exit_reason == "channel_exit"
    # Bar 11 closes below the 2-bar low, so the exit fills at bar 12's open.
    assert t.exit_date == df.index[12]
    assert t.exit_price == pytest.approx(df["open"].iloc[12])
    assert t.pnl == pytest.approx((t.exit_price - t.entry_price) * t.units)
    assert result.equity.iloc[-1] == pytest.approx(10_000 + t.pnl)


def test_spread_and_slippage_cost_exactly_what_they_should():
    df = from_closes(BREAKOUT)
    costs = CostConfig(spread=0.4, slippage=0.1, commission_per_oz=0.05, swap_long_annual=0, swap_short_annual=0)
    free = run_backtest(df, SMALL, NO_COSTS, BIG_ACCOUNT).trades.iloc[0]
    paid = run_backtest(df, SMALL, costs, BIG_ACCOUNT).trades.iloc[0]
    assert paid.entry_price == pytest.approx(free.entry_price + 0.3)
    assert paid.exit_price == pytest.approx(free.exit_price - 0.3)
    assert paid.commission == pytest.approx(0.05 * paid.units * 2)


def test_swap_charged_per_night_including_weekends():
    df = from_closes(BREAKOUT)
    costs = CostConfig(spread=0, slippage=0, swap_long_annual=0.0365, swap_short_annual=0)
    t = run_backtest(df, SMALL, costs, BIG_ACCOUNT).trades.iloc[0]
    held = df.loc[t.entry_date : t.exit_date].iloc[:-1]  # nights from entry to the bar before exit
    nights = (df.index.to_series().shift(-1) - df.index.to_series()).dt.days.loc[held.index]
    expected = (held["close"] * t.units * 0.0001 * nights).sum()
    assert t.swap == pytest.approx(expected)


def test_stop_gap_fills_at_open_not_stop():
    closes = BREAKOUT[:9]
    df = from_closes(closes)
    crash = pd.DataFrame(
        {"open": [80.0], "high": [81.0], "low": [79.0], "close": [80.5]},
        index=[df.index[-1] + pd.offsets.BDay()],
    )
    df = pd.concat([df, crash])
    t = run_backtest(df, SMALL, NO_COSTS, BIG_ACCOUNT).trades.iloc[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(80.0)
    assert t.stop > 80.0


def test_no_lookahead():
    df = synthetic_daily(800, seed=3)
    cfg = StrategyConfig(20, 10, 50, 14, 2.0, True)
    cut = 600
    full = run_backtest(df, cfg, CostConfig(), BIG_ACCOUNT)
    part = run_backtest(df.iloc[:cut], cfg, CostConfig(), BIG_ACCOUNT)
    # Everything before the last bar of the shorter run (where it force-closes) must match.
    stop = df.index[cut - 2]
    pd.testing.assert_series_equal(full.equity.loc[:stop], part.equity.loc[:stop])
    full_closed = full.trades[full.trades.exit_date <= stop].reset_index(drop=True)
    part_closed = part.trades[part.trades.exit_date <= stop].reset_index(drop=True)
    pd.testing.assert_frame_equal(full_closed, part_closed)


def test_drawdown_kill_switch_flattens_and_stops_trading():
    df = synthetic_daily(1500, seed=11)
    cfg = StrategyConfig(10, 5, 20, 10, 1.0, True)
    risk = RiskConfig(risk_per_trade=0.05, lot_step=0.01, max_leverage=100, max_drawdown=0.10)
    result = run_backtest(df, cfg, CostConfig(), risk)
    assert result.halted
    assert result.trades.iloc[-1].exit_reason in ("risk_halt", "stop")
    halt_exit = result.trades.iloc[-1].exit_date
    assert (result.equity.loc[halt_exit:].diff().fillna(0) == 0).all()


def test_skipped_signals_reported_when_too_small():
    df = from_closes(BREAKOUT)
    result = run_backtest(df, SMALL, NO_COSTS, RiskConfig(lot_step=100.0))
    assert result.metrics["trades"] == 0
    assert result.metrics["skipped_signals"] >= 1


# --- data -----------------------------------------------------------------


def test_load_dukascopy_csv(tmp_path):
    path = tmp_path / "duka.csv"
    path.write_text(
        "Gmt time,Open,High,Low,Close,Volume\n"
        "02.01.2024 00:00:00.000 GMT+0000,2063.1,2075.0,2060.2,2072.5,1200\n"
        "03.01.2024 00:00:00.000 GMT+0000,2072.5,2073.9,2040.1,2042.0,1500\n"
        "06.01.2024 00:00:00.000 GMT+0000,2042.0,2042.0,2042.0,2042.0,0\n"
    )
    df = load_ohlc(str(path))
    assert list(df.index) == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]  # flat weekend bar dropped
    assert df["close"].iloc[1] == 2042.0


def test_load_metatrader_csv(tmp_path):
    path = tmp_path / "mt5.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n"
        "2024.01.02\t00:00:00\t2063.1\t2075.0\t2060.2\t2072.5\t1200\n"
        "2024.01.03\t00:00:00\t2072.5\t2073.9\t2040.1\t2042.0\t1500\n"
    )
    df = load_daily(str(path))
    assert df.index[0] == pd.Timestamp("2024-01-02")
    assert df["high"].iloc[0] == 2075.0


def test_load_epoch_ms_csv(tmp_path):
    path = tmp_path / "node.csv"
    path.write_text("timestamp,open,high,low,close\n1704153600000,1,2,0.5,1.5\n1704240000000,1.5,2,1,1.8\n")
    df = load_ohlc(str(path))
    assert df.index[0] == pd.Timestamp("2024-01-02")


def test_to_daily_rolls_at_22_utc():
    idx = pd.to_datetime(["2024-01-07 22:00", "2024-01-08 10:00", "2024-01-08 21:00", "2024-01-08 22:00"])
    df = pd.DataFrame({"open": [1, 2, 3, 4.0], "high": [1.5, 5, 3.5, 4.5], "low": [0.5, 1.5, 2.5, 3.5], "close": [1.2, 2.5, 3.2, 4.2]}, index=idx)
    daily = to_daily(df)
    monday = daily.loc["2024-01-08"]  # Sunday 22:00 through Monday 21:59
    assert (monday.open, monday.high, monday.low, monday.close) == (1, 5, 0.5, 3.2)
    assert daily.loc["2024-01-09"].open == 4


# --- validation -----------------------------------------------------------


def test_walk_forward_only_reports_unseen_periods():
    df = synthetic_daily(2500, seed=5)
    wf = walk_forward(df, StrategyConfig(), {"entry_lookback": [20, 55], "exit_lookback": [10, 20]}, risk=BIG_ACCOUNT, train_years=3, test_years=1)
    first_train_end = pd.Timestamp(wf.windows.iloc[0].train_end)
    assert wf.equity.index[0] > first_train_end
    assert wf.equity.index.is_monotonic_increasing
    assert len(wf.windows) >= 3


def test_monte_carlo_is_reproducible_and_ordered():
    trades = pd.DataFrame({"return_on_equity": [0.02, -0.01, -0.01, 0.03, -0.01, 0.015, -0.01]})
    a, b = monte_carlo(trades, n_sims=2000, seed=1), monte_carlo(trades, n_sims=2000, seed=1)
    assert a == b
    assert a["return_p5"] <= a["return_p50"] <= a["return_p95"]
    assert a["max_dd_p99"] <= a["max_dd_p95"] <= a["max_dd_p50"] <= 0
    assert not math.isnan(a["prob_loss"])


# --- trailing stop ----------------------------------------------------------


def test_trailed_stop_only_tightens():
    from goldbot.strategy import trailed_stop

    assert trailed_stop(1, 95.0, 110.0, 2.0, 3.0) == 104.0  # long: pulled up to close - 3 ATR
    assert trailed_stop(1, 105.0, 110.0, 2.0, 3.0) == 105.0  # never loosened
    assert trailed_stop(-1, 120.0, 110.0, 2.0, 3.0) == 116.0  # short: pulled down
    assert trailed_stop(-1, 115.0, 110.0, 2.0, 3.0) == 115.0
    assert trailed_stop(1, 95.0, 110.0, 2.0, 0.0) == 95.0  # off
    assert trailed_stop(1, 95.0, 110.0, float("nan"), 3.0) == 95.0


def test_trailing_stop_locks_in_rally_gains():
    df = from_closes(BREAKOUT)
    fixed = run_backtest(df, SMALL, NO_COSTS, BIG_ACCOUNT).trades.iloc[0]
    trailed = run_backtest(df, StrategyConfig(**{**SMALL.__dict__, "trail_atr": 1.0}), NO_COSTS, BIG_ACCOUNT).trades.iloc[0]
    assert fixed.exit_reason == "channel_exit"
    assert trailed.exit_reason == "stop"
    assert trailed.stop > fixed.stop
    assert trailed.exit_price > fixed.exit_price
