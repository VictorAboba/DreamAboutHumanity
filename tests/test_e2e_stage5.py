"""End to end, stage 5 (docs/ABANDONED_LANDS.md 14, 15.15): `core:blitz_finds` (mirror3 for two: one start is
Abandoned) with two bots over a real uvicorn server to game_over; the bots set finds off and fight the guards;
`dah replay` recomputes the layout and matches every turn; no seat ever sees a find it does not know; the delayed
spectator sees a find only when it went off and only in its public form, the revealed level the whole layout; the report
gets its finds block (value realized and luck per seat)."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest
import uvicorn

from dah.bots import run_bots
from dah.cli import main
from dah.game import Game
from dah.journal import read_events
from dah.server import create_app
from dah.server.spectate import DELAYED, LIVE, REVEALED, GameCache, snapshot
from dah.store import GameDir, read_json
GAME = "e2e5"
SEATS = ["A:bot-rush:Раш", "B:bot-diplomat:Дипл"]
SEEDS = 12        # games tried until one sets a find off (about half of them do)
PORTS = range(8950, 9000)   # the servers this project's tests may start


class Recorder:
    """ASGI wrapper that keeps every response body with the bearer key of its request."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.responses: list[tuple[str, bytes]] = []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        key = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
        chunks: list[bytes] = []

        async def capture(message: dict) -> None:
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, capture)
        self.responses.append((key, b"".join(chunks)))


def free_port() -> int:
    for port in PORTS:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 8950-8999")


def _play(games, seed: int) -> dict[str, Any]:
    game = f"{GAME}s{seed}"
    argv = ["game", "create", "--scenario", "core:blitz_finds", "--seed", str(seed), "--id", game,
            "--games-dir", str(games)] + [arg for seat in SEATS for arg in ("--seat", seat)]
    assert main(argv) == 0
    gdir = GameDir(games / game)
    keys = {seat: entry["key"] for seat, entry in read_json(gdir.bot_keys_path).items()}
    recorder = Recorder(create_app(str(games), tick_interval=0.2))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(recorder, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        bots = threading.Thread(target=run_bots, args=(gdir.path, url), daemon=True)
        bots.start()
        bots.join(timeout=240)
        assert not bots.is_alive(), "bots did not finish"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return {"games": games, "gdir": gdir, "keys": keys, "recorder": recorder, "game": game}


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    """The first of a few seeds whose game sets a find off (the layout also depends on the secret salt drawn at
    creation, so a seed alone does not fix it)."""
    games = tmp_path_factory.mktemp("games5")
    for seed in range(1, 1 + SEEDS):
        out = _play(games, seed)
        if any(e["type"] == "discovery_found" for e in read_events(out["gdir"].journal_path)):
            return out
    pytest.fail(f"no find went off in {SEEDS} games")


def _journal(played: dict[str, Any]) -> list[dict[str, Any]]:
    return read_events(played["gdir"].journal_path)


def test_game_over_with_abandoned_land_and_finds(played):
    events = _journal(played)
    created = next(e for e in events if e["type"] == "game_created")
    assert "discoveries" in created["systems"]
    assert len([e for e in events if e["type"] == "game_over"]) == 1
    placed = next(e for e in events if e["type"] == "discoveries_placed")
    assert placed["finds"] and placed["accepted"]
    found = [e for e in events if e["type"] == "discovery_found"]
    assert found, "no find went off"
    assert all(set(e["entrants"]) <= {"A", "B"} and set(e["value"]) == set(e["entrants"]) for e in found)
    rejected = [e for e in events if e["type"] in ("orders_submitted", "orders_validated") and e.get("rejected")]
    assert not rejected, rejected[:3]


def test_bots_never_scout_with_one_or_two_units(played):
    """Decision 25 a: the bots send real armies (3+) where a find may lie; never a lone scout."""
    hidden = set(next(e for e in _journal(played) if e["type"] == "discoveries_placed")["finds"])
    for turn in range(1, 7):
        data = read_json(played["gdir"].path / "private" / "orders" / f"turn_{turn}.json") or {}
        for book in data.values():
            for o in book.get("orders", []):
                if o.get("kind") == "move" and not o.get("feint") and o.get("to") in hidden:
                    assert sum(o["units"].values()) >= 3, o
        for e in _journal(played):
            if e["type"] == "discovery_found" and e["turn"] == turn:
                hidden.discard(e["province"])


def test_replay_recomputes_the_layout_and_matches(played, capsys):
    assert main(["replay", played["game"], "--games-dir", str(played["games"])]) == 0
    out = capsys.readouterr().out
    assert "расхождений 0" in out, out


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_no_seat_sees_a_find_it_does_not_know(played):
    events = _journal(played)
    layout = next(e for e in events if e["type"] == "discoveries_placed")["finds"]
    knows: dict[str, set[str]] = {"A": set(), "B": set()}
    for e in events:
        if e["type"] == "discovery_found":
            for s in e["entrants"]:
                knows[s].add(e["province"])
        elif e["type"] == "mission_started" and e.get("target"):
            knows.setdefault(e.get("seat"), set()).add(str(e["target"]))    # an Observation may reveal one
    seat_of = {key: seat for seat, key in played["keys"].items()}
    for key, body in played["recorder"].responses:
        seat = seat_of.get(key)
        if seat is None or not body:
            continue
        text = body.decode("utf-8", "replace")
        assert "discoveries_placed" not in text and "finds_layout" not in text
        payload = json.loads(text)
        for node in _walk(payload):
            if isinstance(node.get("discovery"), dict) and node.get("id") in layout:
                assert node["id"] in knows[seat], (seat, node)


def test_spectator_sees_finds_at_its_fog_level(played):
    game = Game.load(played["gdir"])
    cache = GameCache()
    events = _journal(played)
    layout = next(e for e in events if e["type"] == "discoveries_placed")["finds"]
    found_turns = {e["turn"] for e in events if e["type"] == "discovery_found"}
    for turn in range(0, 6):
        delayed = snapshot(game, cache, turn, DELAYED) if turn < 6 else None
        assert delayed["abandoned"] and "finds_layout" not in delayed
        assert all("discovery" not in p for p in delayed["provinces"])
        for e in delayed["events"]:
            if e["kind"] == "discovery_found":
                assert e.get("public") and "id" not in e and "shares" not in e and "value" not in e, e
        full = snapshot(game, cache, turn, LIVE)
        assert set(full["finds_layout"]) == set(layout)
        if turn in found_turns:
            assert any(e["kind"] == "discovery_found" and e.get("id") for e in full["events"])
    revealed = snapshot(game, cache, 0, REVEALED)
    assert {pid: v["id"] for pid, v in revealed["finds_layout"].items()} == layout
    assert all(v["state"] == "hidden" for v in revealed["finds_layout"].values())    # turn 0: nothing went off yet


def test_report_has_the_finds_block(played, capsys):
    assert main(["report", played["game"], "--games-dir", str(played["games"]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    block = report["discoveries"]
    assert set(block["seats"]) == {"A", "B"} and block["placed"] >= 1 and block["triggered"] >= 1
    assert abs(sum(t["luck"] for t in block["seats"].values())) < 0.02
    assert main(["report", played["game"], "--games-dir", str(played["games"])]) == 0
    assert "НАХОДКИ" in capsys.readouterr().out
