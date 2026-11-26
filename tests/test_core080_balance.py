"""Balance core 0.8.0 (docs/SPY_V2.md and docs/TRADE.md, «Баланс core 0.8.0»): the pack's catch base, the rush's
sabotage skipping a rival great work that stands for want of a share, and the trade thresholds the balance run set."""

from __future__ import annotations

from pathlib import Path

from dah.bots import spy
from dah.bots import trade as bt
from dah.bots.view import Province, StateView
from dah.engine import build_ruleset, default_pack_dirs

ROOT = Path(__file__).resolve().parents[1]
CORE071 = ROOT / "content" / "legacy" / "core-0.7.1"
SPY_SCENARIOS = ("slice3", "slice2", "quad4", "blitz_spy", "blitz_score", "blitz_finds", "blitz_trade")


def test_the_catch_base_and_the_experience_in_every_espionage_scenario_and_in_the_frozen_copy():
    for sid in SPY_SCENARIOS:
        rs = build_ruleset(default_pack_dirs(), sid)
        assert (rs.stat("spy.detect.base"), rs.stat("spy.detect.xp")) == (0.06, 0.03), sid
    old = build_ruleset([str(CORE071)], "quad4")
    assert (old.stat("spy.detect.base"), old.stat("spy.detect.xp")) == (0.05, 0.05)
    assert build_ruleset(default_pack_dirs(), "blitz_test").stat("spy.detect.base") == 0.05


def test_the_rules_print_the_catch_base():
    from dah.engine.rules import rules_topic
    rs = build_ruleset(default_pack_dirs(), "quad4")
    text = rules_topic(rs, "spying")
    assert "Поимка (первой): p = 6% + 5%·D" in text and "− 3%·V" in text


def prov(pid, owner, neighbors, work_left=()):
    return Province(id=pid, name=pid, type="core:industrial", owner=owner, units={}, buildings=(),
                    neighbors=tuple(neighbors), fortified=False, building_now=tuple(b for b, _ in work_left),
                    work_left=tuple(work_left))


def view(turn, left):
    provinces = {"P1": prov("P1", "A", ("P2",)), "P2": prov("P2", "B", ("P1",), (("monument", left),))}
    return StateView(turn=turn, phase="actions", me=frozenset({"A"}), resources={}, command_points=3,
                     provinces=provinces, id_prefix="core:", seat="A", turn_limit=12, players=("A", "B"))


def test_a_rival_great_work_that_stands_is_no_sabotage_site():
    memory = spy.SpyMemory()
    anyone = lambda p: True     # noqa: E731
    assert spy.rival_work_site(view(5, 2), anyone, memory).id == "P2"
    assert spy.rival_work_site(view(5, 2), anyone, memory).id == "P2"      # the same turn: still a site
    assert spy.rival_work_site(view(6, 1), anyone, memory).id == "P2"      # it moved on
    assert spy.rival_work_site(view(7, 1), anyone, memory) is None         # stands for want of a share
    assert spy.rival_work_site(view(7, 1), anyone, None).id == "P2"        # no memory: as before


def test_trade_thresholds_of_the_balance_run():
    assert (bt.SURPLUS, bt.SHORT, bt.OFFER_GIVE, bt.OFFER_WANT) == (35, 20, 14, 8)
    assert (bt.ALLOY_FLOOR, bt.REPORT_PRICES, bt.REPORT_MAX_PRICE, bt.RUSH_REPORT_MAX_PRICE) == (12, (4, 5, 6), 6, 6)
