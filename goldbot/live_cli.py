"""`goldbot live ...` commands for MetaTrader 5."""

import argparse
import logging
import logging.handlers
from dataclasses import replace
from pathlib import Path

from .live import LiveTrader, load_config, lots_for, reset_halt, run_forever
from .notify import Notifier
from .state import load_state
from .strategy import compute_signals


def _setup_logging(state_dir: str) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(Path(state_dir) / "goldbot.log", maxBytes=5_000_000, backupCount=5)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )


def _connect(live_cfg):
    from .mt5_broker import MT5Broker

    broker = MT5Broker(deviation_points=live_cfg.deviation_points)
    broker.connect()
    return broker


def cmd_check(a: argparse.Namespace) -> None:
    live_cfg, strategy, risk = load_config(a.config)
    broker = _connect(live_cfg)
    try:
        acct = broker.account()
        spec = broker.symbol_spec(live_cfg.symbol)
        tick = broker.tick(live_cfg.symbol)
        bars = broker.price_bars(live_cfg.symbol, live_cfg.history_bars, live_cfg.timeframe)
        positions = broker.positions(live_cfg.symbol)
        state = load_state(live_cfg.state_file)

        print(f"Account   {acct.login}  {'REAL MONEY' if acct.is_real else 'demo'}  equity {acct.equity:,.2f} {acct.currency}")
        if acct.is_real and not live_cfg.allow_live:
            print("          The bot will refuse to trade this account until allow_live = true.")
        print(f"Symbol    {spec.name}  {spec.contract_size:g} oz/lot  volume min {spec.volume_min} step {spec.volume_step} max {spec.volume_max}")
        print(f"          digits {spec.digits}  min stop distance {spec.stops_level} points  swap long {spec.swap_long} short {spec.swap_short} (broker units)")
        print(f"Price     bid {tick.bid}  ask {tick.ask}  spread {tick.spread:.2f}  server time {tick.time}")
        print(f"History   {len(bars)} completed {live_cfg.timeframe} bars, {bars.index[0]} to {bars.index[-1]} (need {strategy.warmup})")

        if len(bars) >= strategy.warmup:
            sig = compute_signals(bars, strategy).iloc[-1]
            flags = [k for k in ("long_entry", "short_entry", "long_exit", "short_exit") if sig[k]]
            print(f"Strategy  {strategy.kind}  last close {bars['close'].iloc[-1]:.2f}  trend SMA {sig['trend']:.2f}  ATR {sig['atr']:.2f}  signals: {', '.join(flags) or 'none'}")
            stop = strategy.stop_atr * sig["atr"]
            lots = lots_for(acct.equity, tick.ask, stop, spec, risk)
            if lots:
                print(f"Sizing    a new trade now would be {lots} lots with a {stop:.2f} stop (risk ~{lots * spec.contract_size * stop:,.0f})")
            else:
                print(f"Sizing    TOO SMALL: {risk.risk_per_trade:.1%} risk cannot afford {spec.volume_min} lots with a {stop:.2f} stop")

        ours = [p for p in positions if p.magic == live_cfg.magic]
        print(f"Positions {len(ours)} from this bot (magic {live_cfg.magic}), {len(positions) - len(ours)} others on {spec.name} (ignored)")
        print(f"State     last bar {state.last_bar}  halted {state.halted} {state.halt_reason}  position {state.position}")
        print(f"\nFor backtests on this broker use: --spread {tick.spread:.2f} --lot-step {spec.volume_step * spec.contract_size:g}")
    finally:
        broker.shutdown()


def cmd_run(a: argparse.Namespace) -> None:
    live_cfg, strategy, risk = load_config(a.config)
    if a.dry_run:
        live_cfg = replace(live_cfg, dry_run=True)
    _setup_logging(live_cfg.state_dir)
    broker = _connect(live_cfg)
    trader = LiveTrader(broker, live_cfg, strategy, risk, Notifier.from_env(_alert_prefix(live_cfg)))
    try:
        if a.once:
            print(trader.run_once())
        else:
            run_forever(trader, live_cfg, reconnect=broker.ensure_connected)
    finally:
        broker.shutdown()


def cmd_reset_halt(a: argparse.Namespace) -> None:
    live_cfg, _, _ = load_config(a.config)
    broker = _connect(live_cfg)
    try:
        print(reset_halt(broker, live_cfg))
    finally:
        broker.shutdown()


def cmd_export(a: argparse.Namespace) -> None:
    live_cfg, _, _ = load_config(a.config)
    broker = _connect(live_cfg)
    try:
        timeframe = a.timeframe or live_cfg.timeframe
        bars = broker.price_bars(live_cfg.symbol, a.bars, timeframe)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        bars.to_csv(a.out)
        print(f"Wrote {len(bars):,} {timeframe} bars ({bars.index[0]} to {bars.index[-1]}, broker server time) to {a.out}")
    finally:
        broker.shutdown()


def _alert_prefix(live_cfg) -> str:
    """Label on Telegram alerts, so two bots on one chat can be told apart."""
    return "goldbot" if live_cfg.timeframe == "D1" else f"goldbot {live_cfg.timeframe}"


def cmd_telegram_test(a: argparse.Namespace) -> None:
    import json
    import urllib.request

    live_cfg, _, _ = load_config(a.config)
    from .notify import _setting

    token, chat_id = _setting("TELEGRAM_BOT_TOKEN"), _setting("TELEGRAM_CHAT_ID")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather, then run:\n"
              '  setx TELEGRAM_BOT_TOKEN "<token>"\nand open a new PowerShell window.')
        return
    api = f"https://api.telegram.org/bot{token}"
    if not chat_id:
        with urllib.request.urlopen(f"{api}/getUpdates", timeout=10) as r:
            updates = json.load(r).get("result", [])
        chats = {}
        for u in updates:
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
        if not chats:
            print("No messages found. Send your bot any message in Telegram (e.g. 'hi'), then run this again.")
            return
        for cid, name in chats.items():
            print(f"Chat {cid} ({name})")
        first = next(iter(chats))
        print(f'\nTo use it, run:\n  setx TELEGRAM_CHAT_ID "{first}"\nthen open a new PowerShell window and run this command again.')
        return
    body = json.dumps({"chat_id": chat_id, "text": f"[{_alert_prefix(live_cfg)}] Test alert: Telegram is working."}).encode()
    request = urllib.request.Request(f"{api}/sendMessage", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as r:
        ok = json.load(r).get("ok")
    print("Test message sent; check Telegram." if ok else "Telegram did not accept the message; check the token and chat ID.")


def add_live_commands(sub) -> None:
    live = sub.add_parser("live", help="paper or live trading through MetaTrader 5")
    live_sub = live.add_subparsers(dest="live_command", required=True)

    def command(name, func, help_text):
        p = live_sub.add_parser(name, help=help_text)
        p.add_argument("--config", default="live.toml", help="TOML config file (default live.toml)")
        p.set_defaults(func=func)
        return p

    command("check", cmd_check, "connect and show account, symbol, sizing and state without trading")
    p = command("run", cmd_run, "run the bot")
    p.add_argument("--once", action="store_true", help="do a single pass and exit")
    p.add_argument("--dry-run", action="store_true", help="log orders instead of sending them")
    command("reset-halt", cmd_reset_halt, "clear the kill switch after reviewing what happened")
    command("telegram-test", cmd_telegram_test, "find your Telegram chat ID, or send a test alert")
    p = command("export-history", cmd_export, "save the broker's price bars to CSV for backtesting")
    p.add_argument("--bars", type=int, default=5000, help="how many bars (MT5 caps this at 'Max bars in chart')")
    p.add_argument("--timeframe", choices=["D1", "H4", "H1"], help="bar size (default: the config's timeframe)")
    p.add_argument("--out", default="data/broker_d1.csv")
