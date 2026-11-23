"""Three bots (rush, builder, diplomat) play blitz_test over a real HTTP server to game_over; report and replay work."""

from __future__ import annotations

import socket
import threading
import time

import uvicorn

from dah.bots import run_bots
from dah.cli import main
from dah.game import Game, make_config
from dah.journal import read_events
from dah.server import create_app
from dah.store import GameDir, init_game_dir

SEATS = [{"seat": "A", "nick": "Раш", "kind": "bot", "bot": "rush"},
         {"seat": "B", "nick": "Строй", "kind": "bot", "bot": "builder"},
         {"seat": "C", "nick": "Дип", "kind": "bot", "bot": "diplomat"}]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_three_bots_play_to_game_over(tmp_path, capsys):
    config = make_config(game_id="blitz", scenario="core:blitz_test", seats=SEATS, seed=7)
    gdir, _ = init_game_dir(tmp_path, config)
    Game.create(gdir)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(str(tmp_path), tick_interval=0.2), host="127.0.0.1",
                                           port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        bots = threading.Thread(target=run_bots, args=(gdir.path, f"http://127.0.0.1:{port}"), daemon=True)
        bots.start()
        bots.join(timeout=150)
        assert not bots.is_alive()
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    types = {e["type"] for e in read_events(GameDir(gdir.path).journal_path)}
    assert {"game_created", "seat_info", "phase_open", "phase_close", "recon", "call", "first_call_in_phase",
            "orders_submitted", "ready", "turn_resolved", "game_over"} <= types
    assert {"pact_proposed", "pact_accepted"} <= types   # the diplomat offers pacts, the builder accepts them
    assert main(["replay", "blitz", "--games-dir", str(tmp_path)]) == 0
    assert main(["report", "blitz", "--games-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "расхождений 0" in out and "Итог: ход 6" in out
