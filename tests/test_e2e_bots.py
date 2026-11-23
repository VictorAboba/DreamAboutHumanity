"""End to end: CLI-created blitz_test game, real uvicorn server, three bots, report, replay, fog check, dah-bridge."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from dah.bots import run_bots
from dah.cli import main
from dah.journal import read_events
from dah.server import create_app
from dah.store import GameDir, read_json

GAME = "e2e"
SEATS = ["A:bot-passive:Пасс", "B:bot-builder:Строй", "C:bot-rush:Раш"]
# SLICE.md section 6 types that occur without a waker; spend_report/telemetry are posted by the probe below.
MANDATORY = {"game_created", "seat_info", "phase_open", "phase_close", "call", "first_call_in_phase",
             "orders_validated", "orders_submitted", "ready", "unready", "ready_cleared", "message",
             "turn_resolved", "spend_report", "telemetry", "game_over"}
ROOT = Path(__file__).resolve().parents[1]


class Recorder:
    """ASGI wrapper that keeps every response body together with the bearer key of its request."""

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
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    """Create the game via the CLI, serve it, run a probe and the bots to game_over; yield the context."""
    games = tmp_path_factory.mktemp("games")
    argv = ["game", "create", "--scenario", "core:blitz_test", "--seed", "11", "--id", GAME,
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
        _probe(url, keys["A"])
        bots = threading.Thread(target=run_bots, args=(gdir.path, url), daemon=True)
        bots.start()
        bots.join(timeout=180)
        assert not bots.is_alive(), "bots did not finish"
        yield {"games": games, "gdir": gdir, "keys": keys, "url": url, "recorder": recorder}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _probe(url: str, key: str) -> None:
    """Seat calls bots never make (validate, ready/unready, edit after ready, host telemetry and spend)."""
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=10) as http:
        def call(method: str, path: str, body: dict | None = None) -> dict:
            data = http.request(method, path, json=body).json()
            assert data["ok"], (path, data)
            return data

        status = call("GET", "/v1/phase")
        while status["phase"] != "negotiation_1":  # the 5 s blitz deadline may pass before the server starts
            status = call("GET", f"/v1/wait_phase?after_seq={status['phase_seq']}&max_seconds=10")
        state = call("GET", "/v1/state?format=json")["state"]
        capital = next(p["id"] for p in state["provinces"] if p["owner"] == "A" and p["type"] == "core:capital")
        orders = call("GET", "/v1/orders")
        call("POST", "/v1/orders/validate", {"orders": [{"kind": "fortify", "province": "P99"}]})
        call("POST", "/v1/ready", {"confirm_hash": orders["orders_hash"]})
        call("POST", "/v1/unready")
        call("POST", "/v1/ready", {"confirm_hash": orders["orders_hash"]})
        call("POST", "/v1/orders", {"orders": [{"kind": "fortify", "province": capital}], "mode": "append"})
        call("POST", "/v1/orders", {"orders": [], "mode": "replace_all"})  # leave the passive seat empty again
        call("POST", "/v1/telemetry", {"kind": "continue_sent", "detail": {"probe": True}})
        call("POST", "/v1/spend", {"spent_usd": 0.01, "source": "participant_program"})


def test_game_reaches_game_over_with_all_journal_types(played):
    events = read_events(played["gdir"].journal_path)
    types = {e["type"] for e in events}
    assert MANDATORY <= types, MANDATORY - types
    over = [e for e in events if e["type"] == "game_over"]
    assert len(over) == 1 and over[0]["turn"] == 6


def _orders(gdir: GameDir, turn: int, seat: str) -> list[dict]:
    data = json.loads((gdir.path / "private" / "orders" / f"turn_{turn}.json").read_text(encoding="utf-8"))
    return data.get(seat, {}).get("orders", [])


def test_rush_actually_moves_and_builder_builds(played):
    turns = range(1, 7)
    assert any(o["kind"] == "move" for t in turns for o in _orders(played["gdir"], t, "C"))
    assert any(o["kind"] == "build" for t in turns for o in _orders(played["gdir"], t, "B"))
    assert all(not _orders(played["gdir"], t, "A") for t in turns)


def test_report_has_per_seat_per_phase_timings(played, capsys):
    assert main(["report", GAME, "--games-dir", str(played["games"]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    actions = [p for p in report["phases"] if p["phase"] == "actions"]
    assert len(actions) == 6
    for phase in actions:
        assert set(phase["seats"]) == {"A", "B", "C"}
        for seat in phase["seats"].values():
            assert seat["first_call_s"] is not None and seat["ready_s"] is not None and seat["calls"] > 0
    assert report["result"] is not None
    assert main(["report", GAME, "--games-dir", str(played["games"])]) == 0
    assert "ФАЗЫ" in capsys.readouterr().out


def test_replay_matches_every_turn(played, capsys):
    assert main(["replay", GAME, "--games-dir", str(played["games"])]) == 0
    out = capsys.readouterr().out
    assert out.count(": OK") == 6 and "расхождений 0" in out


def _foreign_resources(node: Any, seat: str) -> list[Any]:
    """Every dict that belongs to another seat and still carries resources."""
    found = []
    if isinstance(node, dict):
        owner = node.get("seat")
        if owner is not None and owner != seat and ("resources" in node or "net_income" in node):
            found.append(node)
        for value in node.values():
            found += _foreign_resources(value, seat)
    elif isinstance(node, list):
        for value in node:
            found += _foreign_resources(value, seat)
    return found


def test_no_seat_ever_sees_another_seats_resources(played):
    seat_of = {key: seat for seat, key in played["keys"].items()}
    checked = 0
    for key, body in played["recorder"].responses:
        seat = seat_of.get(key)
        if seat is None:
            continue
        payload = json.loads(body)
        assert not _foreign_resources(payload, seat), payload
        you = payload.get("state", {}).get("you") if isinstance(payload.get("state"), dict) else None
        if you is not None:
            assert you["seat"] == seat
            checked += 1
    assert checked >= 18  # every bot reads state in every phase it plays


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to spawn dah-bridge")
def test_dah_bridge_against_live_server(played):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    key = played["keys"]["B"]
    params = StdioServerParameters(
        command=shutil.which("uv"), args=["run", "--project", str(ROOT), "dah-bridge"], cwd=str(ROOT),
        env={**os.environ, "DAH_SERVER": played["url"], "DAH_KEY": key},
    )

    async def session() -> dict[str, str]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {"whoami", "rules", "get_state", "phase", "wait_phase", "submit_orders", "ready"} <= names
            out = {}
            for name, args in (("whoami", {}), ("rules", {}), ("get_state", {}), ("phase", {}),
                               ("get_state", {"format": "json"})):
                result = await client.call_tool(name, args)
                assert not result.is_error, (name, result)
                out[f"{name}{args}"] = "".join(c.text for c in result.content if getattr(c, "text", None))
            return out

    out = asyncio.run(asyncio.wait_for(session(), 90))
    assert all(key not in text for text in out.values())
    assert '"seat":"B"' in out["whoami{}"].replace(" ", "")
    assert "Строй" in out["whoami{}"]
    assert len(out["rules{}"]) > 1000 and "ОК" in out["rules{}"]
    assert "ХОД" in out["get_state{}"] and "Казна" in out["get_state{}"]
    assert "game_over" in out["phase{}"]
    assert "core:alloy" in out["get_state{'format': 'json'}"]
