"""Core 0.8.3 (decision 34, measurement m12): the frozen core 0.8.2, the price of an early leave from a pact
(score.betrayal.per_leave: -1 to the leaver in the `betrayal` line, the leave still takes effect at once) and the
report's «заплачено и потеряно из-за конца» (dah.report_end: what each seat paid and lost to the end)."""

from __future__ import annotations

import copy
import importlib.util
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dah.engine import (GameState, SeatOrders, build_ruleset, issue_order_id, new_game_state, orders_hash, resolve,
                        submit_orders)
from dah.engine import diplomacy as dip
from dah.engine import espionage as esp
from dah.engine import scoring as sc
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR
from dah.engine.model import PendingBuild
from dah.engine.rules import RULES_LIMIT, VICTORY_CORE_LIMIT, rules_text, rules_topic
from dah.engine.rules import topics as rule_topics
from dah.game import Game, GameError, make_config, replay
from dah.journal import read_events
from dah.report import build_report, render_report
from dah.report_end import end_losses
from dah.store import GameDir, init_game_dir

ROOT = Path(__file__).resolve().parents[1]
CORE = DEFAULT_PACKS_DIR / "core"
CORE082 = LEGACY_PACKS_DIR / "core-0.8.2"
# ruleset hashes of the frozen core 0.8.2, as core 0.8.2 itself built them at 99edf3d (measurement m12: quad4)
CORE082_HASHES = {"slice3": "0cba71d54674", "slice2": "019bacaceaed", "quad4": "041c803d0b2e",
                  "blitz_test": "5318d905c636", "blitz_spy": "4c3f088006e1", "blitz_score": "eb8d9749b5c5",
                  "blitz_finds": "cbc734075e90", "blitz_trade": "24e1fd60325b"}
SCORED = ("slice3", "slice2", "quad4", "blitz_score", "blitz_finds", "blitz_trade")
RUNS = Path(r"C:\Users\kuzmi\DreamAboutHumanity-runs-2026-11-23")
SEATS3 = [{"seat": "A", "nick": "Альфа", "kind": "agent"}, {"seat": "B", "nick": "Бета", "kind": "agent"},
          {"seat": "C", "nick": "Гамма", "kind": "agent"}]
ENGINE_SEATS = [{"seat": s["seat"], "nick": s["nick"]} for s in SEATS3]


class Clock:
    def __call__(self) -> float:
        return 1_700_000_000.0


def ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, None if game.phase in ("setup", "negotiation_1") else orders_hash(game._book(seat)))


def quad_game(tmp_path: Path, name: str = "g", **kw) -> Game:
    config = make_config(game_id=name, scenario="core:quad4", seats=SEATS3, seed=kw.pop("seed", 3),
                         objectives_salt=kw.pop("objectives_salt", 9), **kw)
    gdir, _ = init_game_dir(tmp_path / name, config)
    game = Game.create(gdir, Clock())
    while game.phase == "setup":
        ready_all(game)
    return game


def copy_game(src: Path, tmp_path: Path) -> Path:
    if not (src / "journal.jsonl").is_file():
        pytest.skip(f"measurement data {src} not present")
    dst = tmp_path / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("keys.json"))
    assert not (dst / "keys.json").exists()
    return dst


# ---------------------------------------------------------------- a) the freeze of core 0.8.2

def test_core_082_is_frozen_exactly_with_its_hashes():
    assert "version: 0.8.3" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    head = (CORE082 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.8.2" in head and "Frozen copy of core 0.8.2" in head and "Do not edit" in head
    for sid, prefix in CORE082_HASHES.items():
        old = build_ruleset([str(CORE082)], sid)
        assert old.hash.startswith(prefix), (sid, old.hash[:12])
        assert old.packs == {"core": "0.8.2"} and not sc.leave_penalty_on(old)


def test_the_frozen_copy_is_byte_equal_to_the_commit_of_core_082():
    try:
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "99edf3d",
                                  "content/packs/core"], capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git history not available")
    files = {p.relative_to(CORE082).as_posix() for p in CORE082.rglob("*") if p.is_file()}
    assert files == {name.removeprefix("content/packs/core/") for name in listing}
    for name in listing:
        rel = name.removeprefix("content/packs/core/")
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"99edf3d:{name}"], capture_output=True,
                              check=True).stdout
        data = (CORE082 / rel).read_bytes()
        if rel == "pack.yaml":          # the frozen header only
            data = b"".join(line for line in data.splitlines(keepends=True) if not line.startswith(b"#"))
        assert data == blob, rel


def test_only_the_version_and_the_scenarios_moved_from_the_frozen_copy():
    changed = {"pack.yaml", "scenarios/scenarios.yaml"}
    for path in CORE082.rglob("*"):
        rel = path.relative_to(CORE082).as_posix()
        if path.is_file() and rel not in changed:
            assert path.read_bytes().replace(b"\r\n", b"\n") == (CORE / rel).read_bytes().replace(b"\r\n", b"\n"), rel


@pytest.mark.parametrize("sid", sorted(CORE082_HASHES))
def test_the_scenarios_change_only_the_leave_penalty(sid):
    new, old = build_ruleset(None, sid), build_ruleset([str(CORE082)], sid)
    a, b = old.to_dict(), new.to_dict()
    assert (a.pop("packs"), b.pop("packs")) == ({"core": "0.8.2"}, {"core": "0.8.3"})
    if sid in SCORED:
        assert sc.leave_penalty_on(new) and int(new.stat("score.betrayal.per_leave")) == 1
        assert int(new.stat("score.betrayal.per_break")) == 3
    else:       # the default scoring: no penalty at all, nothing changes
        assert not sc.leave_penalty_on(new) and a == b
        return
    # the one difference is the new stat, 1 in the scenario's table and in every faction's
    assert b["stats"].pop("score.betrayal.per_leave") == 1 and "score.betrayal.per_leave" not in a["stats"]
    for faction, table in b["faction_stats"].items():
        assert table.pop("score.betrayal.per_leave") == 1, faction
    assert a == b


def test_m12_replays_with_the_frozen_core_082(tmp_path):
    """m7-m11 (and m12 again) in test_core082.test_measured_games_replay_with_their_frozen_packs."""
    rep = replay(GameDir(copy_game(RUNS / "games-m12/m12", tmp_path)))
    assert rep.ok and rep.turns and rep.result_ok and sum(not t.ok for t in rep.turns) == 0
    assert all(t.ok for t in rep.recons)
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == "core-0.8.2"


def test_m12_keeps_its_free_leave_on_the_frozen_pack(tmp_path):
    """m12 turn 7: Гамма left her pact with Бета in П1 and took P06 the same turn; on core 0.8.2 that was free."""
    game = Game.load(GameDir(copy_game(RUNS / "games-m12/m12", tmp_path)))
    assert game.ruleset.packs == {"core": "0.8.2"} and not sc.leave_penalty_on(game.ruleset)
    pact = dip.pact_by_id(game.state, "pact-B-C-1")
    assert pact["leaving"] == "C" and pact["ends_before_turn"] == 7 and pact["until_turn"] == 9
    assert sc.early_leave(pact)                       # on core 0.8.3 it would have cost Гамма 1
    assert "betrayal" not in game.state.result["score_parts"]["C"]
    assert game.state.result["scores"] == {"A": 42, "B": 30, "C": 45}
    rs = build_ruleset(None, "quad4")
    assert sc.pacts_left_early(game.state, "C") == 1 and sc.betrayal(game.state, rs, "C") == -1


# ---------------------------------------------------------------- b) the leave penalty: the rule

def _pact_state(sid: str = "blitz_score", turns: int | None = 3, turn: int = 1) -> tuple:
    rs = build_ruleset(None, sid)
    st = new_game_state(rs, ENGINE_SEATS, 4)
    st.turn = turn
    for op in ({"op": "propose", "id": "k1", "sender": "A", "recipient": "B", "turns": turns, "turn": turn,
                "phase": "negotiation_1"},
               {"op": "respond", "id": "k1", "seat": "B", "accept": True, "turn": turn, "phase": "negotiation_1"}):
        dip.apply_op(st, op)
    return rs, st


def _leave(st: GameState, seat: str, phase: str, pid: str = "k1") -> None:
    dip.apply_op(st, dip.make_leave(st, seat, pid, phase))


def test_a_leave_mid_term_costs_the_leaver_one_point_everyone_knows():
    rs, st = _pact_state()
    assert sc.leave_penalty_on(rs) and sc.betrayal(st, rs, "A") == 0
    _leave(st, "A", "negotiation_1")
    assert sc.pacts_left_early(st, "A") == 1 and sc.pacts_left_early(st, "B") == 0
    parts = sc.score_parts(st, rs)
    assert parts["A"]["betrayal"] == -1 and "betrayal" not in parts["B"]
    for viewer in ("B", "C"):                         # announced to all: every seat knows it exactly
        assert sc.score_known(st, rs, viewer, "A")["parts"]["betrayal"] == -1
    assert "разрыв -1" in sc.score_line(st, rs, "A")
    # the leave takes effect at once (decision 28 unchanged) and the penalty stays after it
    new, _ = resolve(st, {}, rs, 9)
    assert dip.pact_by_id(new, "k1")["status"] == dip.LEFT and sc.betrayal(new, rs, "A") == -1
    final = sc.final_parts(new, rs)
    assert final["A"]["betrayal"] == -1 and sum(final["A"].values()) == sc.final_scores(new, rs)["A"]


@pytest.mark.parametrize("phase,turn,cost", [
    ("negotiation_1", 3, 1),        # П1 of the term's last turn: the last protected resolution is cut
    ("negotiation_2", 3, 0),        # П2 of the last turn: it ends after that resolution anyway
    ("negotiation_2", 2, 1),        # П2 before the last turn: the last turn is cut
])
def test_the_last_turn_of_the_term(phase, turn, cost):
    rs, st = _pact_state(turns=3)                     # until_turn 3
    assert dip.pact_by_id(st, "k1")["until_turn"] == 3
    st.turn = turn
    _leave(st, "A", phase)
    pact = dip.pact_by_id(st, "k1")
    assert sc.early_leave(pact) is bool(cost) and sc.leave_cost(st, rs, "A", pact) == cost
    assert sc.betrayal(st, rs, "A") == -cost


def test_an_indefinite_pact_always_costs_its_leaver():
    rs, st = _pact_state(turns=None)
    _leave(st, "B", "negotiation_2")
    assert sc.early_leave(dip.pact_by_id(st, "k1")) and sc.betrayal(st, rs, "B") == -1


def test_a_renewed_term_counts_to_its_new_end():
    rs, st = _pact_state(turns=2)                     # until 2
    st.turn = 2
    dip.apply_op(st, dip.make_proposal(st, "A", "B", 3, "negotiation_1"))
    renewal = next(p for p in st.pacts if p.get("renews") == "k1")
    dip.apply_op(st, dip.make_response(st, "B", renewal["id"], "accept", "negotiation_1"))
    assert dip.pact_by_id(st, "k1")["until_turn"] == 5
    _leave(st, "A", "negotiation_2")                  # without the renewal this would end nothing early
    assert sc.betrayal(st, rs, "A") == -1


def _break_state(leave_phase: str | None, leaver: str, breaker: str) -> tuple:
    """blitz_score: A and B in a pact for 5 turns; `leaver` gives notice in `leave_phase` of turn 1 (None: nobody),
    then `breaker` strikes the other with "pact":"break" in the same resolution."""
    rs, st = _pact_state(turns=5)
    if leave_phase:
        _leave(st, leaver, leave_phase)
    victim = "B" if breaker == "A" else "A"
    src, dst = next((a.id, n) for a in st.owned(breaker) for n in rs.map.adjacency[a.id]
                    if st.provinces[n].owner == victim)
    st.provinces[src].units = {"core:assault": 6}
    st.phase = "actions"
    res = submit_orders(st, rs, breaker, SeatOrders(), [{"kind": "move", "from": src, "to": dst,
                                                         "units": {"assault": 6}, "pact": "break"}], "actions",
                        id_factory=lambda: issue_order_id(st, breaker))
    assert all(c.ok for c in res.results)
    new, _ = resolve(st, {breaker: res.book}, rs, 9)
    return rs, new


def test_a_leave_and_a_break_of_the_same_pact_cost_the_break_only():
    rs, st = _break_state("negotiation_2", "A", "A")      # notice in П2, the strike in the same resolution
    pact = dip.pact_by_id(st, "k1")
    assert pact["status"] == dip.BROKEN and pact["broken_by"] == ["A"] and pact["leaving"] == "A"
    assert sc.pacts_left_early(st, "A") == 0 and sc.betrayal(st, rs, "A") == -3


def test_a_partner_who_strikes_after_the_notice_pays_the_break_the_leaver_nothing():
    rs, st = _break_state("negotiation_2", "A", "B")
    assert dip.pact_by_id(st, "k1")["broken_by"] == ["B"]
    assert sc.betrayal(st, rs, "A") == 0 and sc.betrayal(st, rs, "B") == -3


def test_leaving_several_pacts_costs_one_point_each_and_lapses_cost_nothing():
    rs, st = _pact_state(turns=4)
    for op in ({"op": "propose", "id": "k2", "sender": "A", "recipient": "C", "turns": None, "turn": 1,
                "phase": "negotiation_1"},
               {"op": "respond", "id": "k2", "seat": "C", "accept": True, "turn": 1, "phase": "negotiation_1"},
               {"op": "propose", "id": "k3", "sender": "B", "recipient": "C", "turns": 2, "turn": 1,
                "phase": "negotiation_1"}):
        dip.apply_op(st, op)
    _leave(st, "A", "negotiation_1", "k1")
    _leave(st, "A", "negotiation_1", "k2")
    _leave(st, "B", "negotiation_1", "k3")            # a proposal withdrawn: no pact, no price
    assert dip.pact_by_id(st, "k3")["status"] == dip.WITHDRAWN
    assert sc.betrayal(st, rs, "A") == -2 and sc.betrayal(st, rs, "B") == 0
    # a pact that expires (or is not renewed) costs nothing
    rs, st = _pact_state(turns=1)
    new, _ = resolve(st, {}, rs, 9)
    assert dip.pact_by_id(new, "k1")["status"] == dip.EXPIRED and sc.betrayal(new, rs, "A") == 0


def test_no_leave_penalty_on_frozen_packs_or_the_default_score():
    for rs in (build_ruleset([str(CORE082)], "quad4"), build_ruleset(None, "blitz_spy"),
               build_ruleset(None, "blitz_score", scoring={"territory": 1})):
        st = new_game_state(rs, ENGINE_SEATS, 4)
        for op in ({"op": "propose", "id": "k1", "sender": "A", "recipient": "B", "turns": 3, "turn": 1,
                    "phase": "negotiation_1"},
                   {"op": "respond", "id": "k1", "seat": "B", "accept": True, "turn": 1, "phase": "negotiation_1"}):
            dip.apply_op(st, op)
        _leave(st, "A", "negotiation_1")
        assert not sc.leave_penalty_on(rs) and "betrayal" not in sc.score_parts(st, rs)["A"]
        assert sc.leave_cost(st, rs, "A", dip.pact_by_id(st, "k1")) == 0


# ---------------------------------------------------------------- b) the leave penalty: the live game and its texts

def _you(game: Game, seat: str) -> dict:
    js = game.state_json(seat)
    return js.get("state", js)["you"]


def _signed(game: Game, turns: int | None = 4) -> str:
    out = game.propose_pact("A", "Бета", turns)
    pid = out["pact"]["id"]
    reply = game.respond_pact("B", pid, "accept")
    assert "выход leave_pact до конца срока −1" in reply["text"] and "−3" in reply["text"], reply["text"]
    return pid


def test_the_leave_reply_the_notice_and_the_journal_name_the_point(tmp_path):
    game = quad_game(tmp_path)
    pid = _signed(game)
    out = game.leave_pact("A", pid)
    assert out["penalty"] == 1 and "досрочный выход (до конца срока пакта): −1 к твоему счёту" in out["text"]
    assert "стоил бы −3" in out["text"]
    for seat in ("B", "C"):                           # the notice reaches every seat, the third one too
        texts = [m["text"] for m in game.inbox(seat)["messages"]]
        assert any("Досрочный выход: Альфа −1 к счёту" in t for t in texts), (seat, texts)
    left = next(e for e in read_events(game.gdir.journal_path) if e["type"] == "pact_left")
    assert left["early"] is True and left["penalty"] == 1
    # the third seat's «≥» of Альфа holds the penalty (its line has no room for the rivals' breakdowns)
    known = sc.score_known(game.state, game.ruleset, "C", "A")
    assert known["parts"]["betrayal"] == -1
    line = next(x for x in game.state_text("C").splitlines() if x.startswith("Счёт:"))
    assert f"Альфа (" in line and f"≥{known['score']} ≤{known['score'] + known['hidden_max']}" in line, line
    assert _you(game, "A")["score_parts"]["betrayal"] == -1


def test_a_leave_from_p2_says_a_strike_in_this_resolution_drops_the_point(tmp_path):
    """The pact still protects the resolution after a notice in П2; if it ends broken there, only the break counts
    (scoring.pacts_left_early), so the reply and the notice say the point is conditional. A notice in П1 ends the
    pact before the resolution: no condition."""
    game = quad_game(tmp_path)
    pid = _signed(game)
    while game.phase != "negotiation_2":
        ready_all(game)
    out = game.leave_pact("A", pid)
    unless = "если в расчёте этого хода пакт будет разорван ударом — только разрыв, выход не в счёт"
    assert out["penalty"] == 1 and f"−1 к твоему счёту ({unless})" in out["text"]
    assert any(f"Досрочный выход: Альфа −1 к счёту ({unless})." in m["text"] for m in game.inbox("C")["messages"])
    first = quad_game(tmp_path, name="p1")
    out = first.leave_pact("A", _signed(first))         # П1 of turn 1: the pact ends before this resolution
    assert out["penalty"] == 1 and "ударом" not in out["text"]
    assert not any("ударом" in m["text"] for m in first.inbox("C")["messages"])


def test_a_leave_that_ends_nothing_early_is_free_and_says_so(tmp_path):
    game = quad_game(tmp_path)
    pid = _signed(game, turns=1)                      # until turn 1
    while game.phase != "negotiation_2":
        ready_all(game)
    out = game.leave_pact("A", pid)
    assert out["penalty"] == 0 and "срок пакта и так кончается — выход без штрафа" in out["text"]
    assert "betrayal" not in _you(game, "A")["score_parts"]


def test_a_frozen_pack_game_keeps_the_free_leave_and_its_texts(tmp_path):
    game = quad_game(tmp_path, pack_dirs=[str(CORE082)])
    pid = game.propose_pact("A", "Бета", 4)["pact"]["id"]
    reply = game.respond_pact("B", pid, "accept")
    assert "leave_pact до конца срока" not in reply["text"]
    out = game.leave_pact("A", pid)
    assert "penalty" not in out and "−1" not in out["text"]
    left = next(e for e in read_events(game.gdir.journal_path) if e["type"] == "pact_left")
    assert "penalty" not in left and "early" not in left
    assert "leave_pact(id)." in game.rules() and "до срока −" not in game.rules()


def test_a_game_with_a_leave_replays(tmp_path):
    game = quad_game(tmp_path, turn_limit="2-3", seed=8)
    pid = _signed(game)
    game.leave_pact("B", pid)
    while not game.game_over:
        ready_all(game)
    assert game.state.result["score_parts"]["B"]["betrayal"] == -1
    rep = replay(game.gdir)
    assert rep.ok and rep.result_ok and sum(not t.ok for t in rep.turns) == 0


@pytest.mark.parametrize("sid", SCORED)
def test_the_rules_name_the_leave_price(sid):
    rs = build_ruleset(None, sid)
    core = rules_text(rs)
    assert "leave_pact(id) (до срока −1)." in core
    victory = next(line for line in core.splitlines() if line.startswith("ПОБЕДА"))
    assert len(victory) <= VICTORY_CORE_LIMIT and "разрыв пакта −3" in victory
    topic = rules_topic(rs, "victory")
    assert "досрочный выход leave_pact (пакт кончается раньше срока; бессрочный — всегда) — −1" in topic
    assert "выход leave_pact — без штрафа" not in topic
    pacts = rules_topic(rs, "pacts")
    assert "Выход до конца срока пакта (из бессрочного — всегда) стоит вышедшему −1 к счёту (удар без выхода — −3)" \
        in pacts
    old = build_ruleset([str(CORE082)], sid)
    assert "выход leave_pact — без штрафа" in rules_topic(old, "victory") and "до срока −" not in rules_text(old)


def _rules_of_core_082():
    """dah.engine.rules as core 0.8.2 had it (99edf3d), loaded beside the current one."""
    try:
        source = subprocess.run(["git", "-C", str(ROOT), "show", "99edf3d:src/dah/engine/rules.py"],
                                capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git history not available")
    return source


@pytest.mark.parametrize("sid", sorted(CORE082_HASHES))
def test_a_frozen_pack_game_gets_the_rules_text_of_core_082(tmp_path, sid):
    """A game on the frozen core 0.8.2 gets the rules text and every topic exactly as core 0.8.2 printed them (the
    victory topic's «Нарушения публичны» included: «и выходы» belongs to a pack with the leave price only)."""
    path = tmp_path / "rules_082.py"
    path.write_bytes(_rules_of_core_082())
    spec = importlib.util.spec_from_file_location("dah.engine._rules_082", path)
    old_rules = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old_rules
    try:
        spec.loader.exec_module(old_rules)
        rs = build_ruleset([str(CORE082)], sid)
        assert rules_text(rs) == old_rules.rules_text(rs)
        assert list(rule_topics(rs)) == list(old_rules.topics(rs))
        for topic in rule_topics(rs):
            assert rules_topic(rs, topic) == old_rules.rules_topic(rs, topic), topic
    finally:
        sys.modules.pop(spec.name, None)
    if sid in SCORED:
        assert "Нарушения публичны, поэтому этот штраф виден всем." in rules_topic(rs, "victory")
        new = rules_topic(build_ruleset(None, sid), "victory")
        assert "Нарушения и выходы публичны, поэтому этот штраф виден всем." in new


def test_the_refusal_that_advises_leaving_an_indefinite_pact_names_the_price(tmp_path):
    game = quad_game(tmp_path)
    pid = _signed(game, turns=None)
    with pytest.raises(GameError) as err:
        game.propose_pact("A", "Бета", 3)
    assert err.value.code == "PACT_EXISTS" and f'leave_pact(id="{pid}")' in err.value.message
    assert "выход из бессрочного пакта — −1 к твоему счёту" in err.value.message
    old = quad_game(tmp_path, name="old", pack_dirs=[str(CORE082)])
    pid = old.propose_pact("A", "Бета", None)["pact"]["id"]
    old.respond_pact("B", pid, "accept")
    with pytest.raises(GameError) as err:
        old.propose_pact("A", "Бета", 3)
    assert err.value.code == "PACT_EXISTS" and "−" not in err.value.message      # a leave is free there


def test_unscored_scenarios_keep_their_pact_texts():
    for sid in ("blitz_test", "blitz_spy"):
        rs, old = build_ruleset(None, sid), build_ruleset([str(CORE082)], sid)
        assert rules_text(rs) == rules_text(old)
        assert {t: rules_topic(rs, t) for t in rule_topics(rs)} == {t: rules_topic(old, t) for t in rule_topics(old)}


@pytest.mark.parametrize("sid", SCORED)
def test_topics_stay_within_their_limits(sid):
    rs, old = build_ruleset(None, sid), build_ruleset([str(CORE082)], sid)
    for t in rule_topics(rs):
        text = rules_topic(rs, t)
        assert len(text) <= RULES_LIMIT, (t, len(text))
        if t not in ("pacts", "victory"):
            assert text == rules_topic(old, t), t      # only the two pact texts changed
    # the pacts topic grew by the one sentence, the victory topic by the leave clause
    assert len(rules_topic(rs, "pacts")) - len(rules_topic(old, "pacts")) <= 170
    assert len(rules_topic(rs, "victory")) - len(rules_topic(old, "victory")) <= 260


def _seat_rules_max(tmp_path: Path, sid: str, seats: int, artifacts: str) -> int:
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    gdir, _ = init_game_dir(tmp_path / f"{sid}{seats}{artifacts}",
                            make_config(game_id="g", scenario=f"core:{sid}", seats=specs, seed=3,
                                        artifacts=artifacts, objectives_salt=9,
                                        systems={"espionage": True, "conquest": True, "discoveries": True,
                                                 "trade": True}))
    game = Game.create(gdir, Clock(), start=False)
    game.start()
    longest = 0
    while True:
        text = game.rules()
        assert "сокращено" not in text
        longest = max(longest, len(text))
        if game.phase != "setup":
            return longest
        ready_all(game)


@pytest.mark.parametrize("sid", ["quad4", "slice3", "slice2", "blitz_score", "blitz_finds", "blitz_trade",
                                 "blitz_spy", "blitz_test"])
def test_rules_stay_within_the_gate_on_every_core_scenario(tmp_path, sid):
    most = build_ruleset(None, sid).map.max_players
    for seats in range(2, min(4, most) + 1):
        for artifacts in ("none", "draft", "points"):
            n = _seat_rules_max(tmp_path, sid, seats, artifacts)
            assert n <= 5970, (sid, seats, artifacts, n)


# ---------------------------------------------------------------- b) fog: the bound with leaves

def _simulate_module():
    spec = importlib.util.spec_from_file_location("dah_simulate", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate"] = module
    spec.loader.exec_module(module)
    return module


def _true_final(state: GameState, rs, seat: str) -> int:
    total = sum(sc.score_parts(state, rs)[seat].values())
    if sc.on(rs, "objectives") and state.players[seat].alive:
        total += rs.weight("objectives") * sc.objective_points(state, rs, seat)
    return total


def _public_penalty(state: GameState, rs, seat: str) -> int:
    """The pact penalty as a third seat can rebuild it: from what everyone knows of a pact (diplomacy.public_pact:
    the terms, renewals included, and the announced leave), its public end (status, broken_by: a break is announced)
    and the seat's faction stat (only factions modify score.*, test_core082). Never from the scoring code."""
    faction = state.players[seat].faction
    out = 0
    for p in state.pacts:
        if p.get("status") == dip.BROKEN:
            if sc.betrayal_on(rs) and seat in (p.get("broken_by") or ()):
                out -= int(rs.stat("score.betrayal.per_break", faction))
            continue
        if p.get("status") not in (dip.ACTIVE, dip.LEFT, dip.ENDED) or not sc.leave_penalty_on(rs):
            continue
        public = dip.public_pact(p)
        end, until = public["ends_before_turn"], public["until_turn"]
        if public["leaving"] == seat and end is not None and (until is None or end <= until):
            out -= int(rs.stat("score.betrayal.per_leave", faction))
    return out


@pytest.mark.parametrize("scenario,kinds", [("quad4", "diplomat,diplomat,diplomat,rush"),
                                            ("blitz_trade", "diplomat,diplomat,builder")])
def test_with_leaves_the_visible_part_and_the_bound_hold_the_true_score(monkeypatch, scenario, kinds):
    """The rival bound treats the pact penalty as known exactly: a leave is announced to all, so a third seat knows
    it, and the check rebuilds it from the public pact terms only (_public_penalty); and since score_known reads the
    penalty from the true state (never from what the viewer has read), the «≥»
    never exceeds the true score and «≥» + bound is never below it, also for a viewer that has not seen the
    notice yet. Diplomats here leave their pacts at random (early, on the last turn of a term, from П1 and П2)."""
    from dah.bots.diplomat import DiplomatBot
    sim = _simulate_module()
    rs = build_ruleset(None, scenario)
    rng = random.Random(34)
    original_diplomacy = DiplomatBot.diplomacy
    leaves = []

    def leaving(self, view, api):
        original_diplomacy(self, view, api)
        for p in view.pacts:
            if p.get("id") and not p.get("leaving") and rng.random() < 0.35:
                try:
                    api.leave_pact(str(p["id"]))
                    leaves.append(p["id"])
                except Exception:       # the simulator's API refuses what the game would
                    pass

    monkeypatch.setattr(DiplomatBot, "diplomacy", leaving)
    checks = []
    original = sc.score_known

    def checked(state, ruleset, viewer, seat):
        known = original(state, ruleset, viewer, seat)
        if not known["exact"]:
            real = _true_final(state, ruleset, seat)
            assert known["parts"].get("betrayal", 0) == _public_penalty(state, ruleset, seat)
            assert known["score"] <= real <= known["score"] + known["hidden_max"], (state.turn, viewer, seat, real,
                                                                                      known)
            checks.append(known["parts"].get("betrayal", 0))
        return known

    monkeypatch.setattr(sc, "score_known", checked)
    seats = {"ABCD"[i]: k for i, k in enumerate(kinds.split(","))}
    from dah.game import setup_config
    for game_seed in ("l1", "l2"):
        sim.play_game(rs, seats, game_seed, setup_config(rs, None, None, None))
    assert leaves and len(checks) > 100 and any(v < 0 for v in checks)


# ---------------------------------------------------------------- c) the report: «заплачено и потеряно из-за конца»

def test_m12_report_names_the_spire_shares_and_the_shades(tmp_path):
    """m12 ended on the hidden turn 14: Бета's Spire (started turn 12) had 3 of 4 shares paid (the first with the
    order, then turns 13 and 14), Гамма's Shade s3 (hired turn 14) never went out and her s2's read_orders (2
    rounds from turn 14, caught on the first) had its round of turn 15 paid."""
    gdir = copy_game(RUNS / "games-m12/m12", tmp_path)
    report = build_report(read_events(gdir / "journal.jsonl"))
    lost = report["lost_at_end"]
    assert lost["end_turn"] == 14 and lost["priced"] == "ruleset" and not lost["at_elimination"]
    a, b, c = (lost["seats"][s] for s in "ABC")
    assert a["total"] == {} and not (a["works"] or a["shades"] or a["rounds"])
    assert b["works"] == [{"province": "P18", "building": "core:dream_spire", "great": True, "shares": 4,
                           "shares_paid": 3, "paid": {"core:alloy": 45, "core:provisions": 75}}]
    assert b["total"] == {"core:alloy": 45, "core:provisions": 75}
    assert c["shades"] == [{"shade": "s3", "turn": 14, "cost": {"core:alloy": 20}}]
    assert c["rounds"] == [{"shade": "s2", "mission": "core:read_orders", "target": "A", "first_turn": 14,
                            "rounds": 2, "lost": 1, "cost": {"core:provisions": 2}, "ended": "caught"}]
    assert c["total"] == {"core:alloy": 20, "core:provisions": 2}
    assert lost["game"]["total"] == {"core:alloy": 65, "core:provisions": 77}
    text = render_report(report)
    assert "ЗАПЛАЧЕНО И ПОТЕРЯНО ИЗ-ЗА КОНЦА (после хода 14" in text
    assert "B Бета: стройки dream_spire в P18 (3/4 дол., alloy 45 provisions 75)" in text
    assert "C Гамма: Тени без задания s3 (нанята на ходу 14, alloy 20); раунды после конца s2 read_orders → Альфа " \
           "(ходы 14–15, потеряно 1, provisions 2; задание кончилось: caught)" in text
    assert "старый журнал" in text and "по правилам партии" in text


@pytest.mark.parametrize("name", ["m7", "m8", "m9", "m10", "m11", "m12"])
def test_old_journals_still_report(tmp_path, name):
    gdir = copy_game(RUNS / f"games-{name}" / name, tmp_path)
    report = build_report(read_events(gdir / "journal.jsonl"))
    lost = report["lost_at_end"]
    assert lost["end_turn"] == report["result"]["turn"]
    assert set(lost["seats"]) | set(lost["at_elimination"]) == set(report["seats"])
    for block in (*lost["seats"].values(), *lost["at_elimination"].values()):
        assert all(n > 0 for n in block["total"].values())
        assert block.get("works_note")                  # an older journal: great works only
    assert "ЗАПЛАЧЕНО И ПОТЕРЯНО ИЗ-ЗА КОНЦА" in render_report(report)


def _events(*extra, unfinished=None, eliminated=None):
    over = {"type": "game_over", "turn": 5, "winners": ["A"], "reason": "turn_limit", "scores": {}}
    if unfinished is not None:
        over["unfinished"] = unfinished
    out = [{"type": "game_created", "seq": 1, "ts": 0.0, "config": {"game_id": "x", "scenario": "core:quad4",
                                                                    "seats": SEATS3}}]
    out += [{"type": "seat_info", "seat": s["seat"], "nick": s["nick"], "kind": "agent", "ts": 0.0} for s in SEATS3]
    out += list(extra)
    if eliminated:
        out.append({"type": "turn_resolved", "turn": eliminated[1], "eliminated": [eliminated[0]], "scores": {},
                    "ts": 0.0})
    out.append({**over, "ts": 1.0})
    return out


def test_the_block_rules_on_a_new_journal():
    hire = {"type": "shade_hired", "seat": "A", "shade": "s2", "turn": 5, "cost": {"core:alloy": 20}, "ts": 0.0}
    used = {"type": "shade_hired", "seat": "A", "shade": "s3", "turn": 3, "cost": {"core:alloy": 20}, "ts": 0.0}
    free = {"type": "shade_hired", "seat": "B", "shade": "s1", "turn": 0, "free": True, "ts": 0.0}
    start = {"type": "mission_started", "seat": "A", "shade": "s3", "mission": "core:observe", "target": "P03",
             "rounds": 4, "first_turn": 4, "turn": 4, "cost": {"core:provisions": 4},
             "round_cost": {"core:provisions": 1}, "ts": 0.0}
    early = {"type": "mission_started", "seat": "B", "shade": "s1", "mission": "core:secret", "target": "A",
             "rounds": 3, "first_turn": 3, "turn": 3, "cost": {"core:provisions": 6},
             "round_cost": {"core:provisions": 2}, "ts": 0.0}
    caught = {"type": "mission_end", "seat": "B", "shade": "s1", "turn": 3, "reason": "caught", "ts": 0.0}
    promise = {"type": "promise_made", "seat": "B", "to": "C", "goods": {"resources": {"core:alloy": 7}},
               "by_turn": 6, "trade_id": "t1", "turn": 2, "ts": 0.0}
    kept_due = {"type": "promise_made", "seat": "B", "to": "C", "goods": {"resources": {"core:alloy": 3}},
                "by_turn": 5, "trade_id": "t2", "turn": 2, "ts": 0.0}
    unfinished = {"A": {"builds": [{"province": "P01", "building": "core:citadel", "paid": {"core:alloy": 40},
                                    "shares_paid": 1, "shares": 1, "turns_left": 1, "great": False}],
                        "research": {"core:war_drums": 6}, "research_resource": "core:creed"}}
    events = _events(hire, used, free, start, early, caught, promise, kept_due, unfinished=unfinished)
    report = build_report(events)
    lost = report["lost_at_end"]
    a, b, c = (lost["seats"][s] for s in "ABC")
    assert a["shades"] == [{"shade": "s2", "turn": 5, "cost": {"core:alloy": 20}}]       # s3 went out
    assert a["rounds"] == [{"shade": "s3", "mission": "core:observe", "target": "P03", "first_turn": 4, "rounds": 4,
                            "lost": 2, "cost": {"core:provisions": 2}, "ended": None}]    # rounds of turns 6 and 7
    assert a["works"][0]["building"] == "core:citadel" and a["research"] == {"core:war_drums": 6}
    assert a["total"] == {"core:alloy": 60, "core:creed": 6, "core:provisions": 2}
    assert b["rounds"] == [] and b["shades"] == []      # caught on turn 3: lost to the capture, not to the end
    assert c["promises"] == [{"from": "B", "goods": {"resources": {"core:alloy": 7}}, "by_turn": 6, "trade": "t1"}]
    assert c["total"] == {} and c["owed"] == {"core:alloy": 7}          # owed to C, not paid by it
    assert lost["priced"] == "journal" and "works_note" not in a
    text = render_report(report)
    assert "исследования war_drums 6 (creed)" in text
    assert "не получено по обещаниям со сроком после конца (причиталось, не заплачено) от Бета к ходу 6: alloy 7"         in text


def test_a_promise_delivered_early_is_not_lost_and_owed_goods_stay_out_of_the_paid_total():
    """F2: the game never carries out `later`, but the giver may deliver early (a gift, or a trade marked `fulfils`);
    the block counts only the part not delivered by the end, and apart from what the seat paid."""
    def promise(giver, to, alloy, trade, provinces=()):
        goods = {"resources": {"core:alloy": alloy}, **({"provinces": list(provinces)} if provinces else {})}
        return {"type": "promise_made", "seat": giver, "to": to, "goods": goods, "by_turn": 9, "trade_id": trade,
                "turn": 2, "ts": 0.0}

    def gift(giver, to, alloy, turn):
        return {"type": "gift", "seat": giver, "to": to, "resources": {"core:alloy": alloy}, "turn": turn, "ts": 0.0}

    hire = {"type": "shade_hired", "seat": "C", "shade": "s2", "turn": 4, "cost": {"core:alloy": 20}, "ts": 0.0}
    events = _events(promise("B", "C", 7, "t1"), gift("B", "C", 7, 3),          # delivered in full on turn 3
                     promise("A", "C", 5, "t2", provinces=["P09"]), gift("A", "C", 2, 4),       # 2 of 5, no province
                     hire, unfinished={})
    report = build_report(events)
    c = report["lost_at_end"]["seats"]["C"]
    assert c["promises"] == [{"from": "A", "goods": {"resources": {"core:alloy": 3}, "provinces": ["P09"]},
                              "by_turn": 9, "trade": "t2"}]
    assert c["total"] == {"core:alloy": 20} and c["owed"] == {"core:alloy": 3}     # the Shade paid; the rest owed
    game = report["lost_at_end"]["game"]
    assert game["total"] == {"core:alloy": 20} and game["owed"] == {"core:alloy": 3}
    text = render_report(report)
    assert "от Бета" not in text.split("ЗАПЛАЧЕНО И ПОТЕРЯНО ИЗ-ЗА КОНЦА")[1]
    assert "от Альфа к ходу 9: alloy 3 + провинции P09" in text
    assert "всего по партии: alloy 20; не получено по обещаниям: alloy 3" in text


def test_a_shade_whose_only_mission_starts_after_the_end_keeps_its_hire_price():
    """F3: a Shade hired earlier and first sent in П2 of the last turn (first round on the turn after the end) played
    nothing: its hire price is lost as an idle one's, and so are the rounds."""
    def hired(sid):
        return {"type": "shade_hired", "seat": "C", "shade": sid, "turn": 4, "cost": {"core:alloy": 20}, "ts": 0.0}

    late = {"type": "mission_started", "seat": "C", "shade": "s3", "mission": "core:read_orders", "target": "A",
            "rounds": 1, "phase": "negotiation_2", "first_turn": 6, "turn": 5, "cost": {"core:alloy": 5},
            "round_cost": {"core:alloy": 5}, "ts": 0.0}
    played = {"type": "mission_started", "seat": "C", "shade": "s5", "mission": "core:read_orders", "target": "A",
              "rounds": 1, "phase": "actions", "first_turn": 5, "turn": 5, "cost": {"core:alloy": 5},
              "round_cost": {"core:alloy": 5}, "ts": 0.0}
    c = build_report(_events(hired("s3"), hired("s4"), hired("s5"), late, played))["lost_at_end"]["seats"]["C"]
    assert [x["shade"] for x in c["shades"]] == ["s3", "s4"]            # s5 played its round on turn 5
    assert [(r["shade"], r["lost"]) for r in c["rounds"]] == [("s3", 1)]
    assert c["total"] == {"core:alloy": 45}


def test_a_mission_given_in_p2_journals_its_prices(tmp_path):
    """F4: a mission ordered in П2 is journaled by the resolution; it carries `cost` and `round_cost` too, so the
    report of a new journal never falls back to the ruleset's prices."""
    game = quad_game(tmp_path, turn_limit="2-4", seed=3)        # the hidden end is turn 3 here
    last = None
    while not game.game_over:
        if game.phase == "negotiation_2":
            order = {"kind": "spy", "shade": "s1", "mission": "read_orders", "target": "Бета", "rounds": 1}
            last = (game.state.turn, bool(game.submit("A", [order])["accepted"]))
        ready_all(game)
    assert game.state.turn == 3 and last == (3, True)       # sent in П2 of the last turn: its round is lost
    events = read_events(game.gdir.journal_path)
    started = [e for e in events if e["type"] == "mission_started" and e["seat"] == "A"]
    assert started and all(e["phase"] == "negotiation_2" for e in started)
    md = game.ruleset.missions["core:read_orders"]
    assert all(e["round_cost"] == dict(md.cost_per_round) for e in started)
    assert started[-1]["cost"] == esp.mission_cost(game.ruleset, "core:read_orders", 1)
    lost = build_report(events)["lost_at_end"]
    assert lost["priced"] == "journal" and [r["first_turn"] for r in lost["seats"]["A"]["rounds"]] == [4]
    assert "по правилам партии" not in render_report(build_report(events))


def test_an_eliminated_seat_reports_its_losses_apart():
    hire = {"type": "shade_hired", "seat": "C", "shade": "s2", "turn": 3, "cost": {"core:alloy": 20}, "ts": 0.0}
    mission = {"type": "mission_started", "seat": "C", "shade": "s1", "mission": "core:secret", "target": "A",
               "rounds": 3, "first_turn": 3, "turn": 3, "cost": {}, "round_cost": {"core:provisions": 2}, "ts": 0.0}
    report = build_report(_events(hire, mission, unfinished={}, eliminated=("C", 3)))
    lost = report["lost_at_end"]
    assert "C" not in lost["seats"] and lost["at_elimination"]["C"]["turn"] == 3
    c = lost["at_elimination"]["C"]
    assert c["shades"][0]["shade"] == "s2" and c["rounds"][0]["lost"] == 2
    assert lost["game"]["total"] == {}                  # the end's total leaves the elimination out
    assert "C Гамма выбыл на ходу 3 — потеряно при выбытии, не из-за конца" in render_report(report)


def test_no_block_before_game_over():
    events = [e for e in _events() if e["type"] != "game_over"]
    report = build_report(events)
    assert "lost_at_end" not in report and "ЗАПЛАЧЕНО" not in render_report(report)
    assert end_losses(events, report) is None


def test_the_game_journals_what_stayed_unfinished(tmp_path):
    game = quad_game(tmp_path, turn_limit="2-2", seed=8)
    st = game.state
    rs = game.ruleset
    a_prov = next(p.id for p in st.owned("A") if p.id not in rs.map.capitals)
    b_prov = next(p.id for p in st.owned("B"))
    st.pending_builds.append(PendingBuild("A", a_prov, "core:dream_spire", 3,
                                          dict(rs.building_cost("core:dream_spire", st.players["A"].faction))))
    st.pending_builds.append(PendingBuild("B", b_prov, "core:citadel", 2))
    tech = next(t for t in rs.techs if t not in st.players["C"].techs)
    st.players["C"].research[tech] = 5
    unfinished = game._unfinished(copy.deepcopy(st))
    spire = unfinished["A"]["builds"][0]
    assert (spire["shares_paid"], spire["shares"], spire["great"]) == (1, 4, True)
    assert spire["paid"] == {"core:alloy": 15, "core:provisions": 25}
    citadel = unfinished["B"]["builds"][0]
    assert citadel["paid"] == dict(rs.building_cost("core:citadel", st.players["B"].faction))
    assert unfinished["C"]["research"] == {tech: 5} and unfinished["C"]["research_resource"] == rs.research_resource
    game.state.pending_builds = []
    game.state.players["C"].research = {}
    while not game.game_over:
        ready_all(game)
    over = next(e for e in read_events(game.gdir.journal_path) if e["type"] == "game_over")
    assert "unfinished" in over
    report = build_report(read_events(game.gdir.journal_path))
    assert report["lost_at_end"]["priced"] == "journal"
    assert not any(b.get("works_note") for b in report["lost_at_end"]["seats"].values())
    # nothing seat-facing: the block is the report's only
    for seat in "ABC":
        assert "потеряно из-за конца" not in game.state_text(seat).lower()
