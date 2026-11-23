"""Engine registry: addressable characteristics (stats), effect types and modifier arithmetic.

The core knows only resource *roles* and stat paths; concrete units/buildings/resources come from content.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

RESOURCE_ROLES: tuple[str, ...] = ("upkeep_food", "build", "research")
REQUIRED_ROLES: tuple[str, ...] = ("upkeep_food", "build")   # exactly one resource each
OPTIONAL_ROLES: tuple[str, ...] = ("research",)              # at most one resource each (none: no techs)

# Order kinds the engine knows (orders.py); `unlock {kind: order}` may name one of them.
ORDER_KINDS: tuple[str, ...] = ("move", "recruit", "build", "fortify", "research")

DEFAULT_UNIT_MOVE = 2      # edges a unit may go through its owner's own land (onto foreign land: always one edge)
# A ruleset without stage-2 content (no techs, branches or artifacts: the slice, core 0.2.0) keeps the slice's one
# edge per move; the rule follows from hashed content, so such a pack still hashes as before (TECH_AND_SETUP.md 5).
SLICE_UNIT_MOVE = 1
MOVE_STAT = "unit.*.move"


def default_unit_move(stage2: bool) -> int:
    return DEFAULT_UNIT_MOVE if stage2 else SLICE_UNIT_MOVE

# Unit abilities (TECH_AND_SETUP.md 1.2). Each is a flag stat `unit.<id>.ability.<name>.on` (0/1) plus parameter
# stats `unit.<id>.ability.<name>.<param>`, so packs, techs and artifacts can grant or tune them with `modify`.
# name -> {param: (kind, lo, hi, default)}
ABILITIES: dict[str, dict[str, tuple[type, float, float, float]]] = {
    "ignore_fortify": {},                               # the defender's fortify bonus does not count against it
    "siege": {"cap": (float, 0.5, 3.0, 1.0)},           # province defense multiplier against it at most `cap`
    "garrison": {"defense": (int, 0, 20, 1)},           # +defense while defending its owner's province
    "fast": {"range": (int, 1, 5, 2)},                  # edges through any land (not only its owner's)
}


@dataclass(frozen=True)
class StatSpec:
    pattern: str          # e.g. "unit.*.attack"; "*" stands for one full entity id
    kind: type            # int or float
    lo: float
    hi: float
    default: float | None = None   # None: the base value comes from content
    seeded: bool = True            # global stat put into every stats table; stage-2 ones fall back to `default`

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
    # stage 2 (TECH_AND_SETUP.md): movement, abilities, techs, province characteristics
    StatSpec(MOVE_STAT, int, 1, 5, DEFAULT_UNIT_MOVE),   # default: default_unit_move()
    StatSpec("tech.*.cost", int, 0, 1000),
    StatSpec("tech.branch.*.cost_mul", float, 0.1, 5.0, 1.0),
    StatSpec("province.defense_mul", float, 0.1, 5.0, 1.0, seeded=False),
    StatSpec("province.income_mul.*", float, 0.0, 5.0, 1.0),
    StatSpec("province.recruit_discount", float, 0.0, 0.9, 0.0, seeded=False),
    *(StatSpec(f"unit.*.ability.{name}.on", int, 0, 1, 0) for name in ABILITIES),
    *(StatSpec(f"unit.*.ability.{name}.{param}", kind, lo, hi, default)
      for name, params in ABILITIES.items() for param, (kind, lo, hi, default) in params.items()),
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
    return {s.pattern: s.default for s in STATS if "*" not in s.pattern and s.default is not None and s.seeded}


# ---------------------------------------------------------------- effects

EFFECT_PARAMS: dict[str, dict[str, tuple[type, ...]]] = {
    "modify": {"target": (str,), "op": (str,), "value": (int, float)},
    "add_resource": {"resource": (str,), "amount": (int,)},
    "grant_units": {"unit": (str,), "count": (int,), "where": (str,)},
    "grant_building": {"building": (str,), "where": (str,)},
    "set_flag": {"flag": (str,)},
    "income": {"resource": (str,), "amount": (int,)},
    "unlock": {"kind": (str,), "id": (str,)},
    "grant_tech": {"tech": (str,)},
}
# Optional parameters: `scope` of a building's modify (province | owner).
EFFECT_OPTIONAL: dict[str, dict[str, tuple[type, ...]]] = {"modify": {"scope": (str,)}}

# Which hook/context each effect may appear in.
EFFECT_CONTEXTS: dict[str, frozenset[str]] = {
    "passive": frozenset({"modify", "income"}),          # faction bonuses/penalties
    "building": frozenset({"income", "modify"}),          # modify needs scope: province | owner
    "on_game_start": frozenset({"add_resource", "grant_units", "grant_building", "set_flag", "grant_tech"}),
    "override": frozenset({"modify"}),                    # scenario overrides
    "tech": frozenset({"modify", "income", "unlock", "set_flag"}),       # owner's passive effects of a tech
    # an artifact: passive (modify, income, unlock) plus one-off effects applied at game start
    "artifact": frozenset({"modify", "income", "unlock", "add_resource", "grant_units", "grant_building",
                           "set_flag", "grant_tech"}),
}
PASSIVE_EFFECTS = frozenset({"modify", "income", "unlock"})
UNLOCK_KINDS = ("unit", "building", "order")
MODIFY_SCOPES = ("province", "owner")

MODIFY_OPS = ("add", "mul", "set")


def normalize_effect(effect: Mapping[str, Any]) -> dict[str, Any]:
    """`effect: modify` (TECH_AND_SETUP.md) and `type: modify` (slice packs) are the same key; returns `type`."""
    out = dict(effect)
    if "type" not in out and "effect" in out:
        out["type"] = out.pop("effect")
    return out


def effect_problems(effect: Mapping[str, Any], context: str) -> list[str]:
    """Human-readable problems with an effect's shape (references are checked by content.py)."""
    effect = normalize_effect(effect)
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
    optional = EFFECT_OPTIONAL.get(etype, {})
    for name in effect:
        if name != "type" and name not in params and name not in optional:
            problems.append(f"effect {etype!r}: unknown field {name!r}")
        elif name in optional and (isinstance(effect[name], bool) or not isinstance(effect[name], optional[name])):
            problems.append(f"effect {etype!r}: field {name!r} has wrong type")
    if etype == "modify" and effect.get("op") not in MODIFY_OPS:
        problems.append(f"modify: op must be one of {MODIFY_OPS}")
    if etype == "modify":
        scope = effect.get("scope")
        if context == "building" and scope not in MODIFY_SCOPES:
            problems.append(f"modify in a building needs scope: one of {MODIFY_SCOPES}")
        elif context != "building" and scope is not None:
            problems.append("modify: scope is allowed only in a building's effects")
    if etype == "unlock" and effect.get("kind") not in UNLOCK_KINDS:
        problems.append(f"unlock: kind must be one of {UNLOCK_KINDS}")
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
