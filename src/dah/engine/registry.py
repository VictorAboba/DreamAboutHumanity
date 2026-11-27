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
                                "hire_shade", "spy", "recall", "ward", "dispose", "policy", "discover")

# Switchable subsystems of stage 3 (scenario `systems`, `dah game create --espionage/--conquest`). A system that is
# off leaves no trace: its order kinds are NOT_AVAILABLE, its stats are not in any table, its content is hidden.
# Stage 5 (docs/ABANDONED_LANDS.md 1): `discoveries` - free starts become Abandoned lands with hidden finds.
# Core 0.8.0 (docs/TRADE.md 8): `trade` - offers «даю X за Y» answered by the other seat (engine/trade.py).
SYSTEMS: tuple[str, ...] = ("espionage", "conquest", "discoveries", "trade")
ORDER_SYSTEMS: dict[str, str] = {"hire_shade": "espionage", "spy": "espionage", "recall": "espionage",
                                 "ward": "espionage", "dispose": "conquest", "policy": "conquest",
                                 "discover": "discoveries"}

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
    # stage 5 (ABANDONED_LANDS.md 8): the choice of a mixed find, like dispose
    "discover": {"p2_new": 0, "p2_edit": "free", "recon_consumed": False},
}

# Behaviours the engine knows; packs name them in missions and dispositions (several pack entries may share one
# behaviour with other numbers). name -> {param: (kind, lo, hi, default)}; missions also say whether they are loud.
# A parameter whose default is None is new (core 0.8.0, docs/SPY_V2.md 4): left out unless the pack gives it, so a
# mission of an older pack keeps its parameters (and its hash) and its behaviour of 0.7.x.
MISSION_BEHAVIORS: dict[str, dict[str, Any]] = {
    # exact: units as exact numbers; planned: recruits and fortify of this province from its owner's actions book
    "observe_province": {"loud": False, "params": {"exact": (bool, 0, 1, None), "planned": (bool, 0, 1, None)}},
    # army_total: «армия всего: lo–hi», the target's units in all its provinces
    "read_ledger": {"loud": False, "params": {"army_total": (bool, 0, 1, None)}},
    "read_orders": {"loud": False, "params": {}},
    # core 0.6.0 (decision 26): the target's secret objectives (SCORING.md 5, SPY_AND_CONQUEST.md 2.2)
    "read_objective": {"loud": False, "params": {}},
    "sabotage": {"loud": True, "params": {"deny_fortify": (bool, 0, 1, True),
                                          "defense_mul": (float, 0.1, 1.0, 0.85),
                                          "income_mul": (float, 0.0, 1.0, 0.0),
                                          # core 0.8.0 (docs/SPY_V2.md 4.5): a build in the province does not advance
                                          # this turn (stall_max: stops of one build a game, 0 = no limit); an
                                          # occupation with <= unrest_units units revolts and keeps revolt_garrison
                                          # {unit: count} without an owner
                                          "stall_build": (bool, 0, 1, None),
                                          "stall_max": (int, 0, 10, None),
                                          "unrest_units": (int, 0, 10, None),
                                          "revolt_garrison": (dict, 1, 50, None)}},
}
# How a mission's success scores (core 0.8.0, docs/SPY_V2.md 5.1): each_turn (the 0.7.0 rule, the default), once a
# game per spy, once per (spy, target seat), effect (a loud mission: only when its strike did something, credited at
# the end of the resolution) or no (information only).
INTEL_KINDS: tuple[str, ...] = ("each_turn", "once", "once_per_target", "effect", "no")
INTEL_DEFAULT = "each_turn"
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
    # core 0.5.0 (m6 fixes): 1 = a fortification lasts while the owner keeps units in the province (lost when it is
    # emptied or captured); 0 = one turn only, as before. Never seeded: stats tables (hashes) of older packs stay.
    StatSpec("combat.fortify_persists", int, 0, 1, 0, seeded=False),
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
    # core 0.6.0 (decision 26): Shades every seat gets for free at the start (counted in spy.shades_max)
    _s3("spy.shades_free", int, 0, 3, 0, _E),
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
    # core 0.8.0 (docs/SPY_V2.md 4.2): the least width of an honest interval (2 = as before)
    _s3("spy.intel.width_min", int, 0, 5, 2, _E),
    # core 0.8.0 (docs/SPY_V2.md 3.2): 1 = R of a round counts the spy's other Shades checked at the same place in
    # this recon step; 0 = R is always 0 (as before)
    _s3("spy.detect.repeat_on", int, 0, 1, 0, _E),
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
    # core 0.8.0 (docs/TRADE.md 8): trade; never seeded, so rulesets without trade hash as before
    _s3("trade.provinces_per_turn", int, 0, 5, 1, "trade"),
    _s3("trade.province_max_value", int, 0, 5, 5, "trade"),
    _s3("trade.reports", int, 0, 1, 1, "trade"),
    _s3("trade.later", int, 0, 1, 1, "trade"),
    _s3("trade.show_promises", int, 0, 1, 0, "trade"),
)
STATS = STATS + STAGE3_STATS

# Stage 4 (docs/SCORING.md 1, 3): sources of victory points in a fixed order. A source with weight 0 is off like a
# switchable system: its "system" is `scoring.<src>` (requires_system, stats), so its content and stats leave no trace.
# Core 0.7.0 (decision 27): `intel`, points for successful spy missions (counted in the recon step); a ruleset that does
# not weigh it (all older packs) hashes and scores as before (content.Ruleset.to_dict drops its zero weight).
SCORE_SOURCES: tuple[str, ...] = ("territory", "science", "works", "war", "intel", "objectives")
TURN_SOURCES: tuple[str, ...] = SCORE_SOURCES[:-1]         # the turn score; objectives count only in the final total
NEW_SCORE_SOURCES: tuple[str, ...] = ("intel",)            # hashed only with a weight > 0
# A score source that only a switchable system can feed: with that system off the source's weight is 0 (and so its
# stats, rules lines, view fields and hash entry go), whatever the scenario or the host's --scoring says.
SCORE_SOURCE_SYSTEMS: dict[str, str] = {"intel": "espionage"}
DEFAULT_SCORING: dict[str, int] = {"territory": 1}          # no `scoring` key: territory only, as before stage 4
SCORE_WEIGHT_MAX = 5
SCORING_SYSTEMS: tuple[str, ...] = tuple(f"scoring.{src}" for src in SCORE_SOURCES)
# Stats of a non-default score as a whole (not of one source): off, and their effects dropped, under the default
# score {territory: 1} (core 0.6.0: the pact-break penalty)
SCORING_ANY = "scoring"

# Kinds of secret objectives (SCORING.md 2.5): behaviour in engine/scoring.py, numbers in the pack.
# kind -> {"params": {name: "province_type" | "branch" | "count"}, "optional"?: {name: (allowed words, the first the
# default)}, "target": "none" | "seat"}
OBJECTIVE_KINDS: dict[str, dict[str, Any]] = {
    "hold_type": {"params": {"type": "province_type", "count": "count"}, "target": "none"},
    "hold_count": {"params": {"count": "count"}, "target": "none"},
    # core 0.8.0 (docs/TRADE.md 6.3): `via: capture` counts only land not got without a battle (a trade, a release);
    # optional, default `any`, kept (and hashed) only as `capture`
    "hold_foreign": {"params": {"count": "count"}, "optional": {"via": ("any", "capture")}, "target": "none"},
    "capital_other": {"params": {"count": "count"}, "target": "none"},
    "techs_branch": {"params": {"branch": "branch", "count": "count"}, "target": "none"},
    "great_work": {"params": {"count": "count"}, "target": "none"},
    "rival_capital_taken": {"params": {}, "target": "seat"},
}
# Difficulty tiers of secret objectives (core 0.6.0, decision 26), easiest first: an objective's `points` must grow
# with its tier within a ruleset (content.py checks it); the numbers are the pack's.
OBJECTIVE_DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")


def _s4(pattern: str, lo: float, hi: float, default: float | None, src: str) -> StatSpec:
    return StatSpec(pattern, int, lo, hi, default, seeded=False, system=f"scoring.{src}")


SCORING_STATS: tuple[StatSpec, ...] = (
    _s4("score.territory.province", 0, 5, 1, "territory"),
    _s4("score.territory.capital", 0, 10, 3, "territory"),
    *(_s4(f"score.science.tier{t}", 0, 5, t, "science") for t in (1, 2, 3, 4)),
    _s4("score.science.branch", 0, 5, 1, "science"),
    _s4("building.*.score", 0, 10, None, "works"),
    _s4("score.war.per_point", 5, 200, 30, "war"),
    _s4("score.war.cap_per_victim", 0, 20, 3, "war"),
    _s4("score.war.cap", 0, 20, 5, "war"),
    _s4("score.war.defense_pct", 0, 100, 50, "war"),
    # core 0.7.0 (decision 27): the war score is a scale per province instead of the price of killed units. While
    # score.war.province_cap > 0: taking a province a living rival seat owned at the start of the turn gives 1 point,
    # holding it +1 per score.war.hold_turns full turns, at most province_cap per (seat, province) over the game, and
    # at most score.war.province_total in all (0 = no total cap); no war_credit then. 0 (the default, older packs):
    # the kill credit of core 0.5.0-0.6.0 with the stats above.
    _s4("score.war.province_cap", 0, 10, 0, "war"),
    _s4("score.war.hold_turns", 1, 10, 2, "war"),
    _s4("score.war.province_total", 0, 50, 0, "war"),
    # core 0.7.1 (decision 28): 1 = a capture (and the holding of it) pays war points only for a province the taker has
    # not owned earlier in this game (Player.tally.owned: owner at the start of the game or at the end of any earlier
    # turn, its own start provinces included); taking back one's own gives the land, 0 war points. 0 (the default,
    # older packs): every capture from a living rival pays, as in core 0.7.0. Read only with province_cap > 0.
    _s4("score.war.no_reclaim", 0, 1, 0, "war"),
    # core 0.7.0 (decision 27): points per successful spy mission against a living rival seat (each target and
    # mission kind once a turn), at most score.intel.cap over the game
    _s4("score.intel.per_success", 0, 5, 1, "intel"),
    _s4("score.intel.cap", 0, 20, 3, "intel"),
    # core 0.8.1 (decision 32, measurement m10): 1 = the deep missions (intel kinds `once`, `once_per_target`: secret,
    # read_orders) score against a pact partner too; `effect` (sabotage) keeps the pact block (a strike at a partner
    # scores nothing; espionage never breaks a pact, SPY_V2.md 8) and `each_turn` never had one. 0 (the default,
    # older packs): no point against a partner (G1, 0.8.0)
    _s4("score.intel.pact_deep", 0, 1, 0, "intel"),
    # core 0.6.0 (decision 26): points a seat loses per pact it broke (a penalty line `betrayal` of the turn score
    # under a non-default scoring); 0 (the default) = no penalty, so older packs score as before
    StatSpec("score.betrayal.per_break", int, 0, 10, 0, seeded=False, system=SCORING_ANY),
)
STATS = STATS + SCORING_STATS

# Core 0.7.0 (decision 27): army fog. 1 = a province of another seat outside the viewer's sight (own provinces and
# their neighbours: resolve.visible_provinces) shows whether an army stands there (`army`, «армия есть»), never its
# size; 0 (the default, older packs) = such a province shows nothing («?»). Sizes are seen only within sight (or by a
# spy's report), as before. Never seeded: stats tables (hashes) of older packs stay. A stat of the espionage system
# (the fog is the other side of a Shade's observation): with espionage off its scenario line drops and leaves no trace.
STATS = STATS + (_s3("fog.army_presence", int, 0, 1, 0, _E),)

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
    # stage 5 (ABANDONED_LANDS.md 3): effects of finds only
    "grant_artifact": {"artifact": (str,)},
    "lose_units": {"frac": (int, float)},
    "spawn_neutral": {"units": (dict,)},
    "set_status": {"status": (str,), "turns": (int,)},
}
# Optional parameters: `scope` of a building's modify (province | owner); `min` of lose_units (units lost at least);
# `grow` {every, add, cap} and `reward` [effects] of spawn_neutral (a lair).
EFFECT_OPTIONAL: dict[str, dict[str, tuple[type, ...]]] = {"modify": {"scope": (str,)}, "lose_units": {"min": (int,)},
                                                           "spawn_neutral": {"grow": (dict,), "reward": (list,)}}

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
    # stage 5 (ABANDONED_LANDS.md 3): a boon or hindrance as armies enter; an outcome of a mixed find's choice
    # (applied in the holder's own province: no neutrals); the reward of a guard or a lair (to the taker)
    "discovery": frozenset({"add_resource", "grant_units", "grant_building", "grant_artifact", "lose_units",
                            "spawn_neutral", "set_status"}),
    "discovery_choice": frozenset({"add_resource", "grant_units", "grant_building", "grant_artifact", "lose_units",
                                   "set_status"}),
    "discovery_reward": frozenset({"add_resource", "grant_units"}),
}
DISCOVERY_CONTEXTS = frozenset({"discovery", "discovery_choice", "discovery_reward"})
PASSIVE_EFFECTS = frozenset({"modify", "income", "unlock"})

# ---------------------------------------------------------------- stage 5: finds (docs/ABANDONED_LANDS.md 3, 4)

DISCOVERY_KINDS: tuple[str, ...] = ("boon", "hindrance", "mixed")
# "<kind>:<rarity>" -> (power from, power to: a half-open interval [lo, hi)), default weight. Engine data, not the
# pack's: a pack cannot sell a strong boon as a common one.
DISCOVERY_RARITY: dict[str, tuple[float, float, int]] = {
    "boon:common": (1.0, 2.5, 6), "boon:uncommon": (2.5, 4.0, 3), "boon:rare": (4.0, 6.0, 1),
    "hindrance:minor": (-1.5, -0.5, 5), "hindrance:major": (-3.0, -1.5, 3),
    "mixed:common": (0.5, 2.5, 3), "mixed:uncommon": (2.5, 4.0, 2), "mixed:rare": (4.0, 6.0, 1),
}
DISCOVERY_RARITIES: dict[str, tuple[str, ...]] = {
    "boon": ("common", "uncommon", "rare"), "hindrance": ("minor", "major"), "mixed": ("common", "uncommon", "rare")}
DISCOVERY_TRIGGERS: tuple[str, ...] = ("army", "shade")       # shade: a warning «ignored in this stage»
DISCOVERY_STATUSES: tuple[str, ...] = ("ravaged", "ruined")
DISCOVERY_MAX_MIN_LOSS = 20
# Power (P) of finds: 1 P = 15 resources = a unit price of 20. The validator values a find against the reference
# army (5 units at 12 = 60, ABANDONED_LANDS.md 3) and against small armies of 1 and 2 of the pack's cheapest common
# unit (decision 25: any army triggers a find, so a scout sweep must not pay: the pool's mean power against them < 0).
P_RESOURCES = 15
P_PRICE = 20
REFERENCE_ARMY = (5, 12)          # (units, base price of each)
SMALL_ARMIES = (1, 2)             # units of the cheapest common unit
# «Души снижены» (ABANDONED_LANDS.md 2.2, decision 25 d): presets of the scenario knob `discoveries.souls`; the keys
# ravaged_turns, ruined_turns, income_mul given next to it override the preset
SOULS_VARIANTS: dict[str, dict[str, Any]] = {
    "ravage": {"ravaged_turns": 3, "ruined_turns": 0, "income_mul": 1.0},
    "halve": {"ravaged_turns": 0, "ruined_turns": 0, "income_mul": 0.5},
    "ashes": {"ravaged_turns": 0, "ruined_turns": 2, "income_mul": 1.0},
}
DEFAULT_SOULS = "ravage"
# Scenario `discoveries` keys and defaults (ABANDONED_LANDS.md 11 as amended by decision 25: no army threshold, no
# compensation). Rings: the "<kind>:<rarity>" allowed in a province of that ring; rings past the last given take it.
# density 0.66, not the spec's 0.67: k = ceil(density x candidates) and 0.67 x 3 = 2.01 gave all 3 provinces of a
# corner a find; the spec means two of three (5.3, 5.7, 5.8: "одна провинция пуста")
DISCOVERY_DEFAULTS: dict[str, Any] = {
    "souls": DEFAULT_SOULS, "density": 0.66, "extra": 0,
    "rings": {2: ("boon:common", "hindrance:minor", "mixed:common"),
              3: ("boon:common", "boon:uncommon", "boon:rare", "hindrance:minor", "hindrance:major", "mixed:common",
                  "mixed:uncommon", "mixed:rare")},
    "ruins_find": False, "max_ring": 3, "w": {2: 1.0, 3: 0.75}, "fair_tolerance": 1.0, "tries": 64,
    "observe_mirrors": 2, "fair_mirror": False,
}
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
    if etype in ("grant_units", "grant_building"):
        where = effect.get("where")
        if context in DISCOVERY_CONTEXTS:
            if where != "here":
                problems.append(f"{etype}: in a find, where must be 'here'")
        elif where not in (None, "capital"):
            problems.append(f"{etype}: where must be 'capital'")
    return problems + _discovery_problems(effect, etype, context)


def _discovery_problems(effect: Mapping[str, Any], etype: str, context: str) -> list[str]:
    """Shapes of the effects of finds (ABANDONED_LANDS.md 3)."""
    problems: list[str] = []
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)     # noqa: E731
    if etype == "add_resource" and context in DISCOVERY_CONTEXTS and effect.get("amount") == 0:
        problems.append("add_resource: amount must not be 0")
    if etype == "add_resource" and context == "discovery_reward" and num(effect.get("amount")) \
            and effect["amount"] < 0:
        problems.append("add_resource: a reward cannot take resources")
    if etype == "lose_units":
        if num(effect.get("frac")) and not 0.0 <= effect["frac"] <= 1.0:
            problems.append("lose_units: frac must be in 0..1")
        low = effect.get("min", 1)
        if num(low) and not 0 <= low <= DISCOVERY_MAX_MIN_LOSS:
            problems.append(f"lose_units: min must be in 0..{DISCOVERY_MAX_MIN_LOSS}")
    if etype == "set_status":
        if effect.get("status") not in DISCOVERY_STATUSES:
            problems.append(f"set_status: status must be one of {DISCOVERY_STATUSES}")
        if num(effect.get("turns")) and not 1 <= effect["turns"] <= 20:
            problems.append("set_status: turns must be in 1..20")
    if etype == "spawn_neutral":
        units = effect.get("units")
        if isinstance(units, dict) and (not units or not all(isinstance(n, int) and not isinstance(n, bool)
                                                             and 1 <= n <= 50 for n in units.values())):
            problems.append("spawn_neutral: units {unit: 1..50}, at least one")
        grow = effect.get("grow")
        if isinstance(grow, dict):
            if set(grow) != {"every", "add", "cap"}:
                problems.append("spawn_neutral: grow needs exactly every, add {unit: n}, cap")
            elif not (isinstance(grow["every"], int) and 1 <= grow["every"] <= 10 and isinstance(grow["add"], dict)
                      and grow["add"] and isinstance(grow["cap"], int) and 1 <= grow["cap"] <= 50
                      and all(isinstance(n, int) and not isinstance(n, bool) and n >= 1
                              for n in grow["add"].values())):
                problems.append("spawn_neutral: grow every 1..10, add {unit: n >= 1}, cap 1..50")
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


# ---------------------------------------------------------------- the hidden last turn (core 0.8.2, decision 33)

def hidden_end(turn_limit: int, end_min: int | None) -> bool:
    """The game ends on one of the turns end_min..turn_limit, the exact one told to no seat before game_over."""
    return bool(end_min) and int(end_min) < int(turn_limit)


def end_span(turn_limit: int, end_min: int | None) -> str:
    """«12» for a fixed end, «11–15» for a hidden one."""
    return f"{end_min}–{turn_limit}" if hidden_end(turn_limit, end_min) else f"{turn_limit}"


def turn_mark(turn: int, turn_limit: int, end_min: int | None, *, over: bool = False) -> str:
    """The turn as a seat may see it: «7/12» for a fixed end; under a hidden end «7 (конец — один из ходов 11–15,
    скрыт)», from turn end_min on «11 (может быть последним; …)», on the latest turn «15 (последний; конец — один из
    ходов 11–15)» (review F3: no later end exists), after the game «13, последний». Never the drawn turn before
    game_over."""
    if not hidden_end(turn_limit, end_min):
        return f"{turn}/{turn_limit}"
    if over:
        return f"{turn}, последний"
    if turn >= turn_limit:
        return f"{turn} (последний; конец — один из ходов {end_span(turn_limit, end_min)})"
    span = f"конец — один из ходов {end_span(turn_limit, end_min)}, скрыт"
    return f"{turn} (может быть последним; {span})" if turn >= int(end_min or 0) else f"{turn} ({span})"
