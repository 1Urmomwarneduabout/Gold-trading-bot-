"""Second opinion from TradingAgents (github.com/TauricResearch/TradingAgents).

Advice only: nothing here places, changes or closes an order. The agents read
public gold data (Yahoo Finance), news and social chatter, debate, and give a
Buy / Overweight / Hold / Underweight / Sell rating, which is printed, saved and
optionally sent to Telegram next to what the bots currently hold.

TradingAgents lives in its own environment so its many dependencies cannot
touch the live bots:  .venv-agents\\Scripts\\goldbot.exe agents
"""

import argparse
import json
import os
from datetime import date
from pathlib import Path

from .notify import Notifier, _setting

# Futures analysts that need company filings (fundamentals) have nothing to read.
ANALYSTS = ("market", "news", "social")
KEY_FOR = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}
TELEGRAM_LIMIT = 4000


def bot_positions(state_files: list[Path]) -> list[str]:
    """One line per bot: what it holds right now, read from its state file."""
    lines = []
    for path in state_files:
        name = path.parent.name
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            lines.append(f"{name}: no state file")
            continue
        pos = state.get("position")
        if state.get("halted"):
            lines.append(f"{name}: HALTED ({state.get('halt_reason', '')})")
        elif pos:
            side = "long" if pos["side"] > 0 else "short"
            lines.append(f"{name}: {side} {pos['volume']} lots at {pos['entry_price']}, stop {pos['sl']}")
        else:
            lines.append(f"{name}: flat")
    return lines


def summary(ticker: str, day: str, rating: str, decision: str, positions: list[str], report: Path | None) -> str:
    head = [f"TradingAgents on {ticker} for {day}: {rating}", "(advice only, the bots are not changed)"]
    if positions:
        head += ["", "Bots now:", *positions]
    if report:
        head += ["", f"Full report: {report}"]
    text = "\n".join(head) + "\n\n" + decision.strip()
    return text if len(text) <= TELEGRAM_LIMIT else text[: TELEGRAM_LIMIT - 3] + "..."


def _config(a: argparse.Namespace) -> dict:
    from tradingagents.default_config import build_default_config

    config = build_default_config()
    config["llm_provider"] = a.provider
    config["quick_think_llm"] = a.quick_model
    config["deep_think_llm"] = a.deep_model
    config["max_debate_rounds"] = a.debate_rounds
    config["max_risk_discuss_rounds"] = a.debate_rounds
    config["results_dir"] = str(Path(a.out).resolve())
    return config


def cmd_agents(a: argparse.Namespace) -> None:
    try:
        _run(a)
    except BaseException as exc:
        # Unattended runs would otherwise fail silently in Task Scheduler.
        if a.notify and not isinstance(exc, KeyboardInterrupt):
            Notifier.from_env("goldbot-agents").send(f"TradingAgents run failed: {exc}", sound=False)
        raise


def _run(a: argparse.Namespace) -> None:
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError:
        raise SystemExit(
            "TradingAgents is not installed in this environment. Run it from its own one:\n"
            "  .venv-agents\\Scripts\\goldbot.exe agents"
        )
    key = KEY_FOR.get(a.provider)
    if key and not os.environ.get(key):
        # Task Scheduler keeps the sign-in environment; pick up a key saved later with setx.
        saved = _setting(key)
        if not saved:
            raise SystemExit(f"Set {key} first, e.g.  setx {key} \"your-key\"  then open a new terminal.")
        os.environ[key] = saved

    day = a.date or date.today().isoformat()
    print(f"Asking the agents about {a.ticker} for {day} ({a.provider}: {a.quick_model} / {a.deep_model}). This takes a few minutes...")
    graph = TradingAgentsGraph(selected_analysts=ANALYSTS, debug=a.verbose, config=_config(a))
    state, rating = graph.propagate(a.ticker, day)
    report = graph.save_reports(state, a.ticker)

    text = summary(a.ticker, day, rating, state.get("final_trade_decision", ""),
                   bot_positions([Path(p) for p in a.state]), report)
    print("\n" + text)
    if a.notify:
        Notifier.from_env("goldbot-agents").send(text, sound=False)


def add_agents_command(sub) -> None:
    p = sub.add_parser("agents", help="ask TradingAgents (multi-agent LLM) for a gold view; advice only")
    p.add_argument("--ticker", default="GC=F", help="Yahoo Finance symbol (default GC=F gold futures; GLD also works)")
    p.add_argument("--date", help="analysis date YYYY-MM-DD (default today)")
    p.add_argument("--provider", default="anthropic", help="LLM provider (default anthropic; openai, google, ollama...)")
    p.add_argument("--quick-model", default="claude-sonnet-5-5", help="model for analysts and debaters")
    p.add_argument("--deep-model", default="claude-opus-5-5", help="model for the managers' final call")
    p.add_argument("--debate-rounds", type=int, default=1, help="bull/bear and risk debate rounds (default 1; more costs more)")
    p.add_argument("--state", nargs="*", default=["state/state.json", "state_h1/state.json"], help="bot state files to show positions from")
    p.add_argument("--out", default="results/agents", help="where reports go (default results/agents)")
    p.add_argument("--notify", action="store_true", help="also send the result to Telegram")
    p.add_argument("--verbose", action="store_true", help="print every agent message as it happens")
    p.set_defaults(func=cmd_agents)
