from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from goldbot.broker import Account, BrokerError, ClosedTrade, Fill, Position, SymbolSpec, Tick
from goldbot.live import LiveConfig, LiveTrader, SafetyError, load_config, lots_for, reset_halt
from goldbot.mt5_broker import MT5Broker
from goldbot.notify import Notifier
from goldbot.risk import RiskConfig
from goldbot.state import load_state
from goldbot.strategy import StrategyConfig, compute_signals

SMALL = StrategyConfig(entry_lookback=3, exit_lookback=2, trend_lookback=3, atr_lookback=3, stop_atr=2.0, allow_short=False)
# Flat base, breakout close on bar 6, rally, then bar 11 closes below the 2-bar low (exit).
BREAKOUT = [100, 100, 100, 100, 100, 100, 103, 105, 107, 109, 111, 106, 104, 103]
SPEC = SymbolSpec("XAUUSD", contract_size=100, volume_min=0.01, volume_max=50, volume_step=0.01, digits=2, point=0.01, stops_level=10)


def from_closes(closes, wick=0.5):
    closes = np.asarray(closes, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(opens, closes) + wick, "low": np.minimum(opens, closes) - wick, "close": closes},
        index=pd.bdate_range("2024-01-01", periods=len(closes), name="time"),
    )


class FakeBroker:
    def __init__(self, upto, closes=BREAKOUT, equity=100_000.0, is_real=False, spread=0.3, minutes_since_open=120):
        self.all_bars = from_closes(closes)
        self.n = upto  # bars completed so far; next_bar() completes one more
        self.equity = equity
        self.is_real = is_real
        self.spread = spread
        self.minutes_since_open = minutes_since_open
        self.open_positions: list[Position] = []
        self.orders, self.closes, self.stops = [], [], []
        self.history: dict[int, ClosedTrade] = {}
        self.next_ticket = 1000

    @property
    def bars(self):
        return self.all_bars.iloc[: self.n]

    def next_bar(self):
        self.n += 1

    def account(self):
        return Account(login=1, equity=self.equity, balance=self.equity, currency="USD", is_real=self.is_real)

    def symbol_spec(self, symbol):
        return SPEC

    def current_bar_open(self, symbol, timeframe="D1"):
        return self.bars.index[-1] + pd.offsets.BDay()

    def tick(self, symbol):
        mid = float(self.bars["close"].iloc[-1])
        now = self.current_bar_open(symbol) + pd.Timedelta(minutes=self.minutes_since_open)
        return Tick(time=now, bid=mid - self.spread / 2, ask=mid + self.spread / 2)

    def price_bars(self, symbol, count, timeframe="D1"):
        return self.bars.iloc[-count:]

    def positions(self, symbol):
        return list(self.open_positions)

    def market_order(self, symbol, side, volume, sl, magic, comment):
        t = self.tick(symbol)
        price = t.ask if side == 1 else t.bid
        self.next_ticket += 1
        self.open_positions.append(Position(self.next_ticket, side, volume, price, sl, magic))
        self.orders.append(dict(side=side, volume=volume, sl=sl, price=price))
        return Fill(self.next_ticket, price, volume)

    def close_position(self, symbol, position, comment):
        t = self.tick(symbol)
        price = t.bid if position.side == 1 else t.ask
        self.open_positions = [p for p in self.open_positions if p.ticket != position.ticket]
        self.closes.append(dict(ticket=position.ticket, price=price, comment=comment))
        return Fill(position.ticket, price, position.volume)

    def set_stop(self, symbol, position, sl):
        self.open_positions = [replace(p, sl=sl) if p.ticket == position.ticket else p for p in self.open_positions]
        self.stops.append((position.ticket, sl))

    def closed_trade(self, ticket):
        return self.history.get(ticket)


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.messages = []

    def send(self, text, level=20):
        self.messages.append(text)


def make(tmp_path, broker, **cfg):
    live = LiveConfig(state_dir=str(tmp_path / "state"), **cfg)
    return LiveTrader(broker, live, SMALL, RiskConfig(), RecordingNotifier())


def entry_broker(**kw):
    return FakeBroker(7, **kw)  # last completed bar is the breakout


# --- entries -------------------------------------------------------------------


def test_enters_on_breakout_with_server_side_stop(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker)
    outcome = trader.run_once()

    assert outcome.startswith("opened long")
    assert len(broker.orders) == 1
    order = broker.orders[0]
    atr = compute_signals(broker.bars, SMALL)["atr"].iloc[-1]
    assert order["sl"] == pytest.approx(round(order["price"] - 2 * atr, 2))
    assert order["volume"] == lots_for(100_000, order["price"], 2 * atr, SPEC, RiskConfig())
    state = load_state(trader.cfg.state_file)
    assert state.position.ticket == broker.open_positions[0].ticket
    assert state.last_bar == broker.bars.index[-1].isoformat()
    assert "entry" in trader.cfg.journal_file.read_text()


def test_acts_once_per_bar(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker)
    trader.run_once()
    assert trader.run_once() == "up to date"
    assert len(broker.orders) == 1


def test_waits_for_spreads_to_settle_after_open(tmp_path):
    broker = entry_broker(minutes_since_open=30)
    trader = make(tmp_path, broker)
    assert "waiting" in trader.run_once()
    assert not broker.orders
    assert load_state(trader.cfg.state_file).last_bar is None


def test_wide_spread_postpones_then_skips_entry(tmp_path):
    broker = entry_broker(spread=3.0)
    trader = make(tmp_path, broker)
    assert "spread" in trader.run_once()
    assert not broker.orders
    broker.minutes_since_open = 7 * 60  # past max_wait_hours
    trader.run_once()
    assert not broker.orders
    assert load_state(trader.cfg.state_file).last_bar is not None
    assert any("Skipped entry" in m for m in trader.notify.messages)


def test_too_small_account_skips_and_warns(tmp_path):
    broker = entry_broker(equity=50.0)
    trader = make(tmp_path, broker)
    assert "skipped" in trader.run_once()
    assert not broker.orders


def test_dry_run_sends_nothing(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker, dry_run=True)
    assert trader.run_once().startswith("DRY RUN")
    assert not broker.orders
    assert "dry_run_entry" in trader.cfg.journal_file.read_text()


def test_refuses_real_account_unless_allowed(tmp_path):
    with pytest.raises(SafetyError):
        make(tmp_path, entry_broker(is_real=True)).run_once()
    broker = entry_broker(is_real=True)
    make(tmp_path / "b", broker, allow_live=True).run_once()
    assert broker.orders


# --- exits and reconciliation -----------------------------------------------------


def test_channel_exit_closes_position(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker)
    trader.run_once()
    outcomes = []
    for _ in range(5):  # bars 7..11; bar 11 closes below the 2-bar low
        broker.next_bar()
        outcomes.append(trader.run_once())
    assert outcomes[:-1] == ["no trade"] * 4
    assert "channel_exit" in outcomes[-1]
    assert len(broker.closes) == 1 and not broker.open_positions
    assert load_state(trader.cfg.state_file).position is None


def test_position_stopped_out_on_broker_is_recorded(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker)
    trader.run_once()
    ticket = broker.open_positions[0].ticket
    broker.open_positions = []
    broker.history[ticket] = ClosedTrade(exit_price=95.0, exit_time=pd.Timestamp("2024-01-10"), profit=-900.0)
    trader.run_once()
    assert load_state(trader.cfg.state_file).position is None
    assert "closed_by_broker" in trader.cfg.journal_file.read_text()
    assert any("closed on the broker side" in m for m in trader.notify.messages)


def test_unknown_position_halts_without_trading(tmp_path):
    broker = entry_broker()
    broker.open_positions.append(Position(555, 1, 0.5, 100.0, 90.0, magic=LiveConfig().magic))
    trader = make(tmp_path, broker)
    assert trader.run_once().startswith("halted")
    assert not broker.orders and not broker.closes
    assert load_state(trader.cfg.state_file).halted


def test_manual_trades_are_ignored(tmp_path):
    broker = entry_broker()
    broker.open_positions.append(Position(555, -1, 1.0, 100.0, 0.0, magic=0))
    trader = make(tmp_path, broker)
    assert trader.run_once().startswith("opened long")


def test_missing_stop_is_restored(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker)
    trader.run_once()
    pos = broker.open_positions[0]
    broker.open_positions = [replace(pos, sl=0.0)]
    trader.run_once()
    assert broker.stops == [(pos.ticket, pos.sl)]


def test_drawdown_kill_switch_closes_and_stays_halted(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker)
    trader.run_once()
    broker.equity = 75_000.0  # 25% below the peak
    assert trader.run_once().startswith("halted")
    assert len(broker.closes) == 1 and not broker.open_positions
    broker.next_bar()
    trader.run_once()
    assert not broker.open_positions and len(broker.orders) == 1

    reset_halt(broker, trader.cfg)
    state = load_state(trader.cfg.state_file)
    assert not state.halted and state.equity_peak == 75_000.0


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    broker = FakeBroker(6)  # no signal on the first pass
    trader = make(tmp_path, broker)
    trader.run_once()
    broker.next_bar()  # breakout bar
    broker.equity = 96_000.0  # lost 4% since the last daily decision
    assert trader.run_once() == "no trade"
    assert not broker.orders


# --- sizing and config ----------------------------------------------------------------


def test_lots_for_respects_broker_volume_rules():
    risk = RiskConfig(risk_per_trade=0.01, max_leverage=100)
    assert lots_for(100_000, 2000, 50, SPEC, risk) == pytest.approx(0.2)  # $1000 / $50 / 100 oz
    assert lots_for(100_000, 2000, 70, SPEC, risk) == pytest.approx(0.14)
    assert lots_for(1_000, 2000, 50, SPEC, risk) == 0.0  # 0.002 lots < 0.01 minimum
    assert lots_for(1e9, 2000, 50, SPEC, risk) == SPEC.volume_max


def test_example_config_loads():
    live, strategy, risk = load_config("live.example.toml")
    assert live.symbol == "XAUUSD" and not live.allow_live
    assert strategy == StrategyConfig()
    assert risk.max_drawdown == 0.20


def test_config_typo_is_an_error(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[risk]\nrisk_per_trad = 0.01\n")
    with pytest.raises(ValueError, match="risk_per_trad"):
        load_config(path)


# --- MT5 adapter against a fake MetaTrader5 module ---------------------------------------


def fake_mt5(filling_mode=2, check_retcode=0):
    sent = []
    module = SimpleNamespace(
        TIMEFRAME_D1=16408, POSITION_TYPE_BUY=0, ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1,
        TRADE_ACTION_DEAL=1, TRADE_ACTION_SLTP=6, ORDER_TIME_GTC=0,
        ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1, ORDER_FILLING_RETURN=2, TRADE_RETCODE_DONE=10009,
        sent=sent,
        last_error=lambda: (1, "fake error"),
        symbol_info=lambda s: SimpleNamespace(filling_mode=filling_mode),
        symbol_info_tick=lambda s: SimpleNamespace(time=1_704_200_000, bid=2050.10, ask=2050.40),
        order_check=lambda r: SimpleNamespace(retcode=check_retcode, comment="No money"),
        account_info=lambda: SimpleNamespace(login=42, equity=10_000.0, balance=10_000.0, currency="USD", trade_mode=0),
        positions_get=lambda symbol: [
            SimpleNamespace(ticket=7, type=1, volume=0.1, price_open=2000.0, sl=2050.0, magic=99, profit=-12.0)
        ],
    )
    module.order_send = lambda r: (sent.append(r), SimpleNamespace(retcode=10009, order=777, price=2050.45, volume=r["volume"], comment="done"))[1]
    return module


def test_mt5_market_order_request():
    mt5 = fake_mt5()
    fill = MT5Broker(mt5).market_order("XAUUSD", 1, 0.25, 2000.0, 99, "goldbot entry")
    req = mt5.sent[0]
    assert req["type"] == mt5.ORDER_TYPE_BUY and req["price"] == 2050.40
    assert req["sl"] == 2000.0 and req["volume"] == 0.25 and req["magic"] == 99
    assert req["type_filling"] == mt5.ORDER_FILLING_IOC
    assert fill == Fill(777, 2050.45, 0.25)


def test_mt5_close_uses_opposite_side_and_position_ticket():
    mt5 = fake_mt5(filling_mode=1)
    broker = MT5Broker(mt5)
    short = broker.positions("XAUUSD")[0]
    assert short.side == -1
    broker.close_position("XAUUSD", short, "goldbot channel_exit")
    req = mt5.sent[0]
    assert req["type"] == mt5.ORDER_TYPE_BUY and req["position"] == 7 and req["price"] == 2050.40
    assert req["type_filling"] == mt5.ORDER_FILLING_FOK


def test_mt5_rejected_order_raises():
    with pytest.raises(BrokerError, match="No money"):
        MT5Broker(fake_mt5(check_retcode=10019)).market_order("XAUUSD", 1, 0.25, 2000.0, 99, "x")


def test_mt5_demo_account_detected():
    assert MT5Broker(fake_mt5()).account().is_real is False


# --- trailing stop ------------------------------------------------------------------


def make_trailing(tmp_path, broker, trail_atr=1.0, **cfg):
    live = LiveConfig(state_dir=str(tmp_path / "state"), **cfg)
    return LiveTrader(broker, live, replace(SMALL, trail_atr=trail_atr), RiskConfig(), RecordingNotifier())


def test_trailing_stop_ratchets_server_stop(tmp_path):
    broker = entry_broker()
    trader = make_trailing(tmp_path, broker)
    trader.run_once()
    entry_sl = broker.orders[0]["sl"]
    for _ in range(3):  # bars 7..9 keep rallying
        broker.next_bar()
        assert "trailed stop" in trader.run_once()
    sls = [sl for _, sl in broker.stops]
    assert len(sls) == 3 and entry_sl < sls[0] < sls[1] < sls[2]
    assert broker.open_positions[0].sl == sls[-1]
    assert load_state(trader.cfg.state_file).position.sl == sls[-1]
    assert "trail_stop" in trader.cfg.journal_file.read_text()


def test_trailing_stop_never_loosens(tmp_path):
    broker = entry_broker()
    trader = make_trailing(tmp_path, broker)
    trader.run_once()
    broker.next_bar()
    trader.run_once()
    tightened = load_state(trader.cfg.state_file).position.sl
    trader.strategy = replace(trader.strategy, trail_atr=50.0)  # would put the stop far below
    broker.next_bar()
    trader.run_once()
    assert load_state(trader.cfg.state_file).position.sl == tightened
    assert len(broker.stops) == 1


def test_trailing_off_leaves_stop_alone(tmp_path):
    broker = entry_broker()
    trader = make(tmp_path, broker)
    trader.run_once()
    for _ in range(3):
        broker.next_bar()
        trader.run_once()
    assert broker.stops == []


def test_trailing_dry_run_sends_nothing(tmp_path):
    broker = entry_broker()
    trader = make_trailing(tmp_path, broker)
    trader.run_once()
    trader.cfg = replace(trader.cfg, dry_run=True)
    broker.next_bar()
    assert "DRY RUN: would trail" in trader.run_once()
    assert broker.stops == []


def test_save_state_retries_while_file_is_locked(tmp_path, monkeypatch):
    import os

    from goldbot import state as state_mod

    real_replace, calls = os.replace, []

    def flaky_replace(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("locked by another program")
        real_replace(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", flaky_replace)
    monkeypatch.setattr(state_mod.time, "sleep", lambda s: None)
    state_mod.save_state(tmp_path / "state.json", state_mod.BotState(last_bar="x"))
    assert len(calls) == 3
    assert load_state(tmp_path / "state.json").last_bar == "x"