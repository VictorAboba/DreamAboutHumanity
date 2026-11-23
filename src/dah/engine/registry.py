"""Engine registry: addressable characteristics (stats), effect types and modifier arithmetic.

The core knows only resource *roles* and stat paths; concrete units/buildings/resources come from content.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

RESOURCE_ROLES: tuple[str, ...] = ("upkeep_food", "build")


@dataclass(frozen=True)
class StatSpec:
    pattern: str          # e.g. "unit.*.attack"; "*" stands for one full entity id
    kind: type            # int or float
    lo: float
    hi: float
    default: float | None = None   # None: the base value comes from content

    def clamp(self, value: float) -> float:
        clamped = min(max(value, self.lo), self.hi)
        return int(round(clamped)) if self.kind is int else float(clamped)

    def in_bounds(self, value: float) -> bool:
        return self.lo <= value <= self.hi


STATS: tuple[StatSpec, ...] = (
    StatSpec("resource.*.income_mul", float, 0.5, 2.0, 1.0),
    StatSpec("unit.*.attack", int, 0, 20),
    StatSpec("unit.*.defense", int, 0, 20),
    StatSpec("unit.*.cost.*", int, 0, 200),
    StatSpec("unit.*.upkeep.*", int, 0, 20),
    StatSpec("building.*.cost.*", int, 0, 300),
    StatSpec("p2.hasty_penalty", float, 0.0, 0.5, 0.15),
    StatSpec("p2.cost.new_attack", int, 1, 5, 2),
    StatSpec("p2.cancel_refund", float, 0.0, 1.0, 0.75),
    StatSpec("command_points", int, 0, 10, 3),
    StatSpec("combat.fortify_bonus", float, 0.0, 1.0, 0.25),
    StatSpec("combat.counter_mul", float, 1.0, 3.0, 1.5),
    StatSpec("starvation.loss_frac", float, 0.0, 1.0, 0.10),
)

_ID = r"[a-z0-9_]+:[a-z0-9_]+"


def _compile(pattern: str) -> re.Pattern[str]:
    parts = [(_ID if p == "*" else re.escape(p)) for p in pattern.split(".")]
    return re.compile(r"^" + r"\.".join(parts) + r"$")


_COMPILED = [(spec, _compile(spec.pattern)) for spec in STATS]


def stat_spec(path: str) -> StatSpec | None:
    """Spec for a fully-qualified stat path (entity ids as ``pack:id``), or None if unknown."""
    for spec, rx in _COMPILED:
        if rx.match(path):
            return spec
    return None


def global_defaults() -> dict[str, float]:
    """Stats that have an engine default (no entity placeholder)."""
    return {s.pattern: s.default for s in STATS if "*" not in s.pattern and s.default is not None}


# ---------------------------------------------------------------- effects

EFFECT_PARAMS: dict[str, dict[str, tuple[type, ...]]] = {
    "modify": {"target": (str,), "op": (str,), "value": (int, float)},
    "add_resource": {"resource": (str,), "amount": (int,)},
    "grant_units": {"unit": (str,), "count": (int,), "where": (str,)},
    "grant_building": {"building": (str,), "where": (str,)},
    "set_flag": {"flag": (str,)},
    "income": {"resource": (str,), "amount": (int,)},
}

# Which hook/context each effect may appear in.
EFFECT_CONTEXTS: dict[str, frozenset[str]] = {
    "passive": frozenset({"modify", "income"}),          # faction bonuses/penalties, buildings
    "building": frozenset({"income"}),
    "on_game_start": frozenset({"add_resource", "grant_units", "grant_building", "set_flag"}),
    "override": frozenset({"modify"}),                    # scenario overrides
}

MODIFY_OPS = ("add", "mul", "set")


def effect_problems(effect: Mapping[str, Any], context: str) -> list[str]:
    """Human-readable problems with an effect's shape (references are checked by content.py)."""
    etype = effect.get("type")
    if etype not in EFFECT_PARAMS:
        return [f"unknown effect type {etype!r}; known: {', '.join(EFFECT_PARAMS)}"]
    problems: list[str] = []
    if etype not in EFFECT_CONTEXTS[context]:
        problems.append(f"effect {etype!r} is not allowed in {context}")
    params = EFFECT_PARAMS[etype]
    for name, types in params.items():
        if name not in effect:
            problems.append(f"effect {etype!r}: missing field {name!r}")
        elif isinstance(effect[name], bool) or not isinstance(effect[name], types):
            problems.append(f"effect {etype!r}: field {name!r} has wrong type")
    for name in effect:
        if name != "type" and name not in params:
            problems.append(f"effect {etype!r}: unknown field {name!r}")
    if etype == "modify" and effect.get("op") not in MODIFY_OPS:
        problems.append(f"modify: op must be one of {MODIFY_OPS}")
    if etype in ("grant_units", "grant_building") and effect.get("where") not in (None, "capital"):
        problems.append(f"{etype}: where must be 'capital'")
    return problems


def apply_modifiers(base: float, modifiers: Iterable[Mapping[str, Any]]) -> float:
    """base -> all add -> all mul -> last set; no clamping."""
    mods = list(modifiers)
    value = float(base)
    value += sum(m["value"] for m in mods if m["op"] == "add")
    value *= math.prod(m["value"] for m in mods if m["op"] == "mul")
    sets = [m["value"] for m in mods if m["op"] == "set"]
    if sets:
        value = float(sets[-1])
    return value
