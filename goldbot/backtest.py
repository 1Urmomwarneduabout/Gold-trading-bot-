"""Daily-bar backtester with realistic gold trading costs.

Prices are treated as mid prices. Buys fill at mid + half spread + slippage,
sells at mid - half spread - slippage. Signals come from the daily close and
fill at the next bar's open, so a trade never uses information it could not
have had.
"""

from dataclasses import asdict, dataclass, field

import pandas as pd

from .metrics import compute_metrics
from .risk import RiskConfig, RiskGuard, position_size
from .strategy import StrategyConfig, compute_signals, decide


@dataclass(frozen=True)
class CostConfig:
    spread: float = 0.40  # full bid/ask spread in USD per oz
    slippage: float = 0.10  # extra USD per oz on every fill
    commission_per_oz: float = 0.0  # per side
    # Overnight financing as an annual rate on notional; positive = you pay.
    # Check your broker's swap table: these vary a lot and change often.
    swap_long_annual: float = 0.06
    swap_short_annual: float = 0.02


@dataclass
class Trade:
    side: int  # +1 long, -1 short
    units: float  # ounces
    entry_date: pd.Timestamp
    entry_price: float
    stop: float
    equity_at_entry: float
    risk_amount: float
    commission: float = 0.0
    swap: float = 0.0
    exit_date: pd.Timestamp | None = None
    exit_price: float = float("nan")
    exit_reason: str = ""

    @property
    def gross_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.units * self.side

    @property
    def pnl(self) -> float:
        return self.gross_pnl - self.commission - self.swap


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict
    halted: bool = False
    halt_reason: str = ""
    config: dict = field(default_factory=dict)


def _trades_frame(trades: list[Trade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        row = asdict(t)
        row["pnl"] = t.pnl
        row["r_multiple"] = t.pnl / t.risk_amount if t.risk_amount else float("nan")
        row["return_on_equity"] = t.pnl / t.equity_at_entry
        rows.append(row)
    columns = list(Trade.__dataclass_fields__) + ["pnl", "r_multiple", "return_on_equity"]
    return pd.DataFrame(rows, columns=columns)


def run_backtest(
    df: pd.DataFrame,
    strategy: StrategyConfig = StrategyConfig(),
    costs: CostConfig = CostConfig(),
    risk: RiskConfig = RiskConfig(),
    initial_equity: float = 10_000.0,
    start: pd.Timestamp | str | None = None,
    end: pd.Timestamp | str | None = None,
) -> BacktestResult:
    """Run the strategy over daily OHLC bars.

    Indicators use all bars up to `end`, so passing an earlier `start` only
    limits when trading may happen; it never shortens indicator history.
    """
    signals = compute_signals(df, strategy)
    idx = df.index
    o, h, l, c = (df[col].to_numpy(float) for col in ("open", "high", "low", "close"))
    atr_v = signals["atr"].to_numpy(float)
    long_entry, short_entry = signals["long_entry"].to_numpy(), signals["short_entry"].to_numpy()
    long_exit, short_exit = signals["long_exit"].to_numpy(), signals["short_exit"].to_numpy()

    first = strategy.warmup - 1
    if start is not None:
        first = max(first, int(idx.searchsorted(pd.Timestamp(start))))
    last = len(idx) - 1 if end is None else int(idx.searchsorted(pd.Timestamp(end), side="right")) - 1
    if last <= first:
        raise ValueError("Not enough bars in the requested range after indicator warm-up.")

    half = costs.spread / 2 + costs.slippage
    cash = initial_equity
    pos: Trade | None = None
    pending: tuple | None = None  # ("enter", side, signal_atr) or ("exit", reason)
    closed: list[Trade] = []
    guard = RiskGuard(risk, initial_equity)
    equity_points = []
    bars_in_market = 0
    skipped_too_small = 0
    equity = initial_equity

    def close_position(trade: Trade, price: float, date, reason: str) -> None:
        nonlocal cash
        commission = costs.commission_per_oz * trade.units
        trade.commission += commission
        trade.exit_price, trade.exit_date, trade.exit_reason = price, date, reason
        cash += trade.gross_pnl - commission
        closed.append(trade)

    for i in range(first, last + 1):
        date = idx[i]
        guard.start_day(equity)

        # 1. Fill yesterday's order at today's open.
        if pending is not None:
            if pending[0] == "exit" and pos is not None:
                close_position(pos, o[i] - pos.side * half, date, pending[1])
                pos = None
            elif pending[0] == "enter" and pos is None:
                _, side, signal_atr = pending
                fill = o[i] + side * half
                stop_distance = strategy.stop_atr * signal_atr
                units = position_size(equity, fill, stop_distance, risk)
                skipped_too_small += units == 0
                if units > 0:
                    commission = costs.commission_per_oz * units
                    cash -= commission
                    pos = Trade(
                        side=side,
                        units=units,
                        entry_date=date,
                        entry_price=fill,
                        stop=fill - side * stop_distance,
                        equity_at_entry=equity,
                        risk_amount=units * stop_distance,
                        commission=commission,
                    )
            pending = None

        # 2. Protective stop. A gap through the stop fills at the open.
        if pos is not None:
            if pos.side == 1 and l[i] <= pos.stop:
                close_position(pos, min(o[i], pos.stop) - half, date, "stop")
                pos = None
            elif pos.side == -1 and h[i] >= pos.stop:
                close_position(pos, max(o[i], pos.stop) + half, date, "stop")
                pos = None

        # 3. Overnight financing for positions carried past today's close.
        if pos is not None and i < last:
            nights = (idx[i + 1] - date).days
            rate = costs.swap_long_annual if pos.side == 1 else costs.swap_short_annual
            swap = c[i] * pos.units * rate / 365 * nights
            cash -= swap
            pos.swap += swap

        unrealized = (c[i] - pos.entry_price) * pos.units * pos.side if pos is not None else 0.0
        equity = cash + unrealized
        guard.update(equity)
        bars_in_market += pos is not None

        # 4. Decide tomorrow's order from today's close.
        if i < last:
            row = {"long_entry": long_entry[i], "short_entry": short_entry[i], "long_exit": long_exit[i], "short_exit": short_exit[i], "atr": atr_v[i]}
            action = decide(pos.side if pos is not None else 0, row, guard.halted, guard.can_open(equity))
            if action is not None and action.kind == "exit":
                pending = ("exit", action.reason)
            elif action is not None:
                pending = ("enter", action.side, atr_v[i])
        elif pos is not None:
            close_position(pos, c[i] - pos.side * half, date, "end_of_data")
            pos = None
            equity = cash

        equity_points.append((date, equity))

    equity_series = pd.Series(dict(equity_points), name="equity")
    trades = _trades_frame(closed)
    metrics = compute_metrics(equity_series, trades)
    metrics["exposure"] = bars_in_market / len(equity_series)
    # Signals dropped because the risk budget was smaller than one lot step.
    metrics["skipped_signals"] = skipped_too_small
    return BacktestResult(
        equity=equity_series,
        trades=trades,
        metrics=metrics,
        halted=guard.halted,
        halt_reason=guard.halt_reason,
        config={"strategy": asdict(strategy), "costs": asdict(costs), "risk": asdict(risk)},
    )
