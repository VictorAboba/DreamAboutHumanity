"""Core 0.7.1 (decision 28, measurement m9): taking back a province one has owned earlier in this game pays no war
points (score.war.no_reclaim), the frozen core 0.7.0 for older games."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dah.engine import build_ruleset, new_game_state, resolve, rules_topic, view_state
from dah.engine import scoring as sc
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR
from dah.engine.model import state_hash
from dah.engine.view import event_text
from dah.game import replay
from dah.report import _score_lines, _scoring
from dah.store import GameDir
from test_core070 import SEATS3, _orders

CORE = DEFAULT_PACKS_DIR / "core"
CORE070 = LEGACY_PACKS_DIR / "core-0.7.0"
CORE071 = LEGACY_PACKS_DIR / "core-0.7.1"     # core 0.8.0 froze 0.7.1: the 0.7.0 -> 0.7.1 step is checked on the copy
# ruleset hashes of the frozen core 0.7.0, as core 0.7.0 itself built them at 5ffd756 (measurement m9: quad4)
CORE070_HASHES = {"slice3": "4ddde183cd4c", "slice2": "e11bb21ab379", "quad4": "e198c9bc95b4",
                  "blitz_test": "c7b543f770ee", "blitz_spy": "3362b0478502", "blitz_score": "d3aee839cf66",
                  "blitz_finds": "f2d4cdc04f76"}
RUNS = Path(r"C:\Users\kuzmi\DreamAboutHumanity-runs-2026-11-23")
SCORED = ("slice3", "slice2", "quad4", "blitz_score", "blitz_finds")


def _state(packs: list[str] | None = None):
    """blitz_score: A (forge P02, 8 Assault) next to C's empty start agri P07; C holds P05 next to P07."""
    rs = build_ruleset(packs, "blitz_score")
    st = new_game_state(rs, SEATS3, 4)
    st.provinces["P02"].units = {"core:assault": 8}
    return rs, st


def _take(st, rs, seat: str, src: str, dst: str = "P07"):
    st.provinces[src].units = {"core:assault": 8}
    st.provinces[dst].units = {}
    book = _orders(st, rs, seat, [{"kind": "move", "from": src, "to": dst, "units": {"assault": 8}}])
    st, rep = resolve(st, {seat: book}, rs, 3)
    assert st.provinces[dst].owner == seat
    return st, [e for e in rep.events if e["kind"] == "war_points"]


# ---------------------------------------------------------------- freeze

def test_core_070_is_frozen_exactly_with_its_hashes():
    assert "version: 0.8.1" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    head = (CORE070 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.7.0" in head and "Frozen copy of core 0.7.0" in head and "Do not edit" in head
    for sid, prefix in CORE070_HASHES.items():
        old = build_ruleset([str(CORE070)], sid)
        assert old.hash.startswith(prefix), (sid, old.hash[:12])
        assert old.packs == {"core": "0.7.0"} and not sc.no_reclaim(old)


def test_only_the_scenarios_and_the_version_moved_from_the_frozen_copy():
    changed = {"pack.yaml", "scenarios/scenarios.yaml"}
    for path in CORE070.rglob("*"):
        rel = path.relative_to(CORE070).as_posix()
        if path.is_file() and rel not in changed:
            assert path.read_bytes().replace(b"\r\n", b"\n") == (CORE071 / rel).read_bytes().replace(b"\r\n", b"\n"), rel


@pytest.mark.parametrize("sid", SCORED)
def test_the_scored_scenarios_turn_no_reclaim_on_and_only_it_changes(sid):
    new, old = build_ruleset([str(CORE071)], sid), build_ruleset([str(CORE070)], sid)
    assert sc.no_reclaim(new) and new.stat("score.war.no_reclaim") == 1
    assert sc.no_reclaim(build_ruleset(None, sid))
    a, b = old.to_dict(), new.to_dict()
    assert (a.pop("packs"), b.pop("packs")) == ({"core": "0.7.0"}, {"core": "0.7.1"})
    for d in (b["stats"], *b["faction_stats"].values()):
        assert d.pop("score.war.no_reclaim") == 1
    assert a == b


def test_plain_scenarios_leave_no_reclaim_off():
    for sid in ("blitz_test", "blitz_spy"):
        assert not sc.no_reclaim(build_ruleset(None, sid))


# ---------------------------------------------------------------- the rule

def test_retaking_ones_own_start_province_scores_nothing_and_holds_nothing():
    rs, st = _state()
    st, ev = _take(st, rs, "A", "P02")
    assert [(e["seat"], e["reason"], e["points"]) for e in ev] == [("A", "capture", 1)]
    st, ev = _take(st, rs, "C", "P05")                        # C takes back its own start province
    assert [(e["seat"], e["province"], e["reason"], e["points"]) for e in ev] == [("C", "P07", "reclaim", 0)]
    assert "возврат своей провинции" in event_text(ev[0], rs, st, "C")
    assert sc.war(st, rs, "C") == 0 and "P07" not in sc.war_provinces(st, "C")
    for _ in range(5):                                         # held for 5 turns: no holding points
        st, rep = resolve(st, {}, rs, 3)
        assert not [e for e in rep.events if e["kind"] == "war_points" and e["seat"] == "C"]
    assert sc.war(st, rs, "C") == 0 and st.provinces["P07"].owner == "C"
    assert sc.war(st, rs, "A") == 1                            # A keeps its point after the loss


def test_retaking_a_province_one_took_earlier_and_lost_scores_nothing():
    rs, st = _state()
    st, _ = _take(st, rs, "A", "P02")                          # +1
    st, _ = _take(st, rs, "C", "P05")                          # 0 (C's own)
    assert sc.war_view(st, rs, "A")["reclaim"] == ["P07"]       # the bot / the player sees it pays nothing
    st, ev = _take(st, rs, "A", "P02")                          # A owned it at the end of an earlier turn
    assert [(e["seat"], e["reason"], e["points"]) for e in ev] == [("A", "reclaim", 0)]
    for _ in range(4):
        st, rep = resolve(st, {}, rs, 3)
        assert not [e for e in rep.events if e["kind"] == "war_points"]
    assert sc.war_provinces(st, "A")["P07"] == {"points": 1, "since": None}
    assert sc.war(st, rs, "A") == 1 and sc.war(st, rs, "C") == 0


def test_the_first_capture_of_a_rivals_start_province_scores_and_holds():
    rs, st = _state()
    st, ev = _take(st, rs, "A", "P02")
    assert [(e["seat"], e["province"], e["reason"], e["total"]) for e in ev] == [("A", "P07", "capture", 1)]
    totals = []
    for _ in range(4):
        st, _ = resolve(st, {}, rs, 3)
        totals.append(sc.war(st, rs, "A"))
    assert totals == [1, 2, 2, 3]
    assert "P07" not in sc.war_view(st, rs, "A")["reclaim"]     # held now: not in the list of others' land


def test_ownership_history_is_in_the_state_and_its_hash():
    rs, st = _state()
    assert "owned" not in st.players["A"].tally                 # nothing before the first resolution
    st, _ = resolve(st, {}, rs, 3)
    assert st.players["C"].tally["owned"] == sorted(p.id for p in st.owned("C"))
    with_history = state_hash(st)
    st.players["C"].tally.pop("owned")                            # the history counts in the hash
    assert state_hash(st) != with_history
    st.players["C"].tally["owned"] = ["P07"]
    assert type(st).from_dict(st.to_dict()).players["C"].tally["owned"] == ["P07"]


def test_rules_name_the_rule_only_when_it_is_on():
    assert "Своё не в счёт" in rules_topic(build_ruleset(None, "quad4"), "victory")
    assert "Своё не в счёт" not in rules_topic(build_ruleset([str(CORE070)], "quad4"), "victory")


def test_the_report_marks_a_reclaim_as_zero():
    events = [{"type": "game_created", "scoring": {"war": 1}},
              {"type": "war_points", "seat": "A", "province": "P07", "points": 1, "reason": "capture"},
              {"type": "war_points", "seat": "C", "province": "P07", "points": 0, "reason": "reclaim"}]
    report = {"seats": {"A": {}, "C": {}}, "result": {}}
    sc_ = _scoring(events, report)
    assert sc_["war_provinces"] == {"A": {"P07": 1}, "C": {"P07": 0}} and sc_["war_reclaims"] == {"C": {"P07": 1}}
    line = next(x for x in _score_lines({**report, "scoring": sc_}, {}) if x.startswith("война по провинциям"))
    assert "C P07 0 (возврат своей ×1: 0)" in line and "A P07 1" in line


# ---------------------------------------------------------------- legacy 0.7.0 unaffected

def test_legacy_070_still_pays_a_retake_and_keeps_no_history():
    rs, st = _state([str(CORE070)])
    st, _ = _take(st, rs, "A", "P02")
    st, ev = _take(st, rs, "C", "P05")
    assert [(e["seat"], e["reason"], e["points"]) for e in ev] == [("C", "capture", 1)]
    assert all("owned" not in p.tally for p in st.players.values())
    assert "reclaim" not in sc.war_view(st, rs, "C")


@pytest.mark.parametrize("name", ["games-m9/m9", "games-m8/m8"])
def test_measured_games_replay_with_their_frozen_packs(tmp_path, name):
    src = RUNS / name
    if not (src / "journal.jsonl").is_file():
        pytest.skip(f"measurement data {src} not present")
    dst = tmp_path / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("keys.json"))
    assert not (dst / "keys.json").exists()
    rep = replay(GameDir(dst))
    assert rep.ok and rep.turns and rep.recons and rep.result_ok
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == ("core-0.7.0" if "m9" in name else "core-0.6.0")
