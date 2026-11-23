"""Techs, research, unit availability and abilities, province characteristics (TECH_AND_SETUP.md section 1).

Everything here reads content through the Ruleset and stats; the engine knows no concrete tech, unit or building.
A seat's stats come from its *stat key* (`stat_key`): its faction id while it has no techs, artifacts or
owner-scoped buildings (exactly the slice's per-faction table), else a `Profile` that adds their modifiers.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Iterable, Mapping

from . import registry
from .content import Profile, Ruleset, StatKey
from .model import GameState, Player

NOT_AVAILABLE = "NOT_AVAILABLE"            # locked by tech, faction or a missing building
ALREADY_RESEARCHED = "ALREADY_RESEARCHED"

RESEARCHED, ACTIVE, AVAILABLE, LOCKED = "researched", "active", "available", "locked"


# ---------------------------------------------------------------- stat keys and province stats

def stat_key(state: GameState, ruleset: Ruleset, seat: str | None) -> StatKey:
    """The seat's key for `Ruleset.stat`: faction id, or a Profile with its techs, artifacts and owner-scoped
    buildings (counted over the provinces it owns now)."""
    player = state.players.get(seat) if seat is not None else None
    if player is None:
        return None
    counts: Counter[str] = Counter()
    if any(b.owner_mods for b in ruleset.buildings.values()):
        for prov in state.owned(player.seat):
            counts.update(b for b in prov.buildings if b in ruleset.buildings and ruleset.buildings[b].owner_mods)
    if not (player.techs or player.artifacts or counts):
        return player.faction
    return Profile(player.faction, tuple(sorted(player.techs)), tuple(sorted(player.artifacts)),
                   tuple(sorted(counts.items())))


def province_stat(state: GameState, ruleset: Ruleset, pid: str, path: str, key: StatKey = ...) -> float:
    """A `province.*` stat of one province: its owner's value (techs, artifacts, owner-scoped buildings) with the
    province-scoped modifiers of the buildings standing in it. `key` defaults to the owner's stat key."""
    prov = state.provinces[pid]
    if key is ...:
        key = stat_key(state, ruleset, prov.owner)
    base = ruleset.stat(path, key)
    mods = [m for b in prov.buildings if b in ruleset.buildings
            for m in ruleset.buildings[b].province_mods if m["target"] == path]
    if not mods:
        return base
    spec = registry.stat_spec(path)
    assert spec is not None
    return spec.clamp(registry.apply_modifiers(base, mods))


def province_defense_mul(state: GameState, ruleset: Ruleset, pid: str, key: StatKey = ...) -> float:
    """Defense multiplier of a province: its type's `defense_mul` x `province.defense_mul`."""
    base = ruleset.province_types[state.provinces[pid].type].defense_mul
    extra = province_stat(state, ruleset, pid, "province.defense_mul", key)
    return base if extra == 1.0 else base * extra


# ---------------------------------------------------------------- availability of units, buildings and techs

def _names(ruleset: Ruleset, ids: Iterable[str]) -> str:
    out = []
    for i in ids:
        entity = (ruleset.techs.get(i) or ruleset.buildings.get(i) or ruleset.factions.get(i)
                  or ruleset.units.get(i))
        out.append(f"«{entity.name}» ({i})" if entity is not None else i)
    return ", ".join(out)


def _faction_names(ruleset: Ruleset, factions: Iterable[str]) -> str:
    return ", ".join(ruleset.factions[f].name if f in ruleset.factions else f.split(":")[-1] for f in factions)


def unit_problem(state: GameState, ruleset: Ruleset, seat: str, unit: str, province: str | None = None,
                 key: StatKey = ...) -> str | None:
    """Why `seat` cannot recruit `unit` (in `province`): faction, techs, buildings; None if it can. An `unlock`
    effect lifts the faction and tech requirements, never the building one."""
    u = ruleset.units[unit]
    player = state.players[seat]
    if key is ...:
        key = stat_key(state, ruleset, seat)
    unlocked = f"unit:{unit}" in ruleset.unlocks(key)
    if u.factions and player.faction not in u.factions and not unlocked:
        return f"«{u.name}» — юнит фракций: {_faction_names(ruleset, u.factions)}"
    missing = [t for t in u.requires_tech if t not in player.techs]
    if missing and not unlocked:
        return f"«{u.name}» требует технологий: {_names(ruleset, missing)}"
    if province is not None and u.requires_building:
        here = state.provinces[province].buildings
        need = [b for b in u.requires_building if b not in here]
        if need:
            return f"«{u.name}» нанимается только там, где есть: {_names(ruleset, need)}"
    return None


def building_problem(state: GameState, ruleset: Ruleset, seat: str, building: str,
                     key: StatKey = ...) -> str | None:
    b = ruleset.buildings[building]
    player = state.players[seat]
    if key is ...:
        key = stat_key(state, ruleset, seat)
    unlocked = f"building:{building}" in ruleset.unlocks(key)
    if b.factions and player.faction not in b.factions and not unlocked:
        return f"«{b.name}» — здание фракций: {_faction_names(ruleset, b.factions)}"
    missing = [t for t in b.requires_tech if t not in player.techs]
    if missing and not unlocked:
        return f"«{b.name}» требует технологий: {_names(ruleset, missing)}"
    return None


def tech_missing(ruleset: Ruleset, player: Player, tech: str) -> list[str]:
    """Unmet requirements of a tech for a player, as Russian phrases (empty: researchable unless researched)."""
    t = ruleset.techs[tech]
    out = []
    if t.factions and player.faction not in t.factions:
        out.append(f"только для фракций: {_faction_names(ruleset, t.factions)}")
    missing = [r for r in t.requires if r not in player.techs]
    if missing:
        out.append(f"нужны: {_names(ruleset, missing)}")
    if t.requires_any and not any(r in player.techs for r in t.requires_any):
        out.append(f"нужна хотя бы одна из: {_names(ruleset, t.requires_any)}")
    rt = t.requires_tier
    if rt:
        done = [x for x in player.techs if x in ruleset.techs and ruleset.techs[x].tier == rt["tier"]
                and (not rt.get("same_branch", True) or ruleset.techs[x].branch == t.branch)]
        if len(done) < rt["count"]:
            where = " этой ветки" if rt.get("same_branch", True) else ""
            out.append(f"нужно {rt['count']} технологии яруса {rt['tier']}{where} (изучено {len(done)})")
    return out


def tech_problem(state: GameState, ruleset: Ruleset, seat: str, tech: str) -> tuple[str, str] | None:
    """(reason, message) why `seat` cannot start researching `tech`; None if it can."""
    if ruleset.research_resource is None:
        return NOT_AVAILABLE, "в этой партии нет ресурса исследований: технологии недоступны"
    player = state.players[seat]
    t = ruleset.techs[tech]
    if tech in player.techs:
        return ALREADY_RESEARCHED, f"«{t.name}» уже изучена"
    missing = tech_missing(ruleset, player, tech)
    if missing:
        return NOT_AVAILABLE, f"«{t.name}» пока недоступна: {'; '.join(missing)}"
    return None


def resolve_tech_ref(ruleset: Ruleset, ref: Any) -> str | None:
    """Full tech id for a full or short id or a name (ru or en), any case."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    full = ruleset.resolve("tech", ref) or ruleset.resolve("tech", ref.lower())
    if full:
        return full
    label = ref.lower().rstrip(".")
    hits = [tid for tid, t in ruleset.techs.items() if label in (t.name.lower(), t.name_en.lower())]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- movement range and abilities

def has_ability(ruleset: Ruleset, unit: str, name: str, key: StatKey) -> bool:
    return bool(ruleset.stat(f"unit.{unit}.ability.{name}.on", key))


def ability_param(ruleset: Ruleset, unit: str, name: str, param: str, key: StatKey) -> float:
    return ruleset.stat(f"unit.{unit}.ability.{name}.{param}", key)


def _distance(adjacency: Mapping[str, Iterable[str]], src: str, dst: str, passable: Callable[[str], bool],
              limit: int) -> int | None:
    """Edges from src to dst when every province in between is passable (dst itself may be anything)."""
    frontier, seen = {src}, {src}
    for dist in range(1, limit + 1):
        nxt: set[str] = set()
        for node in sorted(frontier):
            for n in adjacency[node]:
                if n == dst:
                    return dist
                if n not in seen and passable(n):
                    seen.add(n)
                    nxt.add(n)
        if not nxt:
            return None
        frontier = nxt
    return None


def move_range(ruleset: Ruleset, units: Iterable[str], key: StatKey) -> int:
    """Edges a stack may go through its owner's land: the smallest `unit.<id>.move` of its units."""
    return min((int(ruleset.stat(f"unit.{u}.move", key)) for u in units), default=1)


def move_problem(state: GameState, ruleset: Ruleset, seat: str, src: str, dst: str, units: Iterable[str],
                 key: StatKey = ...) -> str | None:
    """Why a move src -> dst is out of reach (None: it is in reach). One edge always; up to `move` edges when every
    province in between is the seat's own; a stack of only `fast` units goes up to `fast.range` edges through any
    land (it slips past what is in between)."""
    if ruleset.map.adjacent(src, dst):
        return None
    if key is ...:
        key = stat_key(state, ruleset, seat)
    units = sorted(units)
    own_range = move_range(ruleset, units, key)
    if own_range > 1:
        dist = _distance(ruleset.map.adjacency, src, dst, lambda p: state.provinces[p].owner == seat, own_range)
        if dist is not None:
            return None
    if units and all(has_ability(ruleset, u, "fast", key) for u in units):
        fast = min(int(ability_param(ruleset, u, "fast", "range", key)) for u in units)
        if fast > 1 and _distance(ruleset.map.adjacency, src, dst, lambda p: True, fast) is not None:
            return None
    reach = f"; по своей земле — до {own_range} рёбер (промежуточные провинции твои)" if own_range > 1 else ""
    return f"{dst} не соседствует с {src}{reach}"


# ---------------------------------------------------------------- research in resolution

def research_transfer(ruleset: Ruleset, player: Player, key: StatKey) -> dict[str, int] | None:
    """Economy step: move the research resource's stock into the active tech, up to what it still needs; the
    surplus stays in stock. Returns {added, progress, cost} or None (nothing active)."""
    res = ruleset.research_resource
    tech = player.researching
    if res is None or tech is None or tech not in ruleset.techs or tech in player.techs:
        return None
    cost = ruleset.tech_cost(tech, key)
    progress = player.research.get(tech, 0)
    added = max(0, min(player.resources.get(res, 0), cost - progress))
    player.resources[res] = player.resources.get(res, 0) - added
    player.research[tech] = progress + added
    return {"added": added, "progress": progress + added, "cost": cost}


def research_complete(ruleset: Ruleset, player: Player, key: StatKey) -> str | None:
    """Research step: the active tech is done when its progress reaches its cost (any excess goes back to the
    stock). Its effects count from the next turn: stats are read from `player.techs`. Returns the tech or None."""
    tech = player.researching
    if tech is None or tech not in ruleset.techs or tech in player.techs:
        return None
    cost = ruleset.tech_cost(tech, key)
    progress = player.research.get(tech, 0)
    if progress < cost:
        return None
    res = ruleset.research_resource
    if res is not None and progress > cost:
        player.resources[res] = player.resources.get(res, 0) + progress - cost
    player.research.pop(tech, None)
    player.researching = None
    grant_tech(ruleset, player, tech)
    return tech


def grant_tech(ruleset: Ruleset, player: Player, tech: str) -> None:
    """A tech becomes the player's (research done, an artifact or a start effect): its `set_flag` effects apply."""
    if tech in player.techs:
        return
    player.techs.append(tech)
    player.research.pop(tech, None)
    if player.researching == tech:
        player.researching = None
    for eff in ruleset.techs[tech].effects if tech in ruleset.techs else ():
        if eff["type"] == "set_flag" and eff["flag"] not in player.flags:
            player.flags.append(eff["flag"])


# ---------------------------------------------------------------- views

def tech_status(ruleset: Ruleset, player: Player, tech: str) -> str:
    if tech in player.techs:
        return RESEARCHED
    if player.researching == tech:
        return ACTIVE
    return LOCKED if tech_missing(ruleset, player, tech) or ruleset.research_resource is None else AVAILABLE


def branch_list(ruleset: Ruleset) -> list[dict[str, Any]]:
    return [{"id": b.id, "name": b.name, "name_en": b.name_en, "order": b.order, "description": b.description,
             "color": b.color} for b in sorted(ruleset.branches.values(), key=lambda b: (b.order, b.id))]


def techs_view(state: GameState, ruleset: Ruleset, seat: str, research_income: int | None = None) -> dict[str, Any]:
    """The seat's own tech tree: every tech with its status (researched | active | available | locked), effective
    cost, progress, unmet requirements and effects (Russian text). Never shows other seats' techs."""
    from .catalog import describe_effect    # catalog imports this module's helpers

    player = state.players[seat]
    key = stat_key(state, ruleset, seat)
    res = ruleset.research_resource
    order = {b["id"]: i for i, b in enumerate(branch_list(ruleset))}
    techs = []
    for tid, t in sorted(ruleset.techs.items(), key=lambda kv: (order.get(kv[1].branch, 99), kv[1].tier, kv[0])):
        if t.factions and player.faction not in t.factions and tid not in player.techs:
            continue        # another faction's tech: not part of this seat's tree
        techs.append({
            "id": tid, "name": t.name, "name_en": t.name_en, "branch": t.branch, "tier": t.tier,
            "cost": ruleset.tech_cost(tid, key), "base_cost": t.cost, "progress": player.research.get(tid, 0),
            "status": tech_status(ruleset, player, tid), "missing": tech_missing(ruleset, player, tid),
            "requires": list(t.requires), "requires_any": list(t.requires_any), "requires_tier": t.requires_tier,
            "factions": list(t.factions), "effects": [describe_effect(ruleset, e) for e in t.effects],
            "unlocks": list(t.unlocks), "description": t.description})
    active = None
    if player.researching:
        active = {"tech": player.researching, "progress": player.research.get(player.researching, 0),
                  "cost": ruleset.tech_cost(player.researching, key)}
    return {"enabled": bool(ruleset.techs) and res is not None, "resource": res,
            "points": player.resources.get(res, 0) if res else 0, "income": research_income,
            "active": active, "researched": list(player.techs), "branches": branch_list(ruleset), "techs": techs}


def techs_text(view: Mapping[str, Any], ruleset: Ruleset) -> str:
    if not view["enabled"]:
        return "Технологий в этой партии нет."
    res = ruleset.resources[view["resource"]].name
    income = f" (+{view['income']}/ход)" if view.get("income") is not None else ""
    lines = [f"НАУКА: {res} в запасе {view['points']}{income}; изучено {len(view['researched'])}"]
    if view["active"]:
        a = view["active"]
        lines.append(f"Исследуется: {ruleset.techs[a['tech']].name} {a['progress']}/{a['cost']}")
    else:
        lines.append('Исследование не выбрано: приказ {"kind":"research","tech":"<id>"} в фазе Д')
    marks = {RESEARCHED: "✓", ACTIVE: "→", AVAILABLE: "·", LOCKED: "×"}
    branch = None
    for t in view["techs"]:
        if t["branch"] != branch:
            branch = t["branch"]
            lines.append(f"[{ruleset.branches[branch].name if branch in ruleset.branches else branch}]")
        extra = f" {t['progress']}/{t['cost']}" if t["progress"] else f" цена {t['cost']}"
        why = f" — {'; '.join(t['missing'])}" if t["status"] == LOCKED and t["missing"] else ""
        effects = f": {'; '.join(t['effects'])}" if t["effects"] else ""
        unlocks = f" (открывает: {', '.join(_unlock_name(ruleset, x) for x in t['unlocks'])})" if t["unlocks"] else ""
        lines.append(f"{marks[t['status']]} {t['id'].split(':')[-1]} «{t['name']}» ярус {t['tier']}{extra}"
                     f"{effects}{unlocks}{why}")
    return "\n".join(lines)


def _unlock_name(ruleset: Ruleset, full: str) -> str:
    entity = ruleset.units.get(full) or ruleset.buildings.get(full) or ruleset.techs.get(full)
    return entity.name if entity is not None else full.split(":")[-1]


def public_techs(ruleset: Ruleset, player: Player) -> list[str]:
    """The player's techs others may count: all but those its (hidden) artifacts granted. Researched techs and a
    faction's start techs count (factions are public); an artifact's `grant_tech` would give the pick away."""
    hidden = {e["tech"] for a in player.artifacts if a in ruleset.artifacts
              for e in ruleset.artifacts[a].effects if e["type"] == "grant_tech"}
    return [t for t in player.techs if t not in hidden]


def tech_counts(state: GameState, ruleset: Ruleset) -> dict[str, int]:
    """Public: how many techs each seat has (`public_techs`: artifact grants are not counted)."""
    return {seat: len(public_techs(ruleset, p)) for seat, p in state.players.items()}
