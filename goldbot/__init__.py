"""Gold (XAUUSD) trend-following research and trading toolkit."""

from .backtest import BacktestResult, CostConfig, run_backtest
from .data import load_daily, load_ohlc, to_daily
from .risk import RiskConfig, RiskGuard, position_size
from .strategy import StrategyConfig, compute_signals
from .validate import monte_carlo, parameter_sweep, walk_forward

__all__ = [
    "BacktestResult",
    "CostConfig",
    "RiskConfig",
    "RiskGuard",
    "StrategyConfig",
    "compute_signals",
    "load_daily",
    "load_ohlc",
    "monte_carlo",
    "parameter_sweep",
    "position_size",
    "run_backtest",
    "to_daily",
    "walk_forward",
]
