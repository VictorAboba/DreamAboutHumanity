"""Review fixes of core 0.7.0: the total war cap stops the credit (no false «+1»), intel and the army fog need espionage,
a work paid in parts promises its ready turn only as «не раньше», the delayed spectator gets no intel."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

from dah.engine import (build_ruleset, issue_order_id, new_game_state, resolve, rules_text, rules_topic,
                        submit_orders, view_state)
from dah.engine import scoring as sc
from dah.engine.model import GameState, SeatOrders
from dah.engine.view import event_text
from dah.game import Game, make_config
from dah.server.spectate import DELAYED, LIVE, GameCache, live, snapshot
from dah.store import init_game_dir
from test_review_stage2 import FakeClock

ROOT = Path(__file__).resolve().parents[1]
SEATS3 = [{"seat": s, "nick": s} for s in "ABC"]
ESPIONAGE_SCORED = ("quad4", "slice3", "slice2", "blitz_score", "blitz_finds")
AT_CAP = {"P05": {"points": 3, "since": None}, "P06": {"points": 3, "since": None},
          "P08": {"points": 2, "since": None}}


def _orders(st: GameState, rs, seat: str, items: list[dict]) -> SeatOrders:
    st.phase = "actions"
    res = submit_orders(st, rs, seat, SeatOrders(), items, "actions", id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def _capture_state():
    """blitz_score: A next to C's empty agri P07 with a strong army in its forge P02."""
    rs = build_ruleset(None, "blitz_score")
    st = new_game_state(rs, SEATS3, 4)
    st.provinces["P02"].units = {"core:assault": 8}
    return rs, st


def _war_events(rep) -> list[dict]:
    return [e for e in rep.events if e["kind"] == "war_points"]


def _take_p07(rs, st):
    book = _orders(st, rs, "A", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 8}}])
    st, rep = resolve(st, {"A": book}, rs, 3)
    assert st.provinces["P07"].owner == "A"
    return st, rep


# ---------------------------------------------------------------- ENGINE-1 / SPEC-3: the total war cap

def test_a_seat_at_the_total_war_cap_gets_no_capture_point_nor_notice():
    rs, st = _capture_state()
    st.players["A"].tally["war_provinces"] = {k: dict(v) for k, v in AT_CAP.items()}
    assert sc.war(st, rs, "A") == 8 and sc.war_cap_left(st, rs, "A") == 0
    st, rep = _take_p07(rs, st)
    assert not _war_events(rep)                                         # no event, so no «война: +1» notice
    texts = [event_text(e, rs, st, "A") for e in rep.by_seat.get("A", [])]
    assert not any(t.startswith("война: +") for t in texts)
    assert sc.war_provinces(st, "A")["P07"] == {"points": 0, "since": rep.turn}    # held from the capture
    you = view_state(st, rs, "A", None)["you"]["war"]
    assert you["points"] == 8 and sum(you["provinces"].values()) == 8
    for _ in range(4):                                                  # holding at the cap pays nothing either
        st, rep = resolve(st, {}, rs, 3)
        assert not _war_events(rep)
    assert sc.war_provinces(st, "A")["P07"]["points"] == 0


def test_the_last_point_below_the_total_cap_is_credited_and_then_nothing():
    rs, st = _capture_state()
    st.players["A"].tally["war_provinces"] = {**{k: dict(v) for k, v in AT_CAP.items()},
                                              "P08": {"points": 1, "since": None}}
    st, rep = _take_p07(rs, st)
    assert [(e["reason"], e["points"]) for e in _war_events(rep)] == [("capture", 1)]
    assert sc.war(st, rs, "A") == 8
    for _ in range(2):                                                  # 2 turns held: would be a hold point
        st, rep = resolve(st, {}, rs, 3)
        assert not _war_events(rep)
    assert sc.war_provinces(st, "A")["P07"]["points"] == 1


def _simulate_module():
    spec = importlib.util.spec_from_file_location("dah_simulate_review070", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate_review070"] = module
    spec.loader.exec_module(module)
    return module


def test_in_a_bot_war_the_credited_events_never_pass_the_total_cap(monkeypatch):
    sim = _simulate_module()
    events: list[dict] = []
    real = sc.credit_provinces

    def spy(*args, **kwargs):
        out = real(*args, **kwargs)
        events.extend(out)
        return out

    monkeypatch.setattr(sc, "credit_provinces", spy)
    rs = build_ruleset(None, "quad4")
    sim.play_game(rs, {"A": "rush", "B": "rush", "C": "rush", "D": "builder"}, "review070-1")
    assert events                                                       # the bots did fight
    per_seat: dict[str, int] = {}
    for e in events:
        assert e["points"] == (0 if e["reason"] == "reclaim" else 1)       # core 0.7.1: own land back pays 0
        per_seat[e["seat"]] = per_seat.get(e["seat"], 0) + e["points"]
    assert max(per_seat.values()) <= 8, per_seat                         # what the report and the notices sum


# ---------------------------------------------------------------- ENGINE-2 / SPEC-2: intel and fog need espionage

def test_without_espionage_intel_and_the_army_fog_leave_no_trace():
    for sid in ESPIONAGE_SCORED:
        on = build_ruleset(None, sid)
        off = build_ruleset(None, sid, systems={"espionage": False})
        assert sc.on(on, "intel") and on.stat("fog.army_presence") == 1
        assert not off.system("espionage") and not sc.on(off, "intel") and "intel" not in sc.turn_sources(off)
        assert off.stat("fog.army_presence") == 0
        assert "intel" not in off.to_dict()["scenario"]["scoring"]
        plain = build_ruleset(None, sid, systems={"espionage": False},
                              scoring={k: v for k, v in on.scenario.scoring.items() if k != "intel"})
        assert off.hash == plain.hash, sid
        text = rules_text(off)
        victory = next(line for line in text.splitlines() if line.startswith("ПОБЕДА"))
        assert "разведка" not in victory and "Разведка" not in rules_topic(off, "victory")
        assert "есть ли армия" not in text
        st = new_game_state(off, SEATS3[:len(off.map.starts)], 4)
        assert "intel" not in view_state(st, off, "A", None)["you"]
        assert all("intel" not in row for row in sc.score_parts(st, off).values())


def test_the_host_cannot_weigh_intel_without_espionage():
    rs = build_ruleset(None, "quad4", systems={"espionage": False}, scoring={"territory": 1, "intel": 2})
    assert rs.weight("intel") == 0 and sc.turn_sources(rs) == ["territory"]


def test_the_scenarios_with_espionage_keep_their_hashes():
    # core 0.7.0's quad4 (now the frozen copy; measurement m9 was journaled under it)
    legacy = Path(__file__).resolve().parents[1] / "content" / "legacy" / "core-0.7.0"
    assert build_ruleset([str(legacy)], "quad4").hash.startswith("e198c9bc")


# ---------------------------------------------------------------- ENGINE-3: the ready turn of a work paid in parts

def test_a_work_paid_in_parts_promises_its_ready_turn_only_as_not_before():
    rs = build_ruleset(None, "blitz_score")
    st = new_game_state(rs, SEATS3, 4)
    spire = {"kind": "work_started", "seat": "A", "building": "core:dream_spire", "province": "P02", "ready_turn": 8}
    assert rs.buildings["core:dream_spire"].payment == "parts"
    assert "(готова не раньше хода 8)" in event_text(spire, rs, st, "B")
    whole = replace(rs, buildings={**rs.buildings,
                                   "core:dream_spire": replace(rs.buildings["core:dream_spire"], payment="full")})
    assert "(готова к ходу 8)" in event_text(spire, whole, st, "B")


# ---------------------------------------------------------------- FOG-1: the delayed spectator and intel

def test_the_delayed_spectator_sees_no_intel_while_the_game_runs(tmp_path):
    config = make_config(game_id="s", scenario="core:quad4", seats=[{"seat": s, "nick": s, "kind": "agent"}
                                                                     for s in "ABC"], seed=5, turn_limit=5)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock())
    while game.phase == "setup":
        for seat in game.alive_seats():
            game.set_ready(seat, None)
    game.state.players["A"].tally["intel"] = 1          # stands for one successful mission before turn 1 resolves
    while game.state.turn < 3:
        for seat in game.alive_seats():
            game.set_ready(seat, game.phase_info(seat).get("orders_hash"))
    assert not game.game_over
    full = snapshot(game, GameCache(), 1, LIVE)
    a_full = next(p for p in full["players"] if p["seat"] == "A")
    assert a_full["score_parts"]["intel"] == 1
    snap = snapshot(game, GameCache(), 1, DELAYED)
    for p in snap["players"]:
        assert "intel" not in p["score_parts"]
        assert p["score"] == sum(p["score_parts"].values())          # the total does not give it back
    a = next(p for p in snap["players"] if p["seat"] == "A")
    assert a["score"] == a_full["score"] - 1 and snap["scores"]["A"] == a["score"]
    for h in live(game, GameCache(), "spectator", DELAYED)["scores_history"]:
        for seat, row in h.get("by_source", {}).items():
            assert "intel" not in row and h["scores"][seat] == sum(row.values())
    assert live(game, GameCache(), "admin", LIVE)["scores_history"][1]["by_source"]["A"]["intel"] == 1
