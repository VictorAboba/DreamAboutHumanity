"""Content packs: loading, validation (schema -> refs -> roles -> map graph -> bounds) and Ruleset assembly."""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource

from . import registry
from .model import sha256_json

ENGINE_API = 1
SCHEMA_DIR = Path(__file__).parent / "schemas"
DEFAULT_PACKS_DIR = Path(__file__).resolve().parents[3] / "content" / "packs"

# directory name -> schema / entity kind
ENTITY_DIRS: dict[str, str] = {
    "resources": "resource",
    "province_types": "province_type",
    "units": "unit",
    "buildings": "building",
    "factions": "faction",
    "maps": "map",
    "scenarios": "scenario",
}

DEFAULT_MESSAGE_LIMITS: dict[str, Any] = {
    "negotiation_1": {"per_recipient": 6, "private": 20, "public": 2},
    "negotiation_2": {"per_recipient": 10, "private": 10, "public": 1},
    "max_chars": 800,
}

_SUSPICIOUS = re.compile(r"ignore|игнорируй|system|токен|https?://|www\.", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


# ---------------------------------------------------------------- errors

@dataclass(frozen=True)
class Issue:
    file: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.file}: {self.path or '<root>'}: {self.message}"


class ContentError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues[:10]))


# ---------------------------------------------------------------- ruleset types

@dataclass(frozen=True)
class ResourceDef:
    id: str
    name: str
    short: str
    role: str


@dataclass(frozen=True)
class ProvinceTypeDef:
    id: str
    name: str
    short: str
    income: dict[str, int]
    defense_mul: float


@dataclass(frozen=True)
class UnitDef:
    id: str
    name: str
    short: str
    attack: int
    defense: int
    cost: dict[str, int]
    upkeep: dict[str, int]
    counters: tuple[str, ...]


@dataclass(frozen=True)
class BuildingDef:
    id: str
    name: str
    short: str
    cost: dict[str, int]
    build_turns: int
    income: dict[str, int]
    recruit: bool
    max_per_province: int


@dataclass(frozen=True)
class FactionDef:
    id: str
    name: str
    lore: str
    income: dict[str, int]                 # passive flat income from bonuses/penalties
    start_resources: dict[str, int]
    start_units: dict[str, int]
    start_buildings: tuple[str, ...]
    start_effects: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class MapProvince:
    id: str
    name: str
    type: str


@dataclass(frozen=True)
class StartDef:
    capital: str
    provinces: tuple[str, ...]


@dataclass(frozen=True)
class MapDef:
    id: str
    name: str
    max_players: int
    provinces: dict[str, MapProvince]
    adjacency: dict[str, tuple[str, ...]]
    starts: tuple[StartDef, ...]

    @property
    def capitals(self) -> frozenset[str]:
        return frozenset(s.capital for s in self.starts)

    def adjacent(self, a: str, b: str) -> bool:
        return b in self.adjacency.get(a, ())


@dataclass(frozen=True)
class ScenarioDef:
    id: str
    name: str
    map: str
    factions: tuple[str, ...]
    turn_limit: int
    phase_limits: dict[str, int]           # seconds per phase
    message_limits: dict[str, Any]


@dataclass(frozen=True)
class Ruleset:
    packs: dict[str, str]                  # pack id -> version
    resources: dict[str, ResourceDef]
    roles: dict[str, str]                  # role -> resource id
    province_types: dict[str, ProvinceTypeDef]
    units: dict[str, UnitDef]
    buildings: dict[str, BuildingDef]
    factions: dict[str, FactionDef]
    map: MapDef
    scenario: ScenarioDef
    stats: dict[str, float]                # global effective stats (scenario overrides applied)
    faction_stats: dict[str, dict[str, float]]
    warnings: tuple[Issue, ...] = ()
    hash: str = ""

    def stat(self, path: str, faction: str | None = None) -> float:
        table = self.faction_stats.get(faction, self.stats) if faction else self.stats
        if path in table:
            return table[path]
        spec = registry.stat_spec(path)
        if spec is None:
            raise KeyError(f"unknown stat {path!r}")
        return spec.default if spec.default is not None else 0

    def _amounts(self, prefix: str, faction: str | None) -> dict[str, int]:
        out = {r: int(self.stat(f"{prefix}.{r}", faction)) for r in self.resources}
        return {r: v for r, v in out.items() if v}

    def unit_cost(self, unit: str, faction: str | None = None) -> dict[str, int]:
        return self._amounts(f"unit.{unit}.cost", faction)

    def unit_upkeep(self, unit: str, faction: str | None = None) -> dict[str, int]:
        return self._amounts(f"unit.{unit}.upkeep", faction)

    def building_cost(self, building: str, faction: str | None = None) -> dict[str, int]:
        return self._amounts(f"building.{building}.cost", faction)

    def resolve(self, kind: str, ref: str) -> str | None:
        """Full id for a short or full reference to a unit/building/resource/province_type/faction."""
        table: Mapping[str, Any] = getattr(self, {"unit": "units", "building": "buildings",
                                                  "resource": "resources", "province_type": "province_types",
                                                  "faction": "factions"}[kind])
        if not isinstance(ref, str):
            return None
        if ref in table:
            return ref
        matches = [k for k in table if k.split(":", 1)[1] == ref]
        return matches[0] if len(matches) == 1 else None

    def short(self, full_id: str) -> str:
        for table in (self.units, self.buildings, self.resources, self.province_types):
            if full_id in table:
                return table[full_id].short
        return full_id.split(":", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("warnings")
        data.pop("hash")
        return data


# ---------------------------------------------------------------- loading

def _load_schemas() -> dict[str, Draft202012Validator]:
    raw = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in SCHEMA_DIR.glob("*.schema.json")}
    reg: Registry = Registry()
    for schema in raw.values():
        reg = reg.with_resource(schema["$id"], SchemaResource.from_contents(schema))
    return {name.removesuffix(".schema.json"): Draft202012Validator(schema, registry=reg)
            for name, schema in raw.items() if name != "common.schema.json"}


_VALIDATORS = _load_schemas()


@dataclass
class _Raw:
    kind: str
    pack: str
    id: str                    # full id
    data: dict[str, Any]
    file: str
    path: str                  # path prefix inside the file ("" or "[i]")


@dataclass
class _Loaded:
    packs: dict[str, str] = field(default_factory=dict)
    entities: dict[str, dict[str, _Raw]] = field(default_factory=lambda: {k: {} for k in ENTITY_DIRS.values()})
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    requires: list[tuple[list[str], str]] = field(default_factory=list)   # checked once all packs are loaded


def _json_path(prefix: str, parts: Iterable[Any]) -> str:
    out = prefix
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def _schema_issues(kind: str, data: Any, file: str, prefix: str) -> list[Issue]:
    return [Issue(file, _json_path(prefix, err.absolute_path), err.message)
            for err in sorted(_VALIDATORS[kind].iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))]


def _read_yaml(path: Path, rel: str, out: _Loaded) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        out.errors.append(Issue(rel, "", f"cannot read YAML: {exc}"))
        return None


def _load_pack(pack_dir: Path, out: _Loaded) -> None:
    meta_file = pack_dir / "pack.yaml"
    rel_root = pack_dir.name
    if not meta_file.is_file():
        out.errors.append(Issue(f"{rel_root}/pack.yaml", "", "pack.yaml not found"))
        return
    meta = _read_yaml(meta_file, f"{rel_root}/pack.yaml", out)
    issues = _schema_issues("pack", meta, f"{rel_root}/pack.yaml", "")
    if issues:
        out.errors.extend(issues)
        return
    pack_id = meta["id"]
    if pack_id in out.packs:
        out.errors.append(Issue(f"{rel_root}/pack.yaml", "id", f"duplicate pack {pack_id!r}"))
        return
    if int(meta["engine_api"][1:]) != ENGINE_API:
        out.errors.append(Issue(f"{rel_root}/pack.yaml", "engine_api",
                                f"pack needs engine API {meta['engine_api']}, engine is {ENGINE_API}"))
    out.packs[pack_id] = meta["version"]
    for dirname, kind in ENTITY_DIRS.items():
        for path in sorted((pack_dir / dirname).glob("*.yaml")):
            rel = f"{rel_root}/{dirname}/{path.name}"
            doc = _read_yaml(path, rel, out)
            items = doc if isinstance(doc, list) else [doc]
            for i, item in enumerate(items):
                prefix = f"[{i}]" if isinstance(doc, list) else ""
                issues = _schema_issues(kind, item, rel, prefix)
                if issues:
                    out.errors.extend(issues)
                    continue
                full = f"{pack_id}:{item['id']}"
                if full in out.entities[kind]:
                    out.errors.append(Issue(rel, _json_path(prefix, ["id"]), f"duplicate {kind} id {full!r}"))
                    continue
                out.entities[kind][full] = _Raw(kind, pack_id, full, item, rel, prefix)
    out.requires.append((meta.get("requires", []), f"{rel_root}/pack.yaml"))


def _load_all(pack_dirs: Iterable[str | Path]) -> _Loaded:
    out = _Loaded()
    for d in pack_dirs:
        _load_pack(Path(d), out)
    for requires, file in out.requires:
        for i, req in enumerate(requires):
            if req not in out.packs:
                out.errors.append(Issue(file, f"requires[{i}]", f"required pack {req!r} is not loaded"))
    return out


# ---------------------------------------------------------------- assembly

class _Builder:
    def __init__(self, loaded: _Loaded):
        self.l = loaded
        self.errors = loaded.errors
        self.warnings = loaded.warnings

    def err(self, raw: _Raw, parts: Iterable[Any], message: str) -> None:
        self.errors.append(Issue(raw.file, _json_path(raw.path, parts), message))

    def warn(self, raw: _Raw, parts: Iterable[Any], message: str) -> None:
        self.warnings.append(Issue(raw.file, _json_path(raw.path, parts), message))

    def ref(self, raw: _Raw, kind: str, value: str, parts: Iterable[Any]) -> str | None:
        full = value if ":" in value else f"{raw.pack}:{value}"
        if full not in self.l.entities[kind]:
            self.err(raw, parts, f"unknown {kind} {value!r}")
            return None
        return full

    def amounts(self, raw: _Raw, data: Mapping[str, int], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for res, n in sorted(data.items()):
            full = self.ref(raw, "resource", res, [key, res])
            if full:
                out[full] = n
        return out

    def texts(self, raw: _Raw) -> None:
        for key in ("name", "lore"):
            text = raw.data.get(key)
            if isinstance(text, str) and _SUSPICIOUS.search(text):
                self.warn(raw, [key], "text looks like an instruction to agents")

    def target(self, raw: _Raw, target: str, parts: list[Any]) -> str | None:
        """Qualify entity ids inside a stat path: ``unit.assault.attack`` -> ``unit.core:assault.attack``."""
        segs = target.split(".")
        kinds = {"unit": "unit", "building": "building", "resource": "resource"}
        if segs[0] in kinds and len(segs) >= 2:
            full = self.ref(raw, kinds[segs[0]], segs[1], parts)
            if full is None:
                return None
            segs[1] = full
            if len(segs) == 4 and segs[2] in ("cost", "upkeep"):
                res = self.ref(raw, "resource", segs[3], parts)
                if res is None:
                    return None
                segs[3] = res
        path = ".".join(segs)
        if registry.stat_spec(path) is None:
            self.err(raw, parts, f"unknown stat {target!r}")
            return None
        return path

    def effects(self, raw: _Raw, effects: list[dict[str, Any]], key: str, context: str) -> list[dict[str, Any]]:
        out = []
        for i, eff in enumerate(effects):
            problems = registry.effect_problems(eff, context)
            for p in problems:
                self.err(raw, [key, i], p)
            if problems:
                continue
            eff = dict(eff)
            ok = True
            if eff["type"] == "modify":
                eff["target"] = self.target(raw, eff["target"], [key, i, "target"])
                ok = eff["target"] is not None
            for field_name, kind in (("resource", "resource"), ("unit", "unit"), ("building", "building")):
                if field_name in eff:
                    eff[field_name] = self.ref(raw, kind, eff[field_name], [key, i, field_name])
                    ok = ok and eff[field_name] is not None
            if ok:
                out.append(eff)
        return out

    # -- entity builders

    def resources(self) -> dict[str, ResourceDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["resource"].items()):
            d = raw.data
            self.texts(raw)
            out[fid] = ResourceDef(fid, d["name"], d.get("short", d["name"]), d["role"])
        return out

    def province_types(self) -> dict[str, ProvinceTypeDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["province_type"].items()):
            d = raw.data
            self.texts(raw)
            out[fid] = ProvinceTypeDef(fid, d["name"], d.get("short", d["name"]),
                                       self.amounts(raw, d["income"], "income"), float(d.get("defense_mul", 1.0)))
        return out

    def units(self) -> dict[str, UnitDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["unit"].items()):
            d = raw.data
            self.texts(raw)
            counters = [self.ref(raw, "unit", c, ["counters", i]) for i, c in enumerate(d.get("counters", []))]
            out[fid] = UnitDef(fid, d["name"], d.get("short", d["name"]), d["attack"], d["defense"],
                               self.amounts(raw, d["cost"], "cost"), self.amounts(raw, d["upkeep"], "upkeep"),
                               tuple(sorted(c for c in counters if c)))
        return out

    def buildings(self) -> dict[str, BuildingDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["building"].items()):
            d = raw.data
            self.texts(raw)
            income: Counter[str] = Counter()
            for eff in self.effects(raw, d.get("effects", []), "effects", "building"):
                income[eff["resource"]] += eff["amount"]
            out[fid] = BuildingDef(fid, d["name"], d.get("short", d["name"]), self.amounts(raw, d["cost"], "cost"),
                                   d["build_turns"], dict(sorted(income.items())), bool(d.get("recruit", False)),
                                   d.get("max_per_province", 1))
        return out

    def factions(self) -> tuple[dict[str, FactionDef], dict[str, list[dict[str, Any]]]]:
        out, modifiers = {}, {}
        for fid, raw in sorted(self.l.entities["faction"].items()):
            d = raw.data
            self.texts(raw)
            passive = (self.effects(raw, d.get("bonuses", []), "bonuses", "passive")
                       + self.effects(raw, d.get("penalties", []), "penalties", "passive"))
            income: Counter[str] = Counter()
            for eff in passive:
                if eff["type"] == "income":
                    income[eff["resource"]] += eff["amount"]
            modifiers[fid] = [e for e in passive if e["type"] == "modify"]
            st = d["starting"]
            units = {}
            for u, n in sorted(st.get("units", {}).items()):
                full = self.ref(raw, "unit", u, ["starting", "units", u])
                if full:
                    units[full] = n
            buildings = [self.ref(raw, "building", b, ["starting", "buildings", i])
                         for i, b in enumerate(st.get("buildings", []))]
            out[fid] = FactionDef(
                fid, d["name"], d.get("lore", ""), dict(sorted(income.items())),
                self.amounts(raw, st.get("resources", {}), "starting.resources"), units,
                tuple(b for b in buildings if b),
                tuple(self.effects(raw, st.get("effects", []), "starting.effects", "on_game_start")))
        return out, modifiers

    def maps(self) -> dict[str, MapDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["map"].items()):
            m = self.map(raw)
            if m:
                out[fid] = m
        return out

    def map(self, raw: _Raw) -> MapDef | None:
        d = raw.data
        self.texts(raw)
        before = len(self.errors)
        provinces: dict[str, MapProvince] = {}
        for i, p in enumerate(d["provinces"]):
            self.texts(_Raw(raw.kind, raw.pack, raw.id, p, raw.file, _json_path(raw.path, ["provinces", i])))
            if p["id"] in provinces:
                self.err(raw, ["provinces", i, "id"], f"duplicate province {p['id']!r}")
            ptype = self.ref(raw, "province_type", p["type"], ["provinces", i, "type"])
            provinces[p["id"]] = MapProvince(p["id"], p["name"], ptype or p["type"])
        adj: dict[str, set[str]] = {pid: set() for pid in provinces}
        for i, (a, b) in enumerate(d["edges"]):
            bad = [x for x in (a, b) if x not in provinces]
            if bad:
                self.err(raw, ["edges", i], f"unknown province {bad[0]!r}")
            elif a == b:
                self.err(raw, ["edges", i], "edge connects a province to itself")
            else:
                adj[a].add(b)
                adj[b].add(a)
        if provinces and len(_reachable(adj, next(iter(sorted(provinces))), len(provinces))) != len(provinces):
            self.err(raw, ["edges"], "map graph is not connected")
        starts = []
        claimed: set[str] = set()
        for i, s in enumerate(d["starts"]):
            own = [s["capital"], *s.get("provinces", [])]
            for j, pid in enumerate(own):
                where = ["starts", i, "capital"] if j == 0 else ["starts", i, "provinces", j - 1]
                if pid not in provinces:
                    self.err(raw, where, f"unknown province {pid!r}")
                elif pid in claimed:
                    self.err(raw, where, f"province {pid!r} is in two starts")
                claimed.add(pid)
            starts.append(StartDef(s["capital"], tuple(s.get("provinces", []))))
        if len(starts) != d["max_players"]:
            self.err(raw, ["starts"], f"{len(starts)} starts for max_players {d['max_players']}")
        if len(self.errors) > before:
            return None
        adjacency = {pid: tuple(sorted(n)) for pid, n in sorted(adj.items())}
        profiles = [Counter(provinces[p].type for p in _reachable(adj, s.capital, 2)) for s in starts]
        if any(p != profiles[0] for p in profiles):
            self.err(raw, ["starts"], "starts are not symmetric: province types within radius 2 differ")
            return None
        return MapDef(raw.id, d["name"], d["max_players"], dict(sorted(provinces.items())), adjacency, tuple(starts))

    def scenarios(self) -> dict[str, tuple[_Raw, ScenarioDef]]:
        out = {}
        for fid, raw in sorted(self.l.entities["scenario"].items()):
            d = raw.data
            self.texts(raw)
            map_id = self.ref(raw, "map", d["map"], ["map"])
            factions = [self.ref(raw, "faction", f, ["factions", i]) for i, f in enumerate(d["factions"])]
            limits = {k: _merge(DEFAULT_MESSAGE_LIMITS[k], v) if isinstance(v, dict) else v
                      for k, v in _merge(DEFAULT_MESSAGE_LIMITS, d.get("message_limits", {})).items()}
            out[fid] = (raw, ScenarioDef(
                fid, d["name"], map_id or d["map"], tuple(f for f in factions if f), d["turn_limit"],
                {k: _duration(v) for k, v in d["phase_limits"].items()}, limits))
        return out

    def roles(self, resources: Mapping[str, ResourceDef]) -> dict[str, str]:
        by_role: dict[str, list[str]] = {r: [] for r in registry.RESOURCE_ROLES}
        for res in resources.values():
            by_role[res.role].append(res.id)
        for role, ids in by_role.items():
            if len(ids) != 1:
                self.errors.append(Issue("<ruleset>", f"roles.{role}",
                                         f"role {role!r} needs exactly one resource, found {ids or 'none'}"))
        return {role: ids[0] for role, ids in by_role.items() if len(ids) == 1}

    def base_stats(self, resources, units, buildings) -> dict[str, float]:
        stats: dict[str, float] = dict(registry.global_defaults())
        for r in resources:
            stats[f"resource.{r}.income_mul"] = 1.0
        for u in units.values():
            raw = self.l.entities["unit"][u.id]
            stats[f"unit.{u.id}.attack"] = u.attack
            stats[f"unit.{u.id}.defense"] = u.defense
            for res, n in u.cost.items():
                stats[f"unit.{u.id}.cost.{res}"] = n
            for res, n in u.upkeep.items():
                stats[f"unit.{u.id}.upkeep.{res}"] = n
            self.bounds(raw, stats, u.id, "unit", {"attack": u.attack, "defense": u.defense,
                                                   **{f"cost.{k}": v for k, v in u.cost.items()},
                                                   **{f"upkeep.{k}": v for k, v in u.upkeep.items()}})
        for b in buildings.values():
            raw = self.l.entities["building"][b.id]
            for res, n in b.cost.items():
                stats[f"building.{b.id}.cost.{res}"] = n
            self.bounds(raw, stats, b.id, "building", {f"cost.{k}": v for k, v in b.cost.items()})
        return stats

    def bounds(self, raw: _Raw, stats, fid: str, kind: str, values: Mapping[str, int]) -> None:
        for key, value in values.items():
            spec = registry.stat_spec(f"{kind}.{fid}.{key}")
            if spec and not spec.in_bounds(value):
                field_path = key.split(".")
                if len(field_path) == 2:
                    field_path[1] = field_path[1].split(":", 1)[1]
                self.err(raw, field_path, f"value {value} out of bounds {spec.lo}..{spec.hi}")

    def effective(self, base: Mapping[str, float], modifiers: list[dict[str, Any]], where: str) -> dict[str, float]:
        by_target: dict[str, list[dict[str, Any]]] = {}
        for m in modifiers:
            by_target.setdefault(m["target"], []).append(m)
        out = dict(base)
        for target, mods in sorted(by_target.items()):
            spec = registry.stat_spec(target)
            assert spec is not None
            start = base.get(target, spec.default if spec.default is not None else 0)
            value = registry.apply_modifiers(start, mods)
            if not spec.in_bounds(value):
                self.warnings.append(Issue(where, target, f"value {value:g} clamped to {spec.lo}..{spec.hi}"))
            out[target] = spec.clamp(value)
        return out


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    return {**base, **over}


def _duration(value: int | str) -> int:
    if isinstance(value, int):
        return value
    return int(value[:-1]) * _DURATION_UNITS[value[-1]]


def _reachable(adj: Mapping[str, Iterable[str]], start: str, radius: int) -> set[str]:
    seen = {start}
    queue = deque([(start, 0)])
    while queue:
        node, dist = queue.popleft()
        if dist == radius:
            continue
        for nxt in adj[node]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, dist + 1))
    return seen


def _assemble(loaded: _Loaded) -> tuple[_Builder, dict[str, Any]]:
    b = _Builder(loaded)
    parts: dict[str, Any] = {}
    parts["resources"] = b.resources()
    parts["province_types"] = b.province_types()
    parts["units"] = b.units()
    parts["buildings"] = b.buildings()
    parts["factions"], parts["faction_mods"] = b.factions()
    parts["maps"] = b.maps()
    parts["scenarios"] = b.scenarios()
    parts["roles"] = b.roles(parts["resources"])
    parts["base_stats"] = b.base_stats(parts["resources"], parts["units"], parts["buildings"])
    return b, parts


def _ruleset(b: _Builder, parts: dict[str, Any], packs: dict[str, str], scenario_id: str) -> Ruleset:
    scenarios = parts["scenarios"]
    full = scenario_id if scenario_id in scenarios else next(
        (k for k in scenarios if k.split(":", 1)[1] == scenario_id), None)
    if full is None:
        raise ContentError([Issue("<ruleset>", "scenario", f"unknown scenario {scenario_id!r}")])
    raw, scenario = scenarios[full]
    if scenario.map not in parts["maps"]:
        raise ContentError([Issue(raw.file, _json_path(raw.path, ["map"]), f"map {scenario.map!r} is invalid")])
    overrides = b.effects(raw, raw.data.get("overrides", []), "overrides", "override")
    stats = b.effective(parts["base_stats"], overrides, raw.file)
    faction_stats = {f: b.effective(parts["base_stats"], overrides + parts["faction_mods"][f], f)
                     for f in scenario.factions}
    if b.errors:
        raise ContentError(list(b.errors))
    ruleset = Ruleset(
        packs=dict(sorted(packs.items())), resources=parts["resources"], roles=parts["roles"],
        province_types=parts["province_types"], units=parts["units"], buildings=parts["buildings"],
        factions={f: parts["factions"][f] for f in scenario.factions}, map=parts["maps"][scenario.map],
        scenario=scenario, stats=stats, faction_stats=faction_stats, warnings=tuple(b.warnings))
    return Ruleset(**{**ruleset.__dict__, "hash": sha256_json(ruleset.to_dict())})


def build_ruleset(pack_dirs: Iterable[str | Path] | None, scenario_id: str) -> Ruleset:
    """Load and validate packs, then assemble the ruleset for one scenario. Raises ContentError."""
    loaded = _load_all(pack_dirs if pack_dirs is not None else default_pack_dirs())
    b, parts = _assemble(loaded)
    if b.errors:
        raise ContentError(list(b.errors))
    return _ruleset(b, parts, loaded.packs, scenario_id)


def validate_packs(pack_dirs: Iterable[str | Path]) -> tuple[list[Issue], list[Issue]]:
    """(errors, warnings) for all packs and every scenario they define; never raises ContentError."""
    loaded = _load_all(pack_dirs)
    b, parts = _assemble(loaded)
    errors, warnings = list(b.errors), list(b.warnings)
    if not errors:
        for sid in parts["scenarios"]:
            b.errors, b.warnings = [], []
            try:
                _ruleset(b, parts, loaded.packs, sid)
            except ContentError as exc:
                errors.extend(i for i in exc.issues if i not in errors)
            warnings.extend(i for i in b.warnings if i not in warnings)
    return errors, warnings


def default_pack_dirs() -> list[Path]:
    """All pack directories under content/packs of this repository, ``core`` first."""
    dirs = sorted(p for p in DEFAULT_PACKS_DIR.iterdir() if (p / "pack.yaml").is_file())
    return sorted(dirs, key=lambda p: p.name != "core")
