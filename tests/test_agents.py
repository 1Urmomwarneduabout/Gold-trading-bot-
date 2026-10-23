import json

from goldbot.agents import TELEGRAM_LIMIT, bot_positions, summary
from goldbot.cli import build_parser


def _write(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return path


def test_bot_positions_reads_each_state(tmp_path):
    short = _write(tmp_path / "state" / "state.json", {
        "halted": False,
        "position": {"side": -1, "volume": 0.06, "entry_price": 4025.28, "sl": 4194.39},
    })
    flat = _write(tmp_path / "state_h1" / "state.json", {"halted": False, "position": None})
    halted = _write(tmp_path / "other" / "state.json", {"halted": True, "halt_reason": "drawdown", "position": None})
    lines = bot_positions([short, flat, halted, tmp_path / "missing" / "state.json"])
    assert lines == [
        "state: short 0.06 lots at 4025.28, stop 4194.39",
        "state_h1: flat",
        "other: HALTED (drawdown)",
        "missing: no state file",
    ]


def test_summary_says_advice_only_and_fits_telegram():
    text = summary("GC=F", "2026-10-23", "Hold", "x" * 10_000, ["state: flat"], None)
    assert text.startswith("TradingAgents on GC=F for 2026-10-23: Hold")
    assert "advice only" in text
    assert len(text) <= TELEGRAM_LIMIT


def test_agents_command_defaults():
    a = build_parser().parse_args(["agents"])
    assert a.ticker == "GC=F" and a.provider == "anthropic" and not a.notify


def test_scheduled_failure_is_sent_to_telegram(monkeypatch):
    import goldbot.agents as agents

    sent = []
    monkeypatch.setattr(agents.Notifier, "send", lambda self, text, *a, **k: sent.append(text))
    monkeypatch.setattr(agents, "_run", lambda a: (_ for _ in ()).throw(SystemExit("Set ANTHROPIC_API_KEY first")))
    args = build_parser().parse_args(["agents", "--notify"])
    try:
        agents.cmd_agents(args)
    except SystemExit:
        pass
    assert sent == ["TradingAgents run failed: Set ANTHROPIC_API_KEY first"]
