"""Core 0.8.0, espionage v2 part A (docs/SPY_V2.md, decision 30): the frozen core 0.7.1, the mission catalogue
(observe D0 exact with the turn's recruits and fortify, ledger with the army in all, secret once a game, read_orders
at S1 once per rival with the pact breaks first), R for the spy's own Shades at the same place, spy.active_base 2,
the `intel` kinds of missions and their crediting (G1: none against a pact partner; G2: a strike scores only by its
effect, at the end of the resolution), estimate_mission (R, G3) and estimate(defender="r3")."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dah.engine import build_ruleset, issue_order_id, new_game_state, orders_hash, resolve, submit_orders
from dah.engine import diplomacy as dip
from dah.engine import espionage as esp
from dah.engine import scoring as sc
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR
from dah.engine.model import ACTIONS, GameState, Order, SeatOrders
from dah.engine.rules import rules_text, rules_topic
from dah.game import Game, GameError, make_config, replay
from dah.store import GameDir, init_game_dir

CORE = DEFAULT_PACKS_DIR / "core"
CORE071 = LEGACY_PACKS_DIR / "core-0.7.1"
CORE080 = LEGACY_PACKS_DIR / "core-0.8.0"     # core 0.8.1 froze 0.8.0: the 0.7.1 -> 0.8.0 step is checked on the copy
# ruleset hashes of the frozen core 0.7.1, as core 0.7.1 itself built them at 718af36
CORE071_HASHES = {"slice3": "8053b955c6d2", "slice2": "5cde72f644e3", "quad4": "01110b16a0fa",
                  "blitz_test": "4b7b67be2e18", "blitz_spy": "60d008e2efc6", "blitz_score": "96a713f00c26",
                  "blitz_finds": "731ab9e3ca12"}
RUNS = Path(r"C:\Users\kuzmi\DreamAboutHumanity-runs-2026-11-23")
SPY_SCENARIOS = ("slice3", "slice2", "quad4", "blitz_spy", "blitz_score", "blitz_finds")
SEATS3 = [{"seat": s, "nick": f"Ник{s}"} for s in "ABC"]
AS, MIL = "core:assault", "core:militia"
# blitz_score with SEATS3: A holds P01-P04, B P09-P12, C P05-P08 (capitals P01, P09, P05)


def _state(packs: list[str] | None = None, sid: str = "blitz_score"):
    rs = build_ruleset(packs, sid)
    return rs, new_game_state(rs, SEATS3, 4)


def _book(st: GameState, rs, seat: str, items: list[dict]) -> SeatOrders:
    st.phase = "actions"
    res = submit_orders(st, rs, seat, SeatOrders(), items, "actions", id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def _shade(st: GameState, seat: str) -> str:
    player = st.players[seat]
    player.spy_seq["shade"] = player.spy_seq.get("shade", 0) + 1
    sid = f"s{player.spy_seq['shade']}"
    player.shades.append({"id": sid, "xp": 0, "status": "idle", "hired_turn": 0, "mission": None})
    return sid


def _start(st: GameState, seat: str, mission: str, target: str, sid: str = "s1", rounds: int = 1) -> None:
    """A mission set on the Shade directly (no order checks: e.g. a sabotage without S2)."""
    shade = esp.shade_of(st, seat, sid)
    esp.start_mission(shade, Order(id="x", kind="spy", shade=sid, mission=mission, target=target, rounds=rounds),
                      st.turn, ACTIONS)


@pytest.fixture
def sure(monkeypatch):
    """Every round succeeds and nobody is caught."""
    monkeypatch.setattr(esp, "chances", lambda rs, key, **kw: (1.0, 0.0))


def _pact(st: GameState, a: str, b: str) -> None:
    st.phase = "negotiation_1"
    prop = dip.make_proposal(st, a, b, 3, "negotiation_1")
    dip.apply_op(st, prop)
    pid = st.pacts[-1]["id"]
    dip.apply_op(st, dip.make_response(st, b, pid, "accept", "negotiation_1"))
    assert dip.pact_between(st, a, b, st.turn)


def _rounds(*items):
    return [{"seat": s, "mission": m, "target": t, "victim": v, "success": True} for s, m, t, v in items]


# ---------------------------------------------------------------- freeze and the pack

def test_core_071_is_frozen_exactly_with_its_hashes():
    assert "version: 0.8.1" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    head = (CORE071 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.7.1" in head and "Frozen copy of core 0.7.1" in head and "Do not edit" in head
    for sid, prefix in CORE071_HASHES.items():
        old = build_ruleset([str(CORE071)], sid)
        assert old.hash.startswith(prefix), (sid, old.hash[:12])
        assert old.packs == {"core": "0.7.1"} and all(m.intel == "each_turn" for m in old.missions.values())


def test_only_espionage_scenarios_and_the_version_moved_from_the_frozen_copy():
    # step B (docs/SPY_V2.md 4.6): the Ward lines of watch_drill
    # trade step B (docs/TRADE.md 6.3): conqueror's `via: capture`
    changed = {"pack.yaml", "scenarios/scenarios.yaml", "espionage/espionage.yaml", "techs/mirrors.yaml",
               "objectives/objectives.yaml"}
    for path in CORE071.rglob("*"):
        rel = path.relative_to(CORE071).as_posix()
        if path.is_file() and rel not in changed:
            assert path.read_bytes().replace(b"\r\n", b"\n") == (CORE / rel).read_bytes().replace(b"\r\n", b"\n"), rel


@pytest.mark.parametrize("sid", SPY_SCENARIOS)
def test_spy_scenarios_change_only_missions_and_spy_stats(sid):
    new, old = build_ruleset([str(CORE080)], sid, {"trade": False}), build_ruleset([str(CORE071)], sid)     # trade: own step
    a, b = old.to_dict(), new.to_dict()
    assert (a.pop("packs"), b.pop("packs")) == ({"core": "0.7.1"}, {"core": "0.8.0"})
    for d in (b["stats"], *b["faction_stats"].values()):
        assert (d.pop("spy.active_base"), d.pop("spy.intel.width_min"), d.pop("spy.detect.repeat_on")) == (2, 1, 1)
        assert (d.pop("spy.detect.base"), d.pop("spy.detect.xp")) == (0.06, 0.03)     # balance core 0.8.0
        assert d.pop("spy.mission.core:observe.difficulty") == 0 and d.pop("spy.mission.core:read_orders.difficulty") == 2
    for d in (a["stats"], *a["faction_stats"].values()):
        d.pop("spy.active_base", None)
        d.pop("spy.mission.core:observe.difficulty")
        d.pop("spy.mission.core:read_orders.difficulty")
    a.pop("missions"), b.pop("missions")
    drill = b["techs"]["core:watch_drill"]      # step B: the Ward lines (docs/SPY_V2.md 4.6)
    ward = [e for e in drill["effects"] if e["target"].startswith("spy.ward.")]
    assert ward == [{"target": "spy.ward.bonus", "op": "add", "value": 1, "type": "modify"},
                    {"target": "spy.ward.cost.core:alloy", "op": "add", "value": -2, "type": "modify"}]
    drill["effects"] = tuple(e for e in drill["effects"] if e not in ward)
    a.pop("overrides", None), b.pop("overrides", None)
    a["scenario"].pop("overrides", None), b["scenario"].pop("overrides", None)
    conqueror = b.get("objectives", {}).get("core:conqueror")
    if conqueror is not None:       # trade step B (docs/TRADE.md 6.3): only land taken in battle counts
        assert conqueror["params"].pop("via") == "capture"
    assert a == b
    m = new.missions
    kinds = {"observe": "no", "ledger": "no", "secret": "once", "read_orders": "once_per_target", "sabotage": "effect"}
    assert {k.split(":")[1]: v.intel for k, v in m.items()} == {k: v for k, v in kinds.items()
                                                                 if f"core:{k}" in m}
    assert m["core:observe"].params == {"exact": True, "planned": True} and m["core:ledger"].params == {"army_total": True}
    assert m["core:read_orders"].whisper == 1 and m["core:sabotage"].whisper == 2


def test_espionage_off_leaves_no_trace_of_the_new_fields():
    new, old = build_ruleset(None, "blitz_test"), build_ruleset([str(CORE071)], "blitz_test")
    a, b = old.to_dict(), new.to_dict()
    a.pop("packs"), b.pop("packs")
    assert a == b and "missions" not in b


def test_the_validator_checks_the_intel_kind(tmp_path):
    from dah.engine.content import validate_packs
    dst = tmp_path / "core"
    shutil.copytree(CORE, dst)
    path = dst / "espionage" / "espionage.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("intel: once\n", "intel: effect\n", 1), encoding="utf-8")
    errors, _ = validate_packs([str(dst)])
    assert any("only for a loud mission" in e.message for e in errors)
    path.write_text(text.replace("intel: once\n", "intel: sometimes\n", 1), encoding="utf-8")
    assert validate_packs([str(dst)])[0]


# ---------------------------------------------------------------- 14.1-14.2 crediting

def test_no_once_and_once_per_target():
    rs, st = _state()
    got = sc.credit_intel(st, rs, _rounds(("A", "core:observe", "P09", "B"), ("A", "core:ledger", "B", "B")),
                          rs.missions)
    assert got == []                                                         # observe and ledger: information only
    got = sc.credit_intel(st, rs, _rounds(("A", "core:secret", "B", "B")), rs.missions)
    assert [(g["target"], g["kind"]) for g in got] == [("B", "once")]
    assert st.players["A"].tally["intel_kinds"] == ["core:secret"]
    denied: list = []
    assert sc.credit_intel(st, rs, _rounds(("A", "core:secret", "C", "C")), rs.missions, denied) == []
    assert denied == [{"seat": "A", "target": "C", "mission": "core:secret", "kind": "once", "reason": "once"}]
    assert [g["kind"] for g in sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "B", "B")),
                                               rs.missions)] == ["once_per_target"]
    assert sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "B", "B")), rs.missions) == []
    assert st.players["A"].tally["intel_targets"] == ["core:read_orders:B"]
    assert sc.intel(st, rs, "A") == 2
    sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "C", "C")), rs.missions)
    assert sc.intel(st, rs, "A") == 3
    denied = []
    sc.credit_intel(st, rs, _rounds(("B", "core:read_orders", "A", "A")), rs.missions, denied)
    assert sc.intel(st, rs, "B") == 1 and not denied


def test_each_turn_and_the_tally_of_older_packs_stay_as_in_070():
    rs, st = _state([str(CORE071)])
    sc.credit_intel(st, rs, _rounds(("A", "core:observe", "P09", "B")), rs.missions)
    sc.credit_intel(st, rs, _rounds(("A", "core:observe", "P09", "B")), rs.missions)
    assert sc.intel(st, rs, "A") == 2 and set(st.players["A"].tally) == {"intel"}


def test_no_points_against_a_pact_partner_and_the_pair_is_not_spent():
    rs, st = _state([str(CORE080)])         # core 0.8.1 (decision 32): deep missions score (test_core081.py)
    _pact(st, "A", "B")
    denied: list = []
    assert sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "B", "B"), ("A", "core:secret", "B", "B")),
                           rs.missions, denied) == []
    assert [d["reason"] for d in denied] == ["pact", "pact"]
    assert "intel_targets" not in st.players["A"].tally and "intel_kinds" not in st.players["A"].tally
    pact = st.pacts[-1]
    st.phase = "negotiation_1"
    dip.apply_op(st, dip.make_leave(st, "A", pact["id"], "negotiation_1"))     # a leave of P1 acts at once
    assert sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "B", "B")), rs.missions)
    # the old rule knows no pacts
    old, st2 = _state([str(CORE071)])
    _pact(st2, "A", "B")
    assert sc.credit_intel(st2, old, _rounds(("A", "core:ledger", "B", "B")), old.missions)


# ---------------------------------------------------------------- G2: a strike scores by its effect

def test_strike_effects_need_a_loss_of_the_victim():
    def battle(losses, won):
        return {"kind": "battle", "province": "P10", "stage": "defense",
                "sides": [{"owner": "A", "losses": {}, "won": not won}, {"owner": "B", "losses": losses, "won": won}]}
    assert esp.strike_effects([battle({AS: 1}, True)], "P10", "B") == ["battle"]
    assert esp.strike_effects([battle({}, False)], "P10", "B") == ["battle"]
    assert esp.strike_effects([battle({}, True)], "P10", "B") == []            # one unit against it: no loss
    assert esp.strike_effects([battle({AS: 1}, True)], "P11", "B") == []
    assert esp.strike_effects([{"kind": "captured", "province": "P10", "from": "B", "to": "A"}], "P10", "B") == ["battle"]
    assert esp.strike_effects([{"kind": "build_sabotaged", "province": "P10"},
                               {"kind": "occupation_revolt", "province": "P10", "reason": "sabotage"},
                               {"kind": "occupation_revolt", "province": "P10", "reason": "no_garrison"}],
                              "P10", "B") == ["stall", "revolt"]


def _strike_and_resolve(st, rs, books_d=None, books=None):
    st, rep = esp.recon(st, books_d or {}, rs, 4)
    new, report = resolve(st, books or {}, rs, 4)
    return st, rep, new, report


def test_a_sabotage_without_an_effect_scores_nothing(sure):
    rs, st = _state()
    _start(st, "A", "core:sabotage", "P10")
    st, rep, new, report = _strike_and_resolve(st, rs)
    assert "intel_points" not in rep                                          # nothing at the recon step
    eff = [e for e in report.events if e["kind"] == "sabotage_effect"]
    assert [(e["seat"], e["effects"], e["intel_point"], e["reason"]) for e in eff] == [("A", [], False, "no_effect")]
    assert eff[0]["seats"] == ["A"] and sc.intel(new, rs, "A") == 0
    assert eff[0] not in report.by_seat["B"] and not [e for e in report.by_seat["B"] if e["kind"] == "sabotage_effect"]


def test_a_sabotage_with_a_battle_the_victim_lost_scores_at_the_end(sure):
    rs, st = _state()
    st.provinces["P10"].units = {MIL: 1}
    st.provinces["P03"].units = {AS: 8}
    _start(st, "A", "core:sabotage", "P10")
    book = _book(st, rs, "A", [{"kind": "move", "from": "P03", "to": "P10", "units": {"assault": 8}}])
    st, rep, new, report = _strike_and_resolve(st, rs, {"A": book}, {"A": book})
    points = [e for e in report.events if e["kind"] == "intel_point"]
    assert [(p["seat"], p["target"], p["intel_kind"], p["effect"]) for p in points] == [("A", "B", "effect", "battle")]
    assert sc.intel(new, rs, "A") == 1 and not [e for e in report.by_seat["B"] if e["kind"] == "intel_point"]
    eff = next(e for e in report.events if e["kind"] == "sabotage_effect")
    assert eff["effects"] == ["battle"] and eff["intel_point"] is True
    from dah.engine.view import event_text
    assert event_text(eff, rs, new, "A") == "удар Тени s1 по P10: бой с потерями — +1 очко разведки"


def test_the_recon_points_go_first_and_may_fill_the_cap(sure):
    rs, st = _state()
    st.players["A"].tally["intel"] = 2
    _shade(st, "A")
    st.provinces["P10"].units = {MIL: 1}
    st.provinces["P03"].units = {AS: 8}
    _start(st, "A", "core:sabotage", "P10", "s1")
    _start(st, "A", "core:read_orders", "C", "s2")
    book = _book(st, rs, "A", [{"kind": "move", "from": "P03", "to": "P10", "units": {"assault": 8}}])
    st, rep, new, report = _strike_and_resolve(st, rs, {"A": book}, {"A": book})
    assert [p["mission"] for p in rep["intel_points"]] == ["core:read_orders"]
    eff = next(e for e in report.events if e["kind"] == "sabotage_effect")
    assert eff["effects"] == ["battle"] and eff["intel_point"] is False and eff["reason"] == "cap"
    assert sc.intel(new, rs, "A") == 3


def test_two_strikes_on_one_province_one_point_to_the_first_uid(sure):
    rs, st = _state()
    st.provinces["P10"].units = {MIL: 1}
    st.provinces["P03"].units = {AS: 8}
    _start(st, "A", "core:sabotage", "P10")
    _start(st, "C", "core:sabotage", "P10")
    book = _book(st, rs, "A", [{"kind": "move", "from": "P03", "to": "P10", "units": {"assault": 8}}])
    st, rep, new, report = _strike_and_resolve(st, rs, {"A": book}, {"A": book})
    eff = {e["seat"]: e for e in report.events if e["kind"] == "sabotage_effect"}
    assert eff["A"]["intel_point"] is True and eff["C"]["intel_point"] is False and eff["C"]["reason"] == "taken"
    assert (sc.intel(new, rs, "A"), sc.intel(new, rs, "C")) == (1, 0)


def test_a_strike_on_a_pact_partner_scores_nothing(sure):
    rs, st = _state()
    _pact(st, "C", "B")
    st.provinces["P10"].units = {MIL: 1}
    st.provinces["P03"].units = {AS: 8}
    _start(st, "C", "core:sabotage", "P10")
    book = _book(st, rs, "A", [{"kind": "move", "from": "P03", "to": "P10", "units": {"assault": 8}}])
    st, rep, new, report = _strike_and_resolve(st, rs, {"A": book}, {"A": book})
    eff = next(e for e in report.events if e["kind"] == "sabotage_effect")
    assert eff["effects"] == ["battle"] and eff["reason"] == "pact" and sc.intel(new, rs, "C") == 0


def test_an_older_sabotage_scores_at_the_recon_step(sure):
    rs, st = _state([str(CORE071)])
    _start(st, "A", "core:sabotage", "P10")
    st, rep = esp.recon(st, {}, rs, 4)
    assert [p["mission"] for p in rep["intel_points"]] == ["core:sabotage"] and "kind" not in rep["intel_points"][0]
    mark = esp.cq.strike_marks(st.provinces["P10"], st.turn)[0]
    assert "uid" not in mark and "pact" not in mark
    new, report = resolve(st, {}, rs, 4)
    assert not [e for e in report.events if e["kind"] in ("sabotage_effect", "intel_point")]


# ---------------------------------------------------------------- 14.4-14.7 the reports

def test_observe_is_exact_with_the_turns_recruits_and_fortify_only(sure):
    rs, st = _state()
    st.provinces["P09"].units = {AS: 3, MIL: 2}
    st.players["B"].resources["core:alloy"] = 200
    st.players["B"].resources["core:provisions"] = 200
    b = _book(st, rs, "B", [{"kind": "recruit", "province": "P09", "unit": "assault", "count": 2},
                            {"kind": "fortify", "province": "P09"},
                            {"kind": "move", "from": "P09", "to": "P12", "units": {"militia": 2}}])
    _start(st, "A", "core:observe", "P09")
    st, rep = esp.recon(st, {"B": b}, rs, 4)
    report = rep["reports"]["A"][0]
    assert report["facts"]["units"] == {AS: 3, MIL: 2} == report["truth"]["units"]
    assert report["facts"]["planned"] == {"recruit": {AS: 2}, "fortify": True} == report["truth"]["planned"]
    assert "move" not in str(report["facts"]) and "P12" not in str(report["facts"])
    text = esp.report_text(report, rs, st, "A")
    assert "без подходов и уходов" in text and "Найм здесь: штурм×2; Укрепление: да" in text
    assert "штурм×3" in text
    other = SeatOrders([Order(id="o1", kind="recruit", province="P10", unit=AS, count=3)])
    assert esp.planned_here(other, "P09") == {"recruit": {}, "fortify": False}
    assert esp.planned_text(rs, esp.planned_here(other, "P09")) == "найма и укрепления нет"
    # an older pack: intervals, no block
    old, st2 = _state([str(CORE071)])
    st2.provinces["P10"].units = {AS: 3, MIL: 2}
    _start(st2, "A", "core:observe", "P10")
    st2, rep2 = esp.recon(st2, {}, old, 4)
    facts = rep2["reports"]["A"][0]["facts"]
    assert all(isinstance(v, list) for v in facts["units"].values()) and "planned" not in facts


def test_width_min_narrows_small_intervals_and_its_default_keeps_them():
    lo, hi = esp.honest_range(4, "A", "B", "stock", "x", 2, 0.3, 1)
    assert hi - lo == 1 and lo <= 2 <= hi
    assert esp.honest_range(4, "A", "B", "stock", "x", 2, 0.3) == esp.honest_range(4, "A", "B", "stock", "x", 2, 0.3, 2)
    lo2, hi2 = esp.honest_range(4, "A", "B", "stock", "x", 2, 0.3)
    assert hi2 - lo2 == 2


def test_ledger_shows_the_army_in_all(sure):
    rs, st = _state()
    st.provinces["P10"].units = {AS: 4}
    _start(st, "A", "core:ledger", "B")
    st, rep = esp.recon(st, {}, rs, 4)
    report = rep["reports"]["A"][0]
    n = report["truth"]["army_total"]
    lo, hi = report["facts"]["army_total"]
    assert n == sum(p.unit_total() for p in st.owned("B")) and lo <= n <= hi
    assert esp.honest_range(4, "A", "B", "army", "total", n, rs.stat("spy.intel.width"), 1) == [lo, hi]
    assert f"армия всего: {lo}–{hi}" in esp.report_text(report, rs, st, "A")


def test_read_orders_is_open_at_s1_in_080_and_locked_in_071():
    for packs, problem in ((None, None), ([str(CORE071)], "MISSION_LOCKED")):
        rs, st = _state(packs)
        st.phase = "actions"
        o = Order(id="o1", kind="spy", shade="s1", mission="core:read_orders", target="B", rounds=1)
        got = esp.order_problem(st, rs, "A", o, [], esp.stat_key(st, rs, "A"))
        assert (got[0] if got else None) == problem


def test_read_orders_names_the_pact_breaks_first_then_the_moves_on_the_reader():
    rs, st = _state()
    facts = {"seat": "B", "command_points": 3, "new_attack_cp": 2, "free_at_borders": {}, "entries": [
        {"kind": "move", "from": "P11", "to": "P06", "units": {AS: 3}, "pact": "break"},
        {"kind": "move", "from": "P10", "to": "P03", "units": {AS: 2}, "pact": "break", "exposed": True},
        {"kind": "move", "from": "P12", "to": "P04", "units": {MIL: 1}, "pact": "honor"},
        {"kind": "recruit", "province": "P10", "unit": AS, "count": 1}]}
    lines = esp._orders_body(rs, st, facts, "A", False)
    assert lines[0].startswith("НАРУШЕНИЕ ПАКТА: ")
    assert "удар по союзнику НикC без выхода (−3): P11→P06 штурм×3" in lines[0]
    assert "удар по тебе без выхода (−3): P10→P03 штурм×2 [ЛОЖНАЯ ЗАПИСЬ]" in lines[0]
    assert lines[1] == "НА ТЕБЯ: P12→P04 опол×1"
    assert lines[2].startswith("найм P10")
    quiet = esp._orders_body(rs, st, {**facts, "entries": facts["entries"][3:]}, "A", False)
    assert quiet[0] == "ударов по тебе нет"


# ---------------------------------------------------------------- 14.8 R, 14.12 two Shades at S1

def test_two_shades_at_s1_and_r_counts_only_own_shades_at_the_same_place():
    rs, st = _state()
    _shade(st, "A")
    book = _book(st, rs, "A", [{"kind": "spy", "shade": "s1", "mission": "observe", "target": "P09", "rounds": 1},
                               {"kind": "spy", "shade": "s2", "mission": "read_orders", "target": "НикB",
                                "rounds": 1}])
    st.players["A"].shades.append({"id": "s3", "xp": 0, "status": "idle", "hired_turn": 0, "mission": None})
    third = Order(id="o9", kind="spy", shade="s3", mission="core:observe", target="P10", rounds=1)
    st.phase = "actions"
    assert esp.order_problem(st, rs, "A", third, list(book.orders), esp.stat_key(st, rs, "A"))[0] == \
        "TOO_MANY_MISSIONS"
    st.players["A"].shades.pop()
    est = esp.estimate_mission(st, rs, "A", "observe", "P09", 1, shade="s1", book=book)
    assert est["R"] == 1 and "R1" in est["text"]
    assert esp.estimate_mission(st, rs, "A", "observe", "P09", 1, shade="s1")["R"] == 0
    c_book = _book(st, rs, "C", [{"kind": "spy", "shade": "s1", "mission": "observe", "target": "P09", "rounds": 1}])
    new, rep = esp.recon(st, {"A": book, "C": c_book}, rs, 4)
    by = {(r["seat"], r["shade"]): r for r in rep["rounds"]}
    assert by[("A", "s1")]["R"] == 1 and by[("A", "s2")]["R"] == 1 and by[("C", "s1")]["R"] == 0
    assert by[("A", "s1")]["p_det"] == est["per_round"][0]["detect"]
    assert by[("A", "s1")]["p_det"] == pytest.approx(by[("C", "s1")]["p_det"] + 0.08)
    # repeat_on 0 (core 0.7.1): R is 0 whatever stands at the place
    old, st2 = _state([str(CORE071)])
    _shade(st2, "A")
    _start(st2, "A", "core:observe", "P09", "s1")
    _start(st2, "A", "core:ledger", "B", "s2")
    _, rep2 = esp.recon(st2, {}, old, 4)
    assert [r["R"] for r in rep2["rounds"]] == [0, 0]


def test_a_recalled_or_void_mission_does_not_count_in_r():
    rs, st = _state()
    _shade(st, "A")
    _start(st, "A", "core:observe", "P09", "s1")
    _start(st, "A", "core:observe", "P02", "s2")             # its own province: void
    _, rep = esp.recon(st, {}, rs, 4)
    assert next(r for r in rep["rounds"] if r["shade"] == "s1")["R"] == 0
    rs, st = _state()
    _shade(st, "A")
    _start(st, "A", "core:observe", "P09", "s1", rounds=2)
    _start(st, "A", "core:ledger", "B", "s2", rounds=2)
    book = _book(st, rs, "A", [{"kind": "recall", "shade": "s2"}])
    _, rep = esp.recon(st, {"A": book}, rs, 4)
    assert [(r["shade"], r["R"]) for r in rep["rounds"]] == [("s1", 0)]


def test_the_chances_of_the_table_33():
    rs, st = _state()
    key = esp.stat_key(st, rs, "A")
    # balance core 0.8.0 (docs/SPY_V2.md «Баланс core 0.8.0»): the catch base is 6%, one point over the table
    assert esp.chances(rs, key, S=1, C=1, D=0, V=0, K=0) == (0.65, 0.06)
    assert esp.chances(rs, key, S=1, C=1, D=0, V=0, K=0, R=1) == (0.65, 0.14)
    assert esp.chances(rs, key, S=1, C=2, D=2, V=0, K=0) == (0.45, 0.28)
    est = esp.estimate_mission(st, rs, "A", "observe", "P09", 1)
    assert (est["D"], est["per_round"][0]["success"]) == (0, 0.65)
    assert esp.estimate_mission(st, rs, "A", "read_orders", "НикB", 1)["D"] == 2


# ---------------------------------------------------------------- 14.16 G3

def test_estimate_mission_says_what_a_success_scores():
    rs, st = _state()
    obs = esp.estimate_mission(st, rs, "A", "observe", "P10", 1)
    assert obs["intel_points"] == 0 and obs["intel_reason"] == "не даёт очков (сведения)"
    near = esp.estimate_mission(st, rs, "A", "observe", "P07", 1)          # next to A's P02: in sight
    assert "P07 в твоём обзоре" in near["text"] and near["intel_points"] == 0
    assert esp.estimate_mission(st, rs, "A", "secret", "НикB", 1)["intel_points"] == 1
    st.players["A"].tally.update(intel=1, intel_kinds=["core:secret"], intel_targets=["core:read_orders:B"])
    assert esp.estimate_mission(st, rs, "A", "secret", "НикC", 1)["intel_reason"] == "очко за secret уже получено"
    ro = esp.estimate_mission(st, rs, "A", "read_orders", "НикB", 1)
    assert ro["intel_points"] == 0 and "против этого места уже получено" in ro["text"]
    assert esp.estimate_mission(st, rs, "A", "read_orders", "НикC", 1)["intel_points"] == 1
    sab = esp.estimate_mission(st, rs, "A", "sabotage", "P10", 1)
    assert sab["intel_points"] == 1 and "только если удар что-то сделает" in sab["text"]
    _pact(st, "A", "C")
    # core 0.8.1 (decision 32): a deep mission scores against a partner, a sabotage does not (test_core081.py)
    assert esp.estimate_mission(st, rs, "A", "read_orders", "НикC", 1)["intel_reason"] == \
        "партнёр по пакту: очко засчитают, пакт шпионаж не нарушает"
    assert esp.estimate_mission(st, rs, "A", "sabotage", "P05", 1)["intel_reason"] == "партнёр по пакту: очков не даст"


def test_estimate_mission_warns_when_the_books_scoring_entries_fill_the_cap():
    rs, st = _state()
    st.players["A"].tally["intel"] = 2
    _shade(st, "A")
    book = _book(st, rs, "A", [{"kind": "spy", "shade": "s1", "mission": "read_orders", "target": "НикC",
                                "rounds": 1}])
    sab = esp.estimate_mission(st, rs, "A", "sabotage", "P10", 1, shade="s2", book=book)
    assert sab["intel_points"] == 0 and "если останется место в пределе" in sab["text"]
    first = _book(st, rs, "A", [{"kind": "spy", "shade": "s1", "mission": "ledger", "target": "НикC", "rounds": 1}])
    st.players["A"].tally["intel"] = 0
    first.orders.append(Order(id="o8", kind="spy", shade="s1", mission="core:sabotage", target="P10", rounds=1))
    second = esp.estimate_mission(st, rs, "A", "sabotage", "P10", 1, shade="s2", book=first)
    assert second["intel_points"] == 0 and "первому удару" in second["text"]


# ---------------------------------------------------------------- 14.11 the rules

def test_the_rules_name_the_scoring_missions_and_the_new_terms():
    rs = build_ruleset(None, "quad4")
    text = rules_text(rs)
    line = next(x for x in text.splitlines() if x.startswith("ШПИОНАЖ"))
    assert "Очки (+1, ≤3): secret, read_orders, sabotage." in line and "ложные записи" not in line
    topic = rules_topic(rs, "spying")
    for words in ("очко: 1 раз за партию", "очко: 1 раз на соперника", "+ 8%·R", "Завеса шёпота", "feint\":true",
                  "без очков", "С партнёра по пакту очко дают secret и read_orders"):     # 0.8.1: decision 32
        assert words in topic, words
    assert "Очков нет против партнёра по пакту" in rules_topic(build_ruleset([str(CORE080)], "quad4"), "spying")

    assert len(topic) <= 4087 + 500
    old = build_ruleset([str(CORE071)], "quad4")
    assert "Успех задания: +1 очко (≤3)" in rules_text(old) and "R" not in rules_topic(old, "spying").split("Поимка")[1][:80]
    plain = build_ruleset(None, "blitz_spy")                                  # no intel source
    assert "Очки" not in next(x for x in rules_text(plain).splitlines() if x.startswith("ШПИОНАЖ"))


# ---------------------------------------------------------------- 14.15 estimate(defender="r3")

class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def _ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def test_estimate_by_an_observation_report(tmp_path, sure):
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "agent"} for s in "ABC"]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="e", scenario="core:blitz_spy", seats=seats, seed=4))
    game = Game.create(gdir, Clock())
    _ready_all(game)                                                          # П1 -> Д
    b_cap = game.state.players["B"].home
    a_cap = game.state.players["A"].home
    game.submit("A", [{"kind": "spy", "shade": "s1", "mission": "observe", "target": b_cap, "rounds": 1}])
    _ready_all(game)                                                          # разведсрез -> П2
    rid = game.get_intel("A", fmt="json")["intel"]["reports"][0]["id"]
    out = game.estimate({"attacker": {"assault": 6}, "defender": rid}, "A")
    assert out["report"].startswith(f"по донесению {rid}: состав конца Д хода 1 без подходов и уходов; в П2 "
                                    "возможны укрепление (1 ОК) и подход наспех")
    assert "если укрепится в П2: шанс" in out["report"] and "win_prob_if_fortified" in out
    assert out["defender"] == {u: n for u, n in game.state.provinces[b_cap].units.items() if n}
    assert out["province_type"] == game.state.provinces[b_cap].type
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": {"assault": 6}, "defender": "r99"}, "A")
    assert err.value.code == "UNKNOWN_ID"
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": {"assault": 6}, "defender": rid}, "B")       # not B's report
    assert err.value.code == "UNKNOWN_ID"
    _ready_all(game)                                                          # расчёт
    _ready_all(game)                                                          # П1 -> Д of turn 2
    assert "устарело на 1 ход" in game.estimate({"attacker": {"assault": 6}, "defender": rid}, "A")["report"]
    game.submit("A", [{"kind": "spy", "shade": "s1", "mission": "ledger", "target": "НикC", "rounds": 1}])
    _ready_all(game)
    ledger = [r["id"] for r in game.get_intel("A", fmt="json")["intel"]["reports"]]
    if ledger:
        with pytest.raises(GameError) as err:
            game.estimate({"attacker": {"assault": 6}, "defender": ledger[0]}, "A")
        assert err.value.code == "INVALID" and "только Наблюдение" in str(err.value)
    far = next(p for p in game.state.owned("C"))
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": a_cap, "defender": far.id}, "A")
    assert 'defender="r3"' in str(err.value)


# ---------------------------------------------------------------- 14.19 replay of the measured games

@pytest.mark.parametrize("name,frozen", [("games-m10/m10", "core-0.8.0"), ("games-m9/m9", "core-0.7.0"),
                                         ("games-m8/m8", "core-0.6.0"), ("games-m7/m7", None)])
def test_measured_games_still_replay(tmp_path, name, frozen):
    src = RUNS / name
    if not (src / "journal.jsonl").is_file():
        pytest.skip(f"measurement data {src} not present")
    dst = tmp_path / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("keys.json"))
    assert not (dst / "keys.json").exists()
    rep = replay(GameDir(dst))
    assert rep.ok and rep.turns and rep.result_ok
    if frozen:
        assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == frozen


@pytest.mark.parametrize("scenario,seats", [("quad4", 2), ("quad4", 3), ("quad4", 4), ("slice3", 2), ("slice3", 3),
                                            ("slice2", 2), ("blitz_score", 3), ("blitz_finds", 2), ("blitz_spy", 3)])
def test_the_rules_gate_of_spy_v2(tmp_path, scenario, seats):
    """docs/SPY_V2.md 9, 14.11: Game.rules() <= 5970 (30 below the limit) and never cut, the setup phase included."""
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="g", scenario=f"core:{scenario}", seats=specs, seed=3,
                                                  systems={"discoveries": True}, objectives_salt=9))
    game = Game.create(gdir, Clock(), start=False)
    game.start()
    while True:
        text = game.rules()
        assert len(text) <= 5970 and "сокращено" not in text, (scenario, seats, game.phase, len(text))
        if game.phase != "setup":
            break
        for seat in game.alive_seats():
            game.set_ready(seat, None)
