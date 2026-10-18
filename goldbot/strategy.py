"""Donchian channel breakout with a long-term trend filter.

Rules, evaluated on each daily close and executed at the next open:
  * Go long when the close breaks above the highest high of the last
    `entry_lookback` days AND the close is above its `trend_lookback` SMA.
  * Go short (optional) on the mirror image: close below the lowest low
    of the last `entry_lookback` days AND below the trend SMA.
  * Exit a long when the close falls below the lowest low of the last
    `exit_lookback` days (mirror for shorts).
  * Protective stop at `stop_atr` x ATR from the fill price, checked intraday.
"""

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
