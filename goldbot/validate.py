"""Checks that separate a robust strategy from a curve-fitted one."""

import itertools
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from .backtest import BacktestResult, CostConfig, run_backtest
from .metrics import compute_metrics
from .risk import RiskConfig
from .strategy import StrategyConfig


def _combos(grid: dict[str, list], base: StrategyConfig | None = None) -> list[dict]:
    """Every combination in the grid, minus ones `base` rejects (e.g. fast_ma >= slow_ma)."""
    keys = list(grid)
    combos = [dict(zip(keys, values)) for values in itertools.product(*grid.values())]
    if base is None:
        return combos
    valid = []
    for combo in combos:
        try:
            replace(base, **combo)
        except ValueError:
            continue
        valid.append(combo)
    return valid


def _score(result: BacktestResult, objective: str, min_trades: int) -> float:
    value = result.metrics.get(objective, float("nan"))
    if result.metrics["trades"] < min_trades or value is None or np.isnan(value):
        return float("-inf")
    return float(value)


def parameter_sweep(
    df: pd.DataFrame,
    base: StrategyConfig,
    grid: dict[str, list],
    costs: CostConfig = CostConfig(),
    risk: RiskConfig = RiskConfig(),
    **kwargs,
) -> pd.DataFrame:
    """Backtest every parameter combination.

    Look for a broad plateau of decent results. A single sharp peak
    surrounded by losers means the "best" setting is luck.
    """
    rows = []
    for combo in _combos(grid, base):
        result = run_backtest(df, replace(base, **combo), costs, risk, **kwargs)
        m = result.metrics
        rows.append({**combo, **{k: m[k] for k in ("cagr", "sharpe", "max_drawdown", "mar", "trades", "win_rate")}})
    return pd.DataFrame(rows)


@dataclass
class WalkForwardResult:
    windows: pd.DataFrame
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict


def walk_forward(
    df: pd.DataFrame,
    base: StrategyConfig,
    grid: dict[str, list],
    costs: CostConfig = CostConfig(),
    risk: RiskConfig = RiskConfig(),
    train_years: float = 4,
    test_years: float = 1,
    objective: str = "mar",
    min_trades: int = 5,
    initial_equity: float = 10_000.0,
    roll_hour_utc: int = 22,
) -> WalkForwardResult:
    """Optimise on a training window, trade the next unseen window, roll forward.

    Only the out-of-sample (test) windows are stitched into the final equity
    curve, so the result is an honest estimate of trading the optimiser live.
    Positions are closed at the end of each test window.
    """
    combos = _combos(grid, base)
    warmup = max(replace(base, **combo).warmup for combo in combos)
    if len(df) <= warmup:
        raise ValueError("Not enough data for indicator warm-up.")

    train_len = pd.DateOffset(days=round(train_years * 365.25))
    test_len = pd.DateOffset(days=round(test_years * 365.25))
    train_start = df.index[warmup]
    equity = initial_equity
    windows, curves, trades = [], [], []

    while True:
        train_end = train_start + train_len
        test_end = train_end + test_len
        if train_end >= df.index[-1]:
            break

        best_combo, best_score = None, float("-inf")
        for combo in combos:
            result = run_backtest(df, replace(base, **combo), costs, risk, start=train_start, end=train_end, roll_hour_utc=roll_hour_utc)
            score = _score(result, objective, min_trades)
            if score > best_score:
                best_combo, best_score = combo, score

        if best_combo is None:
            windows.append({"train_start": train_start.date(), "train_end": train_end.date(), "params": None})
        else:
            test_start = train_end + pd.Timedelta(days=1)
            try:
                test = run_backtest(
                    df,
                    replace(base, **best_combo),
                    costs,
                    risk,
                    initial_equity=equity,
                    start=test_start,
                    end=test_end,
                    roll_hour_utc=roll_hour_utc,
                )
            except ValueError:  # too few bars left in the final window
                break
            curves.append(test.equity)
            trades.append(test.trades)
            equity = float(test.equity.iloc[-1])
            windows.append(
                {
                    "train_start": train_start.date(),
                    "train_end": train_end.date(),
                    "test_end": min(test_end, df.index[-1]).date(),
                    "params": best_combo,
                    "train_score": best_score,
                    "test_return": test.metrics["total_return"],
                    "test_max_dd": test.metrics["max_drawdown"],
                    "test_trades": test.metrics["trades"],
                }
            )
        train_start = train_start + test_len

    if not curves:
        raise ValueError("No walk-forward window produced a test period; use more data or shorter windows.")

    oos_equity = pd.concat(curves)
    oos_trades = pd.concat(trades, ignore_index=True)
    return WalkForwardResult(pd.DataFrame(windows), oos_equity, oos_trades, compute_metrics(oos_equity, oos_trades))


def monte_carlo(
    trades: pd.DataFrame,
    n_sims: int = 5000,
    initial_equity: float = 10_000.0,
    seed: int = 0,
) -> dict:
    """Bootstrap the trade sequence to see the range of outcomes luck alone produces.

    Use the 95th-percentile drawdown, not the backtest's single drawdown, to
    decide whether you can stomach the strategy, and as the live alarm level:
    a live drawdown beyond it suggests the edge has gone.
    """
    if trades.empty:
        raise ValueError("No trades to resample.")
    rets = trades["return_on_equity"].to_numpy(float)
    rng = np.random.default_rng(seed)
    samples = rng.choice(rets, size=(n_sims, len(rets)), replace=True)
    paths = initial_equity * np.cumprod(1 + samples, axis=1)
    paths = np.hstack([np.full((n_sims, 1), initial_equity), paths])
    drawdowns = (paths / np.maximum.accumulate(paths, axis=1) - 1).min(axis=1)
    final_returns = paths[:, -1] / initial_equity - 1
    return {
        "sims": n_sims,
        "trades_per_sim": len(rets),
        "return_p5": float(np.percentile(final_returns, 5)),
        "return_p50": float(np.percentile(final_returns, 50)),
        "return_p95": float(np.percentile(final_returns, 95)),
        "max_dd_p50": float(np.percentile(drawdowns, 50)),
        "max_dd_p95": float(np.percentile(drawdowns, 5)),  # drawdowns are negative
        "max_dd_p99": float(np.percentile(drawdowns, 1)),
        "prob_loss": float((final_returns < 0).mean()),
    }
