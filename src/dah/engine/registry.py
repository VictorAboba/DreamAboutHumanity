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

# Order kinds the engine knows (orders.py); `unlock {kind: order}` may name one of them. Stage 3
# (docs/SPY_AND_CONQUEST.md 1.2): espionage (hire_shade, spy, recall, ward) and conquest (dispose, policy).
ORDER_KINDS: tuple[str, ...] = ("move", "recruit", "build", "fortify", "research",
                                "hire_shade", "spy", "recall", "ward", "dispose", "policy")

# Switchable subsystems of stage 3 (scenario `systems`, `dah game create --espionage/--conquest`). A system that is
# off leaves no trace: its order kinds are NOT_AVAILABLE, its stats are not in any table, its content is hidden.
SYSTEMS: tuple[str, ...] = ("espionage", "conquest")
ORDER_SYSTEMS: dict[str, str] = {"hire_shade": "espionage", "spy": "espionage", "recall": "espionage",
                                 "ward": "espionage", "dispose": "conquest", "policy": "conquest"}

# Phase rules of order kinds (data, not branches in orders.py; SPY_AND_CONQUEST.md 1.2, 1.4):
# - p2_new: price in CP of a new order given in negotiation_2: a stat path, an int, or "locked" (PHASE_LOCKED);
# - p2_edit: cancelling or amending an order in negotiation_2: "free" (0 CP, reserve back in full), "cp" (the priced
#   P2 edits of orders.py) or "locked";
# - recon_consumed: an entry of the actions book is carried out and paid for by the recon step when actions close,
#   so resolution neither re-checks nor executes it (and P2 cannot touch it).
ORDER_PHASE_RULES: dict[str, dict[str, Any]] = {
    "move": {"p2_new": "p2.cost.new_attack", "p2_edit": "cp", "recon_consumed": False},
    "recruit": {"p2_new": "locked", "p2_edit": "cp", "recon_consumed": False},
    "build": {"p2_new": "locked", "p2_edit": "cp", "recon_consumed": False},
    "fortify": {"p2_new": 1, "p2_edit": "cp", "recon_consumed": False},
    "research": {"p2_new": "locked", "p2_edit": "locked", "recon_consumed": False},
    "hire_shade": {"p2_new": "locked", "p2_edit": "locked", "recon_consumed": True},
    "spy": {"p2_new": "p2.cost.spy", "p2_edit": "locked", "recon_consumed": True},
    "recall": {"p2_new": "p2.cost.recall", "p2_edit": "locked", "recon_consumed": True},
    "ward": {"p2_new": "locked", "p2_edit": "locked", "recon_consumed": True},
    "dispose": {"p2_new": 0, "p2_edit": "free", "recon_consumed": False},
    "policy": {"p2_new": 0, "p2_edit": "free", "recon_consumed": False},
}

# Behaviours the engine knows; packs name them in missions and dispositions (several pack entries may share one
# behaviour with other numbers). name -> {param: (kind, lo, hi, default)}; missions also say whether they are loud.
MISSION_BEHAVIORS: dict[str, dict[str, Any]] = {
    "observe_province": {"loud": False, "params": {}},
    "read_ledger": {"loud": False, "params": {}},
    "read_orders": {"loud": False, "params": {}},
    "sabotage": {"loud": True, "params": {"deny_fortify": (bool, 0, 1, True),
                                          "defense_mul": (float, 0.1, 1.0, 0.85),
                                          "income_mul": (float, 0.0, 1.0, 0.0)}},
}
# What a mission's `target` names: any province (not the spy's own), a province of another seat, or another seat.
MISSION_TARGETS: tuple[str, ...] = ("province", "owned_province", "seat")
DISPOSITION_BEHAVIORS: dict[str, dict[str, tuple[type, float, float, Any]]] = {
    "annex": {},                                       # occupation lifted: full province (costs `cost`)
    "raze": {"ruin_turns": (int, 1, 20, 6)},           # loot; buildings and builds destroyed; ruins for N turns
    "raid": {"ravage_turns": (int, 1, 20, 2)},         # loot; back to the previous owner, ravaged for N turns
    "release": {},                                     # given to `to` / the previous owner / the origin / neutrals
}

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
    system: str | None = None      # stage 3: the switchable system it belongs to (effects on it drop while off)

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


def _s3(pattern: str, kind: type, lo: float, hi: float, default: float | None, system: str) -> StatSpec:
    return StatSpec(pattern, kind, lo, hi, default, seeded=False, system=system)


_E, _C = "espionage", "conquest"
# Stage 3 (SPY_AND_CONQUEST.md 1.2): never seeded, so stats tables (and hashes) of games without them do not change.
STAGE3_STATS: tuple[StatSpec, ...] = (
    _s3("spy.whisper", int, 0, 5, 1, _E),
    _s3("spy.mirrors", int, 0, 5, 1, _E),
    _s3("province.mirrors", int, 0, 5, 0, _E),
    _s3("spy.shades_max", int, 0, 8, 2, _E),
    _s3("spy.active_base", int, 0, 5, 1, _E),
    _s3("spy.rounds_bonus", int, 0, 3, 0, _E),
    _s3("spy.mission.*.difficulty", int, 0, 6, None, _E),
    _s3("spy.mission.*.max_rounds", int, 1, 8, None, _E),
    *(_s3(f"spy.success.{k}", float, 0.0, 1.0, v, _E)
      for k, v in (("base", .55), ("skill", .10), ("xp", .05), ("cover", .05), ("min", .10), ("max", .95))),
    *(_s3(f"spy.detect.{k}", float, 0.0, 1.0, v, _E)
      for k, v in (("base", .05), ("difficulty", .05), ("skill", .12), ("repeat", .08), ("xp", .05),
                   ("cover", .04), ("min", .03), ("max", .90))),
    _s3("spy.detect.army", int, 0, 3, 1, _E),
    *(_s3(f"spy.attribution.{k}", float, 0.0, 1.0, v, _E)
      for k, v in (("base", .50), ("skill", .15), ("min", .10), ("max", .95))),
    *(_s3(f"spy.feint_expose.{k}", float, 0.0, 1.0, v, _E) for k, v in (("base", .25), ("skill", .15), ("max", .90))),
    _s3("spy.intel.width", float, 0.0, 1.0, 0.30, _E),
    _s3("spy.xp_max", int, 0, 10, 3, _E),
    _s3("spy.cover_max", int, 0, 10, 3, _E),
    _s3("spy.ward.bonus", int, 0, 5, 2, _E),
    _s3("spy.shade.cost.*", int, 0, 200, None, _E),
    _s3("spy.shade.upkeep.*", int, 0, 20, None, _E),
    _s3("spy.ward.cost.*", int, 0, 200, None, _E),
    _s3("p2.cost.spy", int, 1, 5, 1, _E),
    _s3("p2.cost.recall", int, 1, 5, 1, _E),
    _s3("feint.cp_cost", int, 0, 3, 1, _E),
    _s3("disposition.*.cost_mul", float, 0.0, 5.0, 1.0, _C),
    _s3("disposition.*.loot_mul", float, 0.0, 5.0, 1.0, _C),
    _s3("conquest.claim_turns", int, 0, 20, 10, _C),
    _s3("conquest.claim_bonus", float, 0.0, 1.0, 0.25, _C),
    _s3("conquest.loot_cooldown", int, 0, 20, 4, _C),
)
STATS = STATS + STAGE3_STATS

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
