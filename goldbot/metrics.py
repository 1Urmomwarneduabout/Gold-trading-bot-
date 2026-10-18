"""Performance statistics for an equity curve and its trades."""

import math

import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough fall, as a negative fraction."""
    return float((equity / equity.cummax() - 1).min())


def compute_metrics(equity: pd.Series, trades: pd.DataFrame) -> dict:
    nan = float("nan")
    rets = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total_return = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 and total_return > -1 else nan
    std = rets.std()
    downside = rets[rets < 0].std()
    mdd = max_drawdown(equity)

    m = {
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
        "final_equity": float(equity.iloc[-1]),
        "total_return": total_return,
        "cagr": cagr,
        "volatility": std * math.sqrt(TRADING_DAYS) if len(rets) > 1 else nan,
        "sharpe": rets.mean() / std * math.sqrt(TRADING_DAYS) if std > 0 else nan,
        "sortino": rets.mean() / downside * math.sqrt(TRADING_DAYS) if downside > 0 else nan,
        "max_drawdown": mdd,
        "mar": cagr / abs(mdd) if mdd < 0 and not math.isnan(cagr) else nan,
        "trades": len(trades),
    }

    if len(trades):
        pnl = trades["pnl"]
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        m.update(
            win_rate=len(wins) / len(pnl),
            avg_win=wins.mean() if len(wins) else nan,
            avg_loss=losses.mean() if len(losses) else nan,
            profit_factor=wins.sum() / -losses.sum() if losses.sum() < 0 else nan,
            expectancy_r=trades["r_multiple"].mean(),
        )
    else:
        m.update(win_rate=nan, avg_win=nan, avg_loss=nan, profit_factor=nan, expectancy_r=nan)
    return m


def format_metrics(m: dict) -> str:
    pct = {"total_return", "cagr", "volatility", "max_drawdown", "win_rate", "exposure", "prob_loss"}
    lines = []
    for key, value in m.items():
        if isinstance(value, float) and math.isnan(value):
            shown = "n/a"
        elif key in pct or key.startswith(("return_p", "max_dd_p")):
            shown = f"{value:.2%}"
        elif isinstance(value, float):
            shown = f"{value:,.2f}"
        else:
            shown = str(value)
        lines.append(f"  {key:<15} {shown}")
    return "\n".join(lines)
