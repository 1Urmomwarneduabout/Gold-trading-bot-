"""Loading and cleaning gold price data.

Accepts CSV exports from Dukascopy (incl. dukascopy-node), MetaTrader 5,
and most brokers: any file with a date/time column plus open, high, low,
close. Intraday files are resampled to daily bars.
"""

import re
import warnings

import numpy as np
import pandas as pd

ALIASES = {
    "date": ["date", "datetime", "time", "timestamp", "gmt time", "local time", "utc"],
    "open": ["open", "o"],
    "high": ["high", "h"],
    "low": ["low", "l"],
    "close": ["close", "c", "last"],
    "volume": ["volume", "vol", "tickvol", "tick_volume", "v"],
}


def _normalise_name(name: str) -> str:
    return name.strip().strip("<>").strip().lower()


def _parse_timestamps(s: pd.Series) -> pd.DatetimeIndex:
    s = s.astype(str).str.strip()
    sample = s.iloc[0]
    if s.str.fullmatch(r"\d{12,13}").all():  # epoch milliseconds (dukascopy-node json/csv)
        return pd.DatetimeIndex(pd.to_datetime(s.astype("int64"), unit="ms"))
    if re.match(r"^\d{2}\.\d{2}\.\d{4}", sample):  # Dukascopy: 02.01.2024 00:00:00.000 GMT+0000
        s = s.str.replace(r"\s*GMT([+-]\d{4})$", r"\1", regex=True)
        parsed = pd.to_datetime(s, format="mixed", dayfirst=True, utc=True)
    else:
        if re.match(r"^\d{4}\.\d{2}\.\d{2}", sample):  # MetaTrader: 2024.01.02
            s = s.str.replace(".", "-", n=2, regex=False)
        parsed = pd.to_datetime(s, format="mixed", utc=True)
    return pd.DatetimeIndex(parsed).tz_convert(None)


def load_ohlc(path: str) -> pd.DataFrame:
    """Read a CSV into a DataFrame indexed by UTC timestamp with open/high/low/close[/volume]."""
    raw = pd.read_csv(path, sep=None, engine="python")
    names = {_normalise_name(col): col for col in raw.columns}

    found = {}
    for field, options in ALIASES.items():
        match = next((names[o] for o in options if o in names), None)
        if match is not None:
            found[field] = match
    missing = [f for f in ("open", "high", "low", "close") if f not in found]
    if missing:
        raise ValueError(f"{path}: could not find columns {missing}; got {list(raw.columns)}")

    # MetaTrader exports split date and time into two columns.
    if "date" in names and "time" in names:
        stamps = raw[names["date"]].astype(str) + " " + raw[names["time"]].astype(str)
    elif "date" in found:
        stamps = raw[found["date"]]
    else:
        raise ValueError(f"{path}: no date/time column found; got {list(raw.columns)}")

    cols = [f for f in ("open", "high", "low", "close", "volume") if f in found]
    df = pd.DataFrame({f: pd.to_numeric(raw[found[f]], errors="coerce") for f in cols})
    df.index = _parse_timestamps(stamps)
    df.index.name = "time"
    return clean(df)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    # Dukascopy pads closed-market hours with flat zero-volume candles.
    if "volume" in df:
        df = df[~((df["volume"] == 0) & (df["high"] == df["low"]))]
    bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad.any():
        warnings.warn(f"Dropping {int(bad.sum())} bars where high/low do not contain open/close.")
        df = df[~bad]
    return df


def is_intraday(df: pd.DataFrame) -> bool:
    return len(df) > 1 and df.index.to_series().diff().median() < pd.Timedelta(hours=20)


def to_daily(df: pd.DataFrame, roll_hour_utc: int = 22) -> pd.DataFrame:
    """Resample intraday bars to daily bars that roll at `roll_hour_utc`.

    Gold's trading day ends at 17:00 New York time, which is 22:00 UTC in
    winter and 21:00 UTC in summer. Bars after the roll belong to the next day,
    so Sunday evening's open becomes part of Monday's bar.
    """
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df:
        agg["volume"] = "sum"
    day = (df.index + pd.Timedelta(hours=24 - roll_hour_utc)).floor("D")
    daily = df.groupby(day).agg(agg)
    daily.index.name = "time"
    return daily


def load_daily(path: str, roll_hour_utc: int = 22) -> pd.DataFrame:
    df = load_ohlc(path)
    return to_daily(df, roll_hour_utc) if is_intraday(df) else df


TIMEFRAME_HOURS = {"H1": 1, "H4": 4}


def to_hours(df: pd.DataFrame, hours: int) -> pd.DataFrame:
    """Resample intraday bars to `hours`-hour bars aligned to midnight of the file's clock."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df:
        agg["volume"] = "sum"
    out = df.groupby(df.index.floor(f"{hours}h")).agg(agg)
    out.index.name = "time"
    return out


def load_bars(path: str, timeframe: str = "D1", roll_hour_utc: int = 22) -> pd.DataFrame:
    """Load a CSV as bars of `timeframe`: D1 (daily), H4 or H1."""
    if timeframe == "D1":
        return load_daily(path, roll_hour_utc)
    if timeframe not in TIMEFRAME_HOURS:
        raise ValueError(f"Unknown timeframe {timeframe!r}; use D1, H4 or H1")
    df = load_ohlc(path)
    step = df.index.to_series().diff().median()
    if step > pd.Timedelta(hours=TIMEFRAME_HOURS[timeframe]):
        raise ValueError(f"{path} has bars about {step} apart, too coarse for {timeframe}")
    return to_hours(df, TIMEFRAME_HOURS[timeframe])


def synthetic_daily(n_days: int = 3000, seed: int = 42, start_price: float = 1800.0) -> pd.DataFrame:
    """SYNTHETIC random prices for tests and demos. Not real gold data.

    A random walk with drifting trend regimes, so the strategy has something
    to trade. Results on it say nothing about real performance.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2010-01-04", periods=n_days)
    regime_drift = np.repeat(rng.normal(0, 0.0012, n_days // 120 + 1), 120)[:n_days]
    log_rets = regime_drift + rng.normal(0, 0.011, n_days)
    close = start_price * np.exp(np.cumsum(log_rets))
    open_ = np.concatenate([[start_price], close[:-1]]) * np.exp(rng.normal(0, 0.002, n_days))
    wick = np.abs(rng.normal(0, 0.006, n_days)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.006, n_days)) * close
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=pd.DatetimeIndex(dates, name="time"))
