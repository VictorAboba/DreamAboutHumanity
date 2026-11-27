"""Stage 4: victory points (docs/SCORING.md, decisions 23-24) on the fixture pack tests/packs/t4.

Map `tri` as in conquest_helpers: capitals A1, B1, C1, forges A2, B2, C2, the neutral agri M. `board` puts seat X on
X1 and X2 whatever the seeded start deal was. Tests follow SCORING.md 12 (compatibility, assembly, each source,
objectives, fog, the end, rules, replay, an end-to-end game).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from dah.engine import (ContentError, GameState, Ruleset, SeatOrders, build_ruleset, issue_order_id, new_game_state,
                        resolve, rules_text, rules_topic, state_hash, submit_orders, view_state)
from dah.engine import scoring as sc
from dah.engine.combat import FightResult
from dah.engine.model import Order
from dah.engine.orders import check_order
from dah.game import Game, make_config, replay
from dah.journal import read_events, view_events
from dah.store import init_game_dir

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "tests" / "packs" / "t4"
PACKS = [str(PACK)]
MIL = "t4:militia"
ALLOY, FOOD, CREED = "t4:alloy", "t4:provisions", "t4:creed"
FACTIONS = {"A": "iron", "B": "iron", "C": "ash"}
SEATS = [{"seat": s, "nick": f"Ник{s}", "kind": "human", "faction": "iron"} for s in "ABC"]


def ruleset(scenario: str = "tri_score", **kw: Any) -> Ruleset:
    return build_ruleset(PACKS, scenario, **kw)


def board(rs: Ruleset, units: dict[str, dict[str, int]] | None = None, seed: int = 1,
          factions: dict[str, str] | None = None) -> GameState:
    """Seats A, B, C on X1 (capital, home) and X2 (forge); M neutral; 3 militia per capital by default."""
    factions = factions or FACTIONS
    st = new_game_state(rs, [{"seat": s, "nick": f"Ник{s}", "faction": factions[s]} for s in "ABC"], seed)
    for pid, prov in st.provinces.items():
        prov.owner = pid[0] if pid[0] in "ABC" else None
        prov.units = {}
        prov.buildings = []
    for seat, player in st.players.items():
        player.resources = {ALLOY: 60, FOOD: 60, CREED: 0}
        player.home = f"{seat}1"
    st.origins = {pid: pid[0] for pid in st.provinces if pid[0] in "ABC"}
    for pid, u in (units if units is not None else {f"{s}1": {MIL: 3} for s in "ABC"}).items():
        st.provinces[pid].units = dict(u)
    return st


def book(st: GameState, rs: Ruleset, seat: str, items: list[dict[str, Any]]) -> SeatOrders:
    res = submit_orders(st, rs, seat, SeatOrders(), items, "actions", id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def mv(src: str, dst: str, n: int = 3) -> dict[str, Any]:
    return {"kind": "move", "from": src, "to": dst, "units": {"militia": n}}


def kinds(report: Any, kind: str) -> list[dict[str, Any]]:
    return [e for e in report.events if e["kind"] == kind]


def pack_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "t4"
    shutil.copytree(PACK, dst)
    return dst


def edit(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = change(data)
    path.write_text(yaml.safe_dump(out if out is not None else data, allow_unicode=True), encoding="utf-8")


def scenario_patch(pack: Path, sid: str, **fields: Any) -> None:
    def change(items):
        for s in items:
            if s["id"] == sid:
                for k, v in fields.items():
                    if v is None:
                        s.pop(k, None)
                    else:
                        s[k] = v
        return items
    edit(pack / "scenarios" / "scenarios.yaml", change)


def objectives_patch(pack: Path, items: list[dict[str, Any]]) -> None:
    (pack / "objectives" / "objectives.yaml").write_text(yaml.safe_dump(items, allow_unicode=True), encoding="utf-8")
    edit(pack / "scenarios" / "scenarios.yaml", lambda d: [s for s in d if s["id"] != "tri_sage"])   # names a pool


# ---------------------------------------------------------------- 12.1 compatibility

def test_no_scoring_key_keeps_hashes_state_and_scores(tmp_path):
    plain = ruleset("tri_plain")
    assert plain.default_scoring and plain.scenario.scoring == {"territory": 1, "science": 0, "works": 0, "war": 0,
                                                               "intel": 0, "objectives": 0}
    data = plain.to_dict()
    assert "scoring" not in data["scenario"] and "objectives" not in data["scenario"] and "objectives" not in data
    assert "t4:monument" not in plain.buildings and not plain.objectives      # requires_system scoring.works
    # the same pack without any stage-4 content hashes the same
    pack = pack_copy(tmp_path)
    shutil.rmtree(pack / "objectives")
    (pack / "buildings" / "great_works.yaml").unlink()
    edit(pack / "scenarios" / "scenarios.yaml", lambda d: [s for s in d if s["id"] == "tri_plain"])
    assert build_ruleset([str(pack)], "tri_plain").hash == plain.hash
    st = new_game_state(plain, [{"seat": s, "nick": s} for s in "AB"], 3)
    assert all(not p.home and not p.objectives and not p.tally for p in st.players.values()) and not st.origins
    assert sc.scores(st, plain) == {"A": 5, "B": 5}            # capital 1 + 3, forge 1
    new, rep = resolve(st, {}, plain, 3)
    assert "scores" not in rep.public and rep.scores == {"A": 5, "B": 5} and rep.score_parts == {}
    assert new.result is None and "war_credit" not in {e["kind"] for e in rep.events}


def test_core_scenarios_scoring():
    """Core 0.5.0 (SCORING.md 11): the played scenarios score every source; the blitz test scenarios keep the
    default score (and its hash rules)."""
    for sid in ("blitz_test", "blitz_spy"):
        rs = build_ruleset(None, sid)
        assert rs.default_scoring and "scoring" not in rs.to_dict()["scenario"]
    for sid in ("slice3", "slice2", "quad4", "blitz_score"):
        rs = build_ruleset(None, sid)
        assert not rs.default_scoring and rs.to_dict()["scenario"]["scoring"] == dict.fromkeys(sc.registry.SCORE_SOURCES, 1)


def test_default_result_keeps_three_keys():
    rs = build_ruleset(PACKS, "tri_plain")
    st = new_game_state(rs, [{"seat": s, "nick": s} for s in "AB"], 3)
    st.turn = rs.scenario.turn_limit
    new, _ = resolve(st, {}, rs, 3)
    assert set(new.result) == {"winners", "reason", "scores"} and new.result["winners"] == ["A", "B"]


# ---------------------------------------------------------------- 12.2 assembly

def test_scoring_weights_and_host_override():
    rs = ruleset("tri_study")
    assert rs.scenario.scoring == {"territory": 0, "science": 1, "works": 1, "war": 0, "intel": 0, "objectives": 0}
    assert rs.to_dict()["scenario"]["scoring"]["science"] == 1
    over = ruleset("tri_plain", scoring={"territory": 1, "war": 2})
    assert over.weight("war") == 2 and over.weight("science") == 0 and not over.default_scoring
    assert over.hash != ruleset("tri_plain").hash
    assert ruleset("tri_score", objectives=2).scenario.objectives["dealt"] == 2
    with pytest.raises(ContentError, match="objectives must be >= 1"):
        ruleset("tri_score", objectives=0)
    with pytest.raises(ContentError, match="other than objectives"):
        ruleset("tri_plain", scoring={"objectives": 1})
    with pytest.raises(ContentError, match="unknown score source"):
        ruleset("tri_plain", scoring={"glory": 1})
    with pytest.raises(ContentError, match="dealt must be >= 1"):
        ruleset("tri_plain", scoring={"territory": 1, "objectives": 1})
    with pytest.raises(ContentError, match="0..5"):
        ruleset("tri_plain", scoring={"territory": 9})


def test_offer_must_equal_dealt_and_schema_errors(tmp_path):
    pack = pack_copy(tmp_path)
    scenario_patch(pack, "tri_score", objectives={"dealt": 1, "offer": 2})
    with pytest.raises(ContentError, match="offer"):
        build_ruleset([str(pack)], "tri_score")
    pack = pack_copy(tmp_path / "b")
    objectives_patch(pack, [{"id": "x", "name": "X", "kind": "glory", "points": 3}])
    with pytest.raises(ContentError, match="unknown objective kind"):
        build_ruleset([str(pack)], "tri_plain")
    objectives_patch(pack, [{"id": "x", "name": "X", "kind": "hold_type", "params": {"count": 2}, "points": 3}])
    with pytest.raises(ContentError, match="needs parameter 'type'"):
        build_ruleset([str(pack)], "tri_plain")
    objectives_patch(pack, [{"id": "x", "name": "X", "kind": "hold_count", "params": {"count": 0}, "points": 3}])
    with pytest.raises(ContentError, match=">= 1"):
        build_ruleset([str(pack)], "tri_plain")
    objectives_patch(pack, [{"id": "x", "name": "X", "kind": "hold_count", "params": {"count": 2, "zz": 1},
                             "points": 3}])
    with pytest.raises(ContentError, match="no parameter 'zz'"):
        build_ruleset([str(pack)], "tri_plain")
    objectives_patch(pack, [{"id": "x", "name": "X", "kind": "hold_count", "params": {"count": 2}, "points": 9}])
    with pytest.raises(ContentError):
        build_ruleset([str(pack)], "tri_plain")


BASE_POOL = [
    {"id": "granary", "name": "Житница", "kind": "hold_type", "params": {"type": "agri", "count": 1}, "points": 3},
    {"id": "throne", "name": "Престол", "kind": "capital_other", "params": {"count": 1}, "points": 3},
    {"id": "king", "name": "Цареубийца", "kind": "rival_capital_taken", "points": 3},
]


@pytest.mark.parametrize("bad, match", [
    ({"kind": "hold_type", "params": {"type": "forge", "count": 1}}, "free"),                 # one forge per start
    ({"kind": "hold_count", "params": {"count": 2}}, "free"),                                 # a start has 2
    ({"kind": "hold_type", "params": {"type": "forge", "count": 4}}, "cannot be met"),        # the map has 3
    ({"kind": "hold_count", "params": {"count": 8}}, "cannot be met"),
    ({"kind": "hold_foreign", "params": {"count": 3}}, "cannot be met"),                      # 2 seats: 2 others'
    ({"kind": "capital_other", "params": {"count": 2}}, "cannot be met"),                     # 2 seats: 1 other
    ({"kind": "techs_branch", "params": {"branch": "war", "count": 3}}, "cannot be met"),     # iron: drill only
])
def test_free_and_impossible_objectives_are_errors(tmp_path, bad, match):
    pack = pack_copy(tmp_path)
    objectives_patch(pack, BASE_POOL + [{"id": "bad", "name": "Плохая", "points": 3, **bad}])
    with pytest.raises(ContentError, match=match):
        build_ruleset([str(pack)], "tri_score")


def test_great_work_objective_needs_works_and_a_buildable_work(tmp_path):
    pack = pack_copy(tmp_path)
    gw = {"id": "gw", "name": "Зодчий", "kind": "great_work", "params": {"count": 1}, "points": 3}
    objectives_patch(pack, BASE_POOL + [gw])
    build_ruleset([str(pack)], "tri_score")
    with pytest.raises(ContentError, match="works have weight 0"):
        build_ruleset([str(pack)], "tri_score", scoring={"territory": 1, "objectives": 1})
    edit(pack / "buildings" / "great_works.yaml", lambda d: [{**b, "factions": ["ash"]} for b in d])
    with pytest.raises(ContentError, match="every faction"):
        build_ruleset([str(pack)], "tri_score")


def test_small_pool_is_an_error_and_filters_apply(tmp_path):
    pack = pack_copy(tmp_path)
    objectives_patch(pack, BASE_POOL[:2])
    with pytest.raises(ContentError, match="at least 3"):
        build_ruleset([str(pack)], "tri_score")
    objectives_patch(pack, BASE_POOL + [{"id": "big", "name": "Большая", "kind": "hold_count",
                                         "params": {"count": 3}, "points": 3, "players": {"min": 3}}])
    rs = build_ruleset([str(pack)], "tri_score")
    assert "t4:big" in rs.objectives
    deal2 = sc.deal_objectives(rs, 1, ["A", "B"])
    assert all(o["id"] != "t4:big" for hand in deal2.values() for o in hand)


def test_requires_system_scoring_hides_and_off_modifies_drop(tmp_path):
    rs = ruleset("tri_score", scoring={"territory": 1, "war": 1, "objectives": 1})
    assert "t4:monument" not in rs.buildings and "t4:architect" not in rs.objectives
    assert "t4:monument" in ruleset("tri_score").buildings
    pack = pack_copy(tmp_path)
    scenario_patch(pack, "tri_plain", overrides=[{"type": "modify", "target": "score.war.cap", "op": "set",
                                                   "value": 9}])
    plain = build_ruleset([str(pack)], "tri_plain")
    assert plain.hash == ruleset("tri_plain").hash                 # war is off: the line does nothing
    on = build_ruleset([str(pack)], "tri_plain", scoring={"territory": 1, "war": 1})
    assert on.stat("score.war.cap") == 9


def test_great_work_warns_when_built_in_one_turn(tmp_path):
    pack = pack_copy(tmp_path)
    edit(pack / "buildings" / "great_works.yaml", lambda d: [{**b, "build_turns": 1} for b in d])
    rs = build_ruleset([str(pack)], "tri_score")
    assert any("great work" in w.message for w in rs.warnings)


# ---------------------------------------------------------------- 12.3 territory

def test_territory_occupation_ruins_ravage_and_any_capital():
    rs = ruleset("tri_score_all")
    st = board(rs)
    assert sc.territory(st, rs, "A") == 5
    st.provinces["B1"].owner = "A"                                         # a captured capital: +3 too
    assert sc.territory(st, rs, "A") == 9
    st.provinces["B1"].status["occupied"] = {"by": "A", "from": "B", "since": 1}
    assert sc.territory(st, rs, "A") == 5
    st.provinces["A2"].status["ruined_until"] = st.turn + 3
    assert sc.territory(st, rs, "A") == 4
    st.provinces["A2"].status = {"ravaged_until": st.turn + 2}
    assert sc.territory(st, rs, "A") == 5


# ---------------------------------------------------------------- 12.4 science

def test_science_counts_only_researched_by_order_and_complete_branches():
    rs = ruleset("tri_score")
    st = board(rs)
    a = st.players["A"]
    a.techs = ["t4:drill"]
    assert sc.researched(rs, a) == ["t4:drill"] and sc.science(st, rs, "A") == 1 + 1   # tier 1 + war done (iron)
    assert sc.complete_branches(rs, a) == ["t4:war"]
    a.artifacts = ["t4:tome"]                                              # the artifact gave drill: 0 tier points
    assert sc.researched(rs, a) == [] and sc.science(st, rs, "A") == 1     # the gift still completes the branch
    c = st.players["C"]                                                    # ash: rite (tier 2) is in its war branch
    c.techs = ["t4:drill"]
    assert sc.complete_branches(rs, c) == [] and sc.science(st, rs, "C") == 1
    c.techs = ["t4:drill", "t4:rite"]
    assert sc.science(st, rs, "C") == 1 + 2 + 1


def test_faction_start_tech_gives_no_tier_points():
    rs = ruleset("tri_sage")
    st = board(rs, factions={"A": "sage", "B": "iron", "C": "ash"})
    st2 = new_game_state(rs, [{"seat": "A", "nick": "a", "faction": "sage"}, {"seat": "B", "nick": "b"}], 1)
    assert st2.players["A"].techs == ["t4:drill"] and sc.researched(rs, st2.players["A"]) == []
    st.players["A"].techs = ["t4:drill"]
    assert sc.science(st, rs, "A") == 1                                     # only the branch bonus


def test_tier_points_come_from_stats(tmp_path):
    pack = pack_copy(tmp_path)
    scenario_patch(pack, "tri_score", overrides=[{"type": "modify", "target": "score.science.tier2", "op": "set",
                                                   "value": 1}])
    rs = build_ruleset([str(pack)], "tri_score")
    st = board(rs)
    st.players["C"].techs = ["t4:rite"]
    assert sc.science(st, rs, "C") == 1


# ---------------------------------------------------------------- 12.5 great works

def test_work_started_is_public_and_points_only_when_ready():
    rs = ruleset("tri_score")
    st = board(rs)
    b = book(st, rs, "A", [{"kind": "build", "province": "A2", "building": "spire"}])
    s1, rep = resolve(st, {"A": b}, rs, 1)
    started = kinds(rep, "work_started")
    assert started and started[0]["ready_turn"] == 2 and started[0]["seat"] == "A"
    assert any(e["kind"] == "work_started" for e in rep.by_seat["B"])      # B sees it
    assert not any(e["kind"] == "build_started" for e in rep.by_seat["B"])
    assert rep.public["works"][0]["kind"] == "work_started"
    assert sc.works(s1, rs, "A") == 0
    s2, rep2 = resolve(s1, {}, rs, 1)
    assert kinds(rep2, "work_completed") and sc.works(s2, rs, "A") == 3
    assert rep2.score_parts["A"]["works"] == 3
    # under fog the others know it (public completion), and see the mark of a work in progress on visible land
    view = view_state(s1, rs, "B")
    assert next(p for p in view["provinces"] if p["id"] == "A2")["building_now"][0]["building"] == "t4:spire"


def test_allowed_types_and_max_per_owner():
    rs = ruleset("tri_score_all")
    st = board(rs)
    c = check_order(st, rs, "A", SeatOrders(), {"kind": "build", "province": "A1", "building": "spire"}, "actions")
    assert not c.ok and c.reason == "NOT_AVAILABLE"
    st.provinces["B2"].owner = "A"                                          # an occupied province counts too
    st.provinces["B2"].status["occupied"] = {"by": "A", "from": "B", "since": 1}
    st.provinces["B2"].buildings = ["t4:spire"]
    c = check_order(st, rs, "A", SeatOrders(), {"kind": "build", "province": "A2", "building": "spire"}, "actions")
    assert not c.ok and c.reason == "ALREADY_BUILT" and "1 из 1" in c.message
    # holding a captured work beyond the limit is fine and it counts (once the province is full)
    st.provinces["B2"].status = {}
    st.provinces["A2"].buildings = ["t4:spire"]
    assert sc.works(st, rs, "A") == 6
    b = book(st, rs, "A", [{"kind": "build", "province": "A1", "building": "monument"}])
    c = check_order(st, rs, "A", b, {"kind": "build", "province": "A2", "building": "monument"}, "actions")
    assert not c.ok and c.reason == "ALREADY_BUILT"


def _taken(rs: Ruleset, policy: str | None, extra: dict[str, Any] | None = None):
    """A takes B2 (B's forge with a ready spire) with 3 militia from A2 and the given conquest policy."""
    st = board(rs, {"A2": {MIL: 6}, "B1": {MIL: 3}, "C1": {MIL: 3}})
    st.provinces["B2"].buildings = ["t4:spire"]
    if policy:
        st.players["A"].conquest_policy = f"t4:{policy}"
    b = book(st, rs, "A", [mv("A2", "B2", 6)])
    return resolve(st, {"A": b}, rs, 1)


def test_work_through_conquest_fates():
    rs = ruleset("tri_score_all")
    new, _ = _taken(rs, None)                                               # occupation: nobody scores
    assert new.provinces["B2"].owner == "A" and sc.works(new, rs, "A") == 0 and sc.works(new, rs, "B") == 0
    new, _ = _taken(rs, "subjugate")                                        # annex: the work is the taker's
    assert sc.works(new, rs, "A") == 3
    new, _ = _taken(rs, "exterminate")                                      # raze: gone
    assert new.provinces["B2"].buildings == [] and sc.works(new, rs, "A") == 0
    new, _ = _taken(rs, "raid")                                             # raid: back to B, with its points
    assert new.provinces["B2"].owner == "B" and sc.works(new, rs, "B") == 3
    plain = ruleset("tri_score")                                            # no conquest: owner change, points move
    new, _ = _taken(plain, None)
    assert sc.works(new, plain, "A") == 3 and sc.works(new, plain, "B") == 0


def test_unfinished_work_is_dropped_on_capture():
    rs = ruleset("tri_score")
    st = board(rs, {"A2": {MIL: 6}})
    from dah.engine.model import PendingBuild
    st.pending_builds.append(PendingBuild("B", "B2", "t4:spire", 1))
    new, _ = resolve(st, {"A": book(st, rs, "A", [mv("A2", "B2", 6)])}, rs, 1)
    assert not new.pending_builds and sc.works(new, rs, "A") == 0


# ---------------------------------------------------------------- 12.6 war

def _fight(a_losses: dict[str, int], b_losses: dict[str, int]) -> FightResult:
    return FightResult(True, 1.0, 1.0, a_losses, b_losses)


def test_war_credit_defense_rate_faction_price_and_neutrals():
    rs = ruleset("tri_score")
    st = board(rs)
    keys = {s: st.players[s].faction for s in "ABC"}
    out = sc.credit_war(st, rs, keys, "A", "C", _fight({MIL: 2}, {MIL: 3}), "C", "C1")
    # A attacks: 3 ash militia at the ash price 5 = 15, in full; C defends: 2 iron militia = 20, at 50%
    assert [(e["killer"], e["value"], e["defender"]) for e in out] == [("A", 15, False), ("C", 10, True)]
    assert st.players["A"].tally["war_value"] == {"C": 15} and st.players["C"].tally["war_value"] == {"A": 10}
    assert sc.credit_war(st, rs, keys, "A", None, _fight({MIL: 2}, {MIL: 3}), None, "M") == []
    out = sc.credit_war(st, rs, keys, "A", "B", _fight({MIL: 1}, {MIL: 1}), None, "A1-B1")   # an edge: both full
    assert [e["value"] for e in out] == [10, 10]


def test_war_points_caps():
    rs = ruleset("tri_score")
    st = board(rs)
    st.players["A"].tally = {"war_value": {"B": 300, "C": 65}}
    assert sc.war(st, rs, "A") == 3 + 2                                     # 10 -> 3 per victim, 2; total cap 5
    st.players["A"].tally = {"war_value": {"B": 300, "C": 300}}
    assert sc.war(st, rs, "A") == 5 and sc.war_cap_left(st, rs, "A") == 0


def test_war_credit_in_resolution_and_kept_for_eliminated():
    rs = ruleset("tri_score")
    st = board(rs, {"A1": {MIL: 9}, "B1": {MIL: 1}, "B2": {}, "C1": {MIL: 3}})
    st.provinces["B2"].owner = "A"
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("A1", "B1", 9)])}, rs, 1)
    credits = kinds(rep, "war_credit")
    assert credits and credits[0]["killer"] == "A" and credits[0]["victim"] == "B" and credits[0]["value"] == 10
    assert any(e["kind"] == "war_credit" for e in rep.by_seat["B"])
    assert not any(e["kind"] == "war_credit" for e in rep.by_seat["C"])
    assert not new.players["B"].alive and new.players["A"].tally["war_value"] == {"B": 10}
    assert new.players["A"].tally["homes_taken"] == ["B1"]


def test_starvation_gives_no_war():
    rs = ruleset("tri_score")
    st = board(rs)
    st.players["A"].resources[FOOD] = 0
    st.provinces["A1"].units = {MIL: 40}
    new, rep = resolve(st, {}, rs, 1)
    assert kinds(rep, "starvation") and not kinds(rep, "war_credit") and not new.players["A"].tally


# ---------------------------------------------------------------- 12.7 objectives

def test_deal_is_deterministic_independent_and_cycles_targets():
    rs = ruleset("tri_score", objectives=2)
    seats = ["A", "B", "C"]
    one = sc.deal_objectives(rs, 7, seats)
    assert one == sc.deal_objectives(rs, 7, seats)
    assert all(len(h) == 2 and len({o["id"] for o in h}) == 2 for h in one.values())
    assert any(sc.deal_objectives(rs, seed, seats) != one for seed in range(8, 20))
    targets = []
    for seed in range(40):
        deal = sc.deal_objectives(rs, seed, seats)
        hands = [o for h in deal.values() for o in h if "target" in o]
        assert len({o["target"] for o in hands}) == len(hands)            # each seat a target at most once
        assert all(o["target"] != s for s, h in deal.items() for o in h if "target" in o)
        targets += hands
    assert targets
    st = new_game_state(rs, [{"seat": s, "nick": s} for s in seats], 7)
    assert {s: p.objectives for s, p in st.players.items()} == one
    assert st.players["A"].home and st.origins                             # read by objectives


def test_deal_depends_on_seed_not_game_id(tmp_path):
    hands = []
    for gid, seed in (("g1", 5), ("g2", 5), ("g3", 6)):
        cfg = make_config(game_id=gid, scenario="tri_score", seats=SEATS, seed=seed, pack_dirs=PACKS)
        gdir, _ = init_game_dir(tmp_path / gid, cfg)
        hands.append({s: p.objectives for s, p in Game.create(gdir, Clock()).state.players.items()})
    assert hands[0] == hands[1]
    assert any(hands[0] != sc.deal_objectives(ruleset(), s, ["A", "B", "C"]) for s in (6, 7, 8))


def _status(st: GameState, rs: Ruleset, seat: str, oid: str, target: str | None = None) -> bool:
    return sc.objective_progress(st, rs, seat, {"id": f"t4:{oid}", **({"target": target} if target else {})})[0]


def test_each_objective_kind():
    rs = ruleset("tri_score_all")
    st = board(rs)
    assert not _status(st, rs, "A", "forge_lord")
    st.provinces["B2"].owner = "A"
    assert _status(st, rs, "A", "forge_lord") and _status(st, rs, "A", "conqueror")
    assert _status(st, rs, "A", "dominion")
    assert not _status(st, rs, "A", "granary")
    st.provinces["M"].owner = "A"
    assert _status(st, rs, "A", "granary")
    st.provinces["M"].status["occupied"] = {"by": "A", "from": None, "since": 1}   # held = full province
    assert not _status(st, rs, "A", "granary")
    assert not _status(st, rs, "A", "foreign_throne")
    st.provinces["C1"].owner = "A"
    assert _status(st, rs, "A", "foreign_throne")
    st.players["A"].techs = ["t4:drill"]
    assert _status(st, rs, "A", "mechanicus")
    st.players["A"].artifacts = ["t4:tome"]                                 # a gift does not count
    assert not _status(st, rs, "A", "mechanicus")
    assert not _status(st, rs, "A", "architect")
    st.provinces["A2"].buildings = ["t4:spire"]
    assert _status(st, rs, "A", "architect")


def test_hold_foreign_is_a_snapshot_and_own_land_does_not_count():
    rs = ruleset("tri_score")
    st = board(rs)
    st.provinces["B2"].owner = "A"
    assert _status(st, rs, "A", "conqueror")
    st.provinces["B2"].owner = "B"                                          # given back: nothing was banked
    assert not _status(st, rs, "A", "conqueror")
    st.provinces["A2"].owner = "B"
    st.provinces["A2"].owner = "A"                                          # retaken own land
    assert not _status(st, rs, "A", "conqueror")


def test_rival_capital_taken():
    rs = ruleset("tri_score_all")
    st = board(rs)
    assert not _status(st, rs, "A", "kingbreaker", "B")
    st.provinces["B1"].owner = "C"                                          # taken by a third seat
    st.players["B"].alive = False                                           # and the target out
    assert not _status(st, rs, "A", "kingbreaker", "B")
    st.provinces["B1"].owner = "A"
    st.provinces["B1"].status = {"ruined_until": st.turn + 5}               # ruins in A's hands: owned
    assert _status(st, rs, "A", "kingbreaker", "B")
    st.provinces["B1"].owner = "C"
    st.players["A"].tally = {"homes_taken": ["B1"]}                         # taken once
    assert _status(st, rs, "A", "kingbreaker", "B")


def test_seed_and_private_scoring_are_cut_from_non_admin_journal_views(tmp_path):
    cfg = make_config(game_id="sv", scenario="tri_score", seats=SEATS, seed=4242, pack_dirs=PACKS)
    gdir, _ = init_game_dir(tmp_path, cfg)
    game = Game.create(gdir, Clock())
    _ready_all(game)
    _ready_all(game)
    _ready_all(game)
    events = read_events(gdir.journal_path)
    created = next(e for e in events if e["type"] == "game_created")
    assert created["seed"] == 4242 and created["config"]["seed"] == 4242 and created["scoring"]["war"] == 1
    for view in (view_events(events, seat="A"), view_events(events, spectator=True)):
        c = next(e for e in view if e["type"] == "game_created")
        assert "seed" not in c and "seed" not in c["config"]
        assert "4242" not in str(c)
    mine = view_events(events, seat="A")
    assert [e["seat"] for e in mine if e["type"] == "objectives_dealt"] == ["A"]
    resolved = next(e for e in mine if e["type"] == "turn_resolved")
    assert set(resolved["scores"]) == {"A"} and set(resolved["score_parts"]) == {"A"}
    assert not [e for e in view_events(events, spectator=True) if e["type"] == "objectives_dealt"]
    full = next(e for e in view_events(events, spectator=True) if e["type"] == "turn_resolved")
    assert set(full["scores"]) == {"A", "B", "C"}
    assert view_events(events, admin=True) == events


# ---------------------------------------------------------------- 12.8 fog

def test_score_under_fog():
    rs = ruleset("tri_score")
    st = board(rs)
    st.provinces["C2"].buildings = ["t4:spire"]
    st.players["C"].techs = ["t4:drill"]
    st.players["C"].tally = {"war_value": {"A": 90}}
    me = sc.score_known(st, rs, "C", "C")
    assert me["exact"] and me["parts"] == {"territory": 5, "science": 1, "works": 3, "war": 3}
    other = sc.score_known(st, rs, "A", "C")
    # A sees C1 (next to A1) and C2 (next to A2): territory 5; the spire is public; science and war unknown
    assert not other["exact"] and other["parts"] == {"territory": 5, "science": None, "works": 3, "war": None}
    assert other["score"] == 8
    st.provinces["M"].owner = "C"                                          # M is next to A2: seen
    st.provinces["A2"].owner = "B"                                         # now A no longer sees C2 or M
    assert sc.score_known(st, rs, "A", "C")["parts"]["territory"] == 4
    view = view_state(st, rs, "A")
    c = next(p for p in view["players"] if p["seat"] == "C")
    assert c["score"] == 7 and c["score_exact"] is False and c["score_known"]["science"] is None
    me = next(p for p in view["players"] if p["seat"] == "A")
    assert me["score_exact"] is True and view["you"]["score_parts"] == {"territory": 4, "science": 0, "works": 0,
                                                                       "war": 0}
    assert view["you"]["war"] == {"points": 0, "cap_left": 5} and len(view["you"]["objectives"]) == 1
    line = sc.score_line(st, rs, "A")
    assert line.startswith("Счёт: ты 4 [зем 4 нау 0 стр 0 вой 0]") and "НикC ≥7 ≤12 [зем ≥4 нау ? стр 3 вой ?]" in line   # core 0.8.2: the bound
    assert "твоя цель «" in line and len(line) <= sc.SCORE_LINE_LIMIT


def test_score_under_fog_with_default_scoring():
    rs = ruleset("tri_plain")
    st = board(rs)
    st.provinces["M"].owner = "C"
    view = view_state(st, rs, "A")
    c = next(p for p in view["players"] if p["seat"] == "C")
    assert c["score"] == 6 and c["score_exact"] is False                   # C1, C2, M all seen from A1/A2
    st.provinces["A2"].owner = "B"
    c = next(p for p in view_state(st, rs, "A")["players"] if p["seat"] == "C")
    assert c["score"] == 4                                                 # only C1 is seen now; truth is 6
    assert "score_parts" not in view["you"] and "objectives" not in view["you"]


def test_read_ledger_reads_exact_score_parts():
    from dah.engine import espionage as esp
    rs = ruleset("tri_score_all")
    st = board(rs)
    st.players["B"].tally = {"war_value": {"C": 40}}
    facts, truth = esp._ledger(st, rs, 1, "A", "B", SeatOrders(), 0.3)
    assert truth["score_parts"] == {"territory": 5, "science": 0, "works": 0, "war": 1}
    assert facts["score_parts"] == truth["score_parts"]
    assert "objectives" not in truth["score_parts"]
    plain = build_ruleset([str(ROOT / "tests" / "packs" / "t3")], "tri_all")
    assert "score_parts" not in esp._ledger(st, plain, 1, "A", "B", SeatOrders(), 0.3)[1]


# ---------------------------------------------------------------- 12.9 the end

def _end(rs: Ruleset, st: GameState):
    st.turn = rs.scenario.turn_limit
    return resolve(st, {}, rs, 1)


def test_final_total_adds_objectives_of_the_living_and_winners_open():
    rs = ruleset("tri_score")
    st = board(rs)
    st.players["A"].objectives = [{"id": "t4:granary"}]
    st.players["B"].objectives = [{"id": "t4:forge_lord"}]
    st.players["C"].objectives = [{"id": "t4:granary"}]
    st.provinces["M"].owner = "A"
    st.provinces["C2"].owner = "B"
    new, rep = _end(rs, st)
    r = new.result
    assert r["reason"] == "turn_limit"
    assert r["score_parts"]["A"] == {"territory": 6, "science": 0, "works": 0, "war": 0, "objectives": 3}
    assert r["scores"] == {"A": 9, "B": 9, "C": 4}
    assert r["winners"] == ["A", "B"]                                       # a tie, no tie-breaks
    assert r["winners_open"] == ["A", "B"]
    assert r["objectives"]["C"] == [{"id": "t4:granary", "target": None, "met": False, "points": 3}]
    assert rep.scores == {"A": 6, "B": 6, "C": 4}                          # the turn score: without objectives


def test_eliminated_cannot_win_and_its_objective_does_not_count():
    rs = ruleset("tri_score")
    st = board(rs)
    for pid in ("C1", "C2"):
        st.provinces[pid].owner = "A"
        st.provinces[pid].units = {}
    st.players["C"].objectives = [{"id": "t4:kingbreaker", "target": "A"}]
    st.players["C"].tally = {"homes_taken": ["A1"]}
    st.players["B"].objectives = [{"id": "t4:granary"}]
    st.players["A"].objectives = [{"id": "t4:granary"}]
    new, _ = _end(rs, st)
    r = new.result
    assert not new.players["C"].alive and "C" not in r["winners"]
    assert r["objectives"]["C"][0]["met"] is True and r["score_parts"]["C"]["objectives"] == 0


def test_last_standing_reveals_objectives():
    rs = ruleset("tri_score")
    st = board(rs, {"A1": {MIL: 3}})
    for pid in ("B1", "B2", "C1", "C2"):
        st.provinces[pid].owner = "A"
    st.players["A"].objectives = [{"id": "t4:granary"}]
    new, _ = resolve(st, {}, rs, 1)
    r = new.result
    assert r["reason"] == "last_standing" and r["winners"] == ["A"] and "objectives" in r


# ---------------------------------------------------------------- 12.10 rules

def test_rules_core_budget_victory_section_and_off_sources():
    for sid in ("tri_score", "tri_score_all", "tri_score_setup", "tri_study"):
        rs = ruleset(sid)
        text = rules_text(rs)
        assert len(text) <= 6000 and "(сокращено" not in text
        victory = next(p for p in text.split("\n\n") if p.startswith("ПОБЕДА"))
        assert len(victory) <= 150
    study = next(p for p in rules_text(ruleset("tri_study")).split("\n\n") if p.startswith("ПОБЕДА"))
    assert "наука+стройки" in study and "земля" not in study and "война" not in study and "цель" not in study
    topic = rules_topic(ruleset("tri_score_all"), "очки")
    assert "Шпиль Сна" in topic and "Хозяин кузниц" in topic and "Счётная книга" in topic
    assert "Война:" not in rules_topic(ruleset("tri_study"), "score")
    plain = rules_text(ruleset("tri_plain"))
    assert "1 за провинцию, +3 за столицу" in plain


def test_describe_objective_and_great_work():
    from dah.engine import describe, find
    rs = ruleset("tri_score")
    kind, fid = find(rs, "Хозяин кузниц")[0]
    assert kind == "objective" and "≥2" in describe(rs, kind, fid)["text"]
    d = describe(rs, "building", "t4:spire")
    assert d["score"] == 3 and "великая стройка" in d["text"]


# ---------------------------------------------------------------- 12.11-12.12 game, replay, end to end

class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def _ready_all(game: Game) -> None:
    from dah.engine import orders_hash
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)) if game.phase != "setup" else game.setup_hash(seat))


def test_config_flags_go_into_config_and_replay(tmp_path):
    cfg = make_config(game_id="fl", scenario="tri_plain", seats=SEATS, seed=3, pack_dirs=PACKS,
                      scoring={"territory": 1, "war": 1, "works": 1, "objectives": 1}, objectives=1)
    assert cfg["scoring"] == {"territory": 1, "science": 0, "works": 1, "war": 1, "intel": 0, "objectives": 1}
    assert cfg["objectives"] == 1
    gdir, _ = init_game_dir(tmp_path, cfg)
    game = Game.create(gdir, Clock())
    assert all(p.objectives for p in game.state.players.values())
    for _ in range(3):
        _ready_all(game)
    assert replay(gdir).ok
    plain = make_config(game_id="pl", scenario="tri_plain", seats=SEATS, seed=3, pack_dirs=PACKS)
    assert "scoring" not in plain and "objectives" not in plain


def test_end_to_end_scoring_game(tmp_path):
    cfg = make_config(game_id="e2e", scenario="tri_score_setup", seats=SEATS, seed=11, pack_dirs=PACKS,
                      turn_limit=3)
    gdir, _ = init_game_dir(tmp_path, cfg)
    game = Game.create(gdir, Clock())
    setup = game.get_setup("A")
    assert setup["objectives"] and "Твоя тайная цель" in setup["text"]
    dealt = {s: list(p.objectives) for s, p in game.state.players.items()}
    _ready_all(game)                                                        # setup closes: the same deal
    assert {s: p.objectives for s, p in game.state.players.items()} == dealt
    homes = {s: game.state.players[s].home for s in "ABC"}
    forge = {s: next(p.id for p in game.state.owned(s) if p.id != homes[s]) for s in "ABC"}
    _ready_all(game)                                                        # П1 -> Д
    game.submit("A", [{"kind": "build", "province": forge["A"], "building": "spire"}])
    game.submit("B", [mv(homes["B"], homes["C"], 3)])
    _ready_all(game)
    _ready_all(game)                                                        # turn 1 resolved
    state_a = game.state_json("A")
    assert state_a["you"]["objectives"][0]["id"] == dealt["A"][0]["id"]
    assert "Счёт: ты" in game.state_text("A")
    assert "Твой счёт после хода" in game.history("A")["text"]
    tech = game.list_techs("A")
    assert "science" in tech and "Очки науки" in tech["text"]
    for _ in range(2):
        for _ in range(3):
            _ready_all(game)
    assert game.game_over
    events = read_events(gdir.journal_path)
    types = [e["type"] for e in events]
    assert "work_started" in types and "work_completed" in types and "war_credit" in types
    assert types.count("objectives_revealed") == 3
    over = next(e for e in events if e["type"] == "game_over")
    assert {"score_parts", "objectives", "winners_open"} <= set(over)
    assert over["score_parts"]["A"]["works"] == 3
    view = game.state_json("B")
    assert all(p["score_exact"] for p in view["players"])
    assert replay(gdir).ok
    from dah.report import build_report, render_report
    rep = build_report(events)
    assert rep["scoring"]["works"]["A"]["completed"] == 1 and rep["scoring"]["war_pairs"]
    assert "ОЧКИ (итог по источникам)" in render_report(rep)
    assert view_events(events, seat="B") == events                       # after game_over all is shown


def test_check_order_rejects_spire_in_capital_with_text():
    rs = ruleset("tri_score")
    st = board(rs)
    c = check_order(st, rs, "A", SeatOrders(), Order("x", "build", province="A1", building="t4:spire").to_dict(),
                    "actions")
    assert c.reason == "NOT_AVAILABLE" and "Кузнечный мир" in c.message
    assert state_hash(st)
