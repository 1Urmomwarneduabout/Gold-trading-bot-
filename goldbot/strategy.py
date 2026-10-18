"""Donchian channel breakout with a long-term trend filter.

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

    @property
    def warmup(self) -> int:
        """Bars needed before every indicator has a value."""
        return max(self.entry_lookback, self.exit_lookback, self.trend_lookback, self.atr_lookback) + 1


def compute_signals(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
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
