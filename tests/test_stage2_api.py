"""Stage 2 over HTTP and the bridge (TECH_AND_SETUP.md section 4): setup routes, list_techs, describe, research as
an ordinary order, fog of war (no opponent picks during setup, others' techs hidden), rules split by topic."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from dah.bridge.client import GameClient
from dah.bridge.tools import TOOLS, Bridge
from dah.cli import goal_text
from dah.engine import RULES_LIMIT, rules_text, rules_topic, topic_key
from dah.game import Game, make_config
from dah.journal import read_events
from dah.server import create_app
from dah.store import init_game_dir
from dah.waker.text import wake_text
from stage2_helpers import PACKS, FakeClock, edit_yaml, pack_copy, ruleset

SEATS = [{"seat": "A", "nick": "Альфа", "kind": "agent"}, {"seat": "B", "nick": "Бета", "kind": "swarm"}]
ADMIN = "admin-secret"


class Env:
    def __init__(self, tmp_path, scenario: str = "duel_draft", **kwargs) -> None:
        self.clock = FakeClock()
        config = make_config(game_id="g2", scenario=scenario, seats=SEATS, seed=7, pack_dirs=PACKS, **kwargs)
        self.gdir, keys = init_game_dir(tmp_path, config)
        Game.create(self.gdir, self.clock)
        self.client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, clock=self.clock, tick_interval=None))
        self.keys = keys
        self.headers = {seat: {"Authorization": f"Bearer {key}"} for seat, key in keys.items()}

    def call(self, seat: str, method: str, path: str, **kwargs) -> dict:
        return self.client.request(method, path, headers=self.headers[seat], **kwargs).json()

    def ok(self, seat: str, method: str, path: str, **kwargs) -> dict:
        data = self.call(seat, method, path, **kwargs)
        assert data["ok"], data
        return data

    def err(self, seat: str, method: str, path: str, **kwargs) -> str:
        data = self.call(seat, method, path, **kwargs)
        assert not data["ok"], data
        return data["error"]["code"]

    def ready(self, seat: str) -> dict:
        if self.ok(seat, "GET", "/v1/phase")["phase"] == "setup":
            return self.ok(seat, "POST", "/v1/ready", json={})
        return self.ok(seat, "POST", "/v1/ready",
                       json={"confirm_hash": self.ok(seat, "GET", "/v1/orders")["orders_hash"]})

    def next_phase(self) -> None:
        self.ready("A")
        self.ready("B")

    def bridge(self, seat: str) -> Bridge:
        """A bridge whose HTTP calls go to this app in process."""
        transport = httpx.MockTransport(lambda request: _forward(self.client, request))
        return Bridge(GameClient("http://game.test", self.keys[seat], transport=transport))

    def events(self, kind: str) -> list[dict]:
        return [e for e in read_events(self.gdir.journal_path) if e["type"] == kind]


def _forward(client: TestClient, request: httpx.Request) -> httpx.Response:
    resp = client.request(request.method, str(request.url.raw_path, "ascii"), headers=dict(request.headers),
                          content=request.content)
    return httpx.Response(resp.status_code, content=resp.content, headers={"content-type": "application/json"})


# ---------------------------------------------------------------- setup routes and fog

def test_setup_phase_routes_and_privacy(tmp_path):
    env = Env(tmp_path)
    phase = env.ok("A", "GET", "/v1/phase")
    assert phase["phase"] == "setup" and phase["turn"] == 1 and "get_setup" in phase["hint"]
    assert {"get_setup", "choose_faction", "choose_artifacts", "describe", "list_techs", "ready"} <= set(
        phase["allowed"])
    assert "submit_orders" not in phase["allowed"] and "СТАРТ" in phase["text"].upper()

    setup_a = env.ok("A", "GET", "/v1/setup")
    assert setup_a["open"] and setup_a["artifacts_mode"] == "draft" and len(setup_a["offer"]) == 2
    assert [f["id"] for f in setup_a["factions"]] == ["t2:iron", "t2:ash"] and "text" in setup_a["factions"][0]
    offer_b = [a["id"] for a in env.ok("B", "GET", "/v1/setup")["offer"]]
    assert not {a["id"] for a in setup_a["offer"]} & set(offer_b)          # B's offer is not shown to A

    res = env.ok("A", "POST", "/v1/setup/faction", json={"prefs": ["ash", "iron"]})
    assert res["factions"] == ["t2:ash", "t2:iron"] and res["confirm_hash"]
    pick = setup_a["offer"][0]["id"]
    assert env.ok("A", "POST", "/v1/setup/artifacts", json={"ids": [pick.split(":")[1]]})["artifacts"] == [pick]
    assert env.err("A", "POST", "/v1/setup/artifacts", json={"ids": [offer_b[0]]}) == "NOT_OFFERED"
    assert env.err("A", "POST", "/v1/setup/faction", json={"prefs": ["nobody"]}) == "UNKNOWN_ID"
    # alternative spellings: one id, a comma list
    assert env.ok("A", "POST", "/v1/setup/faction", json={"faction": "ash"})["factions"] == ["t2:ash"]
    assert env.ok("A", "POST", "/v1/setup/faction", json={"factions": "ash, iron"})["factions"] == ["t2:ash",
                                                                                                    "t2:iron"]

    # B sees nothing of A's choice: not in its setup view, state (text or JSON), nor phase
    b_setup = env.ok("B", "GET", "/v1/setup")
    assert b_setup["your_choice"] == {"factions": None, "artifacts": None}
    assert pick not in json.dumps(b_setup)
    b_state = env.ok("B", "GET", "/v1/state", params={"format": "json"})["state"]
    assert b_state["setup"]["provisional"] is True
    assert next(p for p in b_state["players"] if p["seat"] == "A")["faction"] is None
    # nor the seat's own provisional default: the faction is decided when the phase closes (review finding)
    assert next(p for p in b_state["players"] if p["seat"] == "B")["faction"] is None
    assert b_state["you"]["faction"] is None and b_state["you"]["faction_choice"] is None
    who = env.ok("B", "GET", "/v1/whoami")
    assert who["faction"] is None and who["setup_done"] is False
    phase_b = env.ok("B", "GET", "/v1/phase")
    assert {"rules", "whoami", "phase", "wait_phase", "inbox"} <= set(phase_b["allowed"])
    assert "t2:ash" not in json.dumps(b_state["players"]) and pick not in json.dumps(b_state)
    assert env.ok("B", "GET", "/v1/state")["text"].startswith("ФАЗА СТАРТА")

    # orders are locked; ready works without a hash
    assert env.err("A", "POST", "/v1/orders", json={"orders": [{"kind": "research", "tech": "smithing"}]}) == \
        "PHASE_LOCKED"
    assert env.ok("A", "POST", "/v1/ready", json={})["phase_closed"] is False
    env.ready("B")
    assert env.ok("A", "GET", "/v1/phase")["phase"] == "negotiation_1"

    after = env.ok("B", "GET", "/v1/setup")
    assert after["open"] is False and {p["seat"]: p["faction"] for p in after["players"]}["A"] == "t2:ash"
    assert pick not in json.dumps(after)                                   # A's artifact stays hidden from B
    b_json = env.ok("B", "GET", "/v1/state", params={"format": "json"})["state"]
    a_entry = next(p for p in b_json["players"] if p["seat"] == "A")
    assert a_entry["faction"] == "t2:ash" and "artifacts" not in a_entry and "techs" not in a_entry
    assert env.ok("A", "GET", "/v1/state", params={"format": "json"})["state"]["you"]["artifacts"] == [pick]
    assert env.err("A", "POST", "/v1/setup/faction", json={"prefs": ["iron"]}) == "PHASE_LOCKED"


def test_setup_route_without_setup_phase(tmp_path):
    env = Env(tmp_path, scenario="duel_plain")
    assert env.ok("A", "GET", "/v1/setup")["enabled"] is False
    assert env.err("A", "POST", "/v1/setup/faction", json={"prefs": ["iron"]}) == "PHASE_LOCKED"
    assert "get_setup" not in env.ok("A", "GET", "/v1/phase")["hint"]      # P1's ready hint, no setup hint


# ---------------------------------------------------------------- techs, research, describe

def test_research_is_an_ordinary_order_and_others_techs_stay_hidden(tmp_path):
    env = Env(tmp_path, scenario="duel_plain")
    tree = env.ok("A", "GET", "/v1/techs")
    assert tree["enabled"] and tree["resource"] == "t2:creed" and tree["active"] is None
    smithing = next(t for t in tree["techs"] if t["id"] == "t2:smithing")
    assert smithing["status"] == "available" and smithing["cost"] == 3       # iron: war branch ×0.5
    assert all(t["id"] != "t2:banking" for t in tree["techs"]) and tree["locked"] >= 1    # short by default
    assert "НАУКА" in tree["text"] and "list_techs(full=true)" in tree["text"]
    full = env.ok("A", "GET", "/v1/techs", params={"full": "true"})
    assert next(t for t in full["techs"] if t["id"] == "t2:banking")["status"] == "locked" and full["full"] is True
    assert len(full["text"]) > len(tree["text"])
    short_text, _ = env.bridge("A").call("list_techs", {})
    full_text, _ = env.bridge("A").call("list_techs", {"full": True})
    assert "НАУКА" in short_text and '"techs":' not in short_text and len(full_text) > len(short_text)

    # П1: validate only
    check = env.ok("A", "POST", "/v1/orders/validate", json={"orders": [{"kind": "research", "tech": "Кузнечное дело"}]})
    assert check["ok_count"] == 1
    assert env.err("A", "POST", "/v1/orders", json={"orders": [{"kind": "research", "tech": "smithing"}]}) == \
        "PHASE_LOCKED"
    env.next_phase()                                                        # → actions
    sub = env.ok("A", "POST", "/v1/orders", json={"orders": [{"kind": "research", "technology": "smithing"}]})
    assert [o["tech"] for o in sub["accepted"]] == ["t2:smithing"]
    second = env.ok("A", "POST", "/v1/orders", json={"orders": [{"kind": "research", "tech": "drills"}],
                                                      "mode": "append"})
    assert second["rejected"][0]["reason"] == "INVALID"                     # one research per book
    provinces = env.ok("A", "GET", "/v1/state", params={"format": "json"})["state"]["provinces"]
    capital = next(p["id"] for p in provinces if p["owner"] == "A" and p["type"] == "t2:capital")
    bad = env.ok("A", "POST", "/v1/orders", json={"orders": [{"kind": "recruit", "province": capital,
                                                               "unit": "knight", "count": 1}], "mode": "append"})
    assert bad["rejected"][0]["reason"] == "NOT_AVAILABLE"                  # needs smithing and a smithy
    oid = sub["accepted"][0]["id"]
    amended = env.ok("A", "PATCH", f"/v1/orders/{oid}", json={"tech": "smithing"})
    assert amended["order"]["tech"] == "t2:smithing"
    env.next_phase()                                                        # → П2
    env.next_phase()                                                        # → resolution, turn 2

    mine = env.ok("A", "GET", "/v1/techs")
    assert mine["researched"] == ["t2:smithing"]
    other = env.ok("B", "GET", "/v1/techs")
    assert other["researched"] == [] and "t2:smithing" not in json.dumps(other["researched"])
    b_state = env.ok("B", "GET", "/v1/state", params={"format": "json"})["state"]
    a_entry = next(p for p in b_state["players"] if p["seat"] == "A")
    assert a_entry["techs_count"] == 1 and "techs" not in a_entry and "research" not in a_entry
    assert "t2:smithing" not in json.dumps(b_state)
    assert "smithing" not in env.ok("B", "GET", "/v1/state")["text"].lower()
    assert "smithing" not in env.ok("B", "GET", "/v1/history")["text"].lower()
    assert env.ok("A", "GET", "/v1/state", params={"format": "json"})["state"]["you"]["techs"] == ["t2:smithing"]
    admin = env.client.get("/v1/admin/games/g2", headers={"Authorization": f"Bearer {ADMIN}"}).json()
    assert admin["seats"]["A"]["techs"] == ["t2:smithing"]


def test_describe_any_content_by_id_or_name(tmp_path):
    env = Env(tmp_path)
    knight = env.ok("A", "GET", "/v1/describe", params={"id": "knight"})
    assert knight["kind"] == "unit" and knight["id"] == "t2:knight" and "Рыцарь" in knight["text"]
    assert env.ok("A", "GET", "/v1/describe", params={"name": "Кузнечное дело"})["kind"] == "tech"
    assert env.ok("A", "GET", "/v1/describe", params={"id": "t2:ember"})["kind"] == "artifact"
    assert env.ok("A", "GET", "/v1/describe", params={"id": "iron"})["kind"] == "faction"
    assert env.err("A", "GET", "/v1/describe", params={"id": "nothing"}) == "NOT_FOUND"
    assert env.err("A", "GET", "/v1/describe") == "NOT_FOUND"


def test_estimate_uses_the_callers_side(tmp_path):
    env = Env(tmp_path, scenario="duel_plain")
    body = {"attacker": {"militia": 3}, "defender": {"militia": 3}, "province_type": "forge"}
    plain = env.ok("A", "POST", "/v1/estimate", json=body)
    assert plain["side"] == "attacker" and 0 <= plain["win_prob"] <= 1
    assert env.ok("A", "POST", "/v1/estimate", json={**body, "side": "defender", "opponent": "Бета"})["side"] == \
        "defender"
    assert env.err("A", "POST", "/v1/estimate", json={**body, "side": "middle"}) == "INVALID"
    assert env.err("A", "POST", "/v1/estimate", json={**body, "opponent": "Никто"}) == "UNKNOWN_ID"


# ---------------------------------------------------------------- bridge tools against the real app

def test_bridge_setup_tools_end_to_end(tmp_path):
    env = Env(tmp_path)
    a = env.bridge("A")
    names = {t.name for t in TOOLS}
    assert {"get_setup", "choose_faction", "choose_artifacts", "list_techs", "describe"} <= names
    text, err = a.call("get_setup", {})
    assert not err and text.startswith("ФАЗА СТАРТА")
    text, err = a.call("choose_faction", {"prefs": '["ash", "iron"]'})
    assert not err and "Пепельный Двор" in text
    offer = env.ok("A", "GET", "/v1/setup")["offer"][0]["id"].split(":")[1]
    text, err = a.call("choose_artifacts", {"ids": offer})
    assert not err, text
    text, err = a.call("choose_artifacts", {"ids": ["nope"]})
    assert err and "UNKNOWN_ID" in text
    text, err = a.call("describe", {"id": "ram"})
    assert not err and ("siege" in text.lower() or "оборона провинции" in text)
    text, err = a.call("list_techs", {})
    assert not err and "НАУКА" in text
    text, err = a.call("rules", {"topic": "techs"})
    assert not err and text.startswith("ТЕХНОЛОГИИ") and "Кузнечное дело" in text
    text, err = a.call("ready", {})
    assert not err, text
    text, err = a.call("choose_faction", {})
    assert err and "prefs" in text


def test_bridge_research_order_forms(tmp_path):
    env = Env(tmp_path, scenario="duel_plain")
    env.next_phase()
    a = env.bridge("A")
    text, err = a.call("validate_orders", {"orders": {"kind": "research", "tech": "smithing"}})
    assert not err and '"ok_count":1' in text
    text, err = a.call("submit_orders", {"orders": json.dumps([{"kind": "research", "tech": "Кузнечное дело"}])})
    assert not err and "t2:smithing" in text
    order_id = env.ok("A", "GET", "/v1/orders")["orders"][0]["id"]
    text, err = a.call("amend_order", {"id": order_id, "patch": '{"tech": "drills"}'})
    assert not err and "t2:drills" in text


# ---------------------------------------------------------------- rules split

@pytest.mark.parametrize("scenario", ["duel_draft", "duel_points", "duel_plain"])
def test_rules_core_is_short_with_contents(tmp_path, scenario):
    env = Env(tmp_path, scenario=scenario)
    text = env.ok("A", "GET", "/v1/rules")["text"]
    assert len(text) <= RULES_LIMIT
    assert "ОГЛАВЛЕНИЕ" in text and "techs" in text and "СООБЩЕНИЯ" in text and '"kind":"research"' in text
    techs = env.ok("A", "GET", "/v1/rules", params={"topic": "techs"})["text"]
    assert techs.startswith("ТЕХНОЛОГИИ") and "Кузнечное дело" in techs and "Банки" in techs and "ПРИКАЗЫ" not in techs
    factions = env.ok("A", "GET", "/v1/rules", params={"topic": "factions"})["text"]
    assert factions.startswith("ФРАКЦИИ") and "Железный Обет" in factions
    pacts = env.ok("A", "GET", "/v1/rules", params={"topic": "pacts"})["text"]
    assert pacts.startswith("ДИПЛОМАТИЯ") and "ВИДИМОСТЬ" not in pacts
    combat = env.ok("A", "GET", "/v1/rules", params={"topic": "combat"})["text"]
    assert combat.startswith("РАСЧЁТ") and "siege" in combat
    messages = env.ok("A", "GET", "/v1/rules", params={"topic": "messages"})["text"]
    assert messages.startswith("СООБЩЕНИЯ")
    if scenario != "duel_plain":
        assert env.ok("A", "GET", "/v1/rules", params={"topic": "setup"})["text"].startswith("СТАРТ")
        arts = env.ok("A", "GET", "/v1/rules", params={"topic": "artifacts"})["text"]
        assert arts.startswith("АРТЕФАКТЫ") and "Тлеющая реликвия" in arts
    # a content id is described; an unknown topic lists the topics and gives the core
    assert "Рыцарь" in env.ok("A", "GET", "/v1/rules", params={"topic": "knight"})["text"]
    unknown = env.ok("A", "GET", "/v1/rules", params={"topic": "погода"})["text"]
    assert "не найдена" in unknown and "techs" in unknown


def test_topic_aliases():
    assert topic_key("ТЕХНОЛОГИИ") == "techs" and topic_key("diplomacy") == "pacts" and topic_key("research") == "techs"
    assert topic_key("nope") is None and topic_key("") is None


def test_rules_core_stays_within_limit_when_content_grows(tmp_path):
    pack = pack_copy(tmp_path)

    def more_units(units):
        units += [{"id": f"extra{i}", "name": f"Запасной отряд номер {i}", "short": f"зап{i}", "attack": 2,
                   "defense": 2, "cost": {"alloy": 10}, "upkeep": {"provisions": 1}} for i in range(40)]

    def more_techs(techs):
        techs += [{"id": f"t{i}", "name": f"Технология номер {i}", "branch": "economy", "tier": 1, "cost": 5}
                  for i in range(40)]

    edit_yaml(pack / "units" / "units.yaml", more_units)
    edit_yaml(pack / "techs" / "techs.yaml", more_techs)
    from dah.engine import build_ruleset
    rs = build_ruleset([str(pack)], "duel_draft")
    assert len(rules_text(rs, limit=10 ** 6)) > RULES_LIMIT
    text = rules_text(rs)
    assert len(text) <= RULES_LIMIT and "ОГЛАВЛЕНИЕ" in text and "ЮНИТЫ" in text and '"kind":"move"' in text
    assert "extra39" in rules_topic(rs, "units") and "Технология номер 39" in rules_topic(rs, "techs")


def test_rules_text_without_stage2_content_has_no_stage2_sections():
    text = rules_text(ruleset("duel_plain"), setup=None)
    assert "СТАРТ" not in text and "ФРАКЦИИ" not in text           # one faction, no setup


# ---------------------------------------------------------------- waker and goal texts

def test_wake_text_for_setup_phase():
    text = wake_text({"turn": 1, "turn_limit": 8, "phase": "setup", "deadline": "2026-01-01T10:00:00Z",
                      "seconds_left": 120, "ready": "0/2"})
    assert text.startswith("[DAH] Ход 1/8, фаза СТАРТ") and "dah.get_setup()" in text and "dah.choose_faction" in text


def test_goal_text_mentions_setup_and_research(tmp_path):
    env = Env(tmp_path)
    config = json.loads(env.gdir.config_path.read_text(encoding="utf-8"))
    goal = goal_text(config, "A")
    assert "фаза старта" in goal and "dah.choose_faction" in goal and "dah.choose_artifacts" in goal
    assert '"research"' in goal and "dah.describe" in goal
    plain = Env(tmp_path / "plain", scenario="duel_plain")
    plain_goal = goal_text(json.loads(plain.gdir.config_path.read_text(encoding="utf-8")), "A")
    assert "фаза старта" not in plain_goal
