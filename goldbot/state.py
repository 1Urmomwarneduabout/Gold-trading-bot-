"""Bot state that must survive restarts, plus an append-only trade journal."""

import csv
import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class OpenTrade:
    ticket: int
    side: int
    volume: float  # lots
    entry_price: float
    sl: float
    opened_at: str
    signal_bar: str


@dataclass
class BotState:
    last_bar: str | None = None  # last daily bar the bot has acted on
    equity_peak: float | None = None
    day_start_equity: float | None = None  # equity at the previous daily decision
    halted: bool = False
    halt_reason: str = ""
    position: OpenTrade | None = None


def load_state(path: str | Path) -> BotState:
    path = Path(path)
    if not path.exists():
        return BotState()
    data = json.loads(path.read_text())
    position = data.pop("position", None)
    known = {f.name for f in fields(BotState)}
    state = BotState(**{k: v for k, v in data.items() if k in known})
    state.position = OpenTrade(**position) if position else None
    return state


def save_state(path: str | Path, state: BotState) -> None:
    """Write atomically, so a crash mid-write can never leave a corrupt file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, path)


JOURNAL_FIELDS = ["time", "event", "side", "lots", "price", "sl", "equity", "note"]


def journal(path: str | Path, **row) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)
