"""End to end, core 0.8.0 trade (docs/TRADE.md 14, «Боты и e2e»): `core:blitz_trade` with three bots over a real
uvicorn server to game_over; the bots offer, answer and settle trades through the HTTP routes; every trade event is
in the journal and private to its two seats until game_over; `dah replay` matches every turn; `dah report` prints the
trade figures."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
import uvicorn

from dah.bots import run_bots
from dah.cli import main
from dah.journal import TRADE_TYPES, read_events, view_events
from dah.report import build_report
from dah.server import create_app
from dah.store import GameDir
from test_e2e_stage5 import free_port

GAME = "e2et"
SEATS = ["A:bot-builder:Стро", "B:bot-diplomat:Дипл", "C:bot-builder:Мастер"]
SEEDS = 4       # games tried until one settles a trade


def _play(games, seed: int) -> dict[str, Any]:
    game = f"{GAME}s{seed}"
    argv = ["game", "create", "--scenario", "core:blitz_trade", "--seed", str(seed), "--id", game,
            "--games-dir", str(games)] + [arg for seat in SEATS for arg in ("--seat", seat)]
    assert main(argv) == 0
    gdir = GameDir(games / game)
    server = uvicorn.Server(uvicorn.Config(create_app(str(games), tick_interval=0.2), host="127.0.0.1",
                                           port=(port := free_port()), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        bots = threading.Thread(target=run_bots, args=(gdir.path, f"http://127.0.0.1:{port}"), daemon=True)
        bots.start()
        bots.join(timeout=240)
        assert not bots.is_alive(), "bots did not finish"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return {"games": games, "gdir": gdir, "game": game}


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    games = tmp_path_factory.mktemp("games_trade")
    for seed in range(1, 1 + SEEDS):
        out = _play(games, seed)
        if any(e["type"] == "trade_settled" for e in read_events(out["gdir"].journal_path)):
            return out
    pytest.fail(f"no trade settled in {SEEDS} games")


def test_bots_trade_over_http_to_game_over(played):
    events = read_events(played["gdir"].journal_path)
    assert len([e for e in events if e["type"] == "game_over"]) == 1
    created = next(e for e in events if e["type"] == "game_created")
    assert "trade" in created["systems"]
    proposed = [e for e in events if e["type"] == "trade_proposed"]
    settled = [e for e in events if e["type"] == "trade_settled"]
    assert proposed and settled and all(len(e["seats"]) == 2 for e in proposed)
    # before game_over a third seat never sees a trade of the other two
    for seat in "ABC":
        mine = [e for e in view_events([e for e in events if e["type"] != "game_over"], seat=seat)
                if e["type"] in TRADE_TYPES]
        assert all(seat in e["seats"] for e in mine)


def test_the_trade_game_replays_and_reports(played, capsys):
    assert main(["replay", played["game"], "--games-dir", str(played["games"])]) == 0
    out = capsys.readouterr().out
    assert "расхождений 0" in out, out
    report = build_report(read_events(played["gdir"].journal_path))
    assert report["trade"]["game"]["settled"] >= 1
    assert main(["report", played["game"], "--games-dir", str(played["games"])]) == 0
    assert "ОБМЕН (по журналу" in capsys.readouterr().out
