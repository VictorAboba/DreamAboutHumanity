"""End to end, stage 3 (docs/SPY_AND_CONQUEST.md sections 7, 8, 10): the core scenario with espionage and conquest on
(blitz_spy), three bots (rush, builder, diplomat) over a real uvicorn server to game_over; `dah replay` matches every
recon step and every turn; no seat ever sees another's Shades, reports or truth; the delayed spectator sees the
public marks (occupations, claims) and never Shades or captures; the report gets its stage-3 block."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from dah.bots import run_bots
from dah.cli import main
from dah.game import Game
from dah.journal import read_events
from dah.server import create_app
from dah.server.spectate import DELAYED, LIVE, GameCache, snapshot
from dah.store import GameDir, read_json

GAME = "e2e3"
SEATS = ["A:bot-rush:Раш", "B:bot-builder:Строй", "C:bot-diplomat:Дипл"]
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


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    games = tmp_path_factory.mktemp("games3")
    argv = ["game", "create", "--scenario", "core:blitz_spy", "--seed", "23", "--id", GAME,
            "--games-dir", str(games)] + [arg for seat in SEATS for arg in ("--seat", seat)]
    assert main(argv) == 0
    gdir = GameDir(games / GAME)
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
        yield {"games": games, "gdir": gdir, "keys": keys, "recorder": recorder}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_game_over_with_both_systems_and_their_journal(played):
    events = read_events(played["gdir"].journal_path)
    created = next(e for e in events if e["type"] == "game_created")
    assert sorted(created["systems"]) == ["conquest", "espionage"]
    over = [e for e in events if e["type"] == "game_over"]
    assert len(over) == 1 and over[0]["turn"] == 6
    types = {e["type"] for e in events}
    assert {"recon", "shade_hired", "mission_started", "spy_round", "mission_end", "intel_read"} <= types, types
    recons = [e for e in events if e["type"] == "recon"]
    assert len(recons) == 6 and all(e.get("state_hash") and e.get("recon_hash") for e in recons)
    # the bots read their reports as data and set a conquest policy
    assert any(e["type"] == "intel_read" and e.get("format") == "json" for e in events)
    assert "policy_set" in types


def test_bots_use_espionage_and_conquest_orders(played):
    kinds: dict[str, set[str]] = {}
    for turn in range(1, 7):
        data = read_json(played["gdir"].path / "private" / "orders" / f"turn_{turn}.json")
        for seat, book in data.items():
            kinds.setdefault(seat, set()).update(o["kind"] for o in book.get("orders", []))
    # core 0.6.0: every seat holds a free Shade from the start (journaled `shade_hired` with `free`), so a hire is not
    # needed for a mission
    free = [e for e in read_events(played["gdir"].journal_path) if e["type"] == "shade_hired" and e.get("free")]
    assert sorted(e["seat"] for e in free) == ["A", "B", "C"] and all(e["turn"] == 0 for e in free)
    assert "spy" in kinds["A"] | kinds["B"] | kinds["C"]
    assert "policy" in kinds["A"] | kinds["B"] | kinds["C"]
    assert all(not any(o.get("kind") == "dispose" and not o.get("province") for o in
                       read_json(played["gdir"].path / "private" / "orders" / f"turn_{t}.json").get(s, {})
                       .get("orders", [])) for t in range(1, 7) for s in "ABC")
    rejected = [e for e in read_events(played["gdir"].journal_path)
                if e["type"] in ("orders_submitted", "orders_validated") and e.get("rejected")]
    assert not rejected, rejected[:3]


def test_replay_matches_recon_and_every_turn(played, capsys):
    assert main(["replay", GAME, "--games-dir", str(played["games"])]) == 0
    out = capsys.readouterr().out
    assert out.count(": OK") == 6 and "расхождений 0" in out, out


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_no_seat_sees_truth_or_other_shades(played):
    seat_of = {key: seat for seat, key in played["keys"].items()}
    intel = 0
    for key, body in played["recorder"].responses:
        seat = seat_of.get(key)
        if seat is None:
            continue
        payload = json.loads(body)
        for node in _walk(payload):
            assert "truth" not in node, node
            if node.get("seat") not in (None, seat):
                # a ledger report about another seat holds the number of its Shades (2.2), never the Shades
                assert "espionage" not in node and not isinstance(node.get("shades"), list), node
        if isinstance(payload.get("intel"), dict) and isinstance(payload["intel"].get("reports"), list):
            intel += 1
            assert all(r.get("shade") for r in payload["intel"]["reports"])
            for c in payload["intel"]["caught"]:
                assert set(c) <= {"province", "mission", "target", "owner"}
    assert intel >= 1


def test_spectator_fog_of_stage3(played):
    game = Game.load(played["gdir"])
    cache = GameCache()
    journal = read_events(played["gdir"].journal_path)
    occupied_turns = {e["turn"] for e in journal if e["type"] == "province_captured" and e["status"] == "occupied"}
    claim_turns = {e["turn"] for e in journal if e["type"] == "claim_created"}
    for turn in range(1, 6):        # the delayed level runs one turn behind
        delayed = snapshot(game, cache, turn, DELAYED)
        assert "claims" in delayed
        if turn in claim_turns:
            assert delayed["claims"] and all(c.get("nick") for c in delayed["claims"])
        if turn in occupied_turns:      # the public mark of a fresh occupation, unless a fate settled it at once
            occupied = {p["id"] for p in delayed["provinces"] if (p.get("status") or {}).get("occupied")}
            fresh = {e["province"] for e in journal if e["type"] == "province_captured" and e["turn"] == turn
                     and e["status"] == "occupied"}
            settled = {e["province"] for e in journal if e["type"] == "disposition" and e["turn"] == turn}
            assert fresh - settled <= occupied, (turn, fresh, settled, occupied)
        text = json.dumps(delayed, ensure_ascii=False)
        assert "shade_captured" not in text and '"shades"' not in text and "conquest_policy" not in text
        assert all(e["kind"] not in ("mission_started", "mission_end", "feint", "policy_set", "dispose_failed",
                                     "policy_unaffordable", "strike_cancelled") for e in delayed["events"])
        assert all(not e.get("seats") for e in delayed["events"] if e["kind"] == "sabotage")
        for e in delayed["events"]:
            if e["kind"] == "disposition":
                assert not {"cost", "loot", "source"} & set(e), e
        full = snapshot(game, cache, turn, LIVE)
        assert all("shades" in p and "conquest_policy" in p for p in full["players"])
    caught = [e for e in read_events(played["gdir"].journal_path) if e["type"] == "shade_captured"]
    for c in caught:        # the full level lists every capture of its turn
        full = snapshot(game, cache, c["turn"], LIVE)
        assert any(e["kind"] == "shade_captured" and e["province"] == c["province"] for e in full["events"])


def test_report_has_stage3_block(played, capsys):
    assert main(["report", GAME, "--games-dir", str(played["games"]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    block = report["stage3"]
    assert set(block) == {"A", "B", "C"}
    assert sum(t["shades_hired"] + t["shades_free"] for t in block.values()) >= 3     # 0.6.0: one free each
    assert sum(sum(t["missions"].values()) for t in block.values()) >= 1
    assert main(["report", GAME, "--games-dir", str(played["games"])]) == 0
    assert "ШПИОНАЖ И ЗАВОЕВАНИЕ" in capsys.readouterr().out


def test_no_stage3_block_without_the_systems(tmp_path: Path):
    from dah.report import build_report
    report = build_report([{"type": "game_created", "seq": 1, "ts": 0.0,
                            "config": {"game_id": "g", "scenario": "s", "seats": [{"seat": "A", "nick": "a",
                                                                                   "kind": "bot"}]}}])
    assert "stage3" not in report
