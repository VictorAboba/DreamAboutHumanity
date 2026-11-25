"""Core 0.6.0 (decision 26, measurement m7): the frozen core 0.5.0 for older games, a Shade for 20 alloy and one free
per seat, the secret-objective mission, the pact-break penalty, the Dream Spire's 7 points and objective points by
difficulty — engine, content, rules texts, journal, report and bots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from dah.bots import spy as bot_spy
from dah.bots import view as bv
from dah.bots import score as bot_score
from dah.bots.diplomat import worth_betrayal
from dah.bots.view import parse_state
from dah.engine import (ContentError, build_ruleset, describe, issue_order_id, new_game_state, resolve,
                        rules_text, rules_topic, submit_orders, view_state)
from dah.engine import diplomacy as dip
from dah.engine import espionage as esp
from dah.engine import scoring as sc
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR
from dah.engine.model import SeatOrders
from dah.game import Game, make_config, pick_ruleset, replay
from dah.journal import read_events
from dah.report import build_report, render_report
from dah.store import GameDir, init_game_dir
from test_review_stage2 import FakeClock, _play_out

CORE = DEFAULT_PACKS_DIR / "core"
CORE050 = LEGACY_PACKS_DIR / "core-0.5.0"
# ruleset hashes of the frozen core 0.5.0 (measurement m7 was played on quad4 130d2c216ccf)
CORE050_HASHES = {"slice3": "1981b24e644c", "slice2": "365beb9cdffd", "quad4": "130d2c216ccf",
                  "blitz_test": "b91b21d41575", "blitz_spy": "08172b58472b", "blitz_score": "917db77d9643"}
SEATS3 = [{"seat": s, "nick": n} for s, n in (("A", "Аргос"), ("B", "Борей"), ("C", "Цирс"))]
ALLOY, FOOD = "core:alloy", "core:provisions"


def _seats(n: int) -> list[dict]:
    return [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(n)]


# ---------------------------------------------------------------- freeze

def test_core_050_is_frozen_with_its_hashes():
    assert "version: 0.6.0" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    head = (CORE050 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.5.0" in head and "Frozen copy of core 0.5.0" in head and "Do not edit" in head
    for sid, prefix in CORE050_HASHES.items():
        old = build_ruleset([str(CORE050)], sid)
        assert old.hash.startswith(prefix), (sid, old.hash[:12])
        assert old.packs == {"core": "0.5.0"} and not old.stat("spy.shades_free")
        assert old.stat("score.betrayal.per_break") == 0
        assert all(o.difficulty is None for o in old.objectives.values())


@pytest.mark.parametrize("sid", ["blitz_test", "blitz_spy", "slice3"])
def test_with_the_new_systems_off_only_the_version_changes(sid):
    """Espionage, conquest and the scoring off: 0.6.0 builds exactly the rules of 0.5.0 (only `packs` differs)."""
    off = {"espionage": False, "conquest": False, "discoveries": False}
    a = build_ruleset([str(CORE050)], sid, off, scoring={"territory": 1}).to_dict()
    b = build_ruleset(None, sid, off, scoring={"territory": 1}).to_dict()
    assert (a.pop("packs"), b.pop("packs")) == ({"core": "0.5.0"}, {"core": "0.6.0"})
    assert a == b


def test_a_core_050_game_replays_and_loads_with_the_frozen_pack(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="old", scenario="core:blitz_score", seats=_seats(3), seed=11, turn_limit=2,
                         pack_dirs=[str(CORE050)])
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    old_hash = game.ruleset.hash
    assert old_hash.startswith(CORE050_HASHES["blitz_score"])
    assert not any(e["type"] == "shade_hired" for e in read_events(gdir.journal_path))     # no free Shade in 0.5.0
    raw = json.loads(gdir.config_path.read_text(encoding="utf-8"))
    raw["pack_dirs"] = None
    gdir.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    rep = replay(GameDir(gdir.path))
    assert rep.ok and [t.turn for t in rep.turns] == [1, 2]
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == "core-0.5.0"
    pick = pick_ruleset(raw, old_hash)
    assert pick.matches and pick.ruleset.packs == {"core": "0.5.0"}
    assert Game.load(GameDir(gdir.path), clock).ruleset.hash == old_hash


# ---------------------------------------------------------------- 1. the Shade: 20 alloy, one free

def test_every_seat_starts_with_one_free_idle_shade():
    rs = build_ruleset(None, "quad4")
    assert esp.shade_cost(rs, None) == {ALLOY: 20} and rs.espionage.shade_free == 1
    st = new_game_state(rs, _seats(4), 3)
    for p in st.players.values():
        assert p.shades == [{"id": "s1", "xp": 0, "status": "idle", "hired_turn": 0, "mission": None}]
        assert p.spy_seq == {"shade": 1}
    # the free Shade counts in the limit: a second hire fits (2), a third does not
    assert len(st.players["A"].shades) < int(rs.stat("spy.shades_max"))
    plain = new_game_state(build_ruleset(None, "blitz_test"), _seats(2), 3)
    assert all(not p.shades for p in plain.players.values())
    off = new_game_state(build_ruleset(None, "quad4", {"espionage": False}), _seats(4), 3)
    assert all(not p.shades and not p.spy_seq for p in off.players.values())


def test_free_shade_is_journaled_once_and_shows_in_state_and_report(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="fs", scenario="core:blitz_score", seats=_seats(3), seed=5, turn_limit=1)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    if game.phase == "setup":
        assert not [e for e in read_events(gdir.journal_path) if e["type"] == "shade_hired"]
        clock.now += 10_000
        game.tick()
    hired = [e for e in read_events(gdir.journal_path) if e["type"] == "shade_hired"]
    assert sorted((e["seat"], e["shade"], e["turn"], e["free"]) for e in hired) == \
        [("A", "s1", 0, True), ("B", "s1", 0, True), ("C", "s1", 0, True)]
    assert game.phase == "negotiation_1"
    text = game.state_text("A")
    assert "ТЕНИ: свободна s1 — задание spy в Д; миссии: list_spies" in text
    assert "s1 свободна" in game.list_spies("A")["text"]
    _play_out(game, clock)
    report = build_report(read_events(gdir.journal_path))
    block = report["stage3"]
    assert all(t["shades_free"] == 1 and t["shades_hired"] == 0 for t in block.values())
    assert "нанято 0 (+1 даром)" in render_report(report)


# ---------------------------------------------------------------- 2. the secret objective mission

def test_secret_mission_needs_the_objectives():
    on = build_ruleset(None, "quad4")
    m = on.missions["core:secret"]
    assert (m.behavior, m.target, m.difficulty, m.rounds_min, m.rounds_max, m.whisper) == \
        ("read_objective", "seat", 1, 1, 3, 1)
    assert m.cost == {} and m.cost_per_round == {FOOD: 2} and not m.loud
    assert "core:secret" not in build_ruleset(None, "blitz_spy").missions          # default scoring: no objectives
    assert "core:secret" not in build_ruleset(None, "quad4", scoring={"territory": 1, "science": 1}).missions
    assert "core:secret" not in build_ruleset(None, "quad4", {"espionage": False}).to_dict().get("missions", {})


def _spy_book(st, rs, seat, target):
    res = submit_orders(st, rs, seat, SeatOrders(), [{"kind": "spy", "shade": "s1", "mission": "secret",
                                                      "target": target, "rounds": 1}], "actions",
                        id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def test_secret_report_reads_the_targets_objective_exactly_and_capture_tells_the_victim():
    rs = build_ruleset(None, "blitz_score")
    st = new_game_state(rs, SEATS3, 4)
    st.phase = "actions"
    b = _spy_book(st, rs, "A", "B")
    found = caught = None
    for seed in range(300):
        s2, rep = esp.recon(st, {"A": b}, rs, seed)
        outcome = [r for r in rep["rounds"] if r["seat"] == "A"][0]["outcome"]
        if outcome == "found" and found is None:
            found = (s2, rep)
        if outcome == "lost" and caught is None:
            caught = (s2, rep)
        if found and caught:
            break
    assert found and caught
    s2, rep = found
    report = rep["reports"]["A"][0]
    goal = st.players["B"].objectives[0]
    obj = rs.objectives[goal["id"]]
    assert report["behavior"] == "read_objective" and report["facts"] == report["truth"]
    got = report["facts"]["objectives"][0]
    assert (got["id"], got.get("target"), got["points"]) == (goal["id"], goal.get("target"), obj.points)
    assert {"met", "have", "need"} <= set(got)
    text = esp.report_text(report, rs, s2, "A")
    assert f"цель «{obj.name}» +{obj.points}" in text and "сейчас:" in text and len(text) <= 400
    s3, rep3 = caught
    assert rep3["captures"]["B"][0]["mission"] == "core:secret"
    assert "Тайная цель" in esp.state_line(rep3, rs, s3, "B")
    assert not s3.players["A"].shades


def test_secret_in_rules_and_list_spies():
    rs = build_ruleset(None, "quad4")
    core = rules_text(rs)
    spying = next(line for line in core.splitlines() if line.startswith("ШПИОНАЖ"))
    assert "Тени даром с начала: 1" in spying and "secret — чужая тайная цель" in spying
    topic = rules_topic(rs, "spying")
    assert "secret «Тайная цель»" in topic and "из них 1 — у каждого с начала партии" in topic
    assert "20 Сплав" in topic
    assert "Чужую тайную цель — миссия «Тайная цель»" in rules_topic(rs, "victory")
    st = new_game_state(rs, _seats(4), 2)
    text = esp.spies_text(st, rs, "A")
    assert "secret D1 1–3 S1" in text and "s1 свободна" in text


# ---------------------------------------------------------------- 3. the pact-break penalty

def _broken_pact_state():
    """blitz_score: A and B in a pact; A breaks it with a move into a B province next to its own."""
    rs = build_ruleset(None, "blitz_score")
    st = new_game_state(rs, SEATS3, 4)
    for op in ({"op": "propose", "id": "k1", "sender": "A", "recipient": "B", "turns": None, "turn": 1,
                "phase": "negotiation_1"},
               {"op": "respond", "id": "k1", "seat": "B", "accept": True, "turn": 1, "phase": "negotiation_1"}):
        dip.apply_op(st, op)
    src, dst = next((a.id, n) for a in st.owned("A") for n in rs.map.adjacency[a.id]
                    if st.provinces[n].owner == "B")
    st.provinces[src].units = {"core:assault": 6}
    st.phase = "actions"
    res = submit_orders(st, rs, "A", SeatOrders(), [{"kind": "move", "from": src, "to": dst,
                                                     "units": {"assault": 6}, "pact": "break"}], "actions",
                        id_factory=lambda: issue_order_id(st, "A"))
    assert all(c.ok for c in res.results)
    new, report = resolve(st, {"A": res.book}, rs, 9)
    return rs, new, report


def test_a_broken_pact_costs_its_breaker_points_everyone_knows():
    rs, st, report = _broken_pact_state()
    assert sc.betrayal_on(rs) and rs.stat("score.betrayal.per_break") == 3
    assert sc.pacts_broken(st, "A") == 1 and sc.pacts_broken(st, "B") == 0
    parts = sc.score_parts(st, rs)
    assert parts["A"]["betrayal"] == -3 and "betrayal" not in parts["B"]
    assert sc.scores(st, rs)["A"] == sum(parts["A"].values())
    known = sc.score_known(st, rs, "C", "A")             # breaks are public: a third seat knows the penalty
    assert known["parts"]["betrayal"] == -3
    line = sc.score_line(st, rs, "A")
    assert "разрыв -3" in line and len(line) <= sc.SCORE_LINE_LIMIT
    final = sc.final_parts(st, rs)
    assert final["A"]["betrayal"] == -3 and sum(final["A"].values()) == sc.final_scores(st, rs)["A"]
    result = {"scores": sc.final_scores(st, rs), "score_parts": final, "objectives": {}}
    assert "− разрыв 3" in sc.final_text(st, rs, result)[0]


def test_no_penalty_under_the_default_score_or_in_core_050():
    rs, st, _ = _broken_pact_state()
    land = build_ruleset(None, "blitz_score", scoring={"territory": 1})
    assert not sc.betrayal_on(land) and sc.scores(st, land)["A"] == sc.territory(st, land, "A")
    old = build_ruleset([str(CORE050)], "blitz_score")
    assert not sc.betrayal_on(old) and "betrayal" not in sc.score_parts(st, old)["A"]


def test_victory_core_names_the_penalty_within_150_chars():
    for sid in ("slice3", "slice2", "quad4", "blitz_score"):
        rs = build_ruleset(None, sid)
        victory = next(line for line in rules_text(rs).splitlines() if line.startswith("ПОБЕДА"))
        assert "разрыв пакта −3" in victory and len(victory) <= 150, victory
        assert "- Разрыв пакта: −3 к счёту за каждый пакт" in rules_topic(rs, "victory")
    plain = next(line for line in rules_text(build_ruleset(None, "blitz_spy")).splitlines()
                 if line.startswith("ПОБЕДА"))
    assert "разрыв" not in plain


# ---------------------------------------------------------------- 4. the Dream Spire

def test_dream_spire_scores_7_and_shows_its_points_in_the_core():
    rs = build_ruleset(None, "quad4")
    spire = rs.buildings["core:dream_spire"]
    assert spire.score == 7 and rs.stat("building.core:dream_spire.score") == 7
    assert spire.cost == {FOOD: 100, ALLOY: 60} and spire.build_turns == 4
    assert set(spire.allowed_types) == {"core:forge", "core:agri", "core:waste", "core:shrine"}
    buildings = next(line for line in rules_text(rs).splitlines() if line.startswith("ЗДАНИЯ"))
    assert "«Шпиль Сна» +7 очк." in buildings and "«Монумент Предела» +3 очк." in buildings
    assert "«Шпиль Сна» +7" in rules_topic(rs, "victory")
    assert bv.GREAT_WORKS["dream_spire"][1] == 7


# ---------------------------------------------------------------- 5. objective points by difficulty

def test_objective_difficulty_is_shown_to_its_owner():
    rs = build_ruleset(None, "quad4")
    st = new_game_state(rs, _seats(4), 8)
    for seat in st.players:
        view = view_state(st, rs, seat)["you"]["objectives"]
        obj = rs.objectives[view[0]["id"]]
        assert view[0]["difficulty"] == obj.difficulty and view[0]["points"] == obj.points
        assert f"+{obj.points}" in sc.score_line(st, rs, seat)
    d = describe(rs, "objective", "core:kingbreaker")
    assert d["difficulty"] == "hard" and "+7 очк. (трудная)" in d["text"]
    assert "«Цареубийца» +7 (трудная)" in rules_topic(rs, "victory")


def _objectives_pack(tmp_path: Path, change) -> list[str]:
    import shutil
    dst = tmp_path / "core"
    shutil.copytree(CORE, dst)
    path = dst / "objectives" / "objectives.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change({o["id"]: o for o in data})
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return [str(dst)]


def test_points_must_grow_with_difficulty(tmp_path):
    def cheap_hard(objs):
        objs["kingbreaker"]["points"] = 5            # hard, but worth what a medium one is
    with pytest.raises(ContentError, match="kingbreaker is hard but worth 5"):
        build_ruleset(_objectives_pack(tmp_path / "a", cheap_hard), "quad4")

    def untiered(objs):
        del objs["forge_lord"]["difficulty"]
    rs = build_ruleset(_objectives_pack(tmp_path / "b", untiered), "quad4")
    assert any("forge_lord has no difficulty" in w.message for w in rs.warnings)

    def bad_tier(objs):
        objs["forge_lord"]["difficulty"] = "brutal"
    with pytest.raises(ContentError):
        build_ruleset(_objectives_pack(tmp_path / "c", bad_tier), "quad4")


# ---------------------------------------------------------------- bots

def test_bot_numbers_match_the_pack():
    rs = build_ruleset(None, "quad4")
    assert bv.SHADE_COST_ALLOY == rs.espionage.shade_cost[ALLOY]
    m = rs.missions["core:secret"]
    assert bv.MISSIONS["secret"] == (m.whisper, m.rounds_max, 0, m.cost_per_round[FOOD])
    assert bv.BETRAYAL_PENALTY == rs.stat("score.betrayal.per_break")


def test_bots_read_the_secret_objective_once_and_weigh_the_penalty():
    rs = build_ruleset(None, "quad4")
    st = new_game_state(rs, _seats(4), 8)
    payload = view_state(st, rs, "A")
    view = parse_state(payload, {"A", "Ник0"})
    memory = bot_spy.SpyMemory()
    other = next(s for s in st.players if s != "A")
    assert bot_spy.idle_shades(view) == ["s1"]
    assert bot_spy.secret_wanted(view, memory, other)
    memory.absorb({"turn": 1, "reports": [{"behavior": "read_objective",
                                           "facts": {"seat": other, "objectives": [{"id": "core:dominion"}]}}]})
    assert memory.objectives[other] == [{"id": "core:dominion"}]
    assert not bot_spy.secret_wanted(view, memory, other)
    capital = next(p for p in view.provinces.values() if p.kind == "capital" and p.owner != "A")
    plain = next(p for p in view.provinces.values() if p.kind not in ("capital", "shrine") and p.owner
                 not in (None, "A"))
    assert worth_betrayal(view, capital)           # 1 + 3 points > the penalty 3
    advances = any(bot_score.advances(view, o, plain) for o in view.open_objectives())
    assert worth_betrayal(view, plain) == advances
    old = bv.BETRAYAL_PENALTY
    try:
        bv.BETRAYAL_PENALTY = 0
        assert worth_betrayal(view, plain)
    finally:
        bv.BETRAYAL_PENALTY = old
