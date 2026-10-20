"""Bot state that must survive restarts, plus an append-only trade journal."""

import csv
import json
import os
import time
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
    day_start_equity: float | None = None  # equity at the first decision of the last trading day
    trading_day: str | None = None  # trading day that day_start_equity belongs to
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
    # Windows refuses the replace while another program (an editor, antivirus, a status
    # check) has the file open; that lasts moments, so retry briefly before giving up.
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


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
