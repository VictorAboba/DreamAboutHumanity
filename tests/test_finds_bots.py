"""Stage 5 (docs/ABANDONED_LANDS.md 14, CONCEPT decision 25): the bots in the Abandoned lands, the scout bot, the
simulator's finds tallies and gates, and the report's finds block."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from dah.bots import BOT_TYPES
from dah.bots import finds
from dah.bots.base import Bot
from dah.bots.view import parse_state
from dah.engine import SeatOrders, build_ruleset, default_pack_dirs, new_game_state, view_state
from dah.report import build_report, render_report

ROOT = Path(__file__).resolve().parents[1]


def _duel():
    """slice3 for two, A on start 1 (P01..P04 with P03 next to the Abandoned P10), B on start 2: the corner of P09."""
    rs = build_ruleset(default_pack_dirs(), "core:slice3")
    for seed in range(300):
        st = new_game_state(rs, [{"seat": "A", "nick": "A"}, {"seat": "B", "nick": "B"}], seed)
        if st.players["A"].home == "P01" and st.players["B"].home == "P05":
            return rs, st
    raise AssertionError("no seed puts A on P01 and B on P05")


def _view(rs, st, seat="A"):
    return parse_state(view_state(st, rs, seat, SeatOrders()), {seat})


def test_the_view_reads_the_marks_of_the_abandoned_lands():
    rs, st = _duel()
    v = _view(rs, st)
    assert v.provinces["P10"].abandoned and not v.provinces["P10"].ruins
    assert v.provinces["P09"].abandoned and v.provinces["P09"].ruins and v.provinces["P09"].guard == {"lair": False}
    assert not v.provinces["P03"].abandoned and v.pending_finds == ()


def test_only_a_real_army_enters_where_a_find_may_lie():
    rs, st = _duel()
    v = _view(rs, st)
    memory = finds.FindMemory()
    src, p10 = v.provinces["P03"], v.provinces["P10"]
    assert finds.may_hold_find(p10, memory)
    ok3, ok4 = finds.army_ok(memory, 3), finds.army_ok(memory, 4)
    assert not ok3(src, p10, {"assault": 2}) and ok3(src, p10, {"assault": 3})
    assert not ok4(src, p10, {"assault": 3}) and ok4(src, p10, {"assault": 4})
    st.provinces["P10"].owner = "B"             # someone held it: the find, if any, has gone off
    memory.absorb(_view(rs, st))
    st.provinces["P10"].owner = None
    assert not finds.may_hold_find(_view(rs, st).provinces["P10"], memory)
    # the ruins' guard: a surer fight (a negative bonus), never against units the fog hides
    bonus = finds.attack_bonus(_view(rs, st), finds.FindMemory(), 0.1, 0.25)
    assert bonus.get("P10") == finds.EARLY_BONUS


def test_mixed_finds_by_rank_on_the_visible_score():
    rs, st = _duel()
    v = _view(rs, st)
    entry = {"province": "P10", "id": "sealed_vault", "default": "pry",
             "choices": [{"id": "force", "ev": 0.91, "risk": 3.73}, {"id": "pry", "ev": 0.67, "risk": 0.0}]}
    parts = dict(v.score_parts or {})
    behind = v.__class__(**{**v.__dict__, "scores": {"A": 2, "B": 9}, "score_parts": {**parts, "territory": 2},
                            "score_known": {"B": {"territory": 9}}})
    ahead = v.__class__(**{**v.__dict__, "scores": {"A": 9, "B": 2}, "score_parts": {**parts, "territory": 9},
                           "score_known": {"B": {"territory": 2}}})
    assert finds.rank(behind) == "behind" and finds.choose_find(behind, entry) == "force"
    assert finds.rank(ahead) == "leader" and finds.choose_find(ahead, entry) == "pry"
    # fog: the other's public part is a lower bound; the comparison uses only the sources it shows
    fog = v.__class__(**{**v.__dict__, "scores": {"A": 9, "B": 5},
                         "score_parts": {**parts, "territory": 5, "science": 4},
                         "score_known": {"B": {"territory": 7, "science": None}}})
    assert finds.rank(fog) == "behind"
    pend = v.__class__(**{**v.__dict__, "pending_finds": (entry,), "scores": {"A": 2, "B": 9},
                          "score_parts": {**parts, "territory": 2}, "score_known": {"B": {"territory": 9}}})
    assert finds.discover_orders(pend) == [{"kind": "discover", "province": "P10", "choice": "force"}]


def test_the_passive_bot_leaves_the_default_and_others_decide():
    assert BOT_TYPES["passive"].decides_finds is False
    assert all(BOT_TYPES[k].decides_finds for k in ("rush", "builder", "diplomat", "scout"))
    assert Bot.decides_finds


def test_the_scout_sends_one_cheap_unit_into_every_abandoned_province_next_to_it():
    rs, st = _duel()
    st.provinces["P03"].units = {"core:assault": 2, "core:militia": 1}
    bot = BOT_TYPES["scout"]("A", 1)
    v = _view(rs, st)
    bot.finds.absorb(v)
    pre = bot.pre_moves(v, {})
    assert pre == [{"kind": "move", "from": "P03", "to": "P10", "units": {"core:militia": 1}}]
    ok = bot.army_ok()
    assert not ok(v.provinces["P03"], v.provinces["P10"], {"assault": 9})    # only the sweep goes there
    builder = BOT_TYPES["builder"]("A", 1)
    assert builder.pre_moves(v, {}) == []


def _sim():
    spec = importlib.util.spec_from_file_location("dah_simulate_s5", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate_s5"] = module
    spec.loader.exec_module(module)
    return module


def test_simulator_tallies_finds_and_prints_the_gates(capsys):
    sim = _sim()
    assert sim.main(["--scenario", "core:slice3", "--games", "2", "--config", "rush,builder", "--discoveries", "both",
                     "--scout-sweep", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    on = data["espionage=on,conquest=on,discoveries=on"]
    names = [r["name"] for r in on["discovery_gates"]]
    assert sum(n.startswith(f"G{i}") for i in range(1, 9) for n in names) >= 8
    assert any(n.startswith("scout sweep") for n in names)
    block = on["ALL"]["discoveries"]
    assert block["games_with_abandoned"] == 2 and block["finds_placed_per_game"] >= 1
    assert on["scout_sweep"]["games"] == 2
    assert "discovery_gates" not in data["espionage=on,conquest=on,discoveries=off"]


def test_score_rank_of_the_simulator_matches_the_bots():
    sim = _sim()
    assert sim.score_rank({"A": 2, "B": 9}, "A") == "behind"
    assert sim.score_rank({"A": 9, "B": 2}, "A") == "leader" and sim.score_rank({"A": 5, "B": 3}, "A") == "middle"
    assert sim.score_rank({"A": 5, "B": 6, "C": 3}, "A") == "middle"


def _ev(kind, **kw):
    return {"type": kind, "seq": 0, "ts": 0.0, **kw}


def test_report_sums_finds_per_seat_with_luck_and_the_treasury():
    seats = [{"seat": "A", "nick": "Аргос", "kind": "bot"}, {"seat": "B", "nick": "Бета", "kind": "bot"}]
    events = [
        _ev("game_created", config={"game_id": "g", "scenario": "core:slice3", "seats": seats}),
        _ev("discoveries_placed", turn=0, finds={"P10": "core:cache_alloy", "P11": "core:guarded_depot"},
            accepted=True, spread=0.3, abandoned=["P09", "P10", "P11", "P12"]),
        _ev("discovery_found", turn=2, seats=["A"], province="P10", id="core:cache_alloy", find_kind="boon",
            entrants=["A"], shares={"A": {"resources": {"core:alloy": 30}}}, province_effects={}, value={"A": 2.0}),
        _ev("discovery_found", turn=3, seats=["B"], province="P11", id="core:guarded_depot", find_kind="boon",
            entrants=["B"], shares={"B": {}}, province_effects={"neutrals": {"core:assault": 2}}, value={"B": 0.0}),
        _ev("guard_cleared", turn=5, seats=["B"], province="P11", by="B", lair=False,
            resources={"core:alloy": 45, "core:provisions": 30}, value=5.0),
        _ev("guard_cleared", turn=6, seats=["A"], province="P09", by="A", lair=False,
            resources={"core:alloy": 30, "core:provisions": 15, "core:creed": 15}, value=4.0),
        _ev("game_over", turn=10, winners=["B"], reason="turn_limit", scores={"A": 10, "B": 12}),
    ]
    report = build_report(events)
    d = report["discoveries"]
    assert d["placed"] == 2 and d["triggered"] == 2
    a, b = d["seats"]["A"], d["seats"]["B"]
    assert a["value"] == 2.0 and a["treasury"] == {"province": "P09", "turn": 6, "value": 4.0}   # no luck in it
    assert b["value"] == 5.0 and b["guards_cleared"] == 1 and b["reward_value"] == 5.0
    assert a["luck"] == -1.5 and b["luck"] == 1.5 and d["luckiest"] == "B" and d["luckiest_won"] is True
    text = render_report(report)
    assert "НАХОДКИ" in text and "Казна руин P09" in text and "удачливее всех: Бета (победил)" in text


def test_no_finds_block_without_abandoned_land():
    report = build_report([_ev("game_created", config={"game_id": "g", "scenario": "s",
                                                        "seats": [{"seat": "A", "nick": "a", "kind": "bot"}]})])
    assert "discoveries" not in report
