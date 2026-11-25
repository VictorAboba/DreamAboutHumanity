"""Stage 5: Abandoned lands and finds (docs/ABANDONED_LANDS.md as amended by CONCEPT decision 25) on the fixture
pack tests/packs/t5.

Map `tri5`: starts A (A1 capital, A2, A3), B, C around M1. With two seats the third start is Abandoned: its capital
ruins under a guard (ring 3), its two other provinces ring 2. `board` deals A to start A and B to start B (seed search)
so tests read like the map: C1 ruins, C2 next to A3 (zone A), C3 next to B2 (zone B).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from dah.engine import (ContentError, GameState, SeatOrders, build_ruleset, describe, issue_order_id, new_game_state,
                        render_state_text, resolve, rules_text, rules_topic, submit_orders, view_state)
from dah.engine import discoveries as disc
from dah.engine import espionage as esp
from dah.engine.content import validate_packs
from dah.engine.model import Order, state_hash
from dah.engine.rules import discoveries_section, DISCOVERIES_TOPIC_LIMIT, Section
from dah.game import Game, make_config, replay
from dah.journal import read_events, view_events
from dah.store import init_game_dir
from test_review_stage2 import FakeClock

PACK = Path(__file__).parent / "packs" / "t5"
PACKS = [str(PACK)]
MIL, ASL, WLK = "t5:militia", "t5:assault", "t5:walker"
ALLOY, FOOD, CREED = "t5:alloy", "t5:provisions", "t5:creed"
SEATS = [{"seat": "A", "nick": "Аня", "faction": "t5:iron"}, {"seat": "B", "nick": "Боря", "faction": "t5:iron"}]


def rs_of(scenario: str = "finds", **kw: Any):
    return build_ruleset(PACKS, scenario, **kw)


def ab_seed(rs, salt: Any = None) -> int:
    """A seed that deals start A to seat A and start B to seat B (C is Abandoned)."""
    for seed in range(200):
        st = new_game_state(rs, SEATS, seed, salt)
        if st.players["A"].home == "A1" and st.players["B"].home == "B1":
            return seed
    raise AssertionError("no seed")


def board(rs=None, finds: dict[str, str] | None = None, salt: Any = 5) -> GameState:
    """A on start A, B on start B, C Abandoned; `finds` {pid: short id} replace the layout (all hidden)."""
    rs = rs or rs_of()
    st = new_game_state(rs, SEATS, ab_seed(rs, salt), salt)
    if finds is not None:
        st.discoveries = {pid: {"id": f"t5:{fid}", "state": "hidden", "known": []} for pid, fid in finds.items()}
    for p in st.players.values():
        p.resources = {ALLOY: 50, FOOD: 50, CREED: 0}
    return st


def book(st, rs, seat, items, phase="actions") -> SeatOrders:
    res = submit_orders(st, rs, seat, SeatOrders(), items, phase, id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def mv(src, dst, units) -> dict[str, Any]:
    return {"kind": "move", "from": src, "to": dst, "units": units}


def events(rep, kind):
    return [e for e in rep.events if e["kind"] == kind]


def _sign(st):
    import dah.engine.diplomacy as dip
    op = dip.make_proposal(st, "A", "B", None, "negotiation_1")
    dip.apply_op(st, op)
    dip.apply_op(st, dip.make_response(st, "B", op["id"], "accept", "negotiation_1"))


def run(st, rs, books=None, salt=5):
    return resolve(st, books or {}, rs, 1, salt=salt)


# ---------------------------------------------------------------- 1. content and validator

def test_powers_are_computed_against_reference_and_small_armies():
    rs = rs_of()
    d = rs.discoveries
    assert (d["t5:cache"].power, d["t5:cache"].power_small) == (2.0, (2.0, 2.0))
    # lose_units {0.25, min 2}: 5 x 12 loses 2 (24 >= 15), one cheap unit (militia 10) all of it, two both
    assert (d["t5:plague"].power, d["t5:plague"].power_small) == (-1.2, (-0.5, -1.0))
    assert d["t5:curse"].power == -2.8 and d["t5:curse"].power_small == (-1.5, -2.0)    # seat loss counts in full
    g = d["t5:golem"]
    assert (g.kind, g.power, g.risk, g.ev) == ("mixed", 1.39, 3.7, {"wake": 1.39, "leave": 0.0})
    assert d["t5:lair"].power == -1.6 and d["t5:lair"].power_small == (-0.5, -1.0)   # small armies die to it
    text = describe(rs, "discovery", "t5:golem")["text"]
    assert "смешанная" in text and "риск 3.70" in text and "по умолчанию" in text


def test_scout_sweep_warning_and_its_absence_for_a_hindrance_heavy_pool():
    errors, warnings = validate_packs(PACKS)
    assert not errors
    assert any("scout sweep pays" in str(w) for w in warnings)
    heavy = rs_of(discoveries={"pool": ["curse", "trap", "plague", "relic"]})
    assert set(heavy.discoveries) == {"t5:curse", "t5:trap", "t5:plague", "t5:relic"}
    assert not any("scout sweep" in str(w) for w in heavy.warnings)


def _pack_with(tmp_path: Path, finds_yaml: str) -> list[str]:
    import shutil
    dst = tmp_path / "t5"
    shutil.copytree(PACK, dst)
    (dst / "discoveries" / "discoveries.yaml").write_text(finds_yaml, encoding="utf-8")
    return [str(dst)]


@pytest.mark.parametrize("body,message", [
    ("- {id: x, name: X, kind: boon, rarity: rare, effects: [{effect: add_resource, resource: alloy, amount: 30}]}",
     "out of the range"),
    ("- {id: x, name: X, kind: boon, rarity: common, effects: [{effect: lose_units, frac: 0.5}]}", "power > 0"),
    ("- {id: x, name: X, kind: hindrance, rarity: minor, effects: [{effect: add_resource, resource: alloy, "
     "amount: 20}]}", "power < 0"),
    ("- id: x\n  name: X\n  kind: mixed\n  rarity: common\n  default_choice: a\n  choices:\n"
     "    - {id: a, outcomes: [{p: 0.5, effects: []}]}\n    - {id: b, effects: []}", "sum to 0.5"),
    ("- id: x\n  name: X\n  kind: mixed\n  rarity: common\n  default_choice: zz\n  choices:\n"
     "    - {id: a, effects: [{effect: add_resource, resource: alloy, amount: 30}]}\n    - {id: b, effects: []}",
     "default_choice"),
    ("- id: x\n  name: X\n  kind: mixed\n  rarity: common\n  default_choice: b\n  choices:\n"
     "    - {id: a, effects: [{effect: spawn_neutral, units: {militia: 1}}]}\n    - {id: b, effects: []}",
     "not allowed in discovery_choice"),
    ("- {id: x, name: X, kind: boon, rarity: common, effects: [{effect: grant_units, unit: assault, count: 3}]}",
     "where must be 'here'"),
    ("- {id: x, name: X, kind: boon, rarity: common, effects: [{effect: grant_artifact, artifact: chest}]}",
     "non-passive"),
])
def test_validator_errors(tmp_path, body, message):
    errors, _ = validate_packs(_pack_with(tmp_path, body))
    assert any(message in str(e) for e in errors), [str(e) for e in errors]


def test_shade_trigger_warns_and_here_is_refused_outside_finds(tmp_path):
    body = ("- {id: x, name: X, kind: boon, rarity: common, triggers: [army, shade], "
            "effects: [{effect: add_resource, resource: alloy, amount: 30}]}")
    errors, warnings = validate_packs(_pack_with(tmp_path, body))
    assert not errors and any("ignored in this stage" in str(w) for w in warnings)
    from dah.engine.registry import effect_problems
    assert effect_problems({"type": "grant_units", "unit": "u", "count": 1, "where": "here"}, "artifact")


def test_off_leaves_no_trace_in_the_hash():
    on, off = rs_of(), rs_of(systems={"discoveries": False})
    plain = rs_of("plain")
    assert not off.discoveries and "discoveries" not in off.to_dict()
    data_off, data_plain = off.to_dict(), plain.to_dict()
    for d in (data_off, data_plain):
        d.pop("scenario")
    assert data_off == data_plain                                         # no finds, no knobs, no rotation
    assert "rotation" not in data_off["map"] and "rotation" in on.to_dict()["map"]
    assert on.hash != off.hash
    st = new_game_state(off, SEATS, 3)
    assert not st.abandoned and not st.discoveries
    assert all(not p.status and (p.owner or not p.units) for p in st.provinces.values())


def test_core_hashes_do_not_change_with_discoveries_off():
    from test_core060 import CORE050
    """Core 0.6.0 turns the system on in slice3 and quad4 (test_core060_finds pins the off hash against a copy of
    core without the stage-5 data); blitz_score keeps it off."""
    for sid in ("slice3", "quad4", "blitz_score"):
        a = build_ruleset(None, sid)
        b = build_ruleset(None, sid, {"discoveries": False})
        assert (a.hash == b.hash) == (sid == "blitz_score") and a.system("discoveries") == (sid != "blitz_score")
        assert not b.system("discoveries") and "discoveries" not in b.to_dict()
    assert build_ruleset([str(CORE050)], "quad4").hash.startswith("130d2c216ccf")


# ---------------------------------------------------------------- 2. setup and layout

def test_abandoned_corner_ruins_guard_and_ravage():
    st = board()
    assert st.abandoned == ["C1", "C2", "C3"]
    c1 = st.provinces["C1"]
    assert c1.owner is None and c1.units == {MIL: 2}
    assert c1.status["guard"]["reward"][0] == {"type": "add_resource", "resource": ALLOY, "amount": 30}
    assert all(st.provinces[p].status["ravaged_until"] == 4 for p in st.abandoned)
    assert "C1" not in st.discoveries                                     # no find in the ruins (ruins_find off)
    assert set(st.discoveries) <= {"C2", "C3"}                            # ring 1 is empty: no find in M1 either
    assert all(e["state"] == "hidden" and e["known"] == [] for e in st.discoveries.values())


@pytest.mark.parametrize("souls,status", [("halve", {"income_mul": 0.5}), ("ashes", {"ruined_until": 3}),
                                          ("ravage", {"ravaged_until": 4})])
def test_souls_variants(souls, status):
    rs = rs_of(discoveries={"souls": souls})
    st = board(rs)
    assert {k: v for k, v in st.provinces["C2"].status.items()} == status
    if souls == "halve":
        from dah.engine import conquest as cq
        assert cq.income_factor(st.provinces["C2"], 1) == 0.5


def test_layout_is_deterministic_and_salted():
    rs = rs_of()
    seed = ab_seed(rs)
    a = new_game_state(rs, SEATS, seed, 5).discoveries
    assert a == new_game_state(rs, SEATS, seed, 5).discoveries
    layouts = {tuple(sorted((p, e["id"]) for p, e in new_game_state(rs, SEATS, seed, salt).discoveries.items()))
               for salt in range(12)}
    assert len(layouts) > 1                                               # the salt moves the layout
    nosalt = new_game_state(rs, SEATS, seed).discoveries
    assert nosalt == new_game_state(rs, SEATS, seed).discoveries          # the simulator's path
    # other streams do not move: starts and objectives come out the same
    other = rs_of("finds_all")
    s1, s2 = new_game_state(other, SEATS, seed, 5), new_game_state(other, SEATS, seed, 6)
    assert {s: p.home for s, p in s1.players.items()} == {s: p.home for s, p in s2.players.items()}


def test_layout_budget_parity_and_fair_mirror():
    rs = rs_of()
    st = board(rs)
    info = disc.place(rs, st, disc.capitals_of(st), 1, 5)
    assert info["rings"]["C2"] == 2 and info["zones"]["C2"] == ["A"] and info["zones"]["C3"] == ["B"]
    accepted = sum(disc.place(rs, st, disc.capitals_of(st), seed, 5)["accepted"] for seed in range(60))
    assert accepted >= 54                                                 # >= 90% without discoveries_unfair
    # fair_mirror on a full map with `extra`: three seats are invariant under the rotation, M1 is its own orbit
    full = rs_of(discoveries={"fair_mirror": True, "extra": 1})
    seats3 = SEATS + [{"seat": "C", "nick": "Вера", "faction": "t5:iron"}]
    st3 = new_game_state(full, seats3, 2, 5)
    assert not st3.abandoned
    info3 = disc.place(full, st3, disc.capitals_of(st3), 2, 5)
    assert info3["mirror"] == "applied"
    # two seats on three starts: no rotation keeps them, the layout is the plain one
    info2 = disc.place(full, st, disc.capitals_of(st), 2, 5)
    assert info2["mirror"] == "skipped"


def test_layout_does_not_depend_on_factions():
    rs = rs_of()
    seed = ab_seed(rs, 5)
    other = [dict(s, faction="t5:ash") for s in SEATS]
    assert new_game_state(rs, SEATS, seed, 5).discoveries == new_game_state(rs, other, seed, 5).discoveries


# ---------------------------------------------------------------- 3. triggering (step 3c)

def test_one_unit_triggers_a_boon_and_takes_it_all():
    rs = rs_of()
    st = board(rs, {"C2": "cache"})
    st.provinces["A3"].units = {MIL: 1}
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A3", "C2", {"militia": 1})])})
    found = events(rep, "discovery_found")
    assert len(found) == 1 and found[0]["shares"]["A"]["resources"] == {ALLOY: 30}
    assert "C2" not in new.discoveries and new.provinces["C2"].owner == "A"
    assert new.players["A"].resources[ALLOY] >= 80                      # 50 + 30 (+ income, no upkeep beyond)
    text = render_state_text(new, rs, "A", since_turn=1, since_state=st, reports=[rep])
    assert "находка «Тайник Сплава» — +30 Сплав" in text


def test_hindrance_min_wipes_a_small_army_and_hits_each_entrant_in_full():
    rs = rs_of()
    st = board(rs, {"M1": "plague"})
    st.provinces["A2"].units = {MIL: 1}
    st.provinces["B2"].units = {ASL: 4}
    books = {"A": book(st, rs, "A", [mv("A2", "M1", {"militia": 1})]),
             "B": book(st, rs, "B", [mv("B2", "M1", {"assault": 4})])}
    new, rep = run(st, rs, books)
    shares = events(rep, "discovery_found")[0]["shares"]
    assert shares["A"]["lost"] == {MIL: 1}                                # min 2: the scout is gone before the battle
    assert shares["B"]["lost"] == {ASL: 2}                                # 25% of 48 = 12 -> 1, but min 2
    assert new.provinces["M1"].owner == "B" and new.provinces["M1"].units == {ASL: 2}
    assert not events(rep, "battle")                                      # nothing of A was left to fight


def test_simultaneous_boon_is_shared_by_army_price():
    rs = rs_of()
    st = board(rs, {"M1": "cache"})
    st.provinces["A2"].units = {MIL: 1}                                   # 10
    st.provinces["B2"].units = {MIL: 2}                                   # 20
    books = {"A": book(st, rs, "A", [mv("A2", "M1", {"militia": 1})]),
             "B": book(st, rs, "B", [mv("B2", "M1", {"militia": 2})])}
    _, rep = run(st, rs, books)
    shares = events(rep, "discovery_found")[0]["shares"]
    assert shares["A"]["resources"] == {ALLOY: 10} and shares["B"]["resources"] == {ALLOY: 20}


def test_units_of_a_boon_arrive_after_the_battles():
    rs = rs_of()
    st = board(rs, {"M1": "mercs"})
    st.provinces["A2"].units = {MIL: 3}
    st.provinces["M1"].units = {MIL: 2}                                   # neutral defenders
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "M1", {"militia": 3})])})
    battle = [e for e in events(rep, "battle") if e["province"] == "M1"][0]
    assert ASL not in battle["sides"][0]["units"]                         # the mercenaries did not fight
    arrived = events(rep, "discovery_units")
    assert arrived and arrived[0]["units"] == {ASL: 2}
    where = arrived[0]["province"]
    assert new.provinces[where].owner == "A" and new.provinces[where].units.get(ASL) == 2


def test_spawned_neutrals_fight_the_same_turn_and_stay_after_a_failed_scout():
    rs = rs_of()
    st = board(rs, {"M1": "lair"})
    st.provinces["A2"].units = {MIL: 1}
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "M1", {"militia": 1})])})
    assert events(rep, "battle") and new.provinces["M1"].owner is None
    assert new.provinces["M1"].units == {ASL: 1, MIL: 2}
    assert "grow" in new.provinces["M1"].status["guard"]
    assert "[логово 3/5]" in render_state_text(new, rs, "A")
    # the lair grows every 2 turns while neutral, up to its cap
    grown = new
    for _ in range(4):
        grown, rep = run(grown, rs)
    assert grown.provinces["M1"].unit_total() == 5
    assert any(e["kind"] == "lair_grew" for e in rep.events) or grown.provinces["M1"].unit_total() == 5


def test_bounce_and_staying_troops_do_not_trigger():
    rs = rs_of()
    st = board(rs, {"M1": "cache", "C2": "cache"})
    st.provinces["M1"].units = {}
    st.provinces["A2"].units = {MIL: 1}
    st.provinces["C2"].owner = "B"
    st.provinces["C2"].units = {MIL: 1}                                   # B stands in C2 already
    _sign(st)
    st.provinces["A3"].units = {MIL: 1}
    books = {"A": book(st, rs, "A", [mv("A3", "C2", {"militia": 1})])}   # bounces off the pact partner's land
    new, rep = run(st, rs, books)
    assert events(rep, "pact_bounce") and "C2" in new.discoveries
    assert not events(rep, "discovery_found")


# ---------------------------------------------------------------- 4. mixed finds and `discover`

def _mixed(rs, fid="golem"):
    st = board(rs, {"M1": fid})
    st.provinces["A2"].units = {ASL: 4}
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "M1", {"assault": 4})])})
    assert new.discoveries["M1"]["state"] == "pending"
    assert new.discoveries["M1"]["pending"] == {"seat": "A", "turn": 1}
    return new, rep


def test_mixed_find_goes_site_then_pending_with_a_state_line():
    rs = rs_of()
    new, rep = _mixed(rs)
    assert events(rep, "discovery_pending")
    text = render_state_text(new, rs, "A")
    line = next(x for x in text.splitlines() if x.startswith("НАХОДКА M1"))
    assert "wake" in line and "без приказа — leave" in line and '"kind":"discover"' in line and len(line) <= 300
    assert "[находка «Спящий голем»]" in text
    assert "НАХОДКА" not in render_state_text(new, rs, "B")
    pending = view_state(new, rs, "A")["discoveries_pending"]
    assert [(p["province"], p["id"], p["default"]) for p in pending] == [("M1", "t5:golem", "leave")]
    assert [c["id"] for c in pending[0]["choices"]] == ["wake", "leave"]
    assert "discovery" not in next(p for p in view_state(new, rs, "B")["provinces"] if p["id"] == "M1")


def test_discover_order_rolls_before_movement_and_default_otherwise():
    rs = rs_of()
    new, _ = _mixed(rs)
    for choice in ("wake", None):
        st = copy.deepcopy(new)
        items = [{"kind": "discover", "province": "M1", "choice": choice}] if choice else []
        nxt, rep = run(st, rs, {"A": book(st, rs, "A", items)} if items else {})
        chosen = events(rep, "discovery_chosen")[0]
        assert chosen["choice"] == (choice or "leave") and chosen["by_default"] == (choice is None)
        assert "M1" not in nxt.discoveries
        assert rep.events.index(chosen) < min([rep.events.index(e) for e in rep.events if e["kind"] == "economy"])


def test_discover_errors_and_free_in_p2():
    rs = rs_of()
    new, _ = _mixed(rs)
    res = submit_orders(new, rs, "B", SeatOrders(), [{"kind": "discover", "province": "M1", "choice": "wake"}],
                        "actions", id_factory=lambda: "x1")
    assert res.results[0].reason == "NO_PENDING"
    res = submit_orders(new, rs, "A", SeatOrders(), [{"kind": "discover", "province": "M1", "choice": "dig"}],
                        "actions", id_factory=lambda: "x1")
    assert res.results[0].reason == "BAD_CHOICE" and "wake" in res.results[0].message
    new.phase = "negotiation_2"
    res = submit_orders(new, rs, "A", SeatOrders(), [{"kind": "discover", "province": "M1", "choice": "wake"}],
                        "negotiation_2", id_factory=lambda: "x1")
    assert res.results[0].ok and res.results[0].cp_cost == 0
    off = rs_of(systems={"discoveries": False})
    res = submit_orders(new_game_state(off, SEATS, 1), off, "A", SeatOrders(), [{"kind": "discover", "province": "M1",
                                                               "choice": "wake"}], "actions",
                        id_factory=lambda: "x1")
    assert res.results[0].reason == "NOT_AVAILABLE"


def test_an_outcome_that_takes_units_clips_moves_out_of_the_province():
    rs = rs_of()
    new, _ = _mixed(rs, "vault")
    st = copy.deepcopy(new)
    # force the losing outcome: try salts until the roll is the 40% branch
    for salt in range(40):
        u = disc.stream(disc.secret_seed(1, salt), st.turn, "discoveries", "M1|t5:vault").random()
        if u >= 0.6:
            break
    items = [{"kind": "discover", "province": "M1", "choice": "force"}, mv("M1", "A2", {"assault": 4})]
    nxt, rep = resolve(st, {"A": book(st, rs, "A", items)}, rs, 1, salt=salt)
    chosen = events(rep, "discovery_chosen")[0]
    assert chosen["outcome"] == 1 and chosen["lost"] == {ASL: 1}      # 25% of 48: one unit
    clipped = events(rep, "order_clipped")[0]
    assert clipped["before"] == {ASL: 4} and clipped["after"] == {ASL: 3}
    assert nxt.provinces["A2"].units.get(ASL) == 3 and not events(rep, "order_rejected")


def test_a_mixed_find_nobody_holds_waits_and_elimination_clears_known():
    rs = rs_of()
    st = board(rs, {"M1": "golem"})
    st.provinces["M1"].units = {WLK: 3}                                   # neutrals beat the entrant
    st.provinces["A2"].units = {MIL: 1}
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "M1", {"militia": 1})])})
    assert new.discoveries["M1"]["state"] == "site" and new.discoveries["M1"]["known"] == ["A"]
    step = disc.DiscoveryStep.__new__(disc.DiscoveryStep)
    step.s = new
    step.eliminated(["A"])
    assert new.discoveries["M1"]["known"] == []


# ---------------------------------------------------------------- 5. guards and rewards

def test_guard_reward_goes_to_the_taker_not_to_a_fallen_scout():
    rs = rs_of()
    st = board(rs, {})
    st.provinces["A3"].units = {MIL: 1}
    st.provinces["C2"].owner, st.provinces["C2"].units = "A", {MIL: 1}
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("C2", "C1", {"militia": 1})])})
    assert new.provinces["C1"].owner is None and not events(rep, "guard_cleared")
    new.provinces["C2"].units = {WLK: 3}
    new.provinces["C1"].units = {MIL: 2}
    before = new.players["A"].resources[ALLOY]
    nxt, rep = run(new, rs, {"A": book(new, rs, "A", [mv("C2", "C1", {"walker": 3})])})
    cleared = events(rep, "guard_cleared")
    assert nxt.provinces["C1"].owner == "A" and cleared and cleared[0]["resources"] == {ALLOY: 30, FOOD: 15}
    assert "guard" not in nxt.provinces["C1"].status
    assert nxt.players["A"].resources[ALLOY] >= before + 30 - 20
    assert not [e for e in rep.events if e["kind"] == "war_credit"]       # no war points for neutrals


# ---------------------------------------------------------------- 6. statuses without conquest

def test_ravage_ends_with_conquest_off_and_ruins_score():
    rs = rs_of()
    st = board(rs, {})
    assert not rs.system("conquest")
    for _ in range(3):
        st, rep = run(st, rs)
    assert all("ravaged_until" not in st.provinces[p].status for p in ("C2", "C3"))
    ashes = rs_of(discoveries={"souls": "ashes"})
    st = board(ashes, {})
    st.provinces["C2"].owner = "A"
    from dah.engine import scoring as sc
    assert sc.scores(st, ashes)["A"] == sc.scores(board(ashes, {}), ashes)["A"]     # ruins score nothing
    st, rep = run(st, ashes)
    st, rep = run(st, ashes)
    assert any(e["kind"] == "ruin_ended" for e in rep.events)
    assert "ruined_until" not in st.provinces["C2"].status


# ---------------------------------------------------------------- 7. fog and espionage

def test_hidden_finds_stay_hidden_and_observation_reveals_with_risk():
    rs = rs_of("finds_all")
    st = board(rs, {"C2": "cache"})
    for seat in "AB":
        txt = render_state_text(st, rs, seat)
        assert "Тайник" not in txt and "находка" not in txt
        assert all("discovery" not in p for p in view_state(st, rs, seat)["provinces"])
    assert esp.local_mirrors(st, rs, "C2") == 2 == esp.local_mirrors(st, rs, "C3")     # with a find and without
    assert esp.local_mirrors(st, rs, "M1") == 0
    st.phase = "actions"
    items = [{"kind": "spy", "shade": "s1", "mission": "observe", "target": "C2", "rounds": 1}]
    books = {"A": book(st, rs, "A", items)}
    for salt in range(60):
        st2, rep = esp.recon(st, books, rs, salt)
        if rep["reports"].get("A"):
            break
    report = rep["reports"]["A"][0]
    assert report["facts"]["discovery"] == {"id": "t5:cache", "name": "Тайник Сплава", "kind": "boon"}
    assert st2.discoveries["C2"]["state"] == "hidden" and st2.discoveries["C2"]["known"] == ["A"]
    assert "находка «Тайник Сплава»" in esp.report_text(report, rs, st2, "A")


def test_public_form_for_those_who_only_see_the_province():
    rs = rs_of()
    st = board(rs, {"M1": "cache"})
    st.provinces["A2"].units = {MIL: 1}
    _, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "M1", {"militia": 1})])})
    seen = [e for e in rep.by_seat["B"] if e["kind"] == "discovery_found"]
    assert seen and seen[0]["public"] and "id" not in seen[0] and "shares" not in seen[0]
    assert rep.public["discoveries"][0]["find_kind"] == "boon"


def test_read_orders_shows_discover_without_choice():
    o = Order("o1", "discover", province="M1", choice="wake")
    assert esp._entry(o) == {"kind": "discover", "province": "M1"}
    assert Order.from_dict(o.to_dict()) == o


# ---------------------------------------------------------------- 8. rules text

def test_rules_section_only_with_abandoned_land_and_budget():
    rs = rs_of()
    sec = discoveries_section(rs)
    assert len(sec.core) <= 65 and len(sec.detail) <= DISCOVERIES_TOPIC_LIMIT
    assert "Спящий голем" in sec.detail and "discover" in sec.detail
    with_sec = rules_text(rs, extra=[sec])
    assert "ЗАБРОШЕННЫЕ" in with_sec and "ЗАБРОШЕННЫЕ" not in rules_text(rs)
    assert rules_topic(rs, "находки", extra=[sec]) == sec.detail


@pytest.mark.parametrize("scenario,seats,extra", [("quad4", 3, None), ("quad4", 2, None), ("quad4", 4, None),
                                                  ("quad4", 4, 2), ("slice3", 2, None), ("slice3", 3, None),
                                                  ("slice2", 2, None), ("blitz_score", 2, None),
                                                  ("blitz_spy", 2, None), ("blitz_test", 2, None)])
def test_core_rules_fit_with_every_system_on(tmp_path, scenario, seats, extra):
    """The merge gate (ABANDONED_LANDS.md 13.3, step 3b): rules() <= 6000 and never cut on every core scenario with
    its systems on plus discoveries, with the setup phase and СООБЩЕНИЯ; ЗАБРОШЕННЫЕ only with Abandoned land or finds
    (quad4 with 4 seats has none unless finds go into ordinary neutral provinces)."""
    config = make_config(game_id="g", scenario=f"core:{scenario}",
                         seats=[{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)],
                         seed=3, systems={"discoveries": True}, objectives_salt=9,
                         discoveries={"extra": extra} if extra is not None else None)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock(), start=False)
    has = disc.active(game.ruleset, game.state)
    assert has == bool(game.state.abandoned or game.state.discoveries)
    if (scenario, seats) in (("quad4", 3), ("quad4", 2), ("slice3", 2), ("blitz_score", 2)):
        assert game.state.abandoned
    game.start()
    phases = []
    while True:                                          # the setup phase (when there is one), then П1 of turn 1
        phases.append(game.phase)
        text = game.rules()
        assert ("ЗАБРОШЕННЫЕ" in text) == has and "сокращено" not in text
        assert len(text) <= 6000, (scenario, seats, game.phase, len(text))
        if has:
            assert len(game.rules("discoveries")) <= DISCOVERIES_TOPIC_LIMIT
        if game.phase != "setup":
            break
        for seat in game.alive_seats():
            game.set_ready(seat, None)
    assert phases[-1] == "negotiation_1"


def test_discoveries_topic_says_when_the_match_has_none(tmp_path):
    seats = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(4)]
    for systems, expect in (({"discoveries": True}, "нет Заброшенных земель"), ({"discoveries": False}, "выключены")):
        config = make_config(game_id="g", scenario="core:quad4", seats=seats, seed=3, systems=systems,
                             objectives_salt=9)
        gdir, _ = init_game_dir(tmp_path / str(len(expect)), config)
        game = Game.create(gdir, FakeClock(), start=False)
        assert expect in game.rules("discoveries") and expect in game.rules("находки")


def test_a_move_into_abandoned_land_warns_without_telling_where_finds_lie():
    rs = rs_of()
    st = board(rs, {"C2": "cache"})
    st.provinces["A3"].units = {MIL: 2}
    st.provinces["A2"].units = {MIL: 2}
    res = submit_orders(st, rs, "A", SeatOrders(), [mv("A3", "C2", {"militia": 1}), mv("A2", "M1", {"militia": 1}),
                                                   mv("A3", "A1", {"militia": 1})], "actions",
                        id_factory=lambda: issue_order_id(st, "A"))
    to_c2, to_m1, home = res.results
    assert to_c2.ok and "C2 — Заброшенная: возможна находка" in to_c2.warning and "Тайник" not in to_c2.warning
    assert to_m1.warning is None and home.warning is None       # M1 is an ordinary neutral province
    st.provinces["C3"].units = {}
    res = submit_orders(st, rs, "B", SeatOrders(), [mv("B2", "C3", {"militia": 1})], "actions",
                        id_factory=lambda: "x1") if st.provinces["B2"].units.get(MIL) else None
    assert res is None or "возможна находка" in (res.results[0].warning or "")    # C3 holds no find: the same words
    st.discoveries["C2"]["known"] = ["A"]                                          # observed
    check = submit_orders(st, rs, "A", SeatOrders(), [mv("A3", "C2", {"militia": 1})], "actions",
                          id_factory=lambda: "x2").results[0]
    assert "находка «Тайник Сплава» (сила +2.0)" in check.warning
    off = rs_of("plain")
    assert not disc.move_warning(new_game_state(off, SEATS, 1), off, "A", Order("o", "move", src="A3", dst="C2",
                                                                               units={MIL: 1}))


def test_game_lists_a_discover_order_and_estimate_notes_the_find(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="g", scenario="t5:finds", pack_dirs=PACKS, seed=ab_seed(rs_of(), 5), turn_limit=4,
                         objectives_salt=5, seats=[{"seat": "A", "nick": "Аня", "kind": "agent"},
                                                   {"seat": "B", "nick": "Боря", "kind": "agent"}])
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    pid = next(p for p in sorted(game.state.abandoned) if p not in game.ruleset.map.capitals
               and any(game.state.provinces[n].owner == "A" for n in game.ruleset.map.adjacency[p]))
    game.state.provinces[pid].units = {MIL: 1}
    est = game.estimate({"attacker": {"militia": 3}, "defender": pid}, seat="A")
    assert "находка" in est["note"] and "в прогноз не входит" in est["note"]
    plain = game.estimate({"attacker": {"militia": 3}, "defender": {"militia": 1}, "province_type": "agri"}, seat="A")
    assert "Заброшенная" not in plain["note"]
    game.state.provinces["M1"].owner = "A"
    game.state.discoveries["M1"] = {"id": "t5:golem", "state": "pending", "known": ["A"], "pending": {"seat": "A",
                                                                                                    "turn": 1}}
    for seat in game.alive_seats():
        game.set_ready(seat, None)                       # П1 → Д
    assert game.phase == "actions"
    out = game.submit("A", [{"kind": "discover", "province": "M1", "choice": "wake"}])
    assert not out["rejected"]
    assert "discover M1 wake «Разбудить»" in game.orders("A")["text"]


# ---------------------------------------------------------------- 9. the game layer

def test_game_stores_salt_journals_layout_and_replays(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="g", scenario="t5:finds", pack_dirs=PACKS, seed=4, turn_limit=3, objectives_salt=77,
                         seats=[{"seat": "A", "nick": "Аня", "kind": "agent"}, {"seat": "B", "nick": "Боря",
                                                                                   "kind": "agent"}])
    assert config["objectives_salt"] == 77 and config["systems"]["discoveries"] is True
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    evs = read_events(gdir.journal_path)
    placed = [e for e in evs if e["type"] == "discoveries_placed"]
    assert placed and placed[0]["finds"] == {p: e["id"] for p, e in game.state.discoveries.items()}
    assert not [e for e in view_events(evs, seat="A") if e["type"] == "discoveries_placed"]
    assert "ЗАБРОШЕННЫЕ" in game.rules()
    # A sends its capital's militia into the corner every turn
    game.start()
    while not game.game_over:
        if game.phase == "actions":
            for p in game.state.owned("A"):
                for n in game.ruleset.map.adjacency[p.id]:
                    if n in game.state.abandoned and game.state.provinces[n].owner != "A" and p.units.get(MIL):
                        game.submit("A", [{"kind": "move", "from": p.id, "to": n, "units": {"militia": 1}}])
                        break
        clock.now += 1200
        game.tick()
    rep = replay(gdir)
    assert rep.ok, [(t.turn, t.expected, t.actual) for t in rep.turns]


def test_old_config_without_the_key_keeps_two_switches():
    config = make_config(game_id="g", scenario="t5:finds", pack_dirs=PACKS, seed=4,
                         seats=[{"seat": "A", "nick": "Аня", "kind": "agent"}, {"seat": "B", "nick": "Боря",
                                                                                   "kind": "agent"}],
                         systems={"discoveries": False, "conquest": True})
    assert config["systems"] == {"espionage": False, "conquest": True, "discoveries": False}
    assert "objectives_salt" not in config


def test_pact_partners_entering_together_share_a_boon_and_the_holder_decides():
    rs = rs_of()
    for fid in ("cache", "golem"):
        st = board(rs, {"M1": fid})
        _sign(st)
        st.provinces["A2"].units = {ASL: 3}
        st.provinces["B2"].units = {MIL: 1}
        books = {"A": book(st, rs, "A", [mv("A2", "M1", {"assault": 3})]),
                 "B": book(st, rs, "B", [mv("B2", "M1", {"militia": 1})])}
        new, rep = run(st, rs, books)
        assert events(rep, "pact_joint") and new.provinces["M1"].owner == "A"
        found = events(rep, "discovery_found")[0]
        assert found["entrants"] == ["A", "B"]
        if fid == "cache":       # 36 against 10: the boon is shared, the pact does not double it
            assert found["shares"]["A"]["resources"] == {ALLOY: 23} and found["shares"]["B"]["resources"] == {ALLOY: 7}
        else:
            assert new.discoveries["M1"]["pending"]["seat"] == "A"
            assert new.discoveries["M1"]["known"] == ["A", "B"]
            assert "[находка «Спящий голем»]" in render_state_text(new, rs, "B")
            assert "НАХОДКА" not in render_state_text(new, rs, "B")


def test_with_conquest_and_scoring_on_a_find_is_a_free_capture_and_war_ignores_neutrals():
    rs = rs_of("finds_all")
    st = board(rs, {"C2": "guards"})
    st.provinces["A3"].units = {ASL: 4}
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A3", "C2", {"assault": 4})])})
    captured = [e for e in rep.events if e["kind"] == "captured" and e["province"] == "C2"]
    assert captured and captured[0]["mode"] == "free" and new.provinces["C2"].owner == "A"
    assert not [e for e in rep.events if e["kind"] == "war_credit"]
    assert "occupied" not in new.provinces["C2"].status
    from dah.engine import scoring as sc
    assert sc.score_parts(new, rs)["A"]["territory"] >= 4
