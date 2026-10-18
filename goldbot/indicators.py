"""Technical indicators. Every function only looks backwards, so no future data leaks in."""

import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Average True Range with Wilder smoothing."""
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def channel_high(df: pd.DataFrame, n: int) -> pd.Series:
    """Highest high of the previous n bars, excluding the current bar."""
    return df["high"].rolling(n, min_periods=n).max().shift(1)


def channel_low(df: pd.DataFrame, n: int) -> pd.Series:
    """Lowest low of the previous n bars, excluding the current bar."""
    return df["low"].rolling(n, min_periods=n).min().shift(1)
