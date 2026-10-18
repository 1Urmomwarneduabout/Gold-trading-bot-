"""Position sizing and account-level kill switches.

Kept separate from the backtester so the live bot can use exactly the same rules.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.01  # fraction of equity lost if the stop is hit
    max_leverage: float = 5.0  # cap on notional / equity
    lot_step: float = 1.0  # ounces; 0.01 lot of XAUUSD is 1 oz at most brokers
    max_daily_loss: float = 0.03  # no new entries after losing this much in a day
    max_drawdown: float = 0.20  # flatten and halt until a human reviews


def position_size(equity: float, price: float, stop_distance: float, cfg: RiskConfig) -> float:
    """Ounces to trade so that hitting the stop loses `risk_per_trade` of equity."""
    if equity <= 0 or price <= 0 or stop_distance <= 0:
        return 0.0
    units = equity * cfg.risk_per_trade / stop_distance
    units = min(units, equity * cfg.max_leverage / price)
    # The epsilon stops values like 2.9999999 rounding down a whole step.
    return math.floor(units / cfg.lot_step + 1e-9) * cfg.lot_step


class RiskGuard:
    def __init__(self, cfg: RiskConfig, equity: float):
        self.cfg = cfg
        self.peak = equity
        self.day_start = equity
        self.halted = False
        self.halt_reason = ""

    def start_day(self, equity: float) -> None:
        self.day_start = equity

    def update(self, equity: float) -> None:
        self.peak = max(self.peak, equity)
        if not self.halted and equity <= self.peak * (1 - self.cfg.max_drawdown):
            self.halted = True
            self.halt_reason = f"drawdown exceeded {self.cfg.max_drawdown:.0%}"

    def daily_loss_hit(self, equity: float) -> bool:
        return equity <= self.day_start * (1 - self.cfg.max_daily_loss)

    def can_open(self, equity: float) -> bool:
        return not self.halted and not self.daily_loss_hit(equity)
