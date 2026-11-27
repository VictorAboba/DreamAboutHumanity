"""End to end, stage 2: a slice3 game with a draft setup phase created via the CLI, a real uvicorn server, three bots
(rush, builder, diplomat) choosing factions and artifacts and researching to game_over; replay matches, the report
shows factions, artifacts and techs."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn

from dah.bots import run_bots
from dah.cli import main
from dah.journal import read_events
from dah.server import create_app
from dah.store import GameDir

GAME = "e2e-s2"
SEATS = ["A:bot-rush:Раш", "B:bot-builder:Строй", "C:bot-diplomat:Дип"]
PORTS = range(8900, 8950)  # the range test servers of this project may use


def free_port() -> int:
    for port in PORTS:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 8900-8949")


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    games = tmp_path_factory.mktemp("games")
    argv = ["game", "create", "--scenario", "core:slice3", "--artifacts", "draft", "--seed", "3", "--turns", "4",
            "--id", GAME, "--games-dir", str(games)] + [arg for seat in SEATS for arg in ("--seat", seat)]
    assert main(argv) == 0
    gdir = GameDir(games / GAME)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(str(games), tick_interval=0.2), host="127.0.0.1", port=port,
                                           log_level="warning"))
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
        yield {"games": games, "gdir": gdir}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_bots_play_the_setup_phase_and_reach_game_over(played):
    events = read_events(played["gdir"].journal_path)
    types = [e["type"] for e in events]
    assert types.count("game_over") == 1
    setup_open = next(e for e in events if e["type"] == "phase_open")
    assert setup_open["phase"] == "setup"
    closed = next(e for e in events if e["type"] == "setup_closed")
    assert closed["reason"] == "all_ready"                       # the bots chose and pressed ready
    assert closed["factions"] == {"A": "core:captains", "B": "core:litany", "C": "core:synclite"}
    assert all(len(arts) == 1 for arts in closed["artifacts"].values())
    assert {e["seat"] for e in events if e["type"] == "setup_choice"} == {"A", "B", "C"}
    assert {e["seat"] for e in events if e["type"] == "research_started"} == {"A", "B", "C"}
    orders = json.loads((played["gdir"].path / "private" / "orders" / "turn_1.json").read_text(encoding="utf-8"))
    assert all(any(o["kind"] == "research" for o in orders[s]["orders"]) for s in "ABC")


def test_replay_matches_and_report_shows_setup(played, capsys):
    assert main(["replay", GAME, "--games-dir", str(played["games"])]) == 0
    out = capsys.readouterr().out
    assert out.count(": OK") == 4 and "расхождений 0" in out
    assert main(["report", GAME, "--games-dir", str(played["games"]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["seats"]["B"]["faction"] == "core:litany" and len(report["seats"]["B"]["artifacts"]) == 1
    assert report["setup"]["closed"] == "all_ready"
    assert report["phases"][0]["phase"] == "setup"
    assert main(["report", GAME, "--games-dir", str(played["games"])]) == 0
    assert "фракция captains" in capsys.readouterr().out
