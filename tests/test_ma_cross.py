"""Moving-average crossover strategy and intraday (H1/H4) support."""

import numpy as np
import pandas as pd
import pytest

from goldbot.backtest import CostConfig, run_backtest
from goldbot.data import load_bars
from goldbot.risk import RiskConfig
from goldbot.state import load_state
from goldbot.strategy import StrategyConfig, compute_signals
from goldbot.validate import _combos

from test_live import FakeBroker, make

CROSS = StrategyConfig(kind="ma_cross", fast_ma=2, slow_ma=4, trend_lookback=0, atr_lookback=2, cross_window=2)
SMALL = StrategyConfig(entry_lookback=3, exit_lookback=2, trend_lookback=3, atr_lookback=3, stop_atr=2.0, allow_short=False)
BIG_ACCOUNT = RiskConfig(lot_step=0.01, max_leverage=100)
BREAKOUT = [100, 100, 100, 100, 100, 100, 103, 105, 107, 109, 111, 106, 104, 103]


def hourly(closes, start="2024-01-01 14:00", wick=0.5):
    closes = np.asarray(closes, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(opens, closes) + wick, "low": np.minimum(opens, closes) - wick, "close": closes},
        index=pd.date_range(start, periods=len(closes), freq="h", name="time"),
    )


# --- signals ----------------------------------------------------------------


def test_cross_up_entry_lasts_cross_window_bars_then_exits_on_cross_down():
    df = hourly([10, 10, 10, 10, 9, 8, 9, 11, 13, 15, 17, 12, 8, 6])
    sig = compute_signals(df, CROSS)
    fast, slow = df["close"].rolling(2).mean(), df["close"].rolling(4).mean()
    cross = next(i for i in range(1, len(df)) if fast.iloc[i] > slow.iloc[i] and fast.iloc[i - 1] <= slow.iloc[i - 1])

    assert list(np.flatnonzero(sig["long_entry"])) == [cross, cross + 1]
    first_exit = next(i for i in range(cross, len(df)) if fast.iloc[i] <= slow.iloc[i])
    assert sig["long_exit"].iloc[first_exit] and not sig["long_exit"].iloc[cross:first_exit].any()
    assert sig["short_entry"].iloc[first_exit]


def test_no_entry_from_warmup_alone():
    # Fast is above slow from the first bar both exist, but there was never a cross.
    sig = compute_signals(hourly(range(100, 130)), CROSS)
    assert not sig["long_entry"].any()


def test_trend_filter_blocks_counter_trend_cross():
    closes = [200, 190, 180, 170, 160, 150, 100, 99, 98, 97, 99, 102]
    with_filter = StrategyConfig(kind="ma_cross", fast_ma=2, slow_ma=4, trend_lookback=10, atr_lookback=2)
    assert compute_signals(hourly(closes), CROSS)["long_entry"].any()
    assert not compute_signals(hourly(closes), with_filter)["long_entry"].any()


def test_config_rejects_bad_ma_lengths():
    with pytest.raises(ValueError):
        StrategyConfig(kind="ma_cross", fast_ma=50, slow_ma=50)
    with pytest.raises(ValueError):
        StrategyConfig(kind="breakout")


def test_sweep_grid_skips_fast_not_below_slow():
    combos = _combos({"fast_ma": [10, 50], "slow_ma": [50, 100]}, CROSS)
    assert {"fast_ma": 50, "slow_ma": 50} not in combos
    assert len(combos) == 3


# --- intraday backtests -----------------------------------------------------------


def test_intraday_swap_charged_once_per_rollover():
    df = hourly(BREAKOUT)  # 14:00 on Jan 1 to 03:00 on Jan 2
    costs = CostConfig(spread=0, slippage=0, swap_long_annual=0.0365, swap_short_annual=0)
    t = run_backtest(df, SMALL, costs, BIG_ACCOUNT, roll_hour_utc=0).trades.iloc[0]
    assert t.entry_date < pd.Timestamp("2024-01-02") < t.exit_date
    close_before_rollover = df.loc[:"2024-01-01 23:00", "close"].iloc[-1]
    assert t.swap == pytest.approx(close_before_rollover * t.units * 0.0001)


def test_load_bars_resamples_hourly_to_h4(tmp_path):
    path = tmp_path / "h1.csv"
    hourly(range(100, 108), start="2024-01-01 00:00").to_csv(path)
    h4 = load_bars(str(path), "H4")
    assert list(h4.index.hour) == [0, 4]
    assert h4["open"].iloc[1] == 104 - 1 and h4["close"].iloc[1] == 107
    with pytest.raises(ValueError):
        load_bars(str(load_bars_daily(tmp_path)), "H1")


def load_bars_daily(tmp_path):
    path = tmp_path / "d1.csv"
    df = hourly(range(100, 110))
    df.index = pd.bdate_range("2024-01-01", periods=len(df), name="time")
    df.to_csv(path)
    return path


# --- live bot on hourly bars -----------------------------------------------------------


def hourly_broker(upto, **kw):
    broker = FakeBroker(upto, **kw)
    broker.all_bars.index = pd.date_range("2024-01-01 19:00", periods=len(broker.all_bars), freq="h", name="time")
    return broker


def test_hourly_bot_keeps_start_of_day_equity_and_stays_quiet_without_trades(tmp_path):
    broker = hourly_broker(4)  # bars at 19:00-22:00, no breakout yet
    trader = make(tmp_path, broker, timeframe="H1", wait_after_open_minutes=0)
    assert trader.run_once() == "no trade"
    assert trader.notify.messages == []  # no alert every hour
    assert load_state(trader.cfg.state_file).day_start_equity == 100_000

    broker.equity = 99_000
    broker.next_bar()  # 23:00, same day
    trader.run_once()
    assert load_state(trader.cfg.state_file).day_start_equity == 100_000

    broker.next_bar()  # 00:00, a new trading day
    trader.run_once()
    state = load_state(trader.cfg.state_file)
    assert (state.trading_day, state.day_start_equity) == ("2024-01-02", 99_000)


def test_hourly_bot_trades_and_alerts(tmp_path):
    broker = hourly_broker(7)
    trader = make(tmp_path, broker, timeframe="H1", wait_after_open_minutes=0)
    assert trader.run_once().startswith("opened long")
    assert any(m.startswith("H1 check 2024-01-02T01:00") for m in trader.notify.messages)
