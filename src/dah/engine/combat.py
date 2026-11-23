"""Battle formula (shared by resolution and estimate)."""

from __future__ import annotations

import math
import random
from dataclasses import InitVar, dataclass, field
from typing import Any, Mapping, Sequence

from .content import Ruleset
from .rng import stream

ESTIMATE_RUNS = 200
ESTIMATE_SEED = "estimate"


@dataclass
class Force:
    owner: str | None                      # seat, or None for neutral
    units: dict[str, int]
    hasty: dict[str, int] = field(default_factory=dict)   # how many of `units` came from P2 orders
    origin: InitVar[str | None] = None     # attacker's source province; None for a defender
    parts: dict[str, dict[str, int]] = field(init=False, default_factory=dict)  # origin -> its share of `units`
    breaks: set[str] = field(default_factory=set)  # pact partners this force breaks with (its "break" moves)

    def __post_init__(self, origin: str | None) -> None:
        if origin is not None:
            self.parts = {origin: dict(self.units)}

    def total(self) -> int:
        return sum(self.units.values())

    def remove(self, losses: Mapping[str, int]) -> None:
        """Apply losses; hasty/calm units and origin parts lose in proportion to their share."""
        for unit, n in losses.items():
            before = self.units.get(unit, 0)
            hasty = self.hasty.get(unit, 0)
            _put(self.units, unit, before - n)
            if hasty:
                _put(self.hasty, unit, hasty - share_losses([hasty, before - hasty], n)[0])
            origins = sorted(o for o, part in self.parts.items() if part.get(unit))
            for o, k in zip(origins, share_losses([self.parts[o][unit] for o in origins], n)):
                _put(self.parts[o], unit, self.parts[o][unit] - k)
        self.parts = {o: part for o, part in self.parts.items() if part}

    def merge(self, other: Force) -> None:
        self.breaks |= other.breaks
        for unit, n in other.units.items():
            self.units[unit] = self.units.get(unit, 0) + n
        for unit, n in other.hasty.items():
            self.hasty[unit] = self.hasty.get(unit, 0) + n
        for origin, part in other.parts.items():
            mine = self.parts.setdefault(origin, {})
            for unit, n in part.items():
                mine[unit] = mine.get(unit, 0) + n

    def clear(self) -> None:
        self.units.clear()
        self.hasty.clear()
        self.parts.clear()


def _put(counts: dict[str, int], unit: str, n: int) -> None:
    if n > 0:
        counts[unit] = n
    else:
        counts.pop(unit, None)


def share_losses(pools: Sequence[int], k: int) -> list[int]:
    """Split k losses over pools in proportion to their sizes (largest remainder, ties to the earlier pool)."""
    total = sum(pools)
    if total <= 0 or k <= 0:
        return [0] * len(pools)
    k = min(k, total)
    base = [p * k // total for p in pools]
    order = sorted(range(len(pools)), key=lambda i: (-(pools[i] * k % total), i))
    for i in order[:k - sum(base)]:
        base[i] += 1
    return base


@dataclass(frozen=True)
class FightResult:
    a_won: bool
    strength_a: float
    strength_b: float
    losses_a: dict[str, int]
    losses_b: dict[str, int]


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _ability(ruleset: Ruleset, unit: str, name: str, key: Any) -> bool:
    return bool(ruleset.stat(f"unit.{unit}.ability.{name}.on", key))


def attack_shares(ruleset: Ruleset, units: Mapping[str, int], key: Any) -> tuple[float, float]:
    """(share of the attack power from `ignore_fortify` units, share from `siege` units) of an attacking stack."""
    total = ignore = siege = 0.0
    for unit, n in units.items():
        if n <= 0:
            continue
        power = ruleset.stat(f"unit.{unit}.attack", key) * n
        total += power
        if _ability(ruleset, unit, "ignore_fortify", key):
            ignore += power
        if _ability(ruleset, unit, "siege", key):
            siege += power
    if total <= 0:
        return 0.0, 0.0
    return ignore / total, siege / total


def effective_defense_mul(ruleset: Ruleset, defense_mul: float, attacker_units: Mapping[str, int],
                          attacker_key: Any) -> float:
    """Province defense multiplier against a stack: each `siege` unit's share of the attack power meets at most its
    `siege.cap`, the rest the full multiplier (TECH_AND_SETUP.md 1.2)."""
    if defense_mul <= 1.0:
        return defense_mul
    total = capped = 0.0
    for unit, n in sorted(attacker_units.items()):
        if n <= 0:
            continue
        power = ruleset.stat(f"unit.{unit}.attack", attacker_key) * n
        total += power
        if _ability(ruleset, unit, "siege", attacker_key):
            cap = ruleset.stat(f"unit.{unit}.ability.siege.cap", attacker_key)
            capped += power * min(defense_mul, cap)
        else:
            capped += power * defense_mul
    if total <= 0 or capped == total * defense_mul:
        return defense_mul
    return capped / total


def strength(ruleset: Ruleset, force: Force, enemy_units: Mapping[str, int], *, defending: bool,
             faction: Any = None, defense_mul: float = 1.0, fortified: bool = False,
             enemy_faction: Any = None) -> float:
    """Deterministic part of the formula (without the random factor). `faction`/`enemy_faction` are stat keys
    (faction id or techs.Profile). A defender owned by a seat counts `garrison` (+defense in its own province);
    the attacker's `siege` and `ignore_fortify` units weaken the province multiplier and the fortify bonus."""
    stat = "defense" if defending else "attack"
    counter_mul = ruleset.stat("combat.counter_mul", faction)
    hasty_mul = 1.0 - ruleset.stat("p2.hasty_penalty", faction)
    present = {u for u, n in enemy_units.items() if n > 0}
    total = 0.0
    for unit, n in sorted(force.units.items()):
        value = ruleset.stat(f"unit.{unit}.{stat}", faction)
        if defending and force.owner is not None and _ability(ruleset, unit, "garrison", faction):
            value += ruleset.stat(f"unit.{unit}.ability.garrison.defense", faction)
        if present.intersection(ruleset.units[unit].counters):
            value *= counter_mul
        hasty_n = 0 if defending else force.hasty.get(unit, 0)
        total += value * ((n - hasty_n) + hasty_n * hasty_mul)
    if defending:
        total *= effective_defense_mul(ruleset, defense_mul, enemy_units, enemy_faction)
        if fortified:
            ignore, _ = attack_shares(ruleset, enemy_units, enemy_faction)
            bonus = ruleset.stat("combat.fortify_bonus", faction)
            total *= 1.0 + (bonus if ignore == 0 else bonus * (1.0 - ignore))
    return total


def unit_price(ruleset: Ruleset, unit: str) -> int:
    return sum(ruleset.unit_cost(unit).values())


def pick_losses(ruleset: Ruleset, units: Mapping[str, int], n: int) -> dict[str, int]:
    """Take n units, cheapest first (ties by id)."""
    losses: dict[str, int] = {}
    for unit in sorted(units, key=lambda u: (unit_price(ruleset, u), u)):
        if n <= 0:
            break
        take = min(units[unit], n)
        if take:
            losses[unit] = take
            n -= take
    return losses


def fight(ruleset: Ruleset, a: Force, b: Force, rng: random.Random, *, b_defending: bool,
          factions: Mapping[str | None, str | None], defense_mul: float = 1.0, fortified: bool = False) -> FightResult:
    """Resolve one battle; `a` always attacks. Applies losses to both forces in place."""
    sa = strength(ruleset, a, b.units, defending=False, faction=factions.get(a.owner))
    sb = strength(ruleset, b, a.units, defending=b_defending, faction=factions.get(b.owner),
                  defense_mul=defense_mul if b_defending else 1.0, fortified=fortified and b_defending,
                  enemy_faction=factions.get(a.owner))
    sa *= rng.uniform(0.9, 1.1)
    sb *= rng.uniform(0.9, 1.1)
    a_won = sa > sb
    s_w, s_l = (sa, sb) if a_won else (sb, sa)
    winner, loser = (a, b) if a_won else (b, a)
    loser_frac = 1.0 if s_l <= 0 else min(1.0, 0.5 * s_w / s_l)
    winner_frac = 0.0 if s_w <= 0 else 0.3 * s_l / s_w
    loser_losses = pick_losses(ruleset, loser.units, round_half_up(loser_frac * loser.total()))
    winner_losses = pick_losses(ruleset, winner.units, round_half_up(winner_frac * winner.total()))
    loser.remove(loser_losses)
    winner.remove(winner_losses)
    la, lb = (winner_losses, loser_losses) if a_won else (loser_losses, winner_losses)
    return FightResult(a_won, sa, sb, la, lb)


def estimate(ruleset: Ruleset, attacker_units: Mapping[str, int], defender_units: Mapping[str, int],
             province_type: str, fortified: bool = False, hasty: bool = False,
             runs: int = ESTIMATE_RUNS, *, attacker_key: Any = None, defender_key: Any = None,
             defense_mul: float | None = None) -> dict[str, Any]:
    """Monte-Carlo forecast of one attack, deterministic (fixed seed). Unit/type ids may be short.

    `attacker_key`/`defender_key`: stat keys (faction id or Profile) of the sides, None = base stats;
    `defense_mul`: the province's multiplier when known (type x province.defense_mul), else the type's.
    Returns {win_prob, attacker_losses_avg, defender_losses_avg}. Raises KeyError on unknown ids.
    """
    att = _resolve_units(ruleset, attacker_units)
    dfn = _resolve_units(ruleset, defender_units)
    ptype = ruleset.resolve("province_type", province_type)
    if ptype is None:
        raise KeyError(f"unknown province type {province_type!r}")
    if defense_mul is None:
        defense_mul = ruleset.province_types[ptype].defense_mul
    rng = stream(ESTIMATE_SEED, 0, "estimate", "")
    keys = {"a": attacker_key, "d": defender_key}
    wins = 0
    lost_a = lost_d = 0
    for _ in range(runs):
        a = Force("a", dict(att), dict(att) if hasty else {}, origin="x")
        d = Force("d", dict(dfn))
        if not d.total():
            wins += 1
            continue
        res = fight(ruleset, a, d, rng, b_defending=True, factions=keys, defense_mul=defense_mul, fortified=fortified)
        wins += res.a_won
        lost_a += sum(res.losses_a.values())
        lost_d += sum(res.losses_b.values())
    return {"win_prob": round(wins / runs, 3), "attacker_losses_avg": round(lost_a / runs, 2),
            "defender_losses_avg": round(lost_d / runs, 2)}


def _resolve_units(ruleset: Ruleset, units: Mapping[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ref, n in units.items():
        full = ruleset.resolve("unit", ref)
        if full is None:
            raise KeyError(f"unknown unit {ref!r}")
        if int(n) > 0:
            out[full] = out.get(full, 0) + int(n)
    return out
