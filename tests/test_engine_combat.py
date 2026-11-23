import pytest

from dah.engine import build_ruleset, estimate
from dah.engine.combat import Force, fight, pick_losses, strength
from dah.engine.rng import stream

AS, AR, AT = "core:assault", "core:armor", "core:artillery"


class FixedRng:
    def uniform(self, a, b):
        return 1.0


@pytest.fixture(scope="module")
def rs():
    return build_ruleset(None, "slice3")


def test_rng_streams_are_independent_and_stable():
    a = stream(7, 3, "combat", "P01").random()
    assert a == stream(7, 3, "combat", "P01").random()
    assert a != stream(7, 3, "combat", "P02").random()
    assert a != stream(7, 4, "combat", "P01").random()
    assert a != stream(8, 3, "combat", "P01").random()


def test_counters(rs):
    armor = Force("a", {AR: 1})
    assert strength(rs, armor, {AS: 2}, defending=False) == pytest.approx(5 * 1.5)
    assert strength(rs, armor, {AT: 2}, defending=False) == pytest.approx(5)
    assert strength(rs, Force("a", {AT: 1}), {AR: 1}, defending=False) == pytest.approx(4 * 1.5)
    assert strength(rs, Force("a", {AS: 1}), {AT: 1}, defending=True) == pytest.approx(3 * 1.5)
    # counter cycle decides an otherwise even fight
    res = fight(rs, Force("a", {AR: 2}), Force("b", {AS: 3}), FixedRng(), b_defending=False, factions={})
    assert res.a_won and res.strength_a == pytest.approx(15) and res.strength_b == pytest.approx(9)


def test_hasty_penalty(rs):
    calm = strength(rs, Force("a", {AS: 4}), {}, defending=False)
    hasty = strength(rs, Force("a", {AS: 4}, hasty={AS: 4}), {}, defending=False)
    mixed = strength(rs, Force("a", {AS: 4}, hasty={AS: 2}), {}, defending=False)
    assert hasty == pytest.approx(calm * 0.85)
    assert mixed == pytest.approx(calm * (1 - 0.15 / 2))
    # hasty never applies to defence
    assert strength(rs, Force("d", {AS: 4}, hasty={AS: 4}), {}, defending=True) == pytest.approx(12)


def test_fortify_and_terrain(rs):
    base = strength(rs, Force("d", {AS: 2}), {}, defending=True)
    assert strength(rs, Force("d", {AS: 2}), {}, defending=True, defense_mul=1.5) == pytest.approx(base * 1.5)
    assert strength(rs, Force("d", {AS: 2}), {}, defending=True, defense_mul=1.5,
                    fortified=True) == pytest.approx(base * 1.5 * 1.25)
    assert strength(rs, Force("a", {AS: 2}), {}, defending=False, fortified=True) == pytest.approx(6)


def test_losses_formula(rs):
    a, b = Force("a", {AS: 10}), Force("b", {AS: 10})
    res = fight(rs, a, b, FixedRng(), b_defending=True, factions={}, defense_mul=1.25)
    # S_a = 30, S_b = 37.5: attacker loses min(1, 0.5 * 37.5 / 30) = 62.5% -> 6; defender 0.3 * 30/37.5 = 24% -> 2
    assert not res.a_won
    assert res.losses_a == {AS: 6} and res.losses_b == {AS: 2}
    assert a.units == {AS: 4} and b.units == {AS: 8}


def test_losses_cheapest_first(rs):
    assert pick_losses(rs, {AR: 2, AS: 1, AT: 1}, 3) == {AS: 1, AT: 1, AR: 1}


def test_estimate_deterministic_and_sensible(rs):
    e1 = estimate(rs, {"assault": 6}, {"assault": 3}, "forge")
    assert e1 == estimate(rs, {"core:assault": 6}, {"assault": 3}, "core:forge")
    assert set(e1) == {"win_prob", "attacker_losses_avg", "defender_losses_avg"}
    assert e1["win_prob"] == 1.0
    assert estimate(rs, {"assault": 3}, {"assault": 3}, "capital")["win_prob"] == 0.0
    even = estimate(rs, {"assault": 4}, {"assault": 4}, "forge")
    assert 0.2 < even["win_prob"] < 0.8
    assert estimate(rs, {"assault": 4}, {"assault": 4}, "forge", hasty=True)["win_prob"] < even["win_prob"]
    assert estimate(rs, {"assault": 4}, {"assault": 4}, "forge", fortified=True)["win_prob"] < even["win_prob"]
    assert estimate(rs, {"assault": 1}, {}, "forge")["win_prob"] == 1.0
    with pytest.raises(KeyError):
        estimate(rs, {"dragon": 1}, {}, "forge")


def test_losses_shared_between_hasty_and_calm_units():
    force = Force("a", {AS: 6}, hasty={AS: 3})
    force.remove({AS: 2})
    assert force.units == {AS: 4} and force.hasty == {AS: 2}


def test_losses_shared_between_origins():
    force = Force("a", {AS: 3}, origin="P04")
    force.merge(Force("a", {AS: 3, AR: 1}, origin="P10"))
    force.remove({AS: 4, AR: 1})
    assert force.units == {AS: 2} and force.parts == {"P04": {AS: 1}, "P10": {AS: 1}}
