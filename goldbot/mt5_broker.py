"""MetaTrader 5 adapter using the official `MetaTrader5` Python package.

Requires Windows with the MT5 terminal installed, logged in to your broker,
and "Algo Trading" switched on. Install with: pip install -e ".[mt5]"
"""

import os

import pandas as pd

from .broker import Account, BrokerError, ClosedTrade, Fill, Position, SymbolSpec, Tick

# symbol_info().filling_mode bit flags (SYMBOL_FILLING_FOK / SYMBOL_FILLING_IOC in MQL5).
_FILLING_FOK = 1
_FILLING_IOC = 2
_DEAL_ENTRY_OUT = 1
_ACCOUNT_TRADE_MODE_REAL = 2


def _load_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise BrokerError(
            "The MetaTrader5 package is not installed. It only runs on Windows: pip install -e \".[mt5]\""
        ) from exc
    return mt5


class MT5Broker:
    def __init__(self, mt5=None, deviation_points: int = 50):
        self.mt5 = mt5 or _load_mt5()
        self.deviation_points = deviation_points

    # --- connection -------------------------------------------------------

    def connect(self) -> None:
        """Attach to the MT5 terminal.

        With no environment variables set, this uses whichever account the
        terminal is logged in to. Set MT5_LOGIN, MT5_PASSWORD and MT5_SERVER
        to log in explicitly, and MT5_PATH if terminal64.exe is not found.
        """
        kwargs = {}
        if os.environ.get("MT5_PATH"):
            kwargs["path"] = os.environ["MT5_PATH"]
        if os.environ.get("MT5_LOGIN"):
            kwargs.update(
                login=int(os.environ["MT5_LOGIN"]),
                password=os.environ.get("MT5_PASSWORD", ""),
                server=os.environ.get("MT5_SERVER", ""),
            )
        if not self.mt5.initialize(**kwargs):
            raise BrokerError(f"Could not connect to MetaTrader 5: {self.mt5.last_error()}")
        terminal = self.mt5.terminal_info()
        if terminal is None or not terminal.trade_allowed:
            raise BrokerError("Algo Trading is switched off in the MT5 terminal (toolbar button 'Algo Trading').")

    def ensure_connected(self) -> None:
        if self.mt5.terminal_info() is None:
            self.connect()

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def _check(self, value, what: str):
        if value is None:
            raise BrokerError(f"{what} failed: {self.mt5.last_error()}")
        return value

    # --- market data and account -------------------------------------------

    def account(self) -> Account:
        info = self._check(self.mt5.account_info(), "account_info")
        return Account(
            login=info.login,
            equity=float(info.equity),
            balance=float(info.balance),
            currency=info.currency,
            is_real=info.trade_mode == _ACCOUNT_TRADE_MODE_REAL,
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if not self.mt5.symbol_select(symbol, True):
            raise BrokerError(f"Symbol {symbol!r} not found. Check the exact name in Market Watch (e.g. XAUUSD, GOLD, XAUUSD.m).")
        info = self._check(self.mt5.symbol_info(symbol), "symbol_info")
        return SymbolSpec(
            name=symbol,
            contract_size=float(info.trade_contract_size),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            digits=int(info.digits),
            point=float(info.point),
            stops_level=int(info.trade_stops_level),
            swap_long=float(info.swap_long),
            swap_short=float(info.swap_short),
        )

    def tick(self, symbol: str) -> Tick:
        t = self._check(self.mt5.symbol_info_tick(symbol), "symbol_info_tick")
        return Tick(time=pd.Timestamp(t.time, unit="s"), bid=float(t.bid), ask=float(t.ask))

    def _rates(self, symbol: str, start_pos: int, count: int) -> pd.DataFrame:
        rates = self.mt5.copy_rates_from_pos(symbol, self.mt5.TIMEFRAME_D1, start_pos, count)
        if rates is None or len(rates) == 0:
            raise BrokerError(f"No daily bars for {symbol}: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        # MT5 bar times are broker server time, which most brokers set so that
        # the daily bar closes at 17:00 New York, matching gold's trading day.
        df.index = pd.DatetimeIndex(pd.to_datetime(df["time"], unit="s"), name="time")
        return df.rename(columns={"tick_volume": "volume"})[["open", "high", "low", "close", "volume"]]

    def daily_bars(self, symbol: str, count: int) -> pd.DataFrame:
        return self._rates(symbol, 1, count)  # position 0 is the bar still forming

    def current_bar_open(self, symbol: str) -> pd.Timestamp:
        return self._rates(symbol, 0, 1).index[-1]

    def positions(self, symbol: str) -> list[Position]:
        raw = self.mt5.positions_get(symbol=symbol)
        if raw is None:
            raise BrokerError(f"positions_get failed: {self.mt5.last_error()}")
        return [
            Position(
                ticket=p.ticket,
                side=1 if p.type == self.mt5.POSITION_TYPE_BUY else -1,
                volume=float(p.volume),
                price_open=float(p.price_open),
                sl=float(p.sl),
                magic=int(p.magic),
                profit=float(p.profit),
            )
            for p in raw
        ]

    # --- orders ---------------------------------------------------------------

    def _filling(self, symbol: str) -> int:
        mode = self._check(self.mt5.symbol_info(symbol), "symbol_info").filling_mode
        if mode & _FILLING_IOC:
            return self.mt5.ORDER_FILLING_IOC
        if mode & _FILLING_FOK:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN

    def _send(self, request: dict, what: str):
        check = self.mt5.order_check(request)
        if check is None or check.retcode != 0:
            detail = check.comment if check is not None else self.mt5.last_error()
            raise BrokerError(f"{what} rejected by order_check: {detail}")
        result = self.mt5.order_send(request)
        if result is None:
            raise BrokerError(f"{what} failed: {self.mt5.last_error()}")
        if result.retcode != self.mt5.TRADE_RETCODE_DONE:
            raise BrokerError(f"{what} rejected: retcode {result.retcode} {result.comment}")
        return result

    def market_order(self, symbol: str, side: int, volume: float, sl: float, magic: int, comment: str) -> Fill:
        tick = self.tick(symbol)
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": self.mt5.ORDER_TYPE_BUY if side == 1 else self.mt5.ORDER_TYPE_SELL,
            "price": tick.ask if side == 1 else tick.bid,
            "sl": float(sl),
            "deviation": self.deviation_points,
            "magic": magic,
            "comment": comment[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(symbol),
        }
        result = self._send(request, "Market order")
        # For a new position in netting and hedging modes alike, the order ticket is the position ticket.
        return Fill(ticket=int(result.order), price=float(result.price), volume=float(result.volume))

    def close_position(self, symbol: str, position: Position, comment: str) -> Fill:
        tick = self.tick(symbol)
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": position.volume,
            "type": self.mt5.ORDER_TYPE_SELL if position.side == 1 else self.mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": tick.bid if position.side == 1 else tick.ask,
            "deviation": self.deviation_points,
            "magic": position.magic,
            "comment": comment[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(symbol),
        }
        result = self._send(request, "Close order")
        return Fill(ticket=position.ticket, price=float(result.price), volume=float(result.volume))

    def set_stop(self, symbol: str, position: Position, sl: float) -> None:
        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": position.ticket,
            "sl": float(sl),
            "tp": 0.0,
        }
        self._send(request, "Stop-loss update")

    def closed_trade(self, ticket: int) -> ClosedTrade | None:
        deals = self.mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        exits = [d for d in deals if d.entry == _DEAL_ENTRY_OUT]
        if not exits:
            return None
        last = exits[-1]
        profit = sum(float(d.profit) + float(d.swap) + float(d.commission) for d in deals)
        return ClosedTrade(exit_price=float(last.price), exit_time=pd.Timestamp(last.time, unit="s"), profit=profit)
