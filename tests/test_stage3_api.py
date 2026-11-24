"""Stage 3a game layer, HTTP API, bridge and rules (docs/SPY_AND_CONQUEST.md 6, 8) on the fixture pack tests/packs/t3:
the three espionage tools and their compact answers, lenient forms of the new orders, the P2 intel tail of phase info
and wake texts, the rules sections ШПИОНАЖ/ЗАВОЕВАНИЕ, report texts of espionage events, the spectator's public
sabotage line and the get_state budget of the new sections.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from conquest_helpers import MIL, PACKS, board, book, occupy, ruleset
from dah.bridge.client import GameClient
from dah.bridge.tools import TOOLS, Bridge
from dah.engine import (SeatOrders, TurnReport, amend_order, render_state_text, rules_text, rules_topic,
                        submit_orders, topic_key)
from dah.engine import espionage as esp
from dah.engine.rules import sections
from dah.engine.view import event_text, render_report_text
from dah.game import Game, intel_tail, make_config
from dah.journal import read_events
from dah.server import create_app
from dah.server.spectate import _public_events
from dah.store import init_game_dir
from dah.waker.text import wake_text

SEATS = [{"seat": s, "nick": f"Ник{s}", "kind": "agent", "faction": "iron"} for s in "ABC"]
ADMIN = "admin-secret"


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


class Env:
    def __init__(self, tmp_path, scenario: str = "tri_all", seed: int = 4) -> None:
        self.clock = Clock()
        config = make_config(game_id="s3", scenario=scenario, seats=SEATS, seed=seed, pack_dirs=PACKS)
        self.gdir, self.keys = init_game_dir(tmp_path, config)
        Game.create(self.gdir, self.clock)
        self.client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, clock=self.clock, tick_interval=None))
        self.headers = {seat: {"Authorization": f"Bearer {key}"} for seat, key in self.keys.items()}

    def ok(self, seat: str, method: str, path: str, **kwargs) -> dict:
        data = self.client.request(method, path, headers=self.headers[seat], **kwargs).json()
        assert data["ok"], data
        return data

    def next_phase(self) -> None:
        for seat in "ABC":
            phase = self.ok(seat, "GET", "/v1/phase")
            self.ok(seat, "POST", "/v1/ready", json={"confirm_hash": phase.get("orders_hash")})

    def bridge(self, seat: str) -> Bridge:
        def forward(request: httpx.Request) -> httpx.Response:
            resp = self.client.request(request.method, str(request.url.raw_path, "ascii"),
                                       headers=dict(request.headers), content=request.content)
            return httpx.Response(resp.status_code, content=resp.content,
                                  headers={"content-type": "application/json"})
        return Bridge(GameClient("http://game.test", self.keys[seat], transport=httpx.MockTransport(forward)))

    def events(self, kind: str) -> list[dict]:
        return [e for e in read_events(self.gdir.journal_path) if e["type"] == kind]


# ---------------------------------------------------------------- tools, HTTP and the bridge

def test_espionage_tools_over_bridge_are_compact_and_journaled(tmp_path):
    env = Env(tmp_path)
    a = env.bridge("A")
    phase = env.ok("A", "GET", "/v1/phase")
    assert {"list_spies", "get_intel", "estimate_mission"} <= set(phase["allowed"]) and "intel" not in phase
    env.next_phase()                                                        # П1 -> Д (turn 1)
    text, err = a.call("submit_orders", {"orders": {"kind": "hire"}})       # lenient kind
    assert not err and '"hire_shade"' in text, text
    env.next_phase()                                                        # Д -> разведсрез -> П2
    p2 = env.ok("A", "GET", "/v1/phase")
    assert p2["intel"] == {"reports": 0, "caught": 0} and "донесений: 0" in p2["text"]
    assert "Донесений: 0 — dah.get_intel()." in wake_text(p2)
    env.next_phase()                                                        # расчёт хода 1
    env.next_phase()                                                        # П1 -> Д (turn 2)
    b_land = [p["id"] for p in env.ok("B", "GET", "/v1/state", params={"format": "json"})["state"]["provinces"]
              if p.get("owner") == "B"]
    target = sorted(b_land)[-1]
    text, err = a.call("validate_orders", {"orders": [{"kind": "spy", "shade": 1, "mission": "Наблюдение",
                                                       "province": target, "turns": 2}]})
    assert not err and '"ok":true' in text.replace(" ", ""), text
    text, err = a.call("submit_orders", {"orders": [{"kind": "spy", "shade": "s1", "mission": "observe",
                                                     "target": target, "rounds": 2}]})
    assert not err, text
    env.next_phase()                                                        # разведсрез хода 2 -> П2
    text, err = a.call("list_spies", {})
    assert not err and text.startswith("S1 C1 · Теней 1/2") and len(text) <= 600 and "{" not in text
    text, err = a.call("get_intel", {})
    assert not err and len(text) <= 1500 and not text.rstrip().endswith("}")
    text, err = a.call("estimate_mission", {"mission": "observe", "province": target, "rounds": "1"})
    assert not err and "раунды" in text and len(text) <= 400 and "{" not in text
    text, err = a.call("estimate_mission", {"mission": "nope", "target": target})
    assert err and "INVALID" in text
    p2 = env.ok("A", "GET", "/v1/phase")
    assert set(p2["intel"]) == {"reports", "caught"} and "Донесений:" in wake_text(p2)
    calls = {e["route"] for e in env.events("call")}
    assert {"GET /v1/spies", "GET /v1/intel", "POST /v1/estimate/mission"} <= calls
    assert any(e["via"] == "get_intel" for e in env.events("intel_read"))


def test_espionage_tools_refuse_and_phase_info_is_unchanged_without_espionage(tmp_path):
    env = Env(tmp_path, "tri_conquest")
    a = env.bridge("A")
    for tool, args in (("list_spies", {}), ("get_intel", {}), ("estimate_mission", {"mission": "observe",
                                                                                     "target": "B2"})):
        text, err = a.call(tool, args)
        assert err and "NOT_AVAILABLE" in text
    env.next_phase()
    text, err = a.call("validate_orders", {"orders": [{"kind": "hire"}]})
    assert not err and "INVALID" in text and "hire_shade" not in text    # no alias while the system is off
    env.next_phase()
    p2 = env.ok("A", "GET", "/v1/phase")
    assert p2["phase"] == "negotiation_2" and "intel" not in p2 and "донесений" not in p2["text"]
    assert "Донесений" not in wake_text(p2)


def test_bridge_schemas_list_the_new_orders_and_tools():
    tools = {t.name: t for t in TOOLS}
    item = tools["submit_orders"].schema["properties"]["orders"]["items"]
    assert {"dispose", "policy", "hire_shade", "spy", "recall"} <= set(item["properties"]["kind"]["enum"])
    assert {"disposition", "shade", "mission", "target", "rounds", "feint"} <= set(item["properties"])
    patch = tools["amend_order"].schema["properties"]["patch"]["properties"]
    assert {"disposition", "feint", "to"} <= set(patch)
    assert tools["estimate_mission"].schema["required"] == ["mission", "target"]
    assert "spying" in tools["rules"].description and "conquest" in tools["rules"].description
    assert intel_tail({"reports": 2, "caught": 1}) == "донесений: 2; поймано у тебя: 1 (get_intel, list_spies)"


# ---------------------------------------------------------------- lenient order forms

def test_lenient_forms_of_dispose_policy_and_spy():
    rs = ruleset("tri_all")
    st = board(rs)
    occupy(st, "B2", "A", "B", since=0)
    res = submit_orders(st, rs, "A", SeatOrders(), [{"kind": "fate", "target": "B2", "decision": "exterminate"},
                                                    {"kind": "policy", "policy": "Подчинить"}], "actions",
                        id_factory=iter(["o1", "o2"]).__next__)
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    dispose, policy = res.book.orders
    assert (dispose.kind, dispose.province, dispose.disposition) == ("dispose", "B2", "t3:exterminate")
    assert policy.disposition == "t3:subjugate"
    cleared = amend_order(st, rs, "A", res.book, "o2", {"disposition": None}, "actions")
    assert cleared.ok and cleared.order.disposition is None
    renamed = amend_order(st, rs, "A", res.book, "o1", {"fate": "liberate", "recipient": "НикC"}, "actions")
    assert renamed.ok and renamed.order.disposition == "t3:liberate" and renamed.order.to == "C"
    esp_rs = ruleset("tri_spy")
    st2 = board(esp_rs)
    st2.players["A"].shades.append({"id": "s1", "xp": 0, "status": "idle", "hired_turn": 0, "mission": None})
    b = book(st2, esp_rs, "A", [{"kind": "spy", "shade_id": "1", "type": "ledger", "player": "НикB",
                                 "duration": 1}])
    order = b.orders[0]
    assert (order.shade, order.mission, order.target, order.rounds) == ("s1", "t3:ledger", "B", 1)


def test_lenient_kinds_stay_invalid_without_their_system():
    rs = ruleset("tri_plain")
    st = board(rs)
    res = submit_orders(st, rs, "A", SeatOrders(), [{"kind": "fate", "province": "B2", "disposition": "raze"}],
                        "actions", id_factory=iter(["o1"]).__next__)
    assert res.results[0].reason == "INVALID"


# ---------------------------------------------------------------- rules

@pytest.mark.parametrize("scenario", ["tri_spy", "tri_all"])
def test_spying_rules_section(scenario):
    rs = ruleset(scenario)
    sec = {s.key: s for s in sections(rs)}["spying"]
    assert len(sec.core) <= 450 and len(sec.short) <= 200 and sec.core.startswith("ШПИОНАЖ")
    assert "25 Сплав" in sec.core and "feint:true" in sec.core and "get_intel" in sec.core
    detail = rules_topic(rs, "spying")
    for word in ("Наблюдение", "Счётная книга", "Чтение приказов", "Саботаж", "estimate_mission", "55%",
                 "PHASE_LOCKED", "атрибуция"):
        assert word in detail, word
    assert rules_topic(rs, "шпионаж") == detail and topic_key("espionage") == "spying"
    assert topic_key("intel") == "spying" and topic_key("feint") == "spying"
    core = rules_text(rs)
    assert len(core) <= 6000 and "ШПИОНАЖ" in core and "кроме донесений Теней" in core
    assert "обманыва" not in detail.lower()                                 # decision 17: no advice to deceive


@pytest.mark.parametrize("scenario", ["tri_plain", "tri_conquest"])
def test_no_spying_rules_without_espionage(scenario):
    rs = ruleset(scenario)
    assert rules_topic(rs, "spying") is None and "ШПИОНАЖ" not in rules_text(rs)
    assert "кроме донесений" not in rules_text(rs)


def test_both_systems_keep_the_rules_core_in_limit():
    rs = ruleset("tri_all")
    text = rules_text(rs)
    assert len(text) <= 6000 and "сокращено" not in text
    assert "ЗАВОЕВАНИЕ" in text and "ШПИОНАЖ" in text and 'rules(topic="spying")' in text


# ---------------------------------------------------------------- report texts and the spectator

def test_espionage_events_have_report_texts_without_the_author_for_the_victim():
    rs = ruleset("tri_all")
    st = board(rs)
    strike = {"kind": "sabotage", "province": "B2", "seats": ["B", "A"], "seat": "A", "victim": "B", "shade": "s1",
              "effects": {"deny_fortify": True, "defense_mul": 0.85, "income_mul": 0}}
    own = event_text(strike, rs, st, "A")
    assert own.startswith("удар твоей Тени s1 по B2") and "оборона ×0.85" in own
    seen = esp.sabotage_seen(strike, "B")
    victim = event_text({**seen, "seats": ["B"]}, rs, st, "B")
    assert victim == "саботаж в B2: укрепление сорвано, оборона ×0.85, доход 0; исполнитель не установлен"
    assert "НикA" not in victim and "s1" not in victim
    events = [{"kind": "feint", "province": "A1", "seats": ["A"], "seat": "A", "order_id": "o1",
               "units": {MIL: 2}, "from": "A1", "to": "B1"},
              {"kind": "mission_started", "province": None, "seats": ["A"], "seat": "A", "shade": "s1",
               "mission": "t3:ledger", "target": "B", "rounds": 2, "phase": "negotiation_2", "first_turn": 3},
              {"kind": "mission_end", "province": None, "seats": ["A"], "seat": "A", "shade": "s1",
               "mission": "t3:observe", "target": "B2", "reason": "recalled", "reports": []},
              {"kind": "strike_cancelled", "province": "B2", "seats": ["A"], "seat": "A", "shade": "s1",
               "victim": "B"}]
    report = TurnReport(turn=2, events=events, by_seat={"A": events}, public={"scores": {}})
    text = render_report_text(report, rs, st, "A")
    assert "ложная запись A1→B1 не исполнялась" in text and "«Счётная книга» → НикB" in text
    assert "(отозвана)" in text and "отменён" in text


def test_delayed_spectator_sees_only_where_a_sabotage_struck(tmp_path):
    config = make_config(game_id="sp", scenario="tri_all", seats=SEATS, seed=4, pack_dirs=PACKS)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, Clock())
    report = {"public": {}, "events": [{"kind": "sabotage", "province": "B2", "seats": ["B", "A"], "seat": "A",
                                        "victim": "B", "shade": "s1", "effects": {"defense_mul": 0.85}},
                                       {"kind": "mission_end", "seats": ["A"], "seat": "A", "shade": "s1"}]}
    feed = _public_events(report, game, game.state)
    assert [(e["kind"], e["province"], e["seats"], e["text"]) for e in feed] == [("sabotage", "B2", [],
                                                                                  "саботаж в B2")]


# ---------------------------------------------------------------- get_state budget (2.7)

def test_new_get_state_sections_add_at_most_400_characters():
    rs = ruleset("tri_all")
    plain = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 3}, "C1": {MIL: 3}})
    st = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 3}, "C1": {MIL: 3}})
    for pid, frm in (("B2", "B"), ("C2", "C"), ("B1", "B")):
        occupy(st, pid, "A", frm, since=1)
        occupy(plain, pid, "A", frm, since=1)
        plain.provinces[pid].status = {}
    st.claims = [{"seat": "B", "province": "B2", "until_turn": 11}, {"seat": "C", "province": "C2", "until_turn": 11}]
    st.players["A"].conquest_policy = "t3:subjugate"
    rounds = [{"seat": "A", "shade": f"s{i}", "mission": "t3:observe", "target": "C1", "round": 1, "rounds": 2,
               "outcome": "found" if i == 1 else "nothing", "report_id": "r1" if i == 1 else None}
              for i in (1, 2, 3)]
    rep = {"turn": 1, "rounds": rounds, "reports": {}, "captures": {"A": [{"province": "A1", "mission": "t3:observe",
                                                                           "target": "A1", "owner": None}]}}
    base = render_state_text(plain, rs, "A")
    full = render_state_text(st, rs, "A") + "\n" + esp.state_line(rep, rs, st, "A")
    assert "ОККУПАЦИЯ" in full and "ПРИТЯЗАНИЯ" in full and "ПОЛИТИКА" in full and "ДОНЕСЕНИЯ" in full
    assert len(full) - len(base) <= 400, len(full) - len(base)


# ---------------------------------------------------------------- goal text, core rules

def test_goal_text_names_the_systems_only_when_on():
    from dah.cli import goal_text
    on = make_config(game_id="g", scenario="tri_all", seats=SEATS, seed=1, pack_dirs=PACKS)
    off = make_config(game_id="g", scenario="tri_plain", seats=SEATS, seed=1, pack_dirs=PACKS)
    assert "dah.get_intel()" in goal_text(on, "A") and "dispose и policy" in goal_text(on, "A")
    assert "шпионаж" not in goal_text(off, "A") and "завоевание" not in goal_text(off, "A")


def test_core_rules_with_both_systems_stay_in_limit():
    from dah.engine import build_ruleset
    rs = build_ruleset(None, "quad4")
    text = rules_text(rs)
    assert len(text) <= 6000 and "сокращено" not in text
    if rs.system("espionage"):
        assert "ШПИОНАЖ" in text and rules_topic(rs, "spying")
