"""What the live bot needs from a broker. MT5Broker implements it; tests use a fake."""

from dataclasses import dataclass
from typing import Protocol

import pandas as pd


class BrokerError(RuntimeError):
    """The broker refused a request or the connection failed."""


@dataclass(frozen=True)
class Account:
    login: int
    equity: float
    balance: float
    currency: str
    is_real: bool  # False for demo and contest accounts


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    contract_size: float  # ounces per lot, usually 100 for XAUUSD
    volume_min: float  # lots
    volume_max: float
    volume_step: float
    digits: int
    point: float
    stops_level: int  # minimum stop distance from price, in points
    swap_long: float = 0.0  # as reported by the broker, in its swap_mode units
    swap_short: float = 0.0


@dataclass(frozen=True)
class Tick:
    time: pd.Timestamp  # broker server time
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Position:
    ticket: int
    side: int  # +1 long, -1 short
    volume: float  # lots
    price_open: float
    sl: float  # 0 when no stop is set
    magic: int
    profit: float = 0.0


@dataclass(frozen=True)
class Fill:
    ticket: int  # position ticket
    price: float
    volume: float


@dataclass(frozen=True)
class ClosedTrade:
    exit_price: float
    exit_time: pd.Timestamp
    profit: float  # including swap and commission


class Broker(Protocol):
    def account(self) -> Account: ...

    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    def tick(self, symbol: str) -> Tick: ...

    def price_bars(self, symbol: str, count: int, timeframe: str = "D1") -> pd.DataFrame:
        """Completed bars only (the forming bar excluded), oldest first. timeframe: D1, H4 or H1."""
        ...

    def current_bar_open(self, symbol: str, timeframe: str = "D1") -> pd.Timestamp:
        """Server time at which the still-forming bar of `timeframe` opened."""
        ...

    def positions(self, symbol: str) -> list[Position]: ...

    def market_order(self, symbol: str, side: int, volume: float, sl: float, magic: int, comment: str) -> Fill: ...

    def close_position(self, symbol: str, position: Position, comment: str) -> Fill: ...

    def set_stop(self, symbol: str, position: Position, sl: float) -> None: ...

    def closed_trade(self, ticket: int) -> ClosedTrade | None:
        """How a position that is no longer open was closed, if the broker knows."""
        ...
