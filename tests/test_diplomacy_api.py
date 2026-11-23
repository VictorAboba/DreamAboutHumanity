"""Gifts and pacts over HTTP, in the spectator API (fog by access level) and the bridge's inbox delta."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from dah.bridge.client import GameClient
from dah.bridge.tools import Bridge
from dah.game import Game
from slice_helpers import slice_config
from dah.server import create_app
from dah.store import init_game_dir

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"},
         {"seat": "C", "nick": "Сид", "kind": "agent"}]
ADMIN = "admin-secret"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, tmp_path) -> None:
        self.clock = FakeClock()
        config = slice_config(game_id="g1", scenario="core:slice3", seats=SEATS, seed=7, turn_limit=4)
        self.gdir, keys = init_game_dir(tmp_path, config)
        self.spectator_key = self.gdir.issue_spectator_key()
        Game.create(self.gdir, self.clock)
        self.client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, clock=self.clock, tick_interval=None))
        self.seat_headers = {seat: {"Authorization": f"Bearer {key}"} for seat, key in keys.items()}
        self.watch_headers = {"admin": {"Authorization": f"Bearer {ADMIN}"},
                              "spectator": {"Authorization": f"Bearer {self.spectator_key}"}}

    def call(self, seat: str, method: str, path: str, ok: bool = True, **kwargs) -> dict:
        data = self.client.request(method, path, headers=self.seat_headers[seat], **kwargs).json()
        assert data["ok"] is ok, data
        return data

    def watch(self, who: str, path: str) -> dict:
        resp = self.client.get(f"/v1/spectate/g1/{path}", headers=self.watch_headers[who])
        assert resp.status_code == 200, resp.text
        return resp.json()

    def ready_all(self) -> None:
        for seat in "ABC":
            book = self.call(seat, "GET", "/v1/orders")
            self.call(seat, "POST", "/v1/ready", json={"confirm_hash": book["orders_hash"]})

    def turn(self) -> None:
        for _ in range(3):
            self.ready_all()


def test_http_routes_and_spectator_fog(tmp_path):
    env = Env(tmp_path)
    locked = env.call("A", "POST", "/v1/orders", ok=False, json={"orders": [{"kind": "fortify", "province": "P01"}]})
    assert locked["error"]["code"] == "PHASE_LOCKED"
    gift = env.call("A", "POST", "/v1/gifts", json={"to": "Соло", "resources": {"alloy": 6}})
    assert gift["gift"] == {"to": "Соло", "resources": {"core:alloy": 6}}
    bad = env.call("A", "POST", "/v1/gifts", ok=False, json={"to": "Соло", "resources": {"alloy": "много"}})
    assert bad["error"]["code"] == "INVALID"
    pact_id = env.call("A", "POST", "/v1/pacts", json={"to": "Соло", "turns": None})["pact"]["id"]
    env.call("B", "POST", f"/v1/pacts/{pact_id}/respond", json={"decision": "accept"})
    listed = env.call("C", "GET", "/v1/pacts")
    assert listed["mine"] == [] and listed["public"][0]["seats"] == ["Нико", "Соло"]
    assert pact_id not in json.dumps(listed, ensure_ascii=False)
    inbox_b = env.call("B", "GET", "/v1/inbox")["messages"]
    assert [m["event"] for m in inbox_b] == ["gift", "pact_proposed", "pact_accepted"]
    # the admin sees the turn in progress with amounts; the delayed spectator sees nothing of it yet
    live = env.watch("admin", "turn/1")
    assert {e["kind"] for e in live["events"]} == {"gift", "pact_proposed", "pact_signed"}
    assert next(e for e in live["events"] if e["kind"] == "gift")["resources"] == {"core:alloy": 6}
    assert live["messages"] == []                       # game notices are not player messages
    assert live["pacts"][0]["id"] == pact_id
    env.turn()
    env.turn()
    snap = env.watch("spectator", "turn/1")
    assert snap["full"] is False
    diplomacy = [e for e in snap["events"] if e["kind"] in ("gift", "pact_signed")]
    assert {e["kind"] for e in diplomacy} == {"gift", "pact_signed"}
    dumped = json.dumps(snap, ensure_ascii=False)
    assert pact_id not in dumped and '"resources"' not in dumped and "core:alloy" not in dumped
    assert "подношение: Нико → Соло" in next(e["text"] for e in diplomacy if e["kind"] == "gift")
    assert snap["pacts"] == [{"seats": ["A", "B"], "turns": None, "since_turn": 1, "until_turn": None,
                              "leaving": None, "ends_before_turn": None, "status": "active",
                              "nicks": ["Нико", "Соло"]}]
    assert not [m for m in snap["messages"]]
    assert env.watch("spectator", "turn/0")["pacts"] == []
    full = env.watch("admin", "turn/1")
    assert next(e for e in full["events"] if e["kind"] == "gift")["resources"] == {"core:alloy": 6}
    assert full["pacts"][0]["id"] == pact_id


def make_bridge(reply):
    return Bridge(GameClient("http://game.test", "dah_g1_A_key", transport=httpx.MockTransport(reply)))


def test_bridge_inbox_returns_only_new_entries_by_default():
    seen: list[dict] = []
    entries = [{"id": n, "text": f"m{n}"} for n in (1, 2, 3)]

    def reply(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        since = int(params.get("since", 0))
        out = [e for e in entries if e["id"] > since]
        return httpx.Response(200, json={"ok": True, "messages": out, "last_id": out[-1]["id"] if out else since,
                                         "text": "; ".join(e["text"] for e in out) or "новых сообщений нет"})

    bridge = make_bridge(reply)
    first, _ = bridge.call("inbox", {})
    assert first.startswith("m1; m2; m3") and seen[-1] == {}
    second, _ = bridge.call("inbox", {})
    assert second.startswith("новых сообщений нет") and seen[-1] == {"since": "3"}
    entries.append({"id": 4, "text": "m4"})
    assert bridge.call("inbox", {})[0].startswith("m4") and seen[-1] == {"since": "3"}
    assert bridge.call("inbox", {"all": True})[0].startswith("m1; m2; m3; m4") and seen[-1] == {}
    assert bridge.call("inbox", {"since": 0})[0].startswith("m1") and seen[-1] == {}
    assert bridge.call("inbox", {"since": 2})[0].startswith("m3; m4") and seen[-1] == {"since": "2"}
    assert bridge.call("inbox", {})[0].startswith("новых") and seen[-1] == {"since": "4"}
