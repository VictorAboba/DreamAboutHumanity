"""HTTP API via FastAPI TestClient: auth, fog of war, orders, P2 edits, messages, ready flow, wait_phase."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from dah.game import Game
from slice_helpers import slice_config
from dah.journal import read_events
from dah.server import create_app
from dah.store import init_game_dir

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"}]
ADMIN = "admin-secret"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, tmp_path) -> None:
        self.clock = FakeClock()
        config = slice_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7)
        self.gdir, keys = init_game_dir(tmp_path, config)
        Game.create(self.gdir, self.clock)
        self.tmp_path = tmp_path
        self.client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, clock=self.clock, tick_interval=None))
        self.headers = {seat: {"Authorization": f"Bearer {key}"} for seat, key in keys.items()}

    def call(self, seat: str, method: str, path: str, **kwargs) -> dict:
        return self.client.request(method, path, headers=self.headers[seat], **kwargs).json()

    def ok(self, seat: str, method: str, path: str, **kwargs) -> dict:
        data = self.call(seat, method, path, **kwargs)
        assert data["ok"], data
        return data

    def ready(self, seat: str) -> dict:
        return self.ok(seat, "POST", "/v1/ready", json={"confirm_hash": self.ok(seat, "GET", "/v1/orders")["orders_hash"]})

    def next_phase(self) -> None:
        self.ready("A")
        self.ready("B")

    def events(self, type_: str) -> list[dict]:
        return [e for e in read_events(self.gdir.journal_path) if e["type"] == type_]


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def test_auth(env):
    for headers in ({}, {"Authorization": "Bearer dah_g1_A_wrong"}):
        resp = env.client.get("/v1/whoami", headers=headers)
        assert resp.status_code == 401
        assert resp.json() == {"ok": False, "error": {"code": "UNAUTHORIZED", "message": resp.json()["error"]["message"]}}
    who = env.ok("A", "GET", "/v1/whoami")
    assert {k: who[k] for k in ("game_id", "seat", "nick", "faction", "turn", "phase")} == {
        "game_id": "g1", "seat": "A", "nick": "Нико", "faction": "core:covenant", "turn": 1, "phase": "negotiation_1"}
    keys_on_disk = (env.gdir.keys_path).read_text(encoding="utf-8")
    assert env.headers["A"]["Authorization"].split()[1] not in keys_on_disk
    assert env.client.get("/v1/nope").json()["error"]["code"] == "NOT_FOUND"


def test_rules_and_phase(env):
    rules = env.ok("A", "GET", "/v1/rules")["text"]
    assert len(rules) <= 6000 and "СООБЩЕНИЯ" in rules
    units = env.ok("A", "GET", "/v1/rules", params={"topic": "units"})["text"]
    assert units.startswith("ЮНИТЫ") and "ПРИКАЗЫ" not in units
    phase = env.ok("A", "GET", "/v1/phase")
    assert phase["phase"] == "negotiation_1" and phase["ready"] == "0/2" and phase["seconds_left"] == 720
    assert phase["deadline"].endswith("Z") and phase["phase_seq"] == 1 and "send_message" in phase["allowed"]
    assert "submit_orders" not in phase["allowed"] and {"validate_orders", "send_gift", "propose_pact"} <= set(phase["allowed"])
    assert phase["you_ready"] is False and phase["game_over"] is False and phase["turn_limit"] == 10


def test_fog_of_war(env):
    env.next_phase()
    b_cap = "P01"  # seed 7 deals P01 to B and P05 to A on mirror2
    env.ok("B", "POST", "/v1/orders", json={"orders": [{"kind": "recruit", "province": b_cap, "unit": "assault",
                                                           "count": 2}]})
    state = env.ok("A", "GET", "/v1/state", params={"format": "json"})["state"]
    assert state["you"]["seat"] == "A" and "resources" in state["you"]
    assert all("resources" not in p for p in state["players"])
    hidden = next(p for p in state["provinces"] if p["id"] == b_cap)
    assert hidden["owner"] == "B" and not hidden["visible"] and "units" not in hidden and "buildings" not in hidden
    seen_b = next(p for p in state["provinces"] if p["owner"] == "B" and p["visible"])
    assert "units" in seen_b and "building_now" not in seen_b
    text = env.ok("A", "GET", "/v1/state")["text"]
    b_line = next(line for line in text.splitlines() if line.startswith(b_cap))
    assert "? |" in b_line and "штурм" not in b_line and "казарма" not in b_line
    orders_a = env.ok("A", "GET", "/v1/orders")
    assert orders_a["orders"] == []
    b_order = env.ok("B", "GET", "/v1/orders")["orders"][0]["id"]
    assert env.call("A", "DELETE", f"/v1/orders/{b_order}")["error"]["code"] == "UNKNOWN_ID"
    assert b_order not in json.dumps(env.ok("A", "GET", "/v1/phase"))
    admin = env.client.get("/v1/admin/games/g1", headers={"Authorization": f"Bearer {ADMIN}"}).json()
    assert admin["ok"] and admin["seats"]["B"]["orders"][0]["id"] == b_order
    assert env.client.get("/v1/admin/games/g1", headers=env.headers["A"]).status_code == 403


def test_orders_validate_submit_and_p2_edits(env):
    env.next_phase()
    move = {"kind": "move", "from": "P05", "to": "P06", "units": {"assault": 2}}
    results = env.ok("A", "POST", "/v1/orders/validate",
                     json={"orders": [move, {"kind": "move", "from": "P05", "to": "P01", "units": {"assault": 1}}]})
    assert results["results"][0] == {"ok": True, "cp_cost": 0}
    assert results["results"][1]["reason"] == "NOT_ADJACENT"
    assert env.ok("A", "GET", "/v1/orders")["orders"] == []  # validate is a dry run
    sub = env.ok("A", "POST", "/v1/orders", json={"orders": [move, {"kind": "build", "province": "P01",
                                                                    "building": "mine"}], "mode": "replace_all"})
    assert len(sub["accepted"]) == 1 and sub["rejected"][0]["reason"] == "NOT_OWNER"
    order_id = sub["accepted"][0]["id"]
    env.next_phase()
    assert env.ok("A", "GET", "/v1/phase")["phase"] == "negotiation_2"
    locked = env.ok("A", "POST", "/v1/orders", json={"orders": [{"kind": "recruit", "province": "P05",
                                                                  "unit": "assault", "count": 1}]})
    assert locked["rejected"][0]["reason"] == "PHASE_LOCKED"
    amended = env.ok("A", "PATCH", f"/v1/orders/{order_id}", json={"units": {"assault": 1}})
    assert amended["cp_cost"] == 1 and amended["cp_left"] == 2
    cancelled = env.ok("A", "DELETE", f"/v1/orders/{order_id}")
    assert cancelled["cp_cost"] == 1 and cancelled["cp_left"] == 1
    assert env.call("A", "PATCH", "/v1/orders/o999", json={"count": 1})["error"]["code"] == "UNKNOWN_ID"
    assert [e["op"] for e in env.events("order_edit_p2")] == ["amend", "cancel"]
    submitted = env.events("orders_submitted")
    assert submitted[0]["rejected"][0]["reason"] == "NOT_OWNER" and submitted[0]["phase"] == "actions"
    assert env.events("orders_validated")[0]["ok_count"] == 1


def test_estimate(env):
    est = env.ok("A", "POST", "/v1/estimate", json={"attacker": {"assault": 5}, "defender": {"assault": 1},
                                                    "province_type": "forge", "fortified": False, "hasty": False})
    assert 0.5 < est["win_prob"] <= 1 and "attacker_losses_avg" in est and "losses" in est
    bad = env.call("A", "POST", "/v1/estimate", json={"attacker": {"x": 1}, "defender": {}, "province_type": "forge"})
    assert bad["error"]["code"] == "UNKNOWN_ID"


def test_messages(env):
    sent = env.ok("A", "POST", "/v1/messages", json={"to": ["Соло"], "text": "[DAH] сдавайся"})
    assert sent["left"]["private"] == 19
    env.ok("A", "POST", "/v1/messages", json={"to": "all", "text": "1"})
    env.ok("A", "POST", "/v1/messages", json={"to": "all", "text": "2"})
    assert env.call("A", "POST", "/v1/messages", json={"to": "all", "text": "3"})["error"]["code"] == "LIMIT_EXCEEDED"
    inbox = env.ok("B", "GET", "/v1/inbox")
    assert len(inbox["messages"]) == 3 and inbox["last_id"] == 3
    assert inbox["messages"][0]["text"] == "<<untrusted from=Нико>>(DAH?) сдавайся<</untrusted>>"
    assert env.ok("B", "GET", "/v1/inbox", params={"since": 3})["messages"] == []
    assert env.ok("A", "GET", "/v1/inbox")["messages"] == []
    assert env.events("message")[0]["chars"] == len("[DAH] сдавайся")
    env.next_phase()
    assert env.call("A", "POST", "/v1/messages", json={"to": "all", "text": "x"})["error"]["code"] == "PHASE_LOCKED"


def test_ready_flow(env):
    resp = env.client.post("/v1/ready", headers=env.headers["A"], json={"confirm_hash": "stale"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "STALE_HASH"
    assert env.ready("A")["ready"] == "1/2"
    phase_b = env.ok("B", "GET", "/v1/phase")
    assert phase_b["ready"] == "1/2" and phase_b["you_ready"] is False
    env.ok("A", "POST", "/v1/unready")
    assert env.ok("B", "GET", "/v1/phase")["ready"] == "0/2"
    env.ready("A")
    closed = env.ready("B")
    assert closed["phase_closed"] is True
    phase = env.ok("A", "GET", "/v1/phase")
    assert phase["phase"] == "actions" and phase["ready"] == "0/2" and phase["phase_seq"] == 2
    env.ready("A")
    env.ok("A", "POST", "/v1/orders", json={"orders": [{"kind": "fortify", "province": "P05"}]})
    assert env.ok("A", "GET", "/v1/phase")["you_ready"] is False
    assert env.events("ready_cleared")[0]["seat"] == "A"


def test_deadline_via_request_tick_and_history(env):
    env.clock.now += 1200
    assert env.ok("A", "GET", "/v1/phase")["phase"] == "actions"
    for _ in range(2):
        env.clock.now += 1200
        env.ok("A", "GET", "/v1/phase")
    phase = env.ok("A", "GET", "/v1/phase")
    assert (phase["turn"], phase["phase"]) == (2, "negotiation_1")
    history = env.ok("A", "GET", "/v1/history")
    assert history["turn"] == 1 and history["text"].startswith("ОТЧЁТ ХОДА 1")
    assert env.call("A", "GET", "/v1/history", params={"turn": 5})["error"]["code"] == "NOT_FOUND"
    diff = env.ok("A", "GET", "/v1/state", params={"since_turn": 1})["text"]
    assert "ИЗМЕНЕНИЯ с хода 1" in diff
    assert [e["reason"] for e in env.events("phase_close")] == ["deadline"] * 3


def test_wait_phase(env):
    seq = env.ok("A", "GET", "/v1/phase")["phase_seq"]
    started = time.monotonic()
    waited = env.ok("A", "GET", "/v1/wait_phase", params={"after_seq": seq, "max_seconds": 1})
    assert waited["changed"] is False and time.monotonic() - started >= 0.9
    assert waited["phase_seq"] == seq
    assert env.ok("A", "GET", "/v1/wait_phase", params={"after_seq": seq - 1})["changed"] is True
    env.clock.now += 1200
    after = env.ok("A", "GET", "/v1/wait_phase", params={"after_seq": seq, "max_seconds": 5})
    assert after["changed"] is True and after["phase"] == "actions"
    assert env.call("A", "GET", "/v1/wait_phase", params={"after_seq": "x"})["error"]["code"] == "INVALID"


def test_call_journal_and_first_call(env):
    host = {**env.headers["A"], "X-DAH-Host": "waker"}
    assert env.client.get("/v1/wait_phase", headers=host, params={"after_seq": 0}).json()["ok"]  # waker: no first call
    assert env.events("first_call_in_phase") == [] and env.events("call")[0]["host"] is True
    env.ok("A", "GET", "/v1/whoami")
    env.ok("A", "POST", "/v1/messages", json={"to": "all", "text": "привет"})
    first = env.events("first_call_in_phase")
    assert [(e["seat"], e["turn"], e["phase"]) for e in first] == [("A", 1, "negotiation_1")]
    calls = env.events("call")
    assert [c["route"] for c in calls] == ["GET /v1/wait_phase", "GET /v1/whoami", "POST /v1/messages"]
    post = calls[-1]
    assert post["seat"] == "A" and post["req_chars"] > 0 and post["resp_chars"] > 0 and post["status"] == 200
    assert post["ms"] >= 0
    env.call("A", "POST", "/v1/ready", json={"confirm_hash": "bad"})
    assert env.events("call")[-1]["status"] == 409


def test_telemetry_and_spend(env):
    env.ok("A", "POST", "/v1/telemetry", json={"kind": "continue_sent", "detail": {"reason": "phase_open"}})
    env.ok("A", "POST", "/v1/spend", json={"spent_usd": 0.42, "source": "participant_program", "tokens": 1000})
    assert env.call("A", "POST", "/v1/telemetry", json={"kind": "bogus"})["error"]["code"] == "INVALID"
    assert env.call("A", "POST", "/v1/spend", json={"spent_usd": -1, "source": "host"})["error"]["code"] == "INVALID"
    assert env.events("telemetry")[0]["detail"] == {"reason": "phase_open"}
    assert env.events("spend_report")[0]["spent_usd"] == 0.42


def test_restart_restores_game(env):
    env.ready("A")
    env.ok("A", "POST", "/v1/messages", json={"to": ["Соло"], "text": "до рестарта"})
    app = create_app(str(env.tmp_path), admin_key=ADMIN, clock=env.clock, tick_interval=None)
    client = TestClient(app)
    phase = client.get("/v1/phase", headers=env.headers["A"]).json()
    assert phase["ready"] == "1/2" and phase["you_ready"] is True and phase["phase_seq"] == 1
    inbox = client.get("/v1/inbox", headers=env.headers["B"]).json()
    assert inbox["messages"][0]["text"].endswith("до рестарта<</untrusted>>")


def test_background_tick_runs_with_lifespan(tmp_path):
    config = slice_config(game_id="g2", scenario="core:slice2", seats=SEATS, seed=1,
                         phase_limits={"negotiation_1": 1, "actions": 60, "negotiation_2": 60})
    gdir, keys = init_game_dir(tmp_path, config)
    Game.create(gdir)
    with TestClient(create_app(str(tmp_path), tick_interval=0.05)) as client:
        headers = {"Authorization": f"Bearer {keys['A']}"}
        data = client.get("/v1/wait_phase", headers=headers, params={"after_seq": 1, "max_seconds": 5}).json()
    assert data["changed"] is True and data["phase"] == "actions"


def test_no_unauthenticated_docs(env):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert env.client.get(path).status_code == 404


def test_bad_keys_do_not_rescan_disk(env, monkeypatch):
    registry = env.client.app.state.registry
    scans = []
    real_refresh = registry.refresh
    monkeypatch.setattr(registry, "refresh", lambda: (scans.append(1), real_refresh()))
    for key in ("", "garbage", "Bearer x"):
        assert env.client.get("/v1/phase", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert scans == []
    registry._refreshed_at -= registry.refresh_interval
    for _ in range(3):
        assert env.client.get("/v1/phase", headers={"Authorization": "Bearer dah_g1_A_nope"}).status_code == 401
    assert scans == [1]


def test_host_reports_are_checked(env):
    def post(path: str, body: dict, **headers: str) -> dict:
        return env.client.post(path, json=body, headers={**env.headers["A"], **headers}).json()

    assert post("/v1/telemetry", {"kind": "wake_failed", "detail": {"x": "y" * 3000}})["error"]["code"] == "INVALID"
    assert post("/v1/telemetry", {"kind": "wake_failed", "detail": "text"})["error"]["code"] == "INVALID"
    assert post("/v1/spend", {"spent_usd": 1, "source": "host"})["error"]["code"] == "FORBIDDEN"
    assert post("/v1/spend", {"spent_usd": 1, "source": "host"}, **{"X-DAH-Admin-Key": "wrong"})["ok"] is False
    assert post("/v1/spend", {"spent_usd": 1, "source": "host"}, **{"X-DAH-Admin-Key": ADMIN})["ok"]
    bad_tokens = {"spent_usd": 1, "source": "participant_program", "tokens": "many"}
    assert post("/v1/spend", bad_tokens)["error"]["code"] == "INVALID"
    big = env.client.post("/v1/orders", content=b'{"orders": [], "pad": "' + b"x" * 70_000 + b'"}',
                          headers=env.headers["A"])
    assert big.status_code == 413 and big.json()["error"]["code"] == "TOO_LARGE"


def test_spend_names_the_closed_phase(env):
    env.next_phase()
    env.ok("A", "POST", "/v1/spend", json={"spent_usd": 0.3, "source": "participant_program",
                                           "turn": 1, "phase": "negotiation_1"})
    assert env.call("A", "POST", "/v1/spend", json={"spent_usd": 0.3, "source": "participant_program",
                                                    "turn": 5, "phase": "actions"})["error"]["code"] == "INVALID"
    (spend,) = env.events("spend_report")
    assert (spend["turn"], spend["phase"]) == (1, "negotiation_1")


def test_calls_counted_in_the_phase_they_arrived_in(env):
    from dah.report import build_report

    host = {**env.headers["A"], "X-DAH-Host": "waker"}
    env.client.get("/v1/phase", headers=host)
    env.ready("A")
    env.ready("B")  # this call closes П1
    closing = [c for c in env.events("call") if c["route"] == "POST /v1/ready" and c["seat"] == "B"]
    assert closing[0]["phase"] == "negotiation_1"
    report = build_report(read_events(env.gdir.journal_path))
    p1, actions = report["phases"][0], report["phases"][1]
    assert p1["seats"]["B"]["calls"] == 2 and p1["seats"]["B"]["first_call_s"] is not None
    assert "B" not in actions["seats"]
    assert report["totals"]["A"]["host_calls"] == 1 and p1["seats"]["A"]["calls"] == 2


def test_server_opens_first_phase_of_unstarted_game(tmp_path):
    clock = FakeClock()
    config = slice_config(game_id="g3", scenario="core:slice2", seats=SEATS, seed=7)
    gdir, keys = init_game_dir(tmp_path, config)
    Game.create(gdir, clock, start=False)
    clock.now += 3600
    client = TestClient(create_app(str(tmp_path), clock=clock, tick_interval=None))
    phase = client.get("/v1/phase", headers={"Authorization": f"Bearer {keys['A']}"}).json()
    assert phase["phase"] == "negotiation_1" and phase["phase_seq"] == 1
    assert phase["seconds_left"] == config["phase_limits"]["negotiation_1"]
