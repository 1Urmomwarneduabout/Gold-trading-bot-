"""The live/paper trading loop.

Once per completed bar (daily by default, or H4/H1), the bot runs the same `compute_signals`,
`decide` and `position_size` code as the backtester. Every position carries
a stop-loss held on the broker's server, so it is protected even if this
program or the VPS goes down.
"""

import logging
import math
import time
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .broker import Account, Broker, BrokerError, Position, SymbolSpec, Tick
from .notify import Notifier, ping
from .risk import RiskConfig, RiskGuard, position_size
from .state import BotState, OpenTrade, journal, load_state, save_state
from .strategy import StrategyConfig, compute_signals, decide, trailed_stop

log = logging.getLogger("goldbot")


class SafetyError(RuntimeError):
    """Something a person must look at. The bot stops instead of guessing."""


@dataclass(frozen=True)
class LiveConfig:
    symbol: str = "XAUUSD"
    magic: int = 7861001  # tags this bot's orders so it ignores your manual trades
    timeframe: str = "D1"  # bar size the strategy runs on: D1, H4 or H1
    history_bars: int = 400
    wait_after_open_minutes: int = 60  # spreads are widest right after the daily rollover
    max_spread: float = 1.0  # USD per oz; don't trade while the spread is wider
    max_wait_hours: float = 6  # after this, exits go through anyway and entries are skipped
    deviation_points: int = 50  # allowed slippage on market orders
    allow_live: bool = False  # must be true to trade a real-money account
    dry_run: bool = False  # log the orders it would send, send nothing
    poll_seconds: int = 60
    state_dir: str = "state"
    heartbeat_url: str = ""  # optional healthchecks.io-style URL

    @property
    def state_file(self) -> Path:
        return Path(self.state_dir) / "state.json"

    @property
    def journal_file(self) -> Path:
        return Path(self.state_dir) / "journal.csv"

    @property
    def heartbeat_file(self) -> Path:
        return Path(self.state_dir) / "heartbeat.txt"


def load_config(path: str | Path) -> tuple[LiveConfig, StrategyConfig, RiskConfig]:
    """Read a TOML file with [live], [strategy] and [risk] sections. Unknown keys are errors."""
    data = tomllib.loads(Path(path).read_text())
    unknown_sections = set(data) - {"live", "strategy", "risk"}
    if unknown_sections:
        raise ValueError(f"Unknown config sections: {sorted(unknown_sections)}")

    def build(cls, section):
        values = data.get(section, {})
        allowed = {f.name for f in fields(cls)}
        typos = set(values) - allowed
        if typos:
            raise ValueError(f"Unknown keys in [{section}]: {sorted(typos)}. Allowed: {sorted(allowed)}")
        return cls(**values)

    return build(LiveConfig, "live"), build(StrategyConfig, "strategy"), build(RiskConfig, "risk")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lots_for(equity: float, price: float, stop_distance: float, spec: SymbolSpec, risk: RiskConfig) -> float:
    """Lots to trade, honouring the broker's volume step, minimum and maximum. 0 means too small."""
    ounces = position_size(equity, price, stop_distance, replace(risk, lot_step=spec.volume_step * spec.contract_size))
    lots = min(ounces / spec.contract_size, spec.volume_max)
    lots = math.floor(lots / spec.volume_step + 1e-9) * spec.volume_step
    lots = round(lots, 8)
    return lots if lots >= spec.volume_min else 0.0


class LiveTrader:
    def __init__(self, broker: Broker, cfg: LiveConfig, strategy: StrategyConfig, risk: RiskConfig, notifier: Notifier):
        self.broker = broker
        self.cfg = cfg
        self.strategy = strategy
        self.risk = risk
        self.notify = notifier

    # --- one pass -------------------------------------------------------------

    def run_once(self) -> str:
        cfg, broker = self.cfg, self.broker
        account = broker.account()
        if account.is_real and not cfg.allow_live:
            raise SafetyError(
                f"Account {account.login} is a REAL-money account and allow_live is false. "
                "Paper trade on a demo account first; set allow_live = true in [live] when you are ready."
            )
        spec = broker.symbol_spec(cfg.symbol)
        state = load_state(cfg.state_file)
        position = self._reconcile(state, account)

        guard = RiskGuard(self.risk, account.equity)
        guard.peak = max(state.equity_peak or account.equity, account.equity)
        guard.halted, guard.halt_reason = state.halted, state.halt_reason
        guard.update(account.equity)
        if guard.halted and not state.halted:
            self.notify.send(f"KILL SWITCH: {guard.halt_reason}. Closing positions and stopping.", logging.ERROR)
        state.halted, state.halt_reason, state.equity_peak = guard.halted, guard.halt_reason, guard.peak

        if guard.halted:
            if position is not None:
                self._close(state, position, "risk_halt", account)
            save_state(cfg.state_file, state)
            return f"halted: {state.halt_reason}. Run 'goldbot live reset-halt' after reviewing."

        bars = broker.price_bars(cfg.symbol, cfg.history_bars, cfg.timeframe)
        if len(bars) < self.strategy.warmup:
            raise BrokerError(
                f"Only {len(bars)} {cfg.timeframe} bars available, need {self.strategy.warmup}. "
                f"Scroll the {cfg.timeframe} chart back or raise 'Max bars in chart' in MT5 options."
            )
        bar = bars.index[-1].isoformat()
        if state.last_bar == bar:
            save_state(cfg.state_file, state)
            return "up to date"

        tick = broker.tick(cfg.symbol)
        since_open = tick.time - broker.current_bar_open(cfg.symbol, cfg.timeframe)
        if since_open < timedelta(minutes=cfg.wait_after_open_minutes):
            save_state(cfg.state_file, state)
            return f"new bar {bar}: waiting {cfg.wait_after_open_minutes} min after the open for spreads to settle"

        signal = compute_signals(bars, self.strategy).iloc[-1]
        guard.start_day(state.day_start_equity or account.equity)
        side = state.position.side if state.position else 0
        action = decide(side, signal, guard.halted, guard.can_open(account.equity))

        if action is not None and tick.spread > cfg.max_spread:
            if since_open < timedelta(hours=cfg.max_wait_hours):
                save_state(cfg.state_file, state)
                return f"signal {action.kind} waiting: spread {tick.spread:.2f} > max {cfg.max_spread:.2f}"
            if action.kind == "enter":
                self.notify.send(f"Skipped entry on {bar}: spread stayed above {cfg.max_spread:.2f} for {cfg.max_wait_hours}h.")
                journal(cfg.journal_file, time=_now(), event="skip_spread", side=action.side, note=bar)
                action = None

        if action is None:
            outcome = "no trade"
        elif action.kind == "exit" and position is not None:
            outcome = self._close(state, position, action.reason, account)
        elif action.kind == "exit":
            outcome = "no trade"
        else:
            outcome = self._enter(state, action.side, float(signal["atr"]), spec, tick, account, bar)

        if position is not None and state.position is not None and (action is None or action.kind != "exit"):
            trail = self._trail(state, position, float(bars["close"].iloc[-1]), float(signal["atr"]), spec, tick)
            if trail:
                outcome = f"{outcome}; {trail}"

        state.last_bar = bar
        # The daily loss limit compares against equity at the first decision of the previous
        # trading day; with daily bars that is simply the previous decision.
        trading_day = bars.index[-1].date().isoformat()
        if state.trading_day != trading_day:
            state.day_start_equity = account.equity
            state.trading_day = trading_day
        save_state(cfg.state_file, state)
        held = f"{'long' if state.position.side == 1 else 'short'} {state.position.volume} lots" if state.position else "flat"
        daily = cfg.timeframe == "D1"
        summary = (
            f"{'Daily' if daily else cfg.timeframe} check {bar[:10] if daily else bar[:16]}: {outcome}. Position: {held}. "
            f"Equity {account.equity:,.2f} {account.currency}. "
            f"Close {bars['close'].iloc[-1]:.2f}, trend SMA {signal['trend']:.2f}, ATR {signal['atr']:.2f}."
        )
        if daily or action is not None:
            self.notify.send(summary)
        else:  # intraday bots log quiet bars instead of sending an alert every hour
            log.info(summary)
        return outcome

    # --- helpers ----------------------------------------------------------------

    def _halt(self, state: BotState, reason: str) -> None:
        if not state.halted:
            self.notify.send(f"HALTED: {reason}", logging.ERROR)
        state.halted, state.halt_reason = True, reason

    def _reconcile(self, state: BotState, account: Account) -> Position | None:
        """Make sure what the bot remembers matches what the broker holds.

        Returns the broker position the bot manages, or None. Anything it
        can't explain halts the bot rather than being guessed at.
        """
        cfg = self.cfg
        ours = [p for p in self.broker.positions(cfg.symbol) if p.magic == cfg.magic]
        if len(ours) > 1:
            self._halt(state, f"{len(ours)} positions carry this bot's magic number {cfg.magic}; expected at most 1")
            return None
        if state.position is None:
            if ours:
                self._halt(state, f"found position {ours[0].ticket} with this bot's magic number that it did not open")
            return None
        if not ours:
            closed = self.broker.closed_trade(state.position.ticket)
            if closed is not None:
                note = f"exit {closed.exit_price:.2f} profit {closed.profit:,.2f}"
                price = closed.exit_price
            else:
                note, price = "no deal history found", ""
            journal(cfg.journal_file, time=_now(), event="closed_by_broker", side=state.position.side,
                    lots=state.position.volume, price=price, sl=state.position.sl, equity=account.equity, note=note)
            self.notify.send(f"Position {state.position.ticket} was closed on the broker side (stop-loss hit?): {note}.", sound=True)
            state.position = None
            return None
        position = ours[0]
        if position.ticket != state.position.ticket:
            self._halt(state, f"broker position {position.ticket} does not match remembered ticket {state.position.ticket}")
            return None
        if position.sl == 0:
            self.broker.set_stop(cfg.symbol, position, state.position.sl)
            self.notify.send(f"Position {position.ticket} had no stop-loss on the server; restored it at {state.position.sl}.", logging.WARNING, sound=True)
        return position

    def _enter(self, state: BotState, side: int, atr: float, spec: SymbolSpec, tick: Tick, account: Account, bar: str) -> str:
        cfg = self.cfg
        word = "long" if side == 1 else "short"
        price = tick.ask if side == 1 else tick.bid
        stop_distance = self.strategy.stop_atr * atr
        lots = lots_for(account.equity, price, stop_distance, spec, self.risk)
        if lots == 0:
            msg = (f"skipped {word} entry: risking {self.risk.risk_per_trade:.1%} of {account.equity:,.0f} is less than "
                   f"one minimum lot ({spec.volume_min} lots) with a {stop_distance:.2f} stop")
            journal(cfg.journal_file, time=_now(), event="skip_too_small", side=side, price=price, equity=account.equity, note=bar)
            self.notify.send(msg, logging.WARNING)
            return msg
        if stop_distance < spec.stops_level * spec.point:
            msg = f"skipped {word} entry: stop {stop_distance:.2f} is inside the broker's minimum distance"
            self.notify.send(msg, logging.WARNING)
            return msg
        sl = round(price - side * stop_distance, spec.digits)

        if cfg.dry_run:
            msg = f"DRY RUN: would open {word} {lots} lots near {price:.2f} with stop {sl:.2f}"
            journal(cfg.journal_file, time=_now(), event="dry_run_entry", side=side, lots=lots, price=price, sl=sl, equity=account.equity, note=bar)
            return msg

        fill = self.broker.market_order(cfg.symbol, side, lots, sl, cfg.magic, "goldbot entry")
        opened = [p for p in self.broker.positions(cfg.symbol) if p.magic == cfg.magic]
        ticket = opened[0].ticket if opened else fill.ticket
        state.position = OpenTrade(ticket=ticket, side=side, volume=fill.volume, entry_price=fill.price, sl=sl, opened_at=_now(), signal_bar=bar)
        journal(cfg.journal_file, time=_now(), event="entry", side=side, lots=fill.volume, price=fill.price, sl=sl, equity=account.equity, note=bar)
        msg = f"opened {word} {fill.volume} lots at {fill.price:.2f}, stop {sl:.2f} (risk about {fill.volume * spec.contract_size * stop_distance:,.0f})"
        self.notify.send(msg, sound=True)
        return msg

    def _trail(self, state: BotState, position: Position, close: float, atr: float, spec: SymbolSpec, tick: Tick) -> str:
        """Tighten the server-side stop per the trailing rule. Returns a note, or "" if unchanged."""
        cfg, trade = self.cfg, state.position
        sl = round(trailed_stop(trade.side, trade.sl, close, atr, self.strategy.trail_atr), spec.digits)
        if (sl - trade.sl) * trade.side < spec.point:  # not tighter by at least one point
            return ""
        price = tick.bid if trade.side == 1 else tick.ask
        if (price - sl) * trade.side < spec.stops_level * spec.point:
            log.warning("Trailing stop %.2f is inside the broker's minimum distance from %.2f; keeping %.2f", sl, price, trade.sl)
            return ""
        if cfg.dry_run:
            journal(cfg.journal_file, time=_now(), event="dry_run_trail", side=trade.side, lots=trade.volume, price=price, sl=sl, note=f"from {trade.sl:.2f}")
            return f"DRY RUN: would trail stop {trade.sl:.2f} -> {sl:.2f}"
        self.broker.set_stop(cfg.symbol, position, sl)
        journal(cfg.journal_file, time=_now(), event="trail_stop", side=trade.side, lots=trade.volume, price=price, sl=sl, note=f"from {trade.sl:.2f}")
        msg = f"trailed stop {trade.sl:.2f} -> {sl:.2f}"
        trade.sl = sl
        self.notify.send(msg)
        return msg

    def _close(self, state: BotState, position: Position, reason: str, account: Account) -> str:
        cfg = self.cfg
        if cfg.dry_run:
            journal(cfg.journal_file, time=_now(), event="dry_run_exit", side=position.side, lots=position.volume, equity=account.equity, note=reason)
            return f"DRY RUN: would close position {position.ticket} ({reason})"
        fill = self.broker.close_position(cfg.symbol, position, f"goldbot {reason}")
        journal(cfg.journal_file, time=_now(), event=f"exit_{reason}", side=position.side, lots=fill.volume,
                price=fill.price, sl=position.sl, equity=account.equity, note=f"entry {position.price_open:.2f}")
        state.position = None
        msg = f"closed position {position.ticket} at {fill.price:.2f} ({reason}); open profit before close was {position.profit:,.2f}"
        self.notify.send(msg, sound=True)
        return msg


def reset_halt(broker: Broker, cfg: LiveConfig) -> str:
    """Clear the kill switch after a person has reviewed what happened."""
    state = load_state(cfg.state_file)
    equity = broker.account().equity
    state.halted, state.halt_reason, state.equity_peak = False, "", equity
    save_state(cfg.state_file, state)
    return f"Halt cleared; drawdown is now measured from {equity:,.2f}."


def run_forever(trader: LiveTrader, cfg: LiveConfig, reconnect=None) -> None:
    """Run `run_once` every `poll_seconds` until stopped or a SafetyError occurs."""
    trader.notify.send(f"Started on {cfg.symbol} ({'DRY RUN' if cfg.dry_run else 'trading'}).")
    failures = 0
    last_status = None
    while True:
        try:
            status = trader.run_once()
            failures = 0
            if status != last_status:
                log.info(status)
                last_status = status
            cfg.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.heartbeat_file.write_text(f"{_now()} {status}\n")
            ping(cfg.heartbeat_url)
        except SafetyError as exc:
            trader.notify.send(f"STOPPED: {exc}", logging.ERROR)
            ping(cfg.heartbeat_url, "/fail")
            raise
        except Exception as exc:
            failures += 1
            log.exception("Run failed")
            if failures in (1, 5) or failures % 60 == 0:
                trader.notify.send(f"Error ({failures} in a row): {exc}", logging.ERROR)
            if reconnect is not None:
                try:
                    reconnect()
                except Exception as reconnect_exc:
                    log.warning("Reconnect failed: %s", reconnect_exc)
        time.sleep(cfg.poll_seconds if failures == 0 else min(cfg.poll_seconds * 2 ** min(failures, 4), 900))
