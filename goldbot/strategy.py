"""Trading rules shared by the backtester and the live bot.

Two strategies, picked with `kind`:

"donchian" (default): channel breakout with a long-term trend filter.
Rules, evaluated on each daily close and executed at the next open:
  * Go long when the close breaks above the highest high of the last
    `entry_lookback` days AND the close is above its `trend_lookback` SMA.
  * Go short (optional) on the mirror image: close below the lowest low
    of the last `entry_lookback` days AND below the trend SMA.
  * Exit a long when the close falls below the lowest low of the last
    `exit_lookback` days (mirror for shorts).
  * Protective stop at `stop_atr` x ATR from the fill price, checked intraday.
  * Optional trailing stop: after each daily close, pull the stop up to
    `trail_atr` x ATR behind the close (down, for shorts). It only ever
    tightens. `trail_atr = 0` turns it off.

"ma_cross": moving-average crossover, meant for intraday bars (e.g. 1-hour).
  * Go long when the `fast_ma` SMA crosses above the `slow_ma` SMA, short on
    the mirror image. An entry stays valid for `cross_window` bars after the
    cross, so a reversal can still be taken on the bar after the exit fills.
  * Exit when the fast SMA is back on the wrong side of the slow one.
  * Optional trend filter: only trade in the direction of the
    `trend_lookback` SMA; `trend_lookback = 0` turns it off.
  * Same ATR stop and optional trailing stop as "donchian".

Lookbacks count bars of whatever timeframe the data has, not days.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from .indicators import atr, channel_high, channel_low, sma


@dataclass(frozen=True)
class StrategyConfig:
    entry_lookback: int = 55
    exit_lookback: int = 20
    trend_lookback: int = 200
    atr_lookback: int = 20
    stop_atr: float = 2.0
    allow_short: bool = True
    trail_atr: float = 0.0  # 0 = fixed stop; e.g. 3.0 trails the stop 3 ATRs behind the close
    kind: str = "donchian"  # or "ma_cross"
    fast_ma: int = 20  # ma_cross only
    slow_ma: int = 50  # ma_cross only
    cross_window: int = 2  # ma_cross only: bars after a cross during which an entry is still taken

    def __post_init__(self):
        if self.kind not in ("donchian", "ma_cross"):
            raise ValueError(f"Unknown strategy kind {self.kind!r}; use 'donchian' or 'ma_cross'")
        if self.kind == "ma_cross" and not 0 < self.fast_ma < self.slow_ma:
            raise ValueError("ma_cross needs 0 < fast_ma < slow_ma")

    @property
    def warmup(self) -> int:
        """Bars needed before every indicator has a value."""
        if self.kind == "ma_cross":
            return max(self.slow_ma + self.cross_window, self.trend_lookback, self.atr_lookback) + 1
        return max(self.entry_lookback, self.exit_lookback, self.trend_lookback, self.atr_lookback) + 1


def compute_signals(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if cfg.kind == "ma_cross":
        return _ma_cross_signals(df, cfg)
    close = df["close"]
    out = pd.DataFrame(index=df.index)
    out["trend"] = sma(close, cfg.trend_lookback)
    out["atr"] = atr(df, cfg.atr_lookback)
    entry_high = channel_high(df, cfg.entry_lookback)
    entry_low = channel_low(df, cfg.entry_lookback)
    exit_high = channel_high(df, cfg.exit_lookback)
    exit_low = channel_low(df, cfg.exit_lookback)

    # Comparisons against NaN are False, so nothing fires during warm-up.
    out["long_entry"] = (close > entry_high) & (close > out["trend"])
    out["short_entry"] = (close < entry_low) & (close < out["trend"]) & cfg.allow_short
    out["long_exit"] = close < exit_low
    out["short_exit"] = close > exit_high
    return out


def _ma_cross_signals(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    close = df["close"]
    out = pd.DataFrame(index=df.index)
    out["trend"] = sma(close, cfg.trend_lookback) if cfg.trend_lookback > 0 else float("nan")
    out["atr"] = atr(df, cfg.atr_lookback)
    fast, slow = sma(close, cfg.fast_ma), sma(close, cfg.slow_ma)
    above, below = fast > slow, fast < slow  # both False while either SMA is NaN
    crossed_up = above & (fast.shift(1) <= slow.shift(1))  # NaN history never counts as a cross
    crossed_down = below & (fast.shift(1) >= slow.shift(1))
    recent_up = crossed_up.astype(int).rolling(cfg.cross_window, min_periods=1).max().astype(bool)
    recent_down = crossed_down.astype(int).rolling(cfg.cross_window, min_periods=1).max().astype(bool)
    if cfg.trend_lookback > 0:
        with_trend_up, with_trend_down = close > out["trend"], close < out["trend"]
    else:
        with_trend_up = with_trend_down = True
    out["long_entry"] = recent_up & above & with_trend_up
    out["short_entry"] = recent_down & below & with_trend_down & cfg.allow_short
    out["long_exit"] = ~above & slow.notna()
    out["short_exit"] = ~below & slow.notna()
    return out


def trailed_stop(side: int, stop: float, close: float, atr: float, trail_atr: float) -> float:
    """The stop after a daily close. Shared by the backtester and the live bot.

    Moves the stop to `trail_atr` x ATR behind `close` when that is tighter;
    never loosens it.
    """
    if trail_atr <= 0 or math.isnan(atr):
        return stop
    candidate = close - side * trail_atr * atr
    return max(stop, candidate) if side == 1 else min(stop, candidate)


@dataclass(frozen=True)
class Action:
    kind: str  # "enter" or "exit"
    side: int = 0  # for entries: +1 long, -1 short
    reason: str = ""


def decide(position_side: int, signal: Mapping, halted: bool, can_open: bool) -> Action | None:
    """What to do after a daily close. Shared by the backtester and the live bot.

    `position_side` is +1, -1 or 0 (flat); `signal` is one row of compute_signals.
    """
    if position_side and halted:
        return Action("exit", reason="risk_halt")
    if (position_side == 1 and signal["long_exit"]) or (position_side == -1 and signal["short_exit"]):
        return Action("exit", reason="channel_exit")
    if position_side == 0 and can_open and not math.isnan(signal["atr"]):
        if signal["long_entry"]:
            return Action("enter", side=1)
        if signal["short_entry"]:
            return Action("enter", side=-1)
    return None
