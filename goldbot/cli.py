"""Command line interface: python -m goldbot <command> ..."""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .backtest import CostConfig, run_backtest
from .broker import BrokerError
from .data import load_daily, synthetic_daily
from .live import SafetyError
from .live_cli import add_live_commands
from .metrics import format_metrics
from .risk import RiskConfig
from .strategy import StrategyConfig
from .validate import monte_carlo, parameter_sweep, walk_forward


def _int_list(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def _add_config_args(p: argparse.ArgumentParser) -> None:
    s = p.add_argument_group("strategy")
    s.add_argument("--entry", type=int, default=55, help="breakout lookback in days (default 55)")
    s.add_argument("--exit", type=int, default=20, help="exit channel lookback in days (default 20)")
    s.add_argument("--trend", type=int, default=200, help="trend filter SMA length (default 200)")
    s.add_argument("--atr", type=int, default=20, help="ATR length (default 20)")
    s.add_argument("--stop-atr", type=float, default=2.0, help="stop distance in ATRs (default 2)")
    s.add_argument("--long-only", action="store_true", help="disable short trades")
    s.add_argument("--trail-atr", type=float, default=0.0, help="trail the stop this many ATRs behind the close (default 0 = off)")

    c = p.add_argument_group("costs (set these from your broker)")
    c.add_argument("--spread", type=float, default=0.40, help="full spread, USD/oz (default 0.40)")
    c.add_argument("--slippage", type=float, default=0.10, help="slippage per fill, USD/oz (default 0.10)")
    c.add_argument("--commission", type=float, default=0.0, help="commission per side, USD/oz")
    c.add_argument("--swap-long", type=float, default=0.06, help="annual financing rate on longs (default 0.06)")
    c.add_argument("--swap-short", type=float, default=0.02, help="annual financing rate on shorts (default 0.02)")

    r = p.add_argument_group("risk")
    r.add_argument("--risk", type=float, default=0.01, help="equity risked per trade (default 0.01)")
    r.add_argument("--max-leverage", type=float, default=5.0)
    r.add_argument("--lot-step", type=float, default=1.0, help="smallest size increment in oz (default 1)")
    r.add_argument("--max-daily-loss", type=float, default=0.03)
    r.add_argument("--max-drawdown", type=float, default=0.20)
    r.add_argument("--equity", type=float, default=10_000.0, help="starting equity (default 10000)")


def _configs(a: argparse.Namespace) -> tuple[StrategyConfig, CostConfig, RiskConfig]:
    return (
        StrategyConfig(a.entry, a.exit, a.trend, a.atr, a.stop_atr, not a.long_only, a.trail_atr),
        CostConfig(a.spread, a.slippage, a.commission, a.swap_long, a.swap_short),
        RiskConfig(a.risk, a.max_leverage, a.lot_step, a.max_daily_loss, a.max_drawdown),
    )


def _load(a: argparse.Namespace) -> pd.DataFrame:
    if a.data == "synthetic":
        print("WARNING: using SYNTHETIC random prices. Results say nothing about real gold.\n")
        return synthetic_daily()
    df = load_daily(a.data, a.roll_hour)
    print(f"Loaded {len(df):,} daily bars from {df.index[0].date()} to {df.index[-1].date()}\n")
    return df


def _save_plot(equity: pd.Series, path: Path, title: str) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(install matplotlib to also get an equity chart)")
        return
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, height_ratios=[3, 1])
    ax1.plot(equity.index, equity.values)
    ax1.set_title(title)
    ax1.set_ylabel("Equity")
    ax2.fill_between(equity.index, (equity / equity.cummax() - 1).values, 0, alpha=0.4)
    ax2.set_ylabel("Drawdown")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"Saved {path}")


def cmd_backtest(a: argparse.Namespace) -> None:
    df = _load(a)
    strategy, costs, risk = _configs(a)
    result = run_backtest(df, strategy, costs, risk, a.equity, a.start, a.end)
    print("Backtest results")
    print(format_metrics(result.metrics))
    if result.metrics["skipped_signals"]:
        print(
            f"\n  WARNING: {result.metrics['skipped_signals']} signals skipped because the position size rounded"
            " to zero. Raise --equity or --risk, or use a broker with a smaller --lot-step."
        )
    if result.halted:
        print(f"\n  KILL SWITCH TRIPPED: {result.halt_reason}")

    if len(result.trades) >= 10:
        mc = monte_carlo(result.trades, initial_equity=a.equity)
        print("\nMonte Carlo (5,000 bootstrapped trade sequences)")
        print(format_metrics({k: v for k, v in mc.items() if k not in ("sims",)}))
    else:
        print("\n(Fewer than 10 trades: too few for Monte Carlo or any conclusion.)")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    result.trades.to_csv(out / "trades.csv", index=False)
    result.equity.to_csv(out / "equity.csv")
    (out / "summary.json").write_text(json.dumps({"metrics": result.metrics, "config": result.config}, indent=2, default=str))
    print(f"\nSaved trades, equity and summary to {out}/")
    _save_plot(result.equity, out / "equity.png", "Backtest equity")


def cmd_sweep(a: argparse.Namespace) -> None:
    df = _load(a)
    strategy, costs, risk = _configs(a)
    grid = {"entry_lookback": _int_list(a.entries), "exit_lookback": _int_list(a.exits)}
    table = parameter_sweep(df, strategy, grid, costs, risk, initial_equity=a.equity, start=a.start, end=a.end)
    print("MAR (CAGR / max drawdown) by entry (rows) and exit (columns) lookback:")
    print(table.pivot(index="entry_lookback", columns="exit_lookback", values="mar").round(2).to_string())
    print("\nSharpe:")
    print(table.pivot(index="entry_lookback", columns="exit_lookback", values="sharpe").round(2).to_string())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "sweep.csv", index=False)
    print(f"\nSaved {out}/sweep.csv")


def cmd_walkforward(a: argparse.Namespace) -> None:
    df = _load(a)
    strategy, costs, risk = _configs(a)
    grid = {"entry_lookback": _int_list(a.entries), "exit_lookback": _int_list(a.exits)}
    wf = walk_forward(df, strategy, grid, costs, risk, a.train_years, a.test_years, a.objective, initial_equity=a.equity)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(wf.windows.to_string(index=False))
    print("\nOut-of-sample results (only periods the optimiser never saw)")
    print(format_metrics(wf.metrics))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    wf.windows.to_csv(out / "walkforward_windows.csv", index=False)
    wf.trades.to_csv(out / "walkforward_trades.csv", index=False)
    wf.equity.to_csv(out / "walkforward_equity.csv")
    print(f"\nSaved walk-forward files to {out}/")
    _save_plot(wf.equity, out / "walkforward_equity.png", "Walk-forward out-of-sample equity")


def cmd_resample(a: argparse.Namespace) -> None:
    df = load_daily(a.input, a.roll_hour)
    df.to_csv(a.output)
    print(f"Wrote {len(df):,} daily bars to {a.output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="goldbot", description="Gold trend-following backtester")
    sub = parser.add_subparsers(dest="command", required=True)

    def data_command(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("data", help="CSV of gold prices (daily or intraday), or 'synthetic' for a demo")
        p.add_argument("--start", help="first date to trade, e.g. 2015-01-01")
        p.add_argument("--end", help="last date to trade")
        p.add_argument("--roll-hour", type=int, default=22, help="UTC hour the trading day ends (default 22)")
        p.add_argument("--out", default="results", help="output folder (default results/)")
        _add_config_args(p)
        p.set_defaults(func=func)
        return p

    data_command("backtest", cmd_backtest, "run one backtest plus Monte Carlo")

    for name, func, help_text in (
        ("sweep", cmd_sweep, "test a grid of entry/exit lookbacks"),
        ("walkforward", cmd_walkforward, "rolling optimise-then-test validation"),
    ):
        p = data_command(name, func, help_text)
        p.add_argument("--entries", default="20,40,55,80,100", help="entry lookbacks to try")
        p.add_argument("--exits", default="10,20,30,40", help="exit lookbacks to try")
        if name == "walkforward":
            p.add_argument("--train-years", type=float, default=4)
            p.add_argument("--test-years", type=float, default=1)
            p.add_argument("--objective", default="mar", choices=["mar", "sharpe", "cagr", "sortino"])

    p = sub.add_parser("resample", help="convert an intraday CSV to daily bars")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--roll-hour", type=int, default=22)
    p.set_defaults(func=cmd_resample)

    add_live_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (BrokerError, SafetyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
