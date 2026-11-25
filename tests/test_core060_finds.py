"""Core 0.6.0, stage 5 content (docs/ABANDONED_LANDS.md as amended by CONCEPT decision 25): the pool of finds and its
powers, the serious hindrances (a sweep of 1-2 Militia loses on average), the Treasury of the ruins, the scenario
switches, the maps' rotations, the hashes with the system off and the layout on every core map and seat count."""

from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path

import pytest

from dah.engine import build_ruleset, default_pack_dirs, describe, new_game_state, rules_topic
from dah.engine import discoveries as disc
from dah.engine.content import DEFAULT_PACKS_DIR, validate_packs
from dah.engine.discovery_content import scout_ev
from dah.engine.rules import DISCOVERIES_TOPIC_LIMIT, discoveries_section
from dah.game import Game, make_config, replay
from dah.journal import read_events
from dah.store import init_game_dir
from test_review_stage2 import FakeClock

CORE = DEFAULT_PACKS_DIR / "core"
ALLOY, FOOD, CREED = "core:alloy", "core:provisions", "core:creed"
ASL, MIL = "core:assault", "core:militia"

# id: (kind, rarity, weight, power vs 5 x 12, power vs 1 Militia, vs 2 Militia, risk) — the validator's numbers
POOL = {
    "cache_alloy": ("boon", "common", 1, 2.0, 2.0, 2.0, 0.0),
    "prayer_scrolls": ("boon", "common", 1, 2.0, 2.0, 2.0, 0.0),
    "mercenaries": ("boon", "common", 1, 1.2, 1.2, 1.2, 0.0),
    "guarded_depot": ("boon", "common", 6, 1.3, -0.4, -0.8, 0.0),
    "sealed_reliquary": ("boon", "common", 5, 1.0, -0.4, -0.8, 0.0),
    "intact_mine": ("boon", "uncommon", 1, 2.5, 2.5, 2.5, 0.0),
    "walker_hangar": ("boon", "uncommon", 3, 2.55, -0.4, -0.8, 0.0),
    "command_seal": ("boon", "uncommon", 2, 3.0, 3.0, 3.0, 0.0),
    "lost_arsenal": ("boon", "rare", 1, 4.5, 4.5, 4.5, 0.0),
    "plague": ("hindrance", "minor", 5, -1.47, -0.67, -1.07, 0.0),
    "dream_tithe": ("hindrance", "minor", 5, -1.47, -0.67, -1.07, 0.0),
    "ruin_guard": ("hindrance", "minor", 2, -1.4, -0.4, -0.8, 0.0),
    "machine_trap": ("hindrance", "major", 3, -2.6, -1.2, -1.6, 0.0),
    "blighted_soil": ("hindrance", "major", 4, -2.47, -2.47, -2.47, 0.0),
    "nightmare_lair": ("hindrance", "major", 2, -1.77, -0.4, -0.8, 0.0),
    "sleeping_golem": ("mixed", "common", 2, 0.6, 0.92, 0.76, 4.12),
    "sealed_vault": ("mixed", "common", 2, 0.91, 0.91, 0.91, 3.73),
}
ON = ("slice3", "quad4", "blitz_finds")
OFF = ("slice2", "blitz_test", "blitz_spy", "blitz_score")
# (scenario, seats, finds per game): core sets density 1.0 (step 4, the scout sweep), so every province of a free
# corner past ring 1 but the ruins holds a find: 3 per corner
SEAT_COUNTS = [("slice2", 2, 0), ("slice3", 2, 3), ("slice3", 3, 0), ("quad4", 2, 6), ("quad4", 3, 3),
               ("quad4", 4, 0), ("blitz_finds", 2, 3)]


def rs_of(sid: str = "quad4", **kw):
    return build_ruleset(None, sid, **kw)


def seats(n: int) -> list[dict]:
    return [{"seat": "ABCD"[i], "nick": f"Ник{i}", "faction": "core:covenant"} for i in range(n)]


# ---------------------------------------------------------------- 1. the pool and its powers

def test_the_pool_and_its_powers():
    rs = rs_of()
    got = {fid.split(":")[1]: (d.kind, d.rarity, d.weight, d.power, *d.power_small, d.risk)
           for fid, d in rs.discoveries.items()}
    assert got == POOL
    errors, warnings = validate_packs(default_pack_dirs())
    assert not errors and not warnings, [str(w) for w in (*errors, *warnings)]
    assert len(rs.discoveries) == 17 and all(d.lore or d.description for d in rs.discoveries.values())


@pytest.mark.parametrize("sid", ON)
def test_a_sweep_of_cheap_scouts_loses_and_a_real_army_gains(sid):
    """Decision 25 (a): any army triggers a find, so over the pool and over each ring the weighted mean power against
    one and two Militia is below zero (with a margin); against the reference army it is above zero."""
    rs = rs_of(sid)
    pool = list(rs.discoveries.values())
    sets = {"pool": pool}
    for ring, allowed in rs.scenario.discoveries["rings"].items():
        sets[f"ring {ring}"] = [d for d in pool if f"{d.kind}:{d.rarity}" in allowed]
    for label, items in sets.items():
        assert all(ev <= -0.1 for ev in scout_ev(items)), (label, scout_ev(items))
        weight = sum(d.weight for d in items)
        assert sum(d.weight * d.power for d in items) / weight > 0.05, label
    assert not [w for w in rs.warnings if "scout sweep" in str(w)]


def test_hindrances_are_serious_and_rich_boons_sit_under_a_guard():
    rs = rs_of()
    d = {fid.split(":")[1]: x for fid, x in rs.discoveries.items()}
    # every hindrance costs a lone Militia at least its own price (a toll or a wipe)
    assert all(x.power_small[0] <= -0.4 for x in d.values() if x.kind == "hindrance")
    # the minimum of a loss wipes a small army entirely
    assert disc.losses_for(rs, {MIL: 2}, 0.4, 3) == {MIL: 2}
    assert disc.losses_for(rs, {MIL: 1}, 0.1, 1) == {MIL: 1}
    assert disc.losses_for(rs, {MIL: 2}, 0.1, 2) == {MIL: 2}                   # the tithe and the plague: min 2
    assert disc.losses_for(rs, {ASL: 5}, 0.4, 3) == {ASL: 3}
    # tolls hit every entrant in full whatever its size
    tolls = {k for k, x in d.items() if any(e["type"] == "add_resource" and e["amount"] < 0 for e in x.effects)}
    assert tolls == {"plague", "dream_tithe", "blighted_soil"}
    # the guarded boons: a lone scout dies to the guard (negative for it), a real army takes the reward
    guarded = {k for k, x in d.items() if x.kind == "boon" and any(e["type"] == "spawn_neutral" for e in x.effects)}
    assert guarded == {"guarded_depot", "sealed_reliquary", "walker_hangar"}
    assert all(d[k].power > 0 > d[k].power_small[1] for k in guarded)
    # every guard beats two Militia (16): the second small army dies too
    for k in guarded | {"ruin_guard", "nightmare_lair"}:
        spawn = next(e for e in d[k].effects if e["type"] == "spawn_neutral")
        assert sum(n * sum(rs.units[u].cost.values()) for u, n in spawn["units"].items()) > 16, k


def test_mixed_finds_are_a_risk_lever():
    from dah.engine.discovery_content import _choice_risk
    rs = rs_of()
    for d in (x for x in rs.discoveries.values() if x.kind == "mixed"):
        assert max(d.ev.values()) > d.ev[d.default_choice]                     # a choice beats the default
        risks = {c["id"]: _choice_risk(c, (5, 12), rs.units, rs.buildings, rs.artifacts) for c in d.choices}
        assert risks[d.default_choice] == min(risks.values()) == 0             # the default is the safe one
    text = describe(rs, "discovery", "core:sleeping_golem")["text"]
    assert "смешанная" in text and "по умолчанию" in text


def test_the_ruins_treasury_and_the_souls():
    for sid in ON:
        cfg = rs_of(sid).scenario.discoveries
        assert cfg["garrison"]["units"] == {ASL: 2, MIL: 2}                  # balance 0.6.0 (was 1 + 2)
        reward = {e["resource"]: e["amount"] for e in cfg["garrison"]["reward"]}
        assert reward == {ALLOY: 45, FOOD: 25, CREED: 20}                     # 6.0 P (was 4.0)
        assert (cfg["souls"], cfg["ravaged_turns"], cfg["ruined_turns"], cfg["income_mul"]) == ("halve", 0, 0, 0.5)
        assert (cfg["density"], cfg["extra"], cfg["observe_mirrors"], cfg["ruins_find"], cfg["fair_mirror"]) \
            == (1.0, 0, 2, False, False)


# ---------------------------------------------------------------- 2. scenarios, maps, hashes

def test_scenario_switches():
    for sid in ON:
        rs = rs_of(sid)
        assert rs.system("discoveries") and rs.discoveries and rs.map.rotation
    for sid in OFF:
        rs = rs_of(sid)
        assert not rs.system("discoveries") and not rs.discoveries and "rotation" not in rs.to_dict()["map"]
    blitz = rs_of("blitz_finds")
    assert blitz.map.id == "core:mirror3" and blitz.scenario.turn_limit == 6
    assert all(blitz.system(s) for s in ("espionage", "conquest", "discoveries"))
    assert blitz.weight("objectives") > 0 and blitz.stat("score.betrayal.per_break") == 3


def test_maps_rotate_start_to_start():
    for mid, n in (("mirror2", 2), ("mirror3", 3), ("mirror4", 4)):
        rs = build_ruleset(None, {"mirror2": "slice2", "mirror3": "slice3", "mirror4": "quad4"}[mid],
                           {"discoveries": True})
        rot = rs.map.rotation
        power = dict(rot)
        for _ in range(n - 1):
            assert power != {p: p for p in rot}
            power = {k: rot[v] for k, v in power.items()}
        assert power == {p: p for p in rot}                                   # order n
        caps = [s.capital for s in rs.map.starts]
        assert [rot[c] for c in caps] == caps[1:] + caps[:1]
        for i, s in enumerate(rs.map.starts):                                 # a start goes onto the next one
            nxt = rs.map.starts[(i + 1) % n]
            assert {rot[p] for p in s.provinces} == set(nxt.provinces)
    assert build_ruleset(None, "quad4").map.rotation["P21"] == "P21"                # the centre stays


def _stripped_core(tmp_path: Path) -> Path:
    """A copy of core without the stage-5 data: no discoveries/, no rotation, no switch or knobs, no blitz_finds."""
    dst = tmp_path / "core"
    shutil.copytree(CORE, dst)
    shutil.rmtree(dst / "discoveries")
    for m in (dst / "maps").glob("*.yaml"):
        m.write_text(re.sub(r"(?m)^# rotation.*\n^rotation:.*\n", "", m.read_text(encoding="utf-8")),
                     encoding="utf-8")
    sc = dst / "scenarios" / "scenarios.yaml"
    text = sc.read_text(encoding="utf-8").replace(", discoveries: true}", "}")
    text = re.sub(r"(?m)^  discoveries:\n(?:    .*\n)+", "", text)
    sc.write_text(text[:text.index("- id: blitz_finds")], encoding="utf-8")
    return dst


@pytest.mark.parametrize("sid", [*OFF, "slice3", "quad4"])
def test_with_discoveries_off_the_hash_is_that_of_core_without_them(tmp_path, sid):
    plain = build_ruleset([str(_stripped_core(tmp_path))], sid)
    assert build_ruleset(None, sid, {"discoveries": False}).hash == plain.hash
    if sid in OFF:
        assert build_ruleset(None, sid).hash == plain.hash                    # the scenario's own switch is off
    else:
        assert build_ruleset(None, sid).hash != plain.hash


# ---------------------------------------------------------------- 3. the layout on every core map and seat count

@pytest.mark.parametrize("sid,n,k", SEAT_COUNTS)
def test_layout_on_every_core_map_and_seat_count(sid, n, k):
    rs = build_ruleset(None, sid, {"discoveries": True})
    cfg = rs.scenario.discoveries
    accepted = 0
    shapes: Counter = Counter()
    for seed in range(40):
        st = new_game_state(rs, seats(n), seed, 500 + seed)
        caps = disc.capitals_of(st)
        info = disc.place(rs, st, caps, seed, 500 + seed)
        accepted += info["accepted"]
        assert info["finds"] == {p: e["id"] for p, e in st.discoveries.items()}
        assert len(st.discoveries) == k
        ruins = {s.capital for s in rs.map.starts if s.capital in st.abandoned}
        shapes[tuple(sorted(ruins))] += 1
        assert len(st.abandoned) == 4 * (len(rs.map.starts) - n)
        ring, _ = disc.rings(rs, caps)
        for pid, entry in st.discoveries.items():
            assert pid in st.abandoned and pid not in ruins and ring[pid] >= 2
            d = rs.discoveries[entry["id"]]
            assert f"{d.kind}:{d.rarity}" in disc._by_ring(cfg["rings"], ring[pid])
            assert entry == {"id": d.id, "state": "hidden", "known": []}
        for pid in st.abandoned:
            prov = st.provinces[pid]
            assert prov.owner is None and prov.status.get("income_mul") == 0.5 and "ravaged_until" not in prov.status   # souls: halve
            if pid in ruins:
                assert prov.units == {ASL: 2, MIL: 2} and prov.status["guard"]["reward"]
            else:
                assert not prov.units and "guard" not in prov.status
    assert accepted >= 36                                                     # >= 90% within budget and parity
    if (sid, n) == ("quad4", 2):
        assert len(shapes) >= 4                                               # free corners side by side or across


@pytest.mark.parametrize("sid,n,mirror", [("slice2", 2, "applied"), ("quad4", 4, "applied"), ("slice3", 3, "applied")])
def test_fair_mirror_copies_a_find_over_the_rotation_on_full_maps(sid, n, mirror):
    rs = build_ruleset(None, sid, {"discoveries": True}, discoveries={"extra": n, "fair_mirror": True})
    st = new_game_state(rs, seats(n), 3, 9)
    info = disc.place(rs, st, disc.capitals_of(st), 3, 9)
    assert info["mirror"] == mirror and info["finds"]
    rot = rs.map.rotation
    for pid, fid in info["finds"].items():
        assert info["finds"].get(rot[pid]) == fid                             # the whole orbit has the same find


def test_fair_mirror_on_quad4_for_two_opposite_seats_and_skipped_for_neighbours():
    rs = build_ruleset(None, "quad4", discoveries={"fair_mirror": True})
    got = set()
    for seed in range(40):
        st = new_game_state(rs, seats(2), seed, 1)
        homes = sorted(p.home for p in st.players.values())
        opposite = homes in (["P01", "P11"], ["P06", "P16"])
        info = disc.place(rs, st, disc.capitals_of(st), seed, 1)
        assert info["mirror"] == ("applied" if opposite else "skipped")
        got.add(info["mirror"])
    assert got == {"applied", "skipped"}


# ---------------------------------------------------------------- 4. rules and a played game

def test_the_topic_lists_the_whole_pool():
    for sid in ON:
        rs = rs_of(sid)
        sec = discoveries_section(rs)
        assert len(sec.detail) <= DISCOVERIES_TOPIC_LIMIT and "… ещё" not in sec.detail, len(sec.detail)
        assert all(f"«{d.name}»" in sec.detail for d in rs.discoveries.values())
        assert "штурм×1 опол×2" in sec.detail and rules_topic(rs, "находки", extra=[sec]) == sec.detail


def _toward_the_corner(game: Game, seat: str) -> list[dict]:
    """Every army of `seat` steps toward the nearest Abandoned province it does not hold (all units)."""
    adj = game.ruleset.map.adjacency
    goals = [p for p in game.state.abandoned if game.state.provinces[p].owner != seat]
    dist = {p: 0 for p in goals}
    frontier = list(goals)
    while frontier:
        nxt = []
        for p in frontier:
            for n in adj[p]:
                if n not in dist:
                    dist[n] = dist[p] + 1
                    nxt.append(n)
        frontier = nxt
    orders = []
    for p in game.state.owned(seat):
        if not p.units or p.id not in dist:
            continue
        step = min(adj[p.id], key=lambda n: (dist.get(n, 99), n))
        if dist.get(step, 99) < dist[p.id]:
            orders.append({"kind": "move", "from": p.id, "to": step,
                           "units": {u.split(":")[1]: c for u, c in p.units.items()}})
    return orders


def test_blitz_finds_plays_to_the_end_and_replays(tmp_path):
    """Two seats on mirror3 (the third corner Abandoned): every army walks into the corner; finds trigger, the
    journal has the layout and the find events, and the game replays."""
    clock = FakeClock()
    config = make_config(game_id="bf", scenario="core:blitz_finds", seed=5, objectives_salt=21,
                         seats=[{"seat": s, "nick": n, "kind": "agent"} for s, n in (("A", "Аргос"), ("B", "Борей"))])
    assert config["systems"]["discoveries"] is True and config["objectives_salt"] == 21
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    assert game.state.abandoned and len(game.state.discoveries) == 3
    assert "ЗАБРОШЕННЫЕ" in game.rules() and len(game.rules()) <= 6000
    game.start()
    while not game.game_over:
        if game.phase == "actions":
            for seat in ("A", "B"):
                orders = _toward_the_corner(game, seat)
                if orders:
                    game.submit(seat, orders)
        clock.now += 1200
        game.tick()
    events = read_events(gdir.journal_path)
    kinds = Counter(e["type"] for e in events)
    assert kinds["discoveries_placed"] == 1
    assert kinds["discovery_found"] >= 1, kinds
    rep = replay(gdir)
    assert rep.ok, [(t.turn, t.expected, t.actual) for t in rep.turns]


# ---------------------------------------------------------------- 5. the core finds in a resolution

def _duel_on_ruins(finds: dict[str, str]):
    """slice3 for two with A on start 1 (P01) and B on start 2 (P05): the corner of P09 is Abandoned (P10 next to A's
    P03, P11 next to B's P06, P12 ring 3); `finds` {pid: short id} replace the layout."""
    from dah.engine import issue_order_id, submit_orders
    from dah.engine.model import SeatOrders
    rs = rs_of("slice3")
    for seed in range(300):
        st = new_game_state(rs, seats(2), seed, 7)
        if st.players["A"].home == "P01" and st.players["B"].home == "P05":
            break
    st.discoveries = {p: {"id": f"core:{f}", "state": "hidden", "known": []} for p, f in finds.items()}
    for p in st.players.values():
        p.resources = {ALLOY: 50, FOOD: 50, CREED: 0}

    def book(seat, items, state=st):
        res = submit_orders(state, rs, seat, SeatOrders(), items, "actions",
                            id_factory=lambda: issue_order_id(state, seat))
        assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
        return res.book
    return rs, st, book


def _events(rep, kind):
    return [e for e in rep.events if e["kind"] == kind]


def test_a_lone_scout_dies_to_a_guarded_depot_and_a_real_army_takes_the_reward():
    from dah.engine import resolve
    rs, st, book = _duel_on_ruins({"P10": "guarded_depot"})
    st.provinces["P03"].units = {MIL: 1}
    new, rep = resolve(st, {"A": book("A", [{"kind": "move", "from": "P03", "to": "P10", "units": {"militia": 1}}])},
                       rs, 1, salt=7)
    assert _events(rep, "discovery_found")[0]["province_effects"]["neutrals"] == {ASL: 2}
    assert new.provinces["P10"].owner is None and new.provinces["P10"].units == {ASL: 2}    # the scout fell
    assert "guard" in new.provinces["P10"].status and "P10" not in new.discoveries
    new.provinces["P03"].units = {ASL: 5}
    before = new.players["A"].resources[ALLOY]
    new2, rep2 = resolve(new, {"A": book("A", [{"kind": "move", "from": "P03", "to": "P10", "units": {"assault": 5}}],
                                         new)}, rs, 2, salt=7)
    cleared = _events(rep2, "guard_cleared")
    assert cleared and cleared[0]["by"] == "A" and cleared[0]["resources"] == {ALLOY: 45, FOOD: 30}
    assert new2.provinces["P10"].owner == "A" and "guard" not in new2.provinces["P10"].status
    assert new2.players["A"].resources[ALLOY] > before + 45 - 1


def test_a_trap_wipes_two_militia_and_a_tithe_wipes_a_lone_scout():
    from dah.engine import resolve
    rs, st, book = _duel_on_ruins({"P10": "machine_trap", "P11": "dream_tithe"})
    st.provinces["P03"].units = {MIL: 2}
    st.provinces["P06"].units = {MIL: 1}
    books = {"A": book("A", [{"kind": "move", "from": "P03", "to": "P10", "units": {"militia": 2}}]),
             "B": book("B", [{"kind": "move", "from": "P06", "to": "P11", "units": {"militia": 1}}])}
    new, rep = resolve(st, books, rs, 1, salt=7)
    found = {e["province"]: e for e in _events(rep, "discovery_found")}
    assert found["P10"]["shares"]["A"]["lost"] == {MIL: 2}                          # min 3: all of it
    assert new.provinces["P10"].owner is None and not new.provinces["P10"].units
    assert new.provinces["P10"].status["ravaged_until"] >= 3
    assert found["P11"]["shares"]["B"]["lost"] == {MIL: 1}                          # min 2: the whole scout
    assert found["P11"]["shares"]["B"]["resources"] == {ALLOY: -4}                 # and the toll in full
    assert new.provinces["P11"].owner is None
