"""Content of the Abandoned lands (docs/ABANDONED_LANDS.md 3, 4, 11): finds, their power, the scenario's knobs.

Part of the content assembly (engine/content.py imports it at the end): the validator computes each find's `power`
against the reference army and `power_small` against small armies (decision 25: any army triggers a find, so a sweep
of one-unit scouts must not pay), checks the rarity ranges and signs, and builds the scenario's pool.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from . import registry
from .content import ArtifactDef, BuildingDef, DiscoveryDef, Issue, UnitDef, _json_path, _Raw

if TYPE_CHECKING:       # pragma: no cover
    from .content import ScenarioDef, _Builder

PROB_EPS = 1e-6


# ---------------------------------------------------------------- power (ABANDONED_LANDS.md 3, 4)

def unit_base_price(units: Mapping[str, UnitDef], unit: str) -> int:
    d = units.get(unit)
    return sum(d.cost.values()) if d is not None else 0


def cheapest_price(units: Mapping[str, UnitDef]) -> int:
    """The base price of the cheapest common unit (no `factions`, no requirements): the small army's unit."""
    prices = [sum(u.cost.values()) for u in units.values()
              if not u.factions and not u.requires_tech and not u.requires_building and sum(u.cost.values()) > 0]
    prices = prices or [sum(u.cost.values()) for u in units.values() if sum(u.cost.values()) > 0]
    return min(prices) if prices else registry.REFERENCE_ARMY[1]


def loss_count(prices: list[int], frac: float, low: int) -> int:
    """Units `lose_units {frac, min}` takes from an army with these unit prices, cheapest first: until the price taken
    reaches frac x the army's price and at least `low` units (never more than the army). The engine's rule too."""
    ordered = sorted(prices)
    total = sum(ordered)
    taken = price = 0
    for p in ordered:
        if price >= frac * total - 1e-9 and taken >= low:
            break
        taken += 1
        price += p
    return taken


def _effect_power(e: Mapping[str, Any], army: tuple[int, int], units: Mapping[str, UnitDef],
                  buildings: Mapping[str, BuildingDef], artifacts: Mapping[str, ArtifactDef]) -> float:
    """Power (P) of one effect for an army of `army` = (n units, base price each) entering alone."""
    kind = e["type"]
    n, each = army
    total = n * each
    if kind == "add_resource":
        return e["amount"] / registry.P_RESOURCES
    if kind == "grant_units":
        return e["count"] * unit_base_price(units, e["unit"]) / registry.P_PRICE
    if kind == "grant_building":
        b = buildings.get(e["building"])
        if b is None:
            return 0.0
        return max(sum(b.cost.values()) / registry.P_PRICE, 0.5 * sum(b.income.values()))
    if kind == "grant_artifact":
        a = artifacts.get(e["artifact"])
        return float(a.power) if a is not None else 0.0
    if kind == "lose_units":
        k = loss_count([each] * n, float(e["frac"]), int(e.get("min", 1)))
        return -k * each / registry.P_PRICE
    if kind == "set_status":
        per = 0.4 if e["status"] == "ravaged" else 0.6
        return -per * e["turns"]
    if kind == "spawn_neutral":
        guard = sum(c * unit_base_price(units, u) for u, c in e["units"].items())
        if total < guard:            # the army attacks the neutrals and falls; the find stays
            return -total / registry.P_PRICE
        value = -guard / registry.P_PRICE
        grow = e.get("grow")
        if grow:
            start = sum(e["units"].values())
            add = sum(grow["add"].values())
            each_add = sum(c * unit_base_price(units, u) for u, c in grow["add"].items()) / max(add, 1)
            value -= 0.5 * max(0, grow["cap"] - start) * each_add / registry.P_PRICE
        reward = sum(_effect_power(r, army, units, buildings, artifacts) for r in e.get("reward") or ())
        return value + 0.5 * reward
    return 0.0


def effects_power(effects: Iterable[Mapping[str, Any]], army: tuple[int, int], units: Mapping[str, UnitDef],
                  buildings: Mapping[str, BuildingDef], artifacts: Mapping[str, ArtifactDef]) -> float:
    return sum(_effect_power(e, army, units, buildings, artifacts) for e in effects)


def discovery_power(kind: str, effects: Iterable[Mapping[str, Any]], choices: Iterable[Mapping[str, Any]],
                    army: tuple[int, int], units: Mapping[str, UnitDef], buildings: Mapping[str, BuildingDef],
                    artifacts: Mapping[str, ArtifactDef]) -> tuple[float, float, dict[str, float]]:
    """(power, risk, {choice: EV}) of a find for an army entering alone. A boon or hindrance: the sum of its
    effects, risk 0. A mixed find: EV(choice) = sum p x power(outcome); power = the best EV (its holder decides);
    risk = the widest spread best - worst outcome over the choices."""
    if kind != "mixed":
        return round(effects_power(effects, army, units, buildings, artifacts), 2), 0.0, {}
    evs: dict[str, float] = {}
    risk = 0.0
    for c in choices:
        values = [effects_power(o["effects"], army, units, buildings, artifacts) for o in c["outcomes"]]
        evs[c["id"]] = round(sum(o["p"] * v for o, v in zip(c["outcomes"], values)), 2)
        risk = max(risk, max(values) - min(values)) if values else risk
    return (max(evs.values()) if evs else 0.0), round(risk, 2), evs


# ---------------------------------------------------------------- finds

def _choices(b: _Builder, raw: _Raw, items: list[Any]) -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for i, c in enumerate(items):
        cid = c["id"]
        if cid in seen:
            b.err(raw, ["choices", i, "id"], f"choice {cid!r} given twice")
            continue
        seen.add(cid)
        label = c.get("label", cid)
        if "outcomes" in c and "effects" in c:
            b.err(raw, ["choices", i], "a choice has either outcomes or effects, not both")
            continue
        given = c["outcomes"] if "outcomes" in c else [{"p": 1.0, "effects": c.get("effects") or []}]
        outcomes = []
        for j, o in enumerate(given):
            where = f"choices.{i}.outcomes.{j}.effects" if "outcomes" in c else f"choices.{i}.effects"
            effects = b.effects(raw, list(o.get("effects") or []), where, "discovery_choice")
            outcomes.append({"p": float(o["p"]), "effects": effects})
        total = sum(o["p"] for o in outcomes)
        if abs(total - 1.0) > PROB_EPS:
            b.err(raw, ["choices", i, "outcomes"], f"choice {cid!r}: the shares p sum to {total:g}, not 1")
        out.append({"id": cid, "label": b.name({"name": label}), "outcomes": outcomes})
    return out


def _artifact_problems(b: _Builder, raw: _Raw, effects: Iterable[Mapping[str, Any]], where: list[Any],
                       artifacts: Mapping[str, ArtifactDef]) -> None:
    """A find gives only an ordinary artifact whose effects are all passive (modify, income): ABANDONED_LANDS.md 3."""
    for i, e in enumerate(effects):
        if e["type"] != "grant_artifact":
            continue
        a = artifacts.get(e["artifact"])
        if a is None:
            continue
        if a.factions:
            b.err(raw, [*where, i, "artifact"], f"artifact {a.id} is unique to a faction: a find cannot give it")
        bad = sorted({x["type"] for x in a.effects} - {"modify", "income"})
        if bad:
            b.err(raw, [*where, i, "artifact"], f"artifact {a.id} has non-passive effects ({', '.join(bad)}): "
                                                "a find gives only artifacts with modify/income effects")


def _refs(b: _Builder, raw: _Raw, effects: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Qualify the ids the generic effect check does not know: an artifact, the units of a spawn and its growth, the
    effects of a reward."""
    parts = key.split(".")
    out = []
    for i, e in enumerate(effects):
        e = dict(e)
        ok = True
        if e["type"] == "grant_artifact":
            e["artifact"] = b.ref(raw, "artifact", e["artifact"], [*parts, i, "artifact"])
            ok = e["artifact"] is not None
        if e["type"] == "spawn_neutral":
            spawned = {}
            for u, n in e["units"].items():
                full = b.ref(raw, "unit", str(u), [*parts, i, "units", u])
                ok = ok and full is not None
                if full:
                    spawned[full] = n
            e["units"] = dict(sorted(spawned.items()))
            if e.get("grow"):
                add = {}
                for u, n in e["grow"]["add"].items():
                    full = b.ref(raw, "unit", str(u), [*parts, i, "grow", "add", u])
                    ok = ok and full is not None
                    if full:
                        add[full] = n
                e["grow"] = {"every": e["grow"]["every"], "add": dict(sorted(add.items())), "cap": e["grow"]["cap"]}
            if e.get("reward"):
                e["reward"] = b.effects(raw, list(e["reward"]), f"{key}.{i}.reward", "discovery_reward")
        if ok:
            out.append(e)
    return out


def build_discoveries(b: _Builder, units: Mapping[str, UnitDef], buildings: Mapping[str, BuildingDef],
                      artifacts: Mapping[str, ArtifactDef]) -> dict[str, DiscoveryDef]:
    out: dict[str, DiscoveryDef] = {}
    cheap = cheapest_price(units)
    for fid, raw in sorted(b.l.entities["discovery"].items()):
        d = raw.data
        b.texts(raw)
        kind, rarity = d["kind"], d["rarity"]
        before = len(b.errors)
        if rarity not in registry.DISCOVERY_RARITIES[kind]:
            b.err(raw, ["rarity"], f"a {kind} has rarity {', '.join(registry.DISCOVERY_RARITIES[kind])}, "
                                   f"got {rarity!r}")
            continue
        triggers = tuple(d.get("triggers") or ("army",))
        if "shade" in triggers:
            b.warn(raw, ["triggers"], "trigger 'shade' is ignored in this stage: a Shade never triggers a find")
        effects: list[dict[str, Any]] = []
        choices: list[dict[str, Any]] = []
        default = d.get("default_choice")
        if kind == "mixed":
            if "effects" in d or not d.get("choices"):
                b.err(raw, ["choices"], "a mixed find has choices (and no effects)")
                continue
            choices = _choices(b, raw, d["choices"])
            for i, c in enumerate(choices):
                for j, o in enumerate(c["outcomes"]):
                    o["effects"] = _refs(b, raw, o["effects"], f"choices.{i}.outcomes.{j}.effects")
                    _artifact_problems(b, raw, o["effects"], ["choices", i, "outcomes", j, "effects"], artifacts)
            if default not in {c["id"] for c in choices}:
                b.err(raw, ["default_choice"], f"default_choice {default!r} is not one of the choices")
        else:
            if "choices" in d or "default_choice" in d:
                b.err(raw, ["choices"], f"a {kind} has effects, not choices")
                continue
            effects = _refs(b, raw, b.effects(raw, list(d.get("effects") or []), "effects", "discovery"), "effects")
            _artifact_problems(b, raw, effects, ["effects"], artifacts)
            if not effects:
                b.err(raw, ["effects"], f"a {kind} needs effects")
        players = d.get("players") or {}
        if players.get("min") is not None and players.get("max") is not None and players["min"] > players["max"]:
            b.err(raw, ["players"], f"players min {players['min']} is above max {players['max']}")
        if len(b.errors) > before:
            continue
        ref = registry.REFERENCE_ARMY
        power, risk, evs = discovery_power(kind, effects, choices, ref, units, buildings, artifacts)
        small = tuple(discovery_power(kind, effects, choices, (n, cheap), units, buildings, artifacts)[0]
                      for n in registry.SMALL_ARMIES)
        lo, hi, weight = registry.DISCOVERY_RARITY[f"{kind}:{rarity}"]
        if kind == "boon" and power <= 0:
            b.err(raw, ["effects"], f"a boon must have power > 0, its effects weigh {power:g}")
        elif kind == "hindrance" and power >= 0:
            b.err(raw, ["effects"], f"a hindrance must have power < 0, its effects weigh {power:g}")
        elif not lo <= power < hi:
            b.err(raw, ["rarity"], f"power {power:g} is out of the range of {kind} {rarity} [{lo:g}, {hi:g})")
        if kind == "mixed" and default in evs:
            risks = {c["id"]: _choice_risk(c, ref, units, buildings, artifacts) for c in choices}
            if not any(v > evs[default] for v in evs.values()):
                b.warn(raw, ["default_choice"], "risk lever: no choice has a higher EV than the default one")
            if risks[default] > min(risks.values()):
                b.warn(raw, ["default_choice"], "risk lever: the default choice should be the one with the least "
                                                "risk")
        out[fid] = DiscoveryDef(fid, b.name(d), b.name_en(d), kind, rarity, int(d.get("weight", weight)), triggers,
                                tuple(effects), tuple(choices), default, players.get("min"), players.get("max"),
                                d.get("requires_system"), power, risk, evs, small, d.get("description", ""),
                                b.lore(raw))
    return out


def _choice_risk(c: Mapping[str, Any], army: tuple[int, int], units: Mapping[str, UnitDef],
                 buildings: Mapping[str, BuildingDef], artifacts: Mapping[str, ArtifactDef]) -> float:
    values = [effects_power(o["effects"], army, units, buildings, artifacts) for o in c["outcomes"]]
    return max(values) - min(values) if values else 0.0


def choice_risk(c: Mapping[str, Any], units: Mapping[str, UnitDef], buildings: Mapping[str, BuildingDef],
                artifacts: Mapping[str, ArtifactDef]) -> float:
    """The risk of one choice of a mixed find (best - worst outcome against the reference army), as the validator
    reckons it; `get_state` JSON shows it next to the EV (bots pick by it, ABANDONED_LANDS.md 14.1)."""
    return round(_choice_risk(c, registry.REFERENCE_ARMY, units, buildings, artifacts), 2)


# ---------------------------------------------------------------- the scenario's knobs (ABANDONED_LANDS.md 11)

RING_TOKENS: dict[str, tuple[str, ...]] = {
    "common": ("boon:common",), "uncommon": ("boon:uncommon",), "rare": ("boon:rare",),
    "boon": ("boon:common", "boon:uncommon", "boon:rare"),
    "minor": ("hindrance:minor",), "major": ("hindrance:major",),
    "hindrance": ("hindrance:minor", "hindrance:major"),
    "mixed": ("mixed:common", "mixed:uncommon", "mixed:rare"),
    "mixed_common": ("mixed:common",), "mixed_uncommon": ("mixed:uncommon",), "mixed_rare": ("mixed:rare",),
}
KNOBS = ("souls", "ravaged_turns", "ruined_turns", "income_mul", "garrison", "density", "extra", "rings",
         "ruins_find", "max_ring", "w", "fair_tolerance", "tries", "observe_mirrors", "fair_mirror", "pool")


def _ring_key(value: Any) -> int | None:
    try:
        ring = int(value)
    except (TypeError, ValueError):
        return None
    return ring if ring >= 1 else None


def discovery_config(b: _Builder, raw: _Raw, given: Mapping[str, Any], where: list[Any]) -> dict[str, Any]:
    """Normalized knobs: every key of registry.DISCOVERY_DEFAULTS plus ravaged_turns, ruined_turns, income_mul (from
    the `souls` preset unless given), garrison {units, reward} with qualified ids, rings and w with string keys."""
    def err(path: list[Any], msg: str) -> None:
        b.errors.append(Issue(raw.file, _json_path(raw.path, [*where, *path]), msg))

    unknown = sorted(set(given) - set(KNOBS))
    for k in unknown:
        err([k], f"unknown discoveries knob {k!r}; known: {', '.join(KNOBS)}")
    cfg: dict[str, Any] = {k: v for k, v in registry.DISCOVERY_DEFAULTS.items()}
    souls = given.get("souls", registry.DEFAULT_SOULS)
    if souls not in registry.SOULS_VARIANTS:
        err(["souls"], f"souls must be one of {', '.join(registry.SOULS_VARIANTS)}, got {souls!r}")
        souls = registry.DEFAULT_SOULS
    cfg["souls"] = souls
    cfg.update(registry.SOULS_VARIANTS[souls])
    for k, kind, lo, hi in (("ravaged_turns", int, 0, 20), ("ruined_turns", int, 0, 20), ("income_mul", float, 0.0, 1.0),
                            ("density", float, 0.0, 1.0), ("extra", int, 0, 20), ("max_ring", int, 1, 20),
                            ("fair_tolerance", float, 0.0, 100.0), ("tries", int, 1, 1000),
                            ("observe_mirrors", int, 0, 5)):
        if k not in given:
            continue
        v = given[k]
        ok = not isinstance(v, bool) and isinstance(v, (int, float) if kind is float else int) and lo <= v <= hi
        if not ok:
            err([k], f"{k} must be {'a number' if kind is float else 'an integer'} in {lo}..{hi}, got {v!r}")
            continue
        cfg[k] = float(v) if kind is float else int(v)
    for k in ("ruins_find", "fair_mirror"):
        if k in given:
            if not isinstance(given[k], bool):
                err([k], f"{k} must be true or false")
            else:
                cfg[k] = given[k]
    rings = given.get("rings", registry.DISCOVERY_DEFAULTS["rings"])
    norm_rings: dict[str, tuple[str, ...]] = {}
    if not isinstance(rings, Mapping) or not rings:
        err(["rings"], "rings is {ring: [rarities]}")
        rings = {}
    for key, items in rings.items():
        ring = _ring_key(key)
        if ring is None or ring < 2:
            err(["rings", key], f"a ring is an integer >= 2 (ring 1 stays empty), got {key!r}")
            continue
        allowed: list[str] = []
        for i, token in enumerate(items or ()):
            full = RING_TOKENS.get(token, (token,) if token in registry.DISCOVERY_RARITY else None)
            if full is None:
                err(["rings", key, i], f"unknown rarity {token!r}; known: {', '.join(RING_TOKENS)}")
                continue
            allowed += [x for x in full if x not in allowed]
        norm_rings[str(ring)] = tuple(allowed)
    cfg["rings"] = dict(sorted(norm_rings.items(), key=lambda kv: int(kv[0])))
    w = given.get("w", registry.DISCOVERY_DEFAULTS["w"])
    norm_w: dict[str, float] = {}
    for key, v in (w.items() if isinstance(w, Mapping) else ()):
        ring = _ring_key(key)
        if ring is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 5:
            err(["w", key], f"w is {{ring: weight 0..5}}, got {key!r}: {v!r}")
            continue
        norm_w[str(ring)] = float(v)
    cfg["w"] = dict(sorted(norm_w.items(), key=lambda kv: int(kv[0])))
    garrison = given.get("garrison") or {}
    units: dict[str, int] = {}
    for u, n in (garrison.get("units") or {}).items():
        full = b.ref(raw, "unit", str(u), [*where, "garrison", "units", u])
        if full is not None:
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 50:
                err(["garrison", "units", u], f"a garrison count is 1..50, got {n!r}")
            else:
                units[full] = n
    reward = b.effects(raw, list(garrison.get("reward") or []), ".".join(map(str, [*where, "garrison", "reward"])),
                       "discovery_reward")
    cfg["garrison"] = {"units": dict(sorted(units.items())), "reward": reward}
    pool = given.get("pool") or ()
    cfg["pool"] = tuple(r for r in (b.ref(raw, "discovery", str(x), [*where, "pool", i]) for i, x in enumerate(pool))
                        if r)
    return cfg


def discovery_pool(b: _Builder, raw: _Raw, scenario: ScenarioDef, finds: Mapping[str, DiscoveryDef],
                   off: set[str]) -> dict[str, DiscoveryDef]:
    """The scenario's finds (its `pool`, else all), without those of a system that is off; warns when a sweep of
    small armies would pay (decision 25: the weighted mean power against 1 or 2 cheap units must be below 0)."""
    cfg = scenario.discoveries or {}
    names = cfg.get("pool") or tuple(sorted(finds))
    pool = {fid: finds[fid] for fid in names if fid in finds and finds[fid].requires_system not in off}
    sets: dict[str, list[DiscoveryDef]] = {"pool": list(pool.values())}
    for ring, allowed in (cfg.get("rings") or {}).items():
        sets[f"ring {ring}"] = [d for d in pool.values() if f"{d.kind}:{d.rarity}" in allowed]
    bad = []
    for label, items in sets.items():
        evs = scout_ev(items)
        paying = [f"{n} unit(s) {ev:+.2f}" for n, ev in zip(registry.SMALL_ARMIES, evs) if ev >= 0]
        if paying:
            bad.append(f"{label}: {', '.join(paying)}")
    if bad:
        b.warnings.append(Issue(raw.file, _json_path(raw.path, ["discoveries", "pool"]),
                                "scout sweep pays (mean P per find for small armies; hindrances must outweigh "
                                f"boons for them, decision 25): {'; '.join(bad)}"))
    return dict(sorted(pool.items()))


def scout_ev(items: Iterable[DiscoveryDef]) -> tuple[float, ...]:
    """The weighted mean power of finds against each small army (registry.SMALL_ARMIES): a sweep of scouts into
    every find of this set gets this per find; decision 25 wants it below 0."""
    items = list(items)
    total = sum(d.weight for d in items)
    if not total:
        return tuple(0.0 for _ in registry.SMALL_ARMIES)
    return tuple(round(sum(d.weight * d.power_small[i] for d in items) / total, 3)
                 for i in range(len(registry.SMALL_ARMIES)))


# ---------------------------------------------------------------- map rotation (ABANDONED_LANDS.md 5.6)

def map_rotation(b: _Builder, raw: _Raw, given: Mapping[str, str] | None, adjacency: Mapping[str, tuple[str, ...]],
                 starts: list[Any]) -> dict[str, str] | None:
    """The map's optional `rotation`: a permutation of all provinces that keeps the edges and takes the capital of
    start i to the capital of start i+1 (cyclically). Errors otherwise."""
    if not given:
        return None
    rot = {str(k): str(v) for k, v in given.items()}
    provinces = set(adjacency)
    if set(rot) != provinces or set(rot.values()) != provinces:
        b.err(raw, ["rotation"], "rotation must be a permutation of all provinces of the map")
        return None
    for a, ns in adjacency.items():
        if {rot[n] for n in ns} != set(adjacency[rot[a]]):
            b.err(raw, ["rotation"], f"rotation does not keep the edges of {a}")
            return None
    caps = [s.capital for s in starts]
    for i, c in enumerate(caps):
        if rot[c] != caps[(i + 1) % len(caps)]:
            b.err(raw, ["rotation"], f"rotation must take start {i + 1}'s capital {c} to the next start's capital")
            return None
    return dict(sorted(rot.items()))


def round_up(x: float) -> int:
    return int(math.ceil(x - 1e-9))
