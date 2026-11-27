"""Core 0.8.1 (decision 32, measurement m10): the frozen core 0.8.0, deep missions (secret, read_orders) score against
a pact partner (score.intel.pact_deep), an empty mission says why (the success roll failed, with its chance), seat
targets by nick or letter in any case with the valid targets named, and the rival's «≥» named as the visible part."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dah.cli import goal_text
from dah.engine import build_ruleset, issue_order_id, new_game_state, submit_orders
from dah.engine import diplomacy as dip
from dah.engine import espionage as esp
from dah.engine import scoring as sc
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR
from dah.engine.model import ACTIONS, GameState, Order, SeatOrders
from dah.engine.rules import rules_text, rules_topic
from dah.game import Game, GameError, make_config, replay
from dah.store import GameDir, init_game_dir

ROOT = Path(__file__).resolve().parents[1]
CORE = DEFAULT_PACKS_DIR / "core"
CORE080 = LEGACY_PACKS_DIR / "core-0.8.0"
# ruleset hashes of the frozen core 0.8.0, as core 0.8.0 itself built them at 1d92858 (measurement m10: quad4)
CORE080_HASHES = {"slice3": "77fd89f96e72", "slice2": "d2f154116128", "quad4": "004d3f290589",
                  "blitz_test": "6917e3aa5c63", "blitz_spy": "b79503dfd88e", "blitz_score": "8140c8b8ad47",
                  "blitz_finds": "65598c0b8401", "blitz_trade": "6531f89fac0a"}
RUNS = Path(r"C:\Users\kuzmi\DreamAboutHumanity-runs-2026-11-23")
SEATS3 = [{"seat": "A", "nick": "Альфа"}, {"seat": "B", "nick": "Бета"}, {"seat": "C", "nick": "Гамма"}]
SCORED = ("slice3", "slice2", "quad4", "blitz_score", "blitz_finds", "blitz_trade")


def _state(packs: list[str] | None = None, sid: str = "blitz_score"):
    rs = build_ruleset(packs, sid)
    return rs, new_game_state(rs, SEATS3, 4)


def _shade(st: GameState, seat: str) -> str:
    player = st.players[seat]
    player.spy_seq["shade"] = player.spy_seq.get("shade", 0) + 1
    sid = f"s{player.spy_seq['shade']}"
    player.shades.append({"id": sid, "xp": 0, "status": "idle", "hired_turn": 0, "mission": None})
    return sid


def _start(st: GameState, seat: str, mission: str, target: str, sid: str = "s1", rounds: int = 1) -> None:
    shade = esp.shade_of(st, seat, sid)
    esp.start_mission(shade, Order(id="x", kind="spy", shade=sid, mission=mission, target=target, rounds=rounds),
                      st.turn, ACTIONS)


def _pact(st: GameState, a: str, b: str) -> None:
    st.phase = "negotiation_1"
    dip.apply_op(st, dip.make_proposal(st, a, b, 3, "negotiation_1"))
    dip.apply_op(st, dip.make_response(st, b, st.pacts[-1]["id"], "accept", "negotiation_1"))
    assert dip.pact_between(st, a, b, st.turn)


def _rounds(*items):
    return [{"seat": s, "mission": m, "target": t, "victim": v, "success": True} for s, m, t, v in items]


def _submit(st, rs, seat, item):
    st.phase = "actions"
    res = submit_orders(st, rs, seat, SeatOrders(), [item], "actions", id_factory=lambda: issue_order_id(st, seat))
    return res.results[0]


class Clock:
    def __call__(self) -> float:
        return 1_700_000_000.0


# ---------------------------------------------------------------- a) the freeze of core 0.8.0

def test_core_080_is_frozen_exactly_with_its_hashes():
    assert "version: 0.8.3" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    head = (CORE080 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.8.0" in head and "Frozen copy of core 0.8.0" in head and "Do not edit" in head
    for sid, prefix in CORE080_HASHES.items():
        old = build_ruleset([str(CORE080)], sid)
        assert old.hash.startswith(prefix), (sid, old.hash[:12])
        assert old.packs == {"core": "0.8.0"} and not sc.pact_deep(old)


def test_the_frozen_copy_is_byte_equal_to_the_commit_of_core_080():
    try:
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "1d92858",
                                  "content/packs/core"], capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git history not available")
    files = {p.relative_to(CORE080).as_posix() for p in CORE080.rglob("*") if p.is_file()}
    assert files == {name.removeprefix("content/packs/core/") for name in listing}
    for name in listing:
        rel = name.removeprefix("content/packs/core/")
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"1d92858:{name}"], capture_output=True,
                              check=True).stdout
        data = (CORE080 / rel).read_bytes()
        if rel == "pack.yaml":          # the frozen header only
            data = b"".join(line for line in data.splitlines(keepends=True) if not line.startswith(b"#"))
        assert data == blob, rel


def test_only_the_version_and_the_scenarios_moved_from_the_frozen_copy():
    changed = {"pack.yaml", "scenarios/scenarios.yaml"}
    for path in CORE080.rglob("*"):
        rel = path.relative_to(CORE080).as_posix()
        if path.is_file() and rel not in changed:
            assert path.read_bytes().replace(b"\r\n", b"\n") == (CORE / rel).read_bytes().replace(b"\r\n", b"\n"), rel


@pytest.mark.parametrize("sid", sorted(CORE080_HASHES))
def test_the_scenarios_change_only_the_pact_deep_stat(sid):
    # core 0.8.2 froze 0.8.1: the 0.8.0 -> 0.8.1 step is checked on the copy (test_core082.py checks the next one)
    new, old = build_ruleset([str(LEGACY_PACKS_DIR / "core-0.8.1")], sid), build_ruleset([str(CORE080)], sid)
    a, b = old.to_dict(), new.to_dict()
    assert (a.pop("packs"), b.pop("packs")) == ({"core": "0.8.0"}, {"core": "0.8.1"})
    scored = sid in SCORED
    for d in (b["stats"], *b["faction_stats"].values()):
        assert (d.pop("score.intel.pact_deep", None) == 1) is scored
    assert a == b
    assert sc.pact_deep(new) is scored and not sc.pact_deep(old)


@pytest.mark.parametrize("name,frozen", [("games-m10/m10", "core-0.8.0"), ("games-m9/m9", "core-0.7.0")])
def test_measured_games_replay_with_their_frozen_packs(tmp_path, name, frozen):
    src = RUNS / name
    if not (src / "journal.jsonl").is_file():
        pytest.skip(f"measurement data {src} not present")
    dst = tmp_path / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("keys.json"))
    assert not (dst / "keys.json").exists()
    rep = replay(GameDir(dst))
    assert rep.ok and rep.turns and rep.recons and rep.result_ok
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == frozen


# ---------------------------------------------------------------- b) deep missions against a pact partner

def test_deep_missions_score_against_a_pact_partner_and_spend_their_pair():
    rs, st = _state()
    _pact(st, "A", "B")
    denied: list = []
    got = sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "B", "B"), ("A", "core:secret", "B", "B")),
                          rs.missions, denied)
    assert [(p["mission"], p.get("kind")) for p in got] == [("core:read_orders", "once_per_target"),
                                                            ("core:secret", "once")]
    assert denied == [] and sc.intel(st, rs, "A") == 2
    assert st.players["A"].tally["intel_targets"] == ["core:read_orders:B"]
    assert st.players["A"].tally["intel_kinds"] == ["core:secret"]
    # the pair is spent: a second read of the partner gives nothing, «already scored», not «pact»
    again: list = []
    assert sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "B", "B")), rs.missions, again) == []
    assert [d["reason"] for d in again] == ["once_per_target"]
    # the pact still holds: spying breaks no pact
    assert dip.pact_between(st, "A", "B", st.turn)


def test_the_frozen_080_keeps_the_pact_block():
    old, st = _state([str(CORE080)])
    _pact(st, "A", "B")
    denied: list = []
    assert sc.credit_intel(st, old, _rounds(("A", "core:read_orders", "B", "B"), ("A", "core:secret", "B", "B")),
                           old.missions, denied) == []
    assert [d["reason"] for d in denied] == ["pact", "pact"]


def test_a_sabotage_against_a_partner_still_scores_nothing():
    rs, st = _state()
    _pact(st, "A", "B")
    assert sc.intel_block(st, rs, "A", "B", "core:sabotage", sc.EFFECT, pact=True) == sc.DENY_PACT
    assert sc.credit_intel_effect(st, rs, "A", "B", "core:sabotage", True) == (None, sc.DENY_PACT)
    assert sc.intel_block(st, rs, "A", "B", "core:read_orders", sc.ONCE_PER_TARGET, pact=True) is None
    assert sc.intel_block(st, rs, "A", "B", "core:secret", sc.ONCE, pact=True) is None


def test_a_deep_mission_against_a_partner_scores_at_the_recon_step(monkeypatch):
    monkeypatch.setattr(esp, "chances", lambda rs, key, **kw: (1.0, 0.0))
    rs, st = _state()
    _pact(st, "A", "B")
    _shade(st, "A")
    _start(st, "A", "core:secret", "B")
    st.phase = ACTIONS
    new, rep = esp.recon(st, {}, rs, 4)
    assert [(p["seat"], p["target"], p["mission"]) for p in rep["intel_points"]] == [("A", "B", "core:secret")]
    assert "intel_denied" not in rep and sc.intel(new, rs, "A") == 1


def test_estimate_mission_says_a_partner_scores(monkeypatch):
    rs, st = _state()
    _pact(st, "A", "B")
    sid = _shade(st, "A")
    st.phase = ACTIONS
    est = esp.estimate_mission(st, rs, "A", "read_orders", "Бета", 1, sid)
    assert est["intel_points"] == 1 and est["intel_reason"].startswith("партнёр по пакту: очко засчитают")
    assert "+1 очк. разведки" in est["text"] and "партнёр по пакту: очко засчитают" in est["text"]
    assert len(est["text"]) <= esp.ESTIMATE_CHARS
    sab = esp.estimate_mission(st, rs, "A", "sabotage", "P10", 1, sid)
    assert sab["intel_points"] == 0 and sab["intel_reason"] == "партнёр по пакту: очков не даст"
    plain = esp.estimate_mission(st, rs, "A", "read_orders", "Гамма", 1, sid)       # no pact with C
    assert plain["intel_points"] == 1 and "intel_reason" not in plain
    old, st0 = _state([str(CORE080)])
    _pact(st0, "A", "B")
    sid0 = _shade(st0, "A")
    st0.phase = ACTIONS
    est0 = esp.estimate_mission(st0, old, "A", "read_orders", "B", 1, sid0)
    assert est0["intel_points"] == 0 and est0["intel_reason"] == "партнёр по пакту: очков не даст"


@pytest.mark.parametrize("sid", ("quad4", "slice3", "blitz_score"))
def test_the_rules_name_the_new_pact_rule(sid):
    rs = build_ruleset(None, sid)
    topic = rules_topic(rs, "spying")
    assert "С партнёра по пакту очко дают secret и read_orders." in topic
    assert "Очков нет против партнёра по пакту" not in topic and len(topic) <= 4087 + 500
    victory = rules_topic(rs, "victory")
    assert "secret и read_orders дают очко и против партнёра по пакту, sabotage по партнёру — нет" in victory
    old = build_ruleset([str(CORE080)], sid)
    assert "Очков нет против партнёра по пакту (на разведсрезе)." in rules_topic(old, "spying")
    assert "против партнёра по пакту очков нет" in rules_topic(old, "victory")


# ---------------------------------------------------------------- c) why a mission came back empty

def test_an_empty_mission_says_the_roll_failed_with_its_chance(monkeypatch):
    monkeypatch.setattr(esp, "chances", lambda rs, key, **kw: (0.0, 0.0))     # never found, never caught
    rs, st = _state()
    _shade(st, "A")
    _start(st, "A", "core:read_orders", "B")
    st.phase = ACTIONS
    new, rep = esp.recon(st, {}, rs, 4)
    assert [e["reason"] for e in rep["ended"]] == ["empty"]
    rnd = rep["rounds"][0]
    assert rnd["outcome"] == esp.NOTHING and rnd["p_ok"] == 0.0
    new.phase = "negotiation_2"
    intel = esp.intel_text(rep, rs, new, "A")
    assert "s1 Чтение → Бета 1/1: вернулась ни с чем: успех не выпал (шанс 0%)" in intel
    assert "успех не выпал (шанс 0%)" in esp.notice_text(rep, "A")
    assert "s1 Чтение → Бета 1/1: ничего (шанс успеха 0%)" in esp.state_line(rep, rs, new, "A")
    assert "[ничего не найдено: успех не выпал (шанс 0%)]" in esp.spies_text(new, rs, "A", rep)
    # only the seat that ran it sees it
    assert esp.notice_text(rep, "B") is None and esp.intel_text(rep, rs, new, "B") == "ход 1: донесений нет"
    assert esp.state_line(rep, rs, new, "B") is None


def test_the_texts_of_a_round_by_its_outcome():
    rs, st = _state()
    base = {"seat": "A", "shade": "s1", "mission": "core:read_orders", "target": "B"}
    first = {**base, "round": 1, "rounds": 2, "outcome": esp.NOTHING, "p_ok": 0.45}
    last = {**first, "round": 2}
    assert esp._outcome_text(rs, st, first, "A").endswith("1/2: ничего: успех не выпал (шанс 45%)")
    empty = {"ended": [{"seat": "A", "shade": "s1", "reason": esp.EMPTY}]}
    assert esp._outcome_text(rs, st, last, "A", rep=empty).endswith(
        "2/2: вернулась ни с чем: успех не выпал (шанс 45%)")
    done = {"ended": [{"seat": "A", "shade": "s1", "reason": esp.DONE}]}     # found something in round 1
    assert esp._outcome_text(rs, st, last, "A", rep=done).endswith("2/2: ничего: успех не выпал (шанс 45%)")
    gone = {**base, "round": 1, "rounds": 1, "outcome": esp.VOIDED}
    assert esp._outcome_text(rs, st, gone, "A").endswith("цель выбыла, задание снято")
    obs = {**base, "mission": "core:observe", "target": "P02", "round": 1, "rounds": 1, "outcome": esp.VOIDED}
    assert esp._outcome_text(rs, st, obs, "A").endswith("провинция стала твоей, задание снято")    # P02 is A's
    assert esp._outcome_text(rs, st, {**base, "outcome": esp.LOST}, "A") == "s1: нет связи"           # stays vague


def test_a_mission_that_found_something_earlier_does_not_come_back_empty(monkeypatch):
    # review F1: a 2-round Reading finds #r1 in round 1 and nothing in round 2: it ends DONE, not «ни с чем»
    odds = iter([(1.0, 0.0), (0.0, 0.0)])
    monkeypatch.setattr(esp, "chances", lambda rs, key, **kw: next(odds))
    rs, st = _state()
    _shade(st, "A")
    _start(st, "A", "core:read_orders", "B", rounds=2)
    st.phase = ACTIONS
    mid, rep1 = esp.recon(st, {}, rs, 4)
    assert [r["outcome"] for r in rep1["rounds"]] == [esp.FOUND] and rep1["ended"] == []
    mid.turn += 1
    mid.phase = ACTIONS
    new, rep2 = esp.recon(mid, {}, rs, 4)
    assert [r["outcome"] for r in rep2["rounds"]] == [esp.NOTHING]
    assert [e["reason"] for e in rep2["ended"]] == [esp.DONE]
    new.phase = "negotiation_2"
    intel = esp.intel_text(rep2, rs, new, "A")
    assert "s1 Чтение → Бета 2/2: ничего: успех не выпал (шанс 0%)" in intel and "ни с чем" not in intel
    assert "ни с чем" not in esp.spies_text(new, rs, "A", rep2)


def test_the_compact_line_names_the_chance_as_the_success_chance():
    rs, st = _state()       # review F4: «ничего (шанс 45%)» read as the chance of nothing
    r = {"seat": "A", "shade": "s1", "mission": "core:read_orders", "target": "B", "round": 1, "rounds": 2,
         "outcome": esp.NOTHING, "p_ok": 0.45}
    assert esp._outcome_text(rs, st, r, "A", compact=True).endswith("1/2: ничего (шанс успеха 45%)")
    assert esp.state_line({"rounds": [r]}, rs, st, "A") == "ДОНЕСЕНИЯ: s1 Чтение → Бета 1/2: ничего (шанс успеха 45%)"


def test_a_void_mission_is_explained_by_the_snapshot_that_voided_it():
    # review F3: the cause is stored in the round record; a later state (the battles of that turn) does not change it
    import copy
    rs, st = _state()
    mid = next(k for k, m in rs.missions.items() if m.target == "owned_province")
    neutral = next(p for p, v in sorted(st.provinces.items()) if v.owner is None)
    _shade(st, "A")
    _start(st, "A", mid, neutral)
    st.phase = ACTIONS
    new, rep = esp.recon(st, {}, rs, 4)
    assert rep["rounds"][0]["outcome"] == esp.VOIDED and rep["rounds"][0]["why"] == "neutral"
    for owner in (None, "A", "B"):
        later = copy.deepcopy(new)
        later.provinces[neutral].owner = owner
        assert esp.intel_text(rep, rs, later, "A").endswith("провинция стала ничьей, задание снято")
    # a seat target with no land to check in is not «выбыла»; an older record (no `why`) reads the state as before
    assert esp.void_text(rs, st, "A", "core:read_orders", "B", "no_land").startswith("у цели нет земли")
    assert esp.void_text(rs, st, "A", "core:read_orders", "B") == "цель выбыла, задание снято"
    assert esp.void_cause(st, rs, "A", rs.missions["core:read_orders"], "B") == "no_land"
    st.players["B"].alive = False
    assert esp.void_cause(st, rs, "A", rs.missions["core:read_orders"], "B") == "out"


# ---------------------------------------------------------------- d) seat targets by nick or letter

@pytest.mark.parametrize("target", ["Альфа", "альфа", " АЛЬФА ", "A", "a"])
def test_a_spy_target_takes_a_nick_or_a_letter_in_any_case(target):
    rs, st = _state()
    _shade(st, "B")
    check = _submit(st, rs, "B", {"kind": "spy", "shade": "s1", "mission": "read_orders", "target": target,
                                  "rounds": 1})
    assert check.ok, check.to_dict()
    assert check.order.target == "A"


def test_a_wrong_seat_target_names_the_valid_ones():
    rs, st = _state()
    _shade(st, "B")
    check = _submit(st, rs, "B", {"kind": "spy", "shade": "s1", "mission": "read_orders", "target": "Омега",
                                  "rounds": 1})
    assert check.reason == "UNKNOWN_ID" and "Альфа (A), Гамма (C)" in check.message and "Бета" not in check.message
    me = _submit(st, rs, "B", {"kind": "spy", "shade": "s1", "mission": "read_orders", "target": "Бета",
                               "rounds": 1})
    assert me.reason == "BAD_TARGET" and "Альфа (A), Гамма (C)" in me.message
    prov = _submit(st, rs, "B", {"kind": "spy", "shade": "s1", "mission": "observe", "target": "Альфа", "rounds": 1})
    assert prov.reason == "BAD_TARGET" and "провинция (id вида P05)" in prov.message
    with pytest.raises(ValueError) as err:
        esp.estimate_mission(st, rs, "B", "read_orders", "Омега", 1)
    assert "Альфа (A)" in str(err.value)
    assert esp.estimate_mission(st, rs, "B", "read_orders", "гамма", 1)["target"] == "C"


def test_a_lost_shade_is_named_as_lost_not_as_a_bad_target():
    rs, st = _state()           # m10: B's only Shade was gone; «нет Тени 's1'» read like a bad nick
    st.players["B"].shades, st.players["B"].spy_seq = [], {}
    check = _submit(st, rs, "B", {"kind": "spy", "shade": "s1", "mission": "read_orders", "target": "Альфа",
                                  "rounds": 1})
    assert check.reason == "UNKNOWN_ID" and "у тебя сейчас нет Теней" in check.message
    _shade(st, "B")
    _shade(st, "B")
    _start(st, "B", "core:read_orders", "A", sid="s2")
    check = _submit(st, rs, "B", {"kind": "spy", "shade": "s9", "mission": "read_orders", "target": "A", "rounds": 1})
    assert "твои Тени: s1 (свободна), s2 (на задании)" in check.message


def test_the_game_takes_a_nick_in_another_case(tmp_path):
    seats = [{**s, "kind": "agent"} for s in SEATS3]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="n", scenario="core:blitz_score", seats=seats, seed=4,
                                                  objectives_salt=9))
    game = Game.create(gdir, Clock(), start=False)
    game.start()
    while game.phase == "setup":
        for seat in game.alive_seats():
            game.set_ready(seat, None)
    assert game._seat_ref("бета") == "B" and game._seat_ref("c") == "C" and game._seat_ref("Омега") == "Омега"
    assert game._recipients("A", ["гамма"]) == ["C"]

    with pytest.raises(GameError) as err:
        game.estimate_mission("A", {"mission": "read_orders", "target": "Омега"})
    assert err.value.code == "INVALID" and "Бета (B)" in str(err.value)


# ---------------------------------------------------------------- e) the rival's «≥» is the visible part

def test_the_score_line_says_the_rival_number_is_the_visible_part():
    rs, st = _state()
    line = sc.score_line(st, rs, "A")
    assert "чужой «≥» — только видимое; скрыты: наука, война, разведка, тайная цель" in line
    assert len(line) <= sc.SCORE_LINE_LIMIT
    rs4 = build_ruleset(None, "quad4")
    st4 = new_game_state(rs4, [*SEATS3, {"seat": "D", "nick": "Дельта"}], 4)
    tags = {s: "Хор Пепла" for s in st4.players}
    line4 = sc.score_line(st4, rs4, "A", tags)
    assert "чужой «≥» — только видимое" in line4 and len(line4) <= sc.SCORE_LINE_LIMIT
    plain = build_ruleset(None, "blitz_spy")                 # the default scoring: only land, no note
    assert "только видимое" not in sc.score_line(new_game_state(plain, SEATS3, 4), plain, "A")


def test_one_hidden_source_is_named_in_the_singular(tmp_path):
    # review F5: «скрыты: наука» for one source; the goal text already says «наука скрыта»
    seats = [{**s, "kind": "agent"} for s in SEATS3]
    config = make_config(game_id="s", scenario="core:quad4", seats=seats, seed=3, scoring={"science": 1, "works": 1})
    game = Game.create(init_game_dir(tmp_path, config)[0], Clock(), start=False)
    game.start()
    while game.phase == "setup":
        for seat in game.alive_seats():
            game.set_ready(seat, None)
    line = sc.score_line(game.state, game.ruleset, "A")
    assert line.endswith("чужой «≥» — только видимое; скрыта: наука") and "скрыты" not in line


def test_the_rules_and_the_goal_name_the_hidden_sources(tmp_path):
    rs = build_ruleset(None, "quad4")
    victory = rules_topic(rs, "victory")
    assert "чужой «≥» — только видимая часть, не оценка" in victory
    assert "наука, война, разведка и тайная цель скрыты" in victory
    assert "чужой «≥» — только видимая часть" in rules_topic(rs, "fog")
    seats = [{"seat": s["seat"], "nick": s["nick"], "kind": "agent"} for s in SEATS3]
    config = make_config(game_id="g", scenario="core:quad4", seats=seats, seed=3, objectives_salt=9)
    goal = goal_text(config, "A")
    assert "чужой «≥» — только видимая часть (земля в твоём обзоре, стройки): наука, война, разведка, тайная " \
           "цель скрыты, настоящий чужой счёт может быть намного больше" in goal   # core 0.8.2 adds the bound
    study = make_config(game_id="s", scenario="core:quad4", seats=seats, seed=3, scoring={"science": 1, "works": 1})
    line = next(x for x in goal_text(study, "A").splitlines() if x.startswith("Цель игры"))
    assert "чужой «≥» — только видимая часть (стройки): наука скрыта, настоящий" in line and "земля" not in line


# ---------------------------------------------------------------- the rules gate

@pytest.mark.parametrize("scenario,seats", [("quad4", 2), ("quad4", 3), ("quad4", 4), ("slice3", 2), ("slice3", 3),
                                            ("slice2", 2), ("blitz_score", 3), ("blitz_finds", 2)])
@pytest.mark.parametrize("artifacts", ["none", "draft", "points"])
def test_the_rules_stay_within_5970(tmp_path, scenario, seats, artifacts):
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="g", scenario=f"core:{scenario}", seats=specs, seed=3,
                                                  systems={"discoveries": True}, objectives_salt=9,
                                                  artifacts=artifacts))
    game = Game.create(gdir, Clock(), start=False)
    game.start()
    while True:
        text = game.rules()
        assert len(text) <= 5970 and "сокращено" not in text, (scenario, seats, artifacts, game.phase, len(text))
        if game.phase != "setup":
            break
        for seat in game.alive_seats():
            game.set_ready(seat, None)
    assert "Очки (+1, ≤3): secret, read_orders, sabotage" in rules_text(game.ruleset)
