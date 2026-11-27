"""Core 0.8.0, trade step C (docs/TRADE.md 10, 12, 13, 14): the rules budget with spy v2 and trade on, the bots' trade
policies, the simulator's reply passes and gates G1-G7, the report's trade figures (rates, terms of trade, promises,
reciprocity, manual swaps, transfers), the journaled `order_rejected` of a promised move, the goal text and the
`blitz_trade` scenario."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from dah.bots import trade as bt
from dah.bots.builder import BuilderBot
from dah.bots.diplomat import DiplomatBot
from dah.bots.rush import RushBot
from dah.bots.view import Province, StateView
from dah.cli import goal_text
from dah.engine import build_ruleset, default_pack_dirs, orders_hash
from dah.engine import trade as tr
from dah.game import Game, make_config
from dah.journal import read_events, view_events
from dah.report import build_report, render_report
from dah.report_trade import prices, trade_report
from dah.store import init_game_dir
from slice_helpers import slice_config

ROOT = Path(__file__).resolve().parents[1]
SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"},
         {"seat": "C", "nick": "Сид", "kind": "agent"}]
ALLOY, FOOD, ASSAULT = "core:alloy", "core:provisions", "core:assault"


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def make_game(tmp_path, **systems) -> Game:
    config = slice_config(game_id="t1", scenario="core:slice3", seats=SEATS, seed=7,
                          systems={"espionage": False, "conquest": True, "discoveries": False, "trade": True,
                                   **systems})
    return Game.create(init_game_dir(tmp_path, config)[0], Clock())


def ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def to_next_turn(game: Game) -> None:
    turn = game.state.turn
    while game.state.turn == turn and game.phase != "game_over":
        ready_all(game)


def simulate():
    path = ROOT / "scripts" / "simulate.py"
    spec = importlib.util.spec_from_file_location("dah_simulate_c", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate_c"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- rules (docs/TRADE.md 10, SPY_V2.md 9)

ALL_ON = {"espionage": True, "conquest": True, "discoveries": True, "trade": True}


@pytest.mark.parametrize("seats,artifacts,extra", [(2, "draft", None), (3, "draft", None), (4, "draft", None),
                                                   (3, None, None), (2, None, 2)])
def test_quad4_rules_with_spy_v2_and_trade_fit_the_gate(tmp_path, seats, artifacts, extra):
    """Every system on, spy v2 and trade together: Game.rules() <= 5970 and never cut, the setup phase included; the
    ОБМЕН line and the short pacts line point at their topics, the renewal phrase lives in the pacts topic."""
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    config = make_config(game_id="g", scenario="core:quad4", seats=specs, seed=3, systems=ALL_ON, objectives_salt=9,
                         artifacts=artifacts, discoveries={"extra": extra} if extra is not None else None)
    game = Game.create(init_game_dir(tmp_path, config)[0], Clock(), start=False)
    game.start()
    while True:
        text = game.rules()
        assert len(text) <= 5970 and "сокращено" not in text, (game.phase, len(text))
        assert "ОБМЕН: propose_trade" in text and "ШПИОНАЖ" in text and 'Продление: rules(topic="pacts")' in text
        if game.phase != "setup":
            break
        for seat in game.alive_seats():
            game.set_ready(seat, None)
    assert "Продление: пока пакт со сроком действует" in game.rules("pacts")


def test_blitz_trade_scenario_and_its_rules(tmp_path):
    rs = build_ruleset(default_pack_dirs(), "blitz_trade")
    assert all(rs.system(k) for k in ("trade", "espionage", "conquest")) and not rs.system("discoveries")
    assert tr.provinces_on(rs) and tr.reports_on(rs) and tr.via_capture(rs)
    config = make_config(game_id="bt", scenario="core:blitz_trade", seats=SEATS, seed=3, objectives_salt=9)
    game = Game.create(init_game_dir(tmp_path, config)[0], Clock())
    text = game.rules()
    assert len(text) <= 5970 and "сокращено" not in text and "ОБМЕН: propose_trade" in text


# ---------------------------------------------------------------- bots (docs/TRADE.md 12.1)

def prov(pid, owner, kind="core:industrial", units=None, neighbors=(), building_now=()):
    return Province(id=pid, name=pid, type=kind, owner=owner, units=units, buildings=(), neighbors=tuple(neighbors),
                    fortified=False, building_now=tuple(building_now))


def view_of(resources, trades=(), turn=3, phase="negotiation_1", pacts=({"seats": ["A", "B"]},), **extra):
    provinces = {
        "P1": prov("P1", "A", "core:capital", {"assault": 2}, ("P2", "P3")),
        "P2": prov("P2", "A", "core:industrial", {"assault": 1}, ("P1", "P4")),
        "P3": prov("P3", "A", "core:agri", {}, ("P1", "P5")),
        "P4": prov("P4", "B", "core:shrine", {}, ("P2", "P6")),
        "P5": prov("P5", "C", "core:industrial", None, ("P3", "P6")),
        "P6": prov("P6", "B", "core:capital", None, ("P4", "P5")),
    }
    return StateView(turn=turn, phase=phase, me=frozenset({"A", "Нико"}), resources=dict(resources),
                     command_points=3, provinces=provinces, id_prefix="core:", seat="A", turn_limit=10,
                     players=("A", "B", "C"), pacts=tuple(pacts), trades=tuple(trades),
                     nicks={"Нико": "A", "Соло": "B", "Сид": "C"}, **extra)


def offer(give=None, want=None, **extra):
    return {"id": "trade-A-B-1", "with": "Соло", "mine": False, "status": "proposed", "give": give or {},
            "want": want or {}, "proposed_turn": 3, **extra}


def test_values_floors_and_the_builder_accepts_by_worth():
    v = view_of({"alloy": 40, "provisions": 4, "creed": 0})
    assert bt.floor(v, "provisions") == 3 * 3 and bt.floor(v, "alloy") == bt.ALLOY_FLOOR == 12
    assert bt.value(v, "provisions") > bt.value(v, "alloy") == 1.0
    food_for_alloy = offer({"resources": {FOOD: 10}}, {"resources": {ALLOY: 10}})
    assert bt.accepts(v, food_for_alloy, "builder")
    assert not bt.accepts(v, offer({"resources": {ALLOY: 10}}, {"resources": {FOOD: 1}}), "builder")   # floor
    assert not bt.accepts(v, offer({"resources": {ALLOY: 5}}, {"resources": {ALLOY: 10}}), "builder")  # worth
    assert bt.accepts(v, offer({"resources": {ALLOY: 5}}, {"resources": {ALLOY: 10}},
                               later={"give": {"resources": {ALLOY: 6}}, "by_turn": 5}), "builder")
    assert not bt.accepts(v, offer({}, {}, later={"want": {"provinces": ["P2"]}, "by_turn": 5}), "builder")


def test_rush_accepts_only_a_clear_gain_never_land_and_only_cheap_reports():
    v = view_of({"alloy": 40, "provisions": 30})
    assert bt.accepts(v, offer({"resources": {ALLOY: 10}}), "rush")
    assert not bt.accepts(v, offer({"resources": {ALLOY: 11}}, {"resources": {ALLOY: 10}}), "rush")
    assert bt.accepts(v, offer({"resources": {ALLOY: 13}}, {"resources": {ALLOY: 10}}), "rush")
    assert not bt.accepts(v, offer({"provinces": ["P4"]}), "rush")
    heads = {"r1": {"mission": "m", "target": "B", "turn": 3}}
    cheap = offer({"reports": ["r1"]}, {"resources": {ALLOY: bt.RUSH_REPORT_MAX_PRICE}}, heads=heads)
    assert bt.accepts(v, cheap, "rush")         # balance core 0.8.0: a cheap fresh report on a neighbour
    assert not bt.accepts(v, {**cheap, "want": {"resources": {ALLOY: bt.RUSH_REPORT_MAX_PRICE + 1}}}, "rush")
    assert not bt.accepts(v, {**cheap, "want": {"resources": {FOOD: 2}}}, "rush")
    assert not bt.accepts(v, {**cheap, "give": {"reports": ["r1"], "resources": {ALLOY: 1}}}, "rush")
    assert not bt.accepts(view_of({"alloy": 15}), cheap, "rush")                       # its alloy floor


def test_provinces_bought_by_the_builder_and_sold_by_the_diplomat():
    v = view_of({"alloy": 40, "provisions": 30})
    assert bt.accepts(v, offer({"provinces": ["P4"]}, {"resources": {ALLOY: 12}}), "builder")    # P2's army beside
    assert not bt.accepts(v, offer({"provinces": ["P4"]}, {"resources": {ALLOY: 16}}), "builder")  # too dear
    poor = view_of({"alloy": 40, "provisions": 12})
    assert not bt.accepts(poor, offer({"provinces": ["P4"]}, {"resources": {ALLOY: 5}}), "builder")  # no subjugation
    sell = offer({"resources": {ALLOY: bt.PROVINCE_PRICE}}, {"provinces": ["P3"]})
    assert bt.accepts(v, sell, "diplomat") and not bt.accepts(v, sell, "builder")
    assert not bt.accepts(v, offer({"resources": {ALLOY: 30}}, {"provinces": ["P1"]}), "diplomat")   # a capital
    assert not bt.accepts(v, {**sell, "with": "Сид"}, "diplomat")                                    # no pact
    assert not bt.accepts(v, offer({"resources": {ALLOY: 5}}, {"provinces": ["P3"]}), "diplomat")


def test_report_heads_bought_when_the_target_is_a_neighbour():
    v = view_of({"alloy": 40, "provisions": 30})
    fresh = offer({"reports": ["r1"]}, {"resources": {ALLOY: bt.REPORT_MAX_PRICE}},
                  heads={"r1": {"mission": "m", "target": "C", "turn": 2}})
    assert bt.accepts(v, fresh, "builder")
    assert not bt.accepts(v, {**fresh, "want": {"resources": {ALLOY: bt.REPORT_MAX_PRICE + 1}}}, "builder")
    assert not bt.accepts(v, {**fresh, "heads": {"r1": {"mission": "m", "target": "C", "turn": 1}}}, "builder")
    assert bt.report_useful(v, {"mission": "m", "target": "P5", "turn": 3})
    assert not bt.report_useful(v, {"mission": "m", "target": "P1", "turn": 3})       # our own province


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def send_gift(self, to, resources):
        self.calls.append(("gift", to, resources))
        return {}

    def propose_trade(self, to, give=None, want=None, **extra):
        self.calls.append(("propose", to, give, want, extra))
        return {}

    def respond_trade(self, trade_id, decision):
        self.calls.append(("respond", trade_id, decision))
        return {}


def done(later, turn=3, mine=True):
    return {"id": "trade-A-B-1", "with": "Соло", "mine": mine, "status": "done", "give": {}, "want": {},
            "proposed_turn": turn, "settled_turn": turn, "later": later}


def test_promises_kept_by_the_builder_and_broken_by_the_diplomat_at_the_end():
    builder, diplomat = BuilderBot("A", 1), DiplomatBot("A", 1)
    promise = {"give": {"resources": {ALLOY: 10}}, "by_turn": 5}
    api = FakeApi()
    bt.keep_promises(builder, view_of({"alloy": 40}, [done(promise)], turn=3), api)
    assert api.calls == []                                  # not before the turn after the trade
    bt.keep_promises(builder, view_of({"alloy": 40}, [done(promise)], turn=4), api)
    assert api.calls == [("gift", "B", {ALLOY: 10})] and builder.trade_mem.promises["kept"] == 1
    bt.keep_promises(builder, view_of({"alloy": 40}, [done(promise)], turn=5), api)
    assert len(api.calls) == 1                              # paid once
    late = {"give": {"resources": {ALLOY: 10}}, "by_turn": 9}
    api = FakeApi()
    bt.keep_promises(diplomat, view_of({"alloy": 40}, [done(late, turn=7)], turn=8), api)
    assert api.calls == [] and diplomat.trade_mem.promises["broken"] == 1
    owed = {"want": {"resources": {ALLOY: 4}}, "by_turn": 6}    # the recipient's promise: ours when not `mine`
    api = FakeApi()
    bt.keep_promises(BuilderBot("A", 1), view_of({"alloy": 40}, [done(owed, mine=False)], turn=4), api)
    assert api.calls == [("gift", "B", {ALLOY: 4})]


def test_offers_by_policy():
    builder, diplomat, rush = BuilderBot("A", 2), DiplomatBot("A", 2), RushBot("A", 2)
    assert (builder.trade_policy, diplomat.trade_policy, rush.trade_policy) == ("builder", "diplomat", "rush")
    assert 0.05 <= builder.margin <= 0.25
    rich = view_of({"alloy": 90, "provisions": 30, "creed": 2})
    to, kw = bt.resource_offer(builder, rich)
    assert to == "B" and kw["give"] == {ALLOY: bt.OFFER_GIVE} and set(kw["want"]) == {"core:creed"}
    assert kw["want"]["core:creed"] == round(bt.OFFER_WANT * (1 + builder.margin))
    to, kw = bt.resource_offer(diplomat, rich)
    half = bt.OFFER_GIVE // 2
    assert kw["give"] == {ALLOY: bt.OFFER_GIVE - half} and kw["later"] == {"give": {ALLOY: half}, "by_turn": 5}
    assert bt.resource_offer(builder, view_of({"alloy": bt.SURPLUS, "provisions": 30, "creed": 2})) is None
    assert bt.resource_offer(builder, view_of({"alloy": 90, "provisions": 30, "creed": 2}, pacts=())) is None
    api = FakeApi()
    rush.trade(rich, api)
    assert api.calls == []                                   # rush never offers
    api = FakeApi()
    rush.trade(view_of({"alloy": 40}, [offer({"resources": {ALLOY: 10}})]), api)
    assert api.calls == [("respond", "trade-A-B-1", "accept")]
    hold = view_of({"alloy": 40, "provisions": 30},
                   objectives=({"kind": "hold_type", "params": {"type": "core:shrine"}},))
    to, kw = bt.province_offer(diplomat, hold)
    assert to == "B" and kw == {"give": {ALLOY: bt.PROVINCE_PRICE}, "want": {"provinces": ["P4"]}}
    assert bt.province_offer(diplomat, hold) is None        # asked once


def test_a_report_of_the_last_recon_is_offered_to_a_neighbour_of_its_target():
    bot = BuilderBot("A", 3)
    bot.memory.reports = {"r1": (2, "read_orders", "C"), "r2": (1, "observe", "P5")}
    v = view_of({"alloy": 40}, espionage={"whisper": 1, "shades": []})
    to, kw = bt.report_offer(bot, v)
    assert to == "B" and kw["give"] == {"reports": ["r1"]} and kw["want"][ALLOY] in bt.REPORT_PRICES
    assert bt.report_offer(bot, v) is None                  # once a turn


def test_an_unpayable_accept_becomes_a_reject():
    from dah.bots.client import ApiError

    class Short(FakeApi):
        def respond_trade(self, trade_id, decision):
            if decision == "accept":
                raise ApiError("NOT_ENOUGH_RESOURCES", "в резерве")
            return super().respond_trade(trade_id, decision)

    api = Short()
    assert bt.answer(BuilderBot("A", 1), view_of({"alloy": 40, "provisions": 4},
                                                 [offer({"resources": {FOOD: 10}}, {"resources": {ALLOY: 10}})]),
                     api) == 1
    assert api.calls == [("respond", "trade-A-B-1", "reject")]


def test_the_bots_answer_after_a_proposal_clears_their_ready(tmp_path):
    """Over the game API: an offer clears the recipient's ready; its bot answers in ready_again."""
    game = make_game(tmp_path)
    game.state.players["B"].resources[FOOD] = 2

    class Api:
        def __init__(self, seat):
            self.seat = seat

        def state(self):
            return {"state": game.state_json(self.seat)}

        def orders(self):
            return {"orders_hash": orders_hash(game._book(self.seat))}

        def ready(self, confirm_hash):
            return {}

        def respond_trade(self, trade_id, decision):
            return game.respond_trade(self.seat, trade_id, decision)

    tid = game.propose_trade("A", "Соло", {"provisions": 10}, {"alloy": 5})["id"]
    BuilderBot("B", 1).ready_again(Api("B"), "negotiation_1", {"B", "Соло"})
    assert tr.trade_by_id(game.state, tid)["status"] == "done"


# ---------------------------------------------------------------- simulator (docs/TRADE.md 12.2)

def test_reply_passes_answer_a_later_seats_offer_in_the_same_phase():
    sim = simulate()
    rs = build_ruleset(default_pack_dirs(), "blitz_trade")
    seats = [{"seat": s, "nick": f"bot-{s}"} for s in "ABC"]
    m = sim.Match(rs, seats, "s")
    m.state.phase = "negotiation_1"
    bots = {s: BuilderBot(s, 1) for s in "ABC"}
    apis = {s: sim.EngineApi(m, s) for s in "ABC"}
    m.state.players["A"].resources[FOOD] = 3
    out = apis["C"].propose_trade("bot-A", {FOOD: 10}, {ALLOY: 5})
    assert tr.trade_by_id(m.state, out["id"])["status"] == "proposed"
    sim.reply_passes(m, bots, apis)
    assert tr.trade_by_id(m.state, out["id"])["status"] == "done"
    assert [e["op"]["op"] for e in m.trade_log] == ["trade_offer", "trade_answer"]
    with pytest.raises(sim.ApiError):
        apis["C"].propose_trade("bot-B", {ALLOY: 999})


def test_the_simulator_plays_trade_and_prints_its_gates(tmp_path, capsys):
    sim = simulate()
    base = tmp_path / "off.json"
    assert sim.main(["--scenario", "core:blitz_trade", "--config", "builder,diplomat,builder", "--games", "3",
                     "--trade", "off", "--json-out", str(base)]) == 0
    out = capsys.readouterr().out
    assert "trade gates" not in out
    off = json.loads(base.read_text(encoding="utf-8"))
    assert off["ALL"]["trade"]["games"] == 0 and off["ALL"]["trade"]["idle_resource_turns"]
    assert sim.main(["--scenario", "core:blitz_trade", "--config", "builder,diplomat,builder", "--games", "3",
                     "--trade", "on", "--trade-base", str(base), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    gates = {row["name"].split(":")[0]: row for row in data["trade_gates"]}
    assert {"G1", "G2", "G3", "G4", "G5", "G6", "G7"} <= set(gates)
    names = [row["name"] for row in data["trade_gates"]]
    g1 = next(r for r in data["trade_gates"] if r["name"].startswith("G1"))
    assert g1["status"] in ("PASS", "FAIL") and isinstance(g1["value"], dict)
    g7 = [r for r in data["trade_gates"] if r["name"].startswith("G7")]
    assert all(r["status"] == "PASS" for r in g7), g7
    assert data["ALL"]["trade"]["games"] == 3 and data["ALL"]["trade"]["proposed_per_game"] > 0, names


# ---------------------------------------------------------------- report (docs/TRADE.md 13)

def ev(type_, seq, **fields):
    return {"seq": seq, "ts": float(seq), "type": type_, **fields}


def journal_of_trades():
    res = lambda **kw: {"resources": {f"core:{k}": v for k, v in kw.items()}}      # noqa: E731
    return [
        ev("turn_resolved", 1, turn=0, scores={"A": 1, "B": 1, "C": 1},
           resources={"A": {ALLOY: 10, FOOD: 20}, "B": {ALLOY: 10, FOOD: 20}, "C": {ALLOY: 10, FOOD: 20}}),
        # A gives 10 alloy for 20 provisions now and promises 6 alloy more by turn 3
        ev("trade_proposed", 2, seat="A", to="B", seats=["A", "B"], trade_id="t1", give=res(alloy=10),
           want=res(provisions=20), later={"give": res(alloy=6), "by_turn": 3}, turn=1, phase="negotiation_1"),
        ev("trade_accepted", 5, seat="B", seats=["A", "B"], trade_id="t1", turn=1, phase="negotiation_1"),
        ev("trade_settled", 6, seats=["A", "B"], trade_id="t1", give=res(alloy=10), want=res(provisions=20),
           at="accept", turn=1, phase="negotiation_1"),
        ev("promise_made", 7, seat="A", to="B", goods=res(alloy=6), by_turn=3, seats=["A", "B"], trade_id="t1",
           turn=1),
        # a payment inside a new trade does not keep it ...
        ev("trade_proposed", 8, seat="A", to="B", seats=["A", "B"], trade_id="t2", give=res(alloy=6),
           want=res(provisions=12), turn=2, phase="negotiation_1"),
        ev("trade_accepted", 9, seat="B", seats=["A", "B"], trade_id="t2", turn=2, phase="negotiation_1"),
        ev("trade_settled", 10, seats=["A", "B"], trade_id="t2", give=res(alloy=6), want=res(provisions=12),
           at="accept", turn=2, phase="negotiation_1"),
        # ... a gift of 4 keeps it in part; B and C swap gifts in one turn; C's offer to A is rejected, then lapses
        ev("gift", 11, seat="A", to="B", resources={ALLOY: 4}, turn=2, phase="negotiation_2"),
        ev("gift", 12, seat="B", to="C", resources={FOOD: 3}, turn=2, phase="negotiation_1"),
        ev("gift", 13, seat="C", to="B", resources={ALLOY: 2}, turn=2, phase="negotiation_2"),
        ev("trade_proposed", 14, seat="C", to="A", seats=["A", "C"], trade_id="t3", give=res(alloy=1), want={},
           turn=2, phase="negotiation_1"),
        ev("trade_rejected", 16, seat="A", seats=["A", "C"], trade_id="t3", turn=2, phase="negotiation_1"),
        ev("trade_proposed", 17, seat="C", to="B", seats=["B", "C"], trade_id="t4", give=res(alloy=1), want={},
           turn=3, phase="negotiation_1"),
        ev("trade_lapsed", 18, seats=["B", "C"], trade_id="t4", turn=3),
        # B promises 5 alloy by turn 9, after the end: void
        ev("promise_made", 19, seat="B", to="A", goods=res(alloy=5), by_turn=9, seats=["A", "B"], trade_id="t2",
           turn=2),
        # a province sold by B to C and taken back by B the next turn
        ev("province_transferred", 20, province="P4", status="occupation", to="C", seats=["B", "C"],
           trade_id="t5", turn=2, **{"from": "B"}),
        ev("province_captured", 21, province="P4", by="B", turn=3, mode="annex", status="full", **{"from": "C"}),
        ev("order_rejected", 22, seat="C", order={"id": "o1"}, reason="PROVINCE_PROMISED", turn=2),
        ev("turn_resolved", 23, turn=3, scores={"A": 5, "B": 4, "C": 1},
           resources={"A": {ALLOY: 10, FOOD: 20}, "B": {ALLOY: 10, FOOD: 20}, "C": {ALLOY: 10, FOOD: 20}}),
        ev("game_over", 24, turn=3, winners=["A"]),
    ]


def test_the_trade_report_from_the_journal():
    events = journal_of_trades()
    rep = trade_report(events, ["A", "B", "C"])
    a, b, c = (rep["seats"][s] for s in "ABC")
    assert (a["proposed"], b["received"], b["accepted_as_answerer"], a["accepted_as_proposer"]) == (2, 3, 2, 2)
    assert a["rejected"] == 1 and c["lapsed"] == 1 and a["settled"] == 2 and b["accept_rate"] == 0.667
    assert b["answer_median_s"] == 2.0                      # 3 s and 1 s after the offers
    assert prices(events) == {"alloy": 1.0, "provisions": 0.5}
    rate = rep["game"]["rates"]["alloy→provisions"]
    assert rate["n"] == 2 and rate["median"] == 2.0 and rate["by_turn"] == {"1": 2.0, "2": 2.0}
    # A gave 16 alloy (16) for 32 provisions (16): even terms
    assert a["terms_of_trade"] == 1.0 and a["traded_out"] == 16.0 and b["traded_in"] == 16.0
    rows = {p["trade"]: p for p in rep["game"]["promises"]["rows"]}
    assert rows["t1"]["status"] == "partial"                # the payment in t2 does not count, the gift of 4 does
    assert rows["t2"]["status"] == "void"
    assert a["promises_made"] == 1 and a["promises_partial"] == 1 and a["promises_kept_rate"] == 0.0
    assert rep["game"]["manual_swaps"]["count"] == 1 and rep["game"]["manual_swaps"]["rows"][0]["first"] == "B"
    assert b["one_sided_received"] == 2 and b["repaid_within_3"] == 2       # A's gift and C's gift, both repaid
    pr = rep["game"]["provinces"]
    assert pr["transfer_recapture"] == 1 and b["provinces_sold"] == 1 and c["provinces_bought"] == 1
    assert rep["game"]["promised_move_rejected"] == 1 and c["promised_move_rejected"] == 1
    assert c["kingmaking"] == 0.0 and trade_report([e for e in events if not e["type"].startswith(
        ("trade", "promise", "province_t", "intel_c"))], ["A"]) is None


def test_fulfils_keeps_a_promise_and_fifo_shares_the_transfers():
    res = lambda n: {"resources": {ALLOY: n}}       # noqa: E731
    events = [
        ev("trade_proposed", 1, seat="A", to="B", seats=["A", "B"], trade_id="t1", give={}, want=res(1),
           later={"give": res(5), "by_turn": 2}, turn=1),
        ev("trade_settled", 2, seats=["A", "B"], trade_id="t1", give={}, want=res(1), at="accept", turn=1),
        ev("promise_made", 3, seat="A", to="B", goods=res(5), by_turn=2, trade_id="t1", seats=["A", "B"], turn=1),
        ev("promise_made", 4, seat="A", to="B", goods=res(5), by_turn=3, trade_id="t9", seats=["A", "B"], turn=1),
        ev("trade_proposed", 5, seat="A", to="B", seats=["A", "B"], trade_id="t2", give=res(5), want=res(1),
           fulfils="t1", turn=2),
        ev("trade_settled", 6, seats=["A", "B"], trade_id="t2", give=res(5), want=res(1), at="accept", turn=2),
        ev("gift", 7, seat="A", to="B", resources={ALLOY: 3}, turn=3),
        ev("game_over", 8, turn=5, winners=["A"]),
    ]
    rows = {p["trade"]: p for p in trade_report(events, ["A", "B"])["game"]["promises"]["rows"]}
    assert rows["t1"]["status"] == "kept" and not rows["t1"]["early"]
    assert rows["t9"]["status"] == "partial"        # the fulfils trade went to t1 first; the gift of 3 is t9's


def test_a_promise_of_a_seat_eliminated_before_it_is_due_is_void():
    res = lambda n: {"resources": {ALLOY: n}}       # noqa: E731
    events = [
        ev("promise_made", 1, seat="C", to="B", goods=res(7), by_turn=12, trade_id="t1", seats=["B", "C"], turn=3),
        ev("promise_made", 2, seat="A", to="C", goods=res(2), by_turn=12, trade_id="t2", seats=["A", "C"], turn=3),
        ev("promise_made", 3, seat="A", to="B", goods=res(2), by_turn=6, trade_id="t3", seats=["A", "B"], turn=3),
        ev("turn_resolved", 4, turn=11, scores={"A": 5, "B": 5}, eliminated=["C"]),
        ev("game_over", 5, turn=12, winners=["A"]),
    ]
    rep = trade_report(events, ["A", "B", "C"])
    rows = {p["trade"]: p["status"] for p in rep["game"]["promises"]["rows"]}
    assert rows == {"t1": "void", "t2": "void", "t3": "broken"}     # giver out, taker out, both alive
    assert rep["seats"]["C"]["promises_void"] == 1 and rep["seats"]["C"]["promises_broken"] == 0
    old = [e for e in events if e["type"] != "turn_resolved"] + [   # older journals: only a claim / pact end says it
        ev("claim_ended", 6, turn=11, province="P13", seat="C", reason="eliminated")]
    rows = {p["trade"]: p["status"] for p in trade_report(old, ["A", "B", "C"])["game"]["promises"]["rows"]}
    assert rows["t1"] == "void"


def test_a_real_game_journals_trades_and_the_report_counts_them(tmp_path):
    game = make_game(tmp_path)
    game.state.provinces["P02"].units = {ASSAULT: 2}           # B stands next to A's P07
    sold = game.propose_trade("A", "Соло", {"P07": 1}, {"alloy": 5})["id"]
    tid = game.propose_trade("B", "Сид", {"alloy": 6}, {"provisions": 6},
                             later={"give": {"alloy": 4}, "by_turn": 2})["id"]
    game.respond_trade("C", tid, "accept")
    ready_all(game)
    game.submit("B", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 2}}])
    ready_all(game)
    game.respond_trade("B", sold, "accept")                    # after the move: step 1 drops it
    to_next_turn(game)
    game.send_gift("B", "Сид", {"alloy": 4})
    to_next_turn(game)
    events = read_events(game.gdir.journal_path)
    rejected = [e for e in events if e["type"] == "order_rejected"]
    assert [(e["seat"], e["reason"]) for e in rejected] == [("B", "PROVINCE_PROMISED")]
    assert not [e for e in view_events(events, seat="A") if e["type"] == "order_rejected"]
    report = build_report(events)
    t = report["trade"]
    assert t["seats"]["A"]["provinces_sold"] == 1 and t["seats"]["B"]["provinces_bought"] == 1
    assert t["seats"]["B"]["promised_move_rejected"] == 1
    assert t["game"]["promises"]["rows"][0]["status"] == "kept" and t["seats"]["B"]["promises_kept_rate"] == 1.0
    text = render_report(report)
    assert "ОБМЕН (по журналу" in text and "обмен: предложено 1" in text


# ---------------------------------------------------------------- goal text

def test_the_goal_names_the_trade_tools_and_the_spy_payoff(tmp_path):
    config = make_config(game_id="g", scenario="core:blitz_trade", seats=SEATS, seed=3, objectives_salt=9)
    text = goal_text(config, "A")
    trade = next(x for x in text.splitlines() if x.startswith("В партии есть торговля"))
    assert all(x in trade for x in ("dah.propose_trade", "dah.respond_trade", "dah.list_trades", "копия донесения"))
    spy = next(x for x in text.splitlines() if x.startswith("В партии есть шпионаж"))
    assert 'estimate(defender=\\"r3\\")' not in spy and 'dah.estimate(defender="r3")' in spy
    assert "удары по тебе и нарушения пактов" in spy
    off = make_config(game_id="g", scenario="core:blitz_spy", seats=SEATS, seed=3)
    assert "торговля" not in goal_text(off, "A")
