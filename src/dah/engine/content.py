"""Content packs: loading, validation (schema -> refs -> roles -> map graph -> bounds) and Ruleset assembly."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, replace
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
# Frozen earlier versions of packs (content/legacy/<id>-<version>): never loaded by default; a game whose journaled
# ruleset hash matches one of them replays and continues with it (dah.game.pick_ruleset).
LEGACY_PACKS_DIR = DEFAULT_PACKS_DIR.parent / "legacy"

# directory name -> schema / entity kind
ENTITY_DIRS: dict[str, str] = {
    "resources": "resource",
    "province_types": "province_type",
    "units": "unit",
    "buildings": "building",
    "factions": "faction",
    "maps": "map",
    "scenarios": "scenario",
    # stage 2 (TECH_AND_SETUP.md): optional, a pack without them stays valid
    "branches": "branch",
    "techs": "tech",
    "artifacts": "artifact",
}

DEFAULT_MESSAGE_LIMITS: dict[str, Any] = {
    "negotiation_1": {"per_recipient": 6, "private": 20, "public": 2},
    "negotiation_2": {"per_recipient": 10, "private": 10, "public": 1},
    "max_chars": 800,
}
# Per seat and per negotiation phase: gifts sent and pact proposals made (scenario `diplomacy_limits`).
DEFAULT_DIPLOMACY_LIMITS: dict[str, int] = {"gifts_per_phase": 3, "pact_proposals_per_phase": 3}

_SUSPICIOUS = re.compile(r"ignore|игнорируй|system|токен|https?://|www\.", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}
_LORE_FILE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.md$")
MAX_LORE_FILE_CHARS = 4000

# Scenario `setup` (TECH_AND_SETUP.md section 2); absent or "none": no setup phase, as in the slice.
ARTIFACT_MODES = ("draft", "points", "none")
DEFAULT_SETUP: dict[str, Any] = {"unique_factions": True, "artifacts": "draft", "artifact_offer": 3,
                                 "artifact_points": 3, "artifact_pool": ()}
DEFAULT_SETUP_LIMIT = 300   # seconds of the setup phase when the scenario's phase_limits has no `setup`


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


# Stage-2 fields below have empty defaults and are left out of the ruleset hash while empty (Ruleset.to_dict), so a
# pack that does not use them hashes exactly as before they existed.

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
    move: int | None = None                       # None: Ruleset.move_default (stat unit.<id>.move)
    requires_tech: tuple[str, ...] = ()
    requires_building: tuple[str, ...] = ()       # in the province of recruitment
    factions: tuple[str, ...] = ()                # unique unit: only these factions (empty: everyone)
    abilities: dict[str, dict[str, float]] = field(default_factory=dict)   # name -> params given in the pack
    lore: str = ""


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
    requires_tech: tuple[str, ...] = ()
    factions: tuple[str, ...] = ()
    province_mods: tuple[dict[str, Any], ...] = ()   # modify, scope province: province.* stats of its province
    owner_mods: tuple[dict[str, Any], ...] = ()      # modify, scope owner: the owner's stats (once per building)
    lore: str = ""


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
    bonuses: tuple[dict[str, Any], ...] = ()     # as written (normalized), for descriptions
    penalties: tuple[dict[str, Any], ...] = ()
    description: str = ""


@dataclass(frozen=True)
class BranchDef:
    id: str
    name: str
    name_en: str
    order: int
    description: str
    color: str


@dataclass(frozen=True)
class TechDef:
    id: str
    name: str
    name_en: str
    branch: str
    tier: int
    cost: int                              # base; the effective cost is Ruleset.tech_cost (stats)
    requires: tuple[str, ...]
    requires_any: tuple[str, ...]
    requires_tier: dict[str, Any] | None   # {tier, count, same_branch}
    factions: tuple[str, ...]
    effects: tuple[dict[str, Any], ...]    # context "tech"
    unlocks: tuple[str, ...]               # informational
    description: str
    lore: str


@dataclass(frozen=True)
class ArtifactDef:
    id: str
    name: str
    name_en: str
    power: int
    cost: int
    factions: tuple[str, ...]
    exclusive_with: tuple[str, ...]
    effects: tuple[dict[str, Any], ...]    # context "artifact"
    description: str
    lore: str


@dataclass(frozen=True)
class Profile:
    """A seat's stat key beyond its faction: researched techs, artifacts and the owner-scoped buildings it holds
    (engine/techs.py `stat_key`). Ruleset.stat accepts it wherever it accepts a faction id."""
    faction: str
    techs: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    buildings: tuple[tuple[str, int], ...] = ()     # (building, count) of buildings with owner-scoped modifiers

    @property
    def plain(self) -> bool:
        return not (self.techs or self.artifacts or self.buildings)


StatKey = Any   # None | faction id | Profile


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
    # Display layout {province: (x, y)} in 0..100, from `pos` in the map file or computed (`layout_computed`).
    # Cosmetic only: left out of the ruleset hash, so moving a province on screen never changes a game.
    layout: dict[str, tuple[float, float]] = field(default_factory=dict)
    layout_computed: bool = False

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
    diplomacy_limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_DIPLOMACY_LIMITS))
    # None: no setup phase (as in the slice); else DEFAULT_SETUP keys. A game setting (the game config may override
    # the artifact mode), not hashed.
    setup: dict[str, Any] | None = None


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
    branches: dict[str, BranchDef] = field(default_factory=dict)
    techs: dict[str, TechDef] = field(default_factory=dict)
    artifacts: dict[str, ArtifactDef] = field(default_factory=dict)
    # Inputs of per-seat stat tables (Profile); derived from the hashed content, so not hashed themselves.
    base_stats: dict[str, float] = field(default_factory=dict)
    override_mods: tuple[dict[str, Any], ...] = ()
    faction_mods: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    # Default `unit.<id>.move`: registry.default_unit_move of whether the packs have stage-2 content (not hashed:
    # it follows from hashed content), so the frozen slice keeps one edge per move.
    move_default: int = registry.DEFAULT_UNIT_MOVE

    def __post_init__(self) -> None:
        object.__setattr__(self, "_cache", {})     # Profile -> stats table (not a field: never hashed or copied)

    def _table(self, key: StatKey) -> Mapping[str, float]:
        if not key:
            return self.stats
        if isinstance(key, Profile):
            if key.plain:
                return self.faction_stats.get(key.faction, self.stats)
            cache: dict[Any, Any] = self._cache     # type: ignore[attr-defined]
            table = cache.get(key)
            if table is None:
                mods = list(self.override_mods) + list(self.faction_mods.get(key.faction, ()))
                for tech in key.techs:
                    if tech in self.techs:
                        mods += [e for e in self.techs[tech].effects if e["type"] == "modify"]
                for art in key.artifacts:
                    if art in self.artifacts:
                        mods += [e for e in self.artifacts[art].effects if e["type"] == "modify"]
                for bid, n in key.buildings:
                    if bid in self.buildings:
                        mods += list(self.buildings[bid].owner_mods) * n
                table = effective_stats(self.base_stats or self.stats, mods, self.move_default)[0]
                cache[key] = table
            return table
        return self.faction_stats.get(key, self.stats)

    def stat(self, path: str, faction: StatKey = None) -> float:
        """Effective stat for a faction id, a Profile (faction + techs + artifacts + owner buildings) or None."""
        table = self._table(faction)
        if path in table:
            return table[path]
        spec = registry.stat_spec(path)
        if spec is None:
            raise KeyError(f"unknown stat {path!r}")
        if spec.pattern == registry.MOVE_STAT:
            return self.move_default
        return spec.default if spec.default is not None else 0

    def _passive(self, key: StatKey) -> tuple[list[dict[str, Any]], str | None]:
        if isinstance(key, Profile):
            effects = [e for t in key.techs if t in self.techs for e in self.techs[t].effects]
            effects += [e for a in key.artifacts if a in self.artifacts for e in self.artifacts[a].effects]
            return effects, key.faction
        return [], key

    def passive_income(self, key: StatKey) -> dict[str, int]:
        """Flat income per turn of a faction (bonuses/penalties) plus `income` effects of techs and artifacts."""
        effects, faction = self._passive(key)
        total: Counter[str] = Counter(self.factions[faction].income if faction in self.factions else {})
        for e in effects:
            if e["type"] == "income":
                total[e["resource"]] += e["amount"]
        return dict(total)

    def unlocks(self, key: StatKey) -> frozenset[str]:
        """`<kind>:<id>` unlocked by `unlock` effects of the key's techs and artifacts."""
        effects, _ = self._passive(key)
        return frozenset(f"{e['kind']}:{e['id']}" for e in effects if e["type"] == "unlock")

    def tech_cost(self, tech: str, key: StatKey = None) -> int:
        """`tech.<id>.cost` x `tech.branch.<branch>.cost_mul`, rounded half up, at least 0."""
        t = self.techs[tech]
        value = self.stat(f"tech.{tech}.cost", key) * self.stat(f"tech.branch.{t.branch}.cost_mul", key)
        return max(0, int(math.floor(value + 0.5)))

    @property
    def research_resource(self) -> str | None:
        return self.roles.get("research")

    @property
    def gated_orders(self) -> frozenset[str]:
        """Order kinds some `unlock {kind: order}` effect of a tech or an artifact names: locked until unlocked."""
        cache: dict[Any, Any] = self._cache     # type: ignore[attr-defined]
        if "gated_orders" not in cache:
            cache["gated_orders"] = frozenset(
                e["id"] for d in (*self.techs.values(), *self.artifacts.values()) for e in d.effects
                if e["type"] == "unlock" and e["kind"] == "order")
        return cache["gated_orders"]

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
                                                  "faction": "factions", "tech": "techs", "branch": "branches",
                                                  "artifact": "artifacts"}[kind])
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
        for derived in ("base_stats", "override_mods", "faction_mods", "move_default"):
            data.pop(derived)
        data["map"].pop("layout", None)
        data["map"].pop("layout_computed", None)
        # Game settings, not rules of resolution: every game's config records its own values, and a new default
        # must not make `dah replay` of an older game report changed content.
        data["scenario"].pop("phase_limits", None)
        data["scenario"].pop("diplomacy_limits", None)
        data["scenario"].pop("setup", None)
        # Stage-2 content left out while empty: a pack without it hashes as before (TECH_AND_SETUP.md section 5).
        for key in ("branches", "techs", "artifacts"):
            if not data[key]:
                data.pop(key)
        for table in ("units", "buildings", "factions"):
            for entry in data[table].values():
                for k in [k for k, v in entry.items() if k in _NEW_FIELDS and v in (None, "", (), [], {})]:
                    entry.pop(k)
        # Texts for people (lore, descriptions, English names, branch colours) are not rules: editing a lore file
        # must not make `dah replay` report changed content. A faction's `lore` was hashed before stage 2, so a
        # pack without stage-2 content (no techs, branches, artifacts) keeps it and hashes exactly as before.
        stage2 = any(k in data for k in ("branches", "techs", "artifacts"))
        for table in ("units", "buildings", "factions", "techs", "branches", "artifacts"):
            for entry in (data.get(table) or {}).values():
                for k in _COSMETIC_FIELDS & entry.keys():
                    if k == "lore" and table == "factions" and not stage2:
                        continue
                    entry.pop(k)
        return data


_NEW_FIELDS = frozenset({"move", "requires_tech", "requires_building", "factions", "abilities", "lore",
                         "province_mods", "owner_mods", "bonuses", "penalties", "description"})
_COSMETIC_FIELDS = frozenset({"lore", "description", "name_en", "color"})


def effective_stats(base: Mapping[str, float], modifiers: Iterable[Mapping[str, Any]],
                    move_default: int = registry.DEFAULT_UNIT_MOVE) -> tuple[dict[str, float], list[tuple[str, float]]]:
    """Apply modifiers (base -> add -> mul -> last set -> clamp) per target. Returns (table, [(target, raw value
    out of bounds)]). `move_default`: the base of a `unit.<id>.move` the pack does not set."""
    by_target: dict[str, list[Mapping[str, Any]]] = {}
    for m in modifiers:
        by_target.setdefault(m["target"], []).append(m)
    out = dict(base)
    clamped = []
    for target, mods in sorted(by_target.items()):
        spec = registry.stat_spec(target)
        assert spec is not None
        default = move_default if spec.pattern == registry.MOVE_STAT else spec.default
        start = base.get(target, default if default is not None else 0)
        value = registry.apply_modifiers(start, mods)
        if not spec.in_bounds(value):
            clamped.append((target, value))
        out[target] = spec.clamp(value)
    return out, clamped


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
    dirs: dict[str, Path] = field(default_factory=dict)                   # pack id -> its directory (lore files)


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
    out.dirs[pack_id] = pack_dir
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
        for key in ("name", "lore", "description"):
            text = raw.data.get(key)
            texts = list(text.values()) if isinstance(text, dict) else [text]
            if any(isinstance(t, str) and _SUSPICIOUS.search(t) for t in texts):
                self.warn(raw, [key], "text looks like an instruction to agents")

    @staticmethod
    def name(d: Mapping[str, Any]) -> str:
        """Russian name of an entity: `name` is a string or {ru, en}."""
        value = d["name"]
        return value["ru"] if isinstance(value, dict) else value

    @staticmethod
    def name_en(d: Mapping[str, Any]) -> str:
        value = d["name"]
        return value.get("en", "") if isinstance(value, dict) else ""

    def lore(self, raw: _Raw) -> str:
        """`lore` inline, or a pack file `lore/<x>.md` (read here, so the text is part of the ruleset)."""
        value = raw.data.get("lore") or ""
        if not _LORE_FILE.match(value) or ".." in value.split("/"):
            return value
        path = self.l.dirs.get(raw.pack, Path(".")) / value
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            self.warn(raw, ["lore"], f"lore file {value!r} not found in pack {raw.pack!r}")
            return ""
        if len(text) > MAX_LORE_FILE_CHARS:
            self.warn(raw, ["lore"], f"lore file {value!r} longer than {MAX_LORE_FILE_CHARS} chars: cut")
            text = text[:MAX_LORE_FILE_CHARS]
        if _SUSPICIOUS.search(text):
            self.warn(raw, ["lore"], "text looks like an instruction to agents")
        return text

    def refs(self, raw: _Raw, kind: str, values: Iterable[str], parts: list[Any]) -> tuple[str, ...]:
        out = [self.ref(raw, kind, v, [*parts, i]) for i, v in enumerate(values)]
        return tuple(r for r in out if r)

    def target(self, raw: _Raw, target: str, parts: list[Any]) -> str | None:
        """Qualify entity ids inside a stat path: ``unit.assault.attack`` -> ``unit.core:assault.attack``,
        ``tech.branch.industry.cost_mul``, ``province.income_mul.alloy``."""
        segs = target.split(".")
        kinds = {"unit": "unit", "building": "building", "resource": "resource"}
        if segs[0] == "tech" and len(segs) == 4 and segs[1] == "branch":
            full = self.ref(raw, "branch", segs[2], parts)
            if full is None:
                return None
            segs[2] = full
        elif segs[0] == "tech" and len(segs) == 3:
            full = self.ref(raw, "tech", segs[1], parts)
            if full is None:
                return None
            segs[1] = full
        elif segs[0] == "province" and len(segs) == 3 and segs[1] == "income_mul":
            full = self.ref(raw, "resource", segs[2], parts)
            if full is None:
                return None
            segs[2] = full
        elif segs[0] in kinds and len(segs) >= 2:
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
        parts = key.split(".")
        for i, eff in enumerate(effects):
            problems = registry.effect_problems(eff, context)
            for p in problems:
                self.err(raw, [*parts, i], p)
            if problems:
                continue
            eff = registry.normalize_effect(eff)
            ok = True
            if eff["type"] == "modify":
                eff["target"] = self.target(raw, eff["target"], [*parts, i, "target"])
                ok = eff["target"] is not None
                if ok and eff.get("scope") == "province" and not eff["target"].startswith("province."):
                    self.err(raw, [*parts, i, "target"], "scope province: target must be a province.* stat")
                    ok = False
            for field_name, kind in (("resource", "resource"), ("unit", "unit"), ("building", "building"),
                                     ("tech", "tech")):
                if field_name in eff:
                    eff[field_name] = self.ref(raw, kind, eff[field_name], [*parts, i, field_name])
                    ok = ok and eff[field_name] is not None
            if eff["type"] == "unlock":
                if eff["kind"] == "order":
                    if eff["id"] not in registry.ORDER_KINDS:
                        self.err(raw, [*parts, i, "id"], f"unknown order kind {eff['id']!r}")
                        ok = False
                else:
                    eff["id"] = self.ref(raw, eff["kind"], eff["id"], [*parts, i, "id"])
                    ok = ok and eff["id"] is not None
            if ok:
                out.append(eff)
        return out

    def abilities(self, raw: _Raw, items: list[Any]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for i, item in enumerate(items):
            name = item if isinstance(item, str) else item["id"]
            params = {} if isinstance(item, str) else {k: v for k, v in item.items() if k != "id"}
            spec = registry.ABILITIES.get(name)
            if spec is None:
                self.err(raw, ["abilities", i], f"unknown ability {name!r}; known: {', '.join(registry.ABILITIES)}")
                continue
            if name in out:
                self.err(raw, ["abilities", i], f"ability {name!r} given twice")
                continue
            for param, value in params.items():
                if param not in spec:
                    self.err(raw, ["abilities", i, param], f"ability {name!r} has no parameter {param!r}")
                elif not spec[param][1] <= value <= spec[param][2]:
                    self.err(raw, ["abilities", i, param],
                             f"value {value} out of bounds {spec[param][1]}..{spec[param][2]}")
            out[name] = {k: v for k, v in sorted(params.items()) if k in spec}
        return dict(sorted(out.items()))

    # -- entity builders

    def resources(self) -> dict[str, ResourceDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["resource"].items()):
            d = raw.data
            self.texts(raw)
            out[fid] = ResourceDef(fid, self.name(d), d.get("short", self.name(d)), d["role"])
        return out

    def province_types(self) -> dict[str, ProvinceTypeDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["province_type"].items()):
            d = raw.data
            self.texts(raw)
            out[fid] = ProvinceTypeDef(fid, self.name(d), d.get("short", self.name(d)),
                                       self.amounts(raw, d["income"], "income"), float(d.get("defense_mul", 1.0)))
        return out

    def units(self) -> dict[str, UnitDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["unit"].items()):
            d = raw.data
            self.texts(raw)
            counters = [self.ref(raw, "unit", c, ["counters", i]) for i, c in enumerate(d.get("counters", []))]
            requires = d.get("requires", {})
            out[fid] = UnitDef(fid, self.name(d), d.get("short", self.name(d)), d["attack"], d["defense"],
                               self.amounts(raw, d["cost"], "cost"), self.amounts(raw, d["upkeep"], "upkeep"),
                               tuple(sorted(c for c in counters if c)), d.get("move"),
                               self.refs(raw, "tech", requires.get("tech", []), ["requires", "tech"]),
                               self.refs(raw, "building", requires.get("building", []), ["requires", "building"]),
                               self.refs(raw, "faction", d.get("factions", []), ["factions"]),
                               self.abilities(raw, d.get("abilities", [])), self.lore(raw))
        return out

    def buildings(self) -> dict[str, BuildingDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["building"].items()):
            d = raw.data
            self.texts(raw)
            income: Counter[str] = Counter()
            effects = self.effects(raw, d.get("effects", []), "effects", "building")
            for eff in effects:
                if eff["type"] == "income":
                    income[eff["resource"]] += eff["amount"]
            mods = [{k: v for k, v in e.items() if k != "scope"} | {"scope": e["scope"]}
                    for e in effects if e["type"] == "modify"]
            requires = d.get("requires", {})
            out[fid] = BuildingDef(fid, self.name(d), d.get("short", self.name(d)),
                                   self.amounts(raw, d["cost"], "cost"), d["build_turns"], dict(sorted(income.items())),
                                   bool(d.get("recruit", False)), d.get("max_per_province", 1),
                                   self.refs(raw, "tech", requires.get("tech", []), ["requires", "tech"]),
                                   self.refs(raw, "faction", d.get("factions", []), ["factions"]),
                                   tuple(m for m in mods if m["scope"] == "province"),
                                   tuple(m for m in mods if m["scope"] == "owner"), self.lore(raw))
        return out

    def factions(self) -> tuple[dict[str, FactionDef], dict[str, list[dict[str, Any]]]]:
        out, modifiers = {}, {}
        for fid, raw in sorted(self.l.entities["faction"].items()):
            d = raw.data
            self.texts(raw)
            bonuses = self.effects(raw, d.get("bonuses", []), "bonuses", "passive")
            penalties = self.effects(raw, d.get("penalties", []), "penalties", "passive")
            passive = bonuses + penalties
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
                fid, self.name(d), self.lore(raw), dict(sorted(income.items())),
                self.amounts(raw, st.get("resources", {}), "starting.resources"), units,
                tuple(b for b in buildings if b),
                tuple(self.effects(raw, st.get("effects", []), "starting.effects", "on_game_start")),
                tuple(bonuses), tuple(penalties), d.get("description", ""))
        return out, modifiers

    def branches(self) -> dict[str, BranchDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["branch"].items()):
            d = raw.data
            self.texts(raw)
            out[fid] = BranchDef(fid, self.name(d), self.name_en(d), d.get("order", 0), d.get("description", ""),
                                 d.get("color", ""))
        return out

    def techs(self) -> dict[str, TechDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["tech"].items()):
            d = raw.data
            self.texts(raw)
            branch = self.ref(raw, "branch", d["branch"], ["branch"])
            requires = self.refs(raw, "tech", d.get("requires", []), ["requires"])
            requires_any = self.refs(raw, "tech", d.get("requires_any", []), ["requires_any"])
            if fid in requires or fid in requires_any:
                self.err(raw, ["requires"], "a tech cannot require itself")
            rt = d.get("requires_tier")
            unlocks = []
            for i, ref in enumerate(d.get("unlocks", [])):
                full = ref if ":" in ref else f"{raw.pack}:{ref}"
                if not any(full in self.l.entities[k] for k in ("unit", "building", "tech")):
                    self.warn(raw, ["unlocks", i], f"unlocks {ref!r}: no such unit, building or tech")
                unlocks.append(full)
            if d["cost"] > registry.stat_spec("tech.x:x.cost").hi:      # type: ignore[union-attr]
                self.err(raw, ["cost"], f"value {d['cost']} out of bounds 0..1000")
            out[fid] = TechDef(
                fid, self.name(d), self.name_en(d), branch or d["branch"], d["tier"], d["cost"], requires,
                requires_any,
                {"tier": rt["tier"], "count": rt["count"], "same_branch": rt.get("same_branch", True)} if rt else None,
                self.refs(raw, "faction", d.get("factions", []), ["factions"]),
                tuple(self.effects(raw, d.get("effects", []), "effects", "tech")), tuple(unlocks),
                d.get("description", ""), self.lore(raw))
        self.tech_cycles(out)
        return out

    def tech_cycles(self, techs: Mapping[str, TechDef]) -> None:
        """`requires` must not loop (a tech in a loop can never be researched)."""
        state: dict[str, int] = {}

        def visit(tid: str, path: list[str]) -> None:
            state[tid] = 1
            for req in techs[tid].requires:
                if req not in techs:
                    continue
                if state.get(req) == 1:
                    raw = self.l.entities["tech"][tid]
                    self.err(raw, ["requires"], f"requirement loop: {' -> '.join([*path, tid, req])}")
                elif not state.get(req):
                    visit(req, [*path, tid])
            state[tid] = 2

        for tid in sorted(techs):
            if not state.get(tid):
                visit(tid, [])

    def artifacts(self, units: Mapping[str, UnitDef], buildings: Mapping[str, BuildingDef],
                  techs: Mapping[str, TechDef]) -> dict[str, ArtifactDef]:
        out = {}
        for fid, raw in sorted(self.l.entities["artifact"].items()):
            d = raw.data
            self.texts(raw)
            effects = tuple(self.effects(raw, d.get("effects", []), "effects", "artifact"))
            exclusive = self.refs(raw, "artifact", d.get("exclusive_with", []), ["exclusive_with"])
            if fid in exclusive:
                self.err(raw, ["exclusive_with"], "an artifact cannot exclude itself")
            guess = artifact_power_guess(effects, units, buildings, techs)
            if abs(d["power"] - guess) > max(1.0, 0.5 * guess):
                self.warn(raw, ["power"], f"power {d['power']} looks off: its effects weigh about {guess:g}")
            out[fid] = ArtifactDef(fid, self.name(d), self.name_en(d), d["power"], d.get("cost", 1),
                                   self.refs(raw, "faction", d.get("factions", []), ["factions"]), exclusive,
                                   effects, d.get("description", ""), self.lore(raw))
        return out

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
        with_pos = [i for i, p in enumerate(d["provinces"]) if "pos" in p]
        if with_pos and len(with_pos) != len(d["provinces"]):
            missing = next(i for i, p in enumerate(d["provinces"]) if "pos" not in p)
            self.err(raw, ["provinces", missing], "pos must be given for every province of the map or for none")
        if len(self.errors) > before:
            return None
        adjacency = {pid: tuple(sorted(n)) for pid, n in sorted(adj.items())}
        if with_pos:
            layout = {p["id"]: (float(p["pos"][0]), float(p["pos"][1])) for p in d["provinces"]}
            seen_pos: dict[tuple[float, float], str] = {}
            for i, p in enumerate(d["provinces"]):
                xy = layout[p["id"]]
                if xy in seen_pos:
                    self.warn(raw, ["provinces", i, "pos"], f"same pos as {seen_pos[xy]!r}")
                seen_pos.setdefault(xy, p["id"])
        else:
            layout = circular_layout(sorted(provinces), [(s.capital, *s.provinces) for s in starts])
        profiles = [Counter(provinces[p].type for p in _reachable(adj, s.capital, 2)) for s in starts]
        if any(p != profiles[0] for p in profiles):
            self.err(raw, ["starts"], "starts are not symmetric: province types within radius 2 differ")
            return None
        return MapDef(raw.id, d["name"], d["max_players"], dict(sorted(provinces.items())), adjacency, tuple(starts),
                      dict(sorted(layout.items())), not with_pos)

    def scenarios(self) -> dict[str, tuple[_Raw, ScenarioDef]]:
        out = {}
        for fid, raw in sorted(self.l.entities["scenario"].items()):
            d = raw.data
            self.texts(raw)
            map_id = self.ref(raw, "map", d["map"], ["map"])
            factions = [self.ref(raw, "faction", f, ["factions", i]) for i, f in enumerate(d["factions"])]
            limits = {k: _merge(DEFAULT_MESSAGE_LIMITS[k], v) if isinstance(v, dict) else v
                      for k, v in _merge(DEFAULT_MESSAGE_LIMITS, d.get("message_limits", {})).items()}
            setup = d.get("setup")
            if isinstance(setup, dict):
                pool = self.refs(raw, "artifact", setup.get("artifact_pool", []), ["setup", "artifact_pool"])
                setup = {**DEFAULT_SETUP, **setup, "artifact_pool": pool}
            else:
                setup = None
            out[fid] = (raw, ScenarioDef(
                fid, d["name"], map_id or d["map"], tuple(f for f in factions if f), d["turn_limit"],
                {k: _duration(v) for k, v in d["phase_limits"].items()}, limits,
                _merge(DEFAULT_DIPLOMACY_LIMITS, d.get("diplomacy_limits", {})), setup))
        return out

    def roles(self, resources: Mapping[str, ResourceDef]) -> dict[str, str]:
        by_role: dict[str, list[str]] = {r: [] for r in registry.RESOURCE_ROLES}
        for res in resources.values():
            by_role[res.role].append(res.id)
        for role, ids in by_role.items():
            if role in registry.REQUIRED_ROLES and len(ids) != 1:
                self.errors.append(Issue("<ruleset>", f"roles.{role}",
                                         f"role {role!r} needs exactly one resource, found {ids or 'none'}"))
            elif role in registry.OPTIONAL_ROLES and len(ids) > 1:
                self.errors.append(Issue("<ruleset>", f"roles.{role}",
                                         f"role {role!r} takes at most one resource, found {ids}"))
        return {role: ids[0] for role, ids in by_role.items() if len(ids) == 1}

    def base_stats(self, resources, units, buildings, techs=None) -> dict[str, float]:
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
            # stage-2 stats only where the pack sets them; elsewhere the registry default applies (same hash)
            if u.move is not None:
                stats[f"unit.{u.id}.move"] = u.move
            for name, params in u.abilities.items():
                stats[f"unit.{u.id}.ability.{name}.on"] = 1
                for param, value in params.items():
                    stats[f"unit.{u.id}.ability.{name}.{param}"] = value
        for b in buildings.values():
            raw = self.l.entities["building"][b.id]
            for res, n in b.cost.items():
                stats[f"building.{b.id}.cost.{res}"] = n
            self.bounds(raw, stats, b.id, "building", {f"cost.{k}": v for k, v in b.cost.items()})
        for t in (techs or {}).values():
            stats[f"tech.{t.id}.cost"] = t.cost
        return stats

    def bounds(self, raw: _Raw, stats, fid: str, kind: str, values: Mapping[str, int]) -> None:
        for key, value in values.items():
            spec = registry.stat_spec(f"{kind}.{fid}.{key}")
            if spec and not spec.in_bounds(value):
                field_path = key.split(".")
                if len(field_path) == 2:
                    field_path[1] = field_path[1].split(":", 1)[1]
                self.err(raw, field_path, f"value {value} out of bounds {spec.lo}..{spec.hi}")

    def effective(self, base: Mapping[str, float], modifiers: list[dict[str, Any]], where: str,
                  move_default: int = registry.DEFAULT_UNIT_MOVE) -> dict[str, float]:
        out, clamped = effective_stats(base, modifiers, move_default)
        for target, value in clamped:
            spec = registry.stat_spec(target)
            assert spec is not None
            self.warnings.append(Issue(where, target, f"value {value:g} clamped to {spec.lo}..{spec.hi}"))
        return out


def artifact_power_guess(effects: Iterable[Mapping[str, Any]], units: Mapping[str, UnitDef],
                         buildings: Mapping[str, BuildingDef], techs: Mapping[str, TechDef]) -> float:
    """Rough weight of an artifact's effects, to check its declared `power` (a validator warning, never an error):
    a tech counts its tier, 15 resources 1, a unit or a building its price / 20, +1 income per turn 0.5, a modify or
    an unlock 1."""
    total = 0.0
    for e in effects:
        kind = e["type"]
        if kind == "grant_tech":
            total += techs[e["tech"]].tier if e["tech"] in techs else 1
        elif kind == "add_resource":
            total += e["amount"] / 15
        elif kind == "grant_units":
            price = sum(units[e["unit"]].cost.values()) if e["unit"] in units else 15
            total += e["count"] * price / 20
        elif kind == "grant_building":
            total += sum(buildings[e["building"]].cost.values()) / 20 if e["building"] in buildings else 1
        elif kind == "income":
            total += e["amount"] * 0.5
        elif kind in ("modify", "unlock"):
            total += 1
    return round(total, 1)


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    return {**base, **over}


def _duration(value: int | str) -> int:
    if isinstance(value, int):
        return value
    return int(value[:-1]) * _DURATION_UNITS[value[-1]]


def circular_layout(province_ids: Iterable[str],
                    groups: Iterable[Iterable[str]] = ()) -> dict[str, tuple[float, float]]:
    """Deterministic fallback layout in 0..100 for a map without `pos`: provinces of the starts (capital first,
    start by start) on an outer circle, the rest (contested land) on an inner circle around the centre."""
    ids = sorted(province_ids)
    outer: list[str] = []
    for group in groups:
        outer += [p for p in group if p in ids and p not in outer]
    inner = [p for p in ids if p not in outer]
    if not outer:
        outer, inner = inner, []
    out: dict[str, tuple[float, float]] = {}
    for ring, radius in ((outer, 40.0), (inner, 18.0)):
        if len(ring) == 1 and radius < 40:
            out[ring[0]] = (50.0, 50.0)
            continue
        for i, pid in enumerate(ring):
            angle = -math.pi / 2 + 2 * math.pi * i / len(ring)
            out[pid] = (round(50 + radius * math.cos(angle), 1), round(50 + radius * math.sin(angle), 1))
    return out


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
    parts["branches"] = b.branches()
    parts["techs"] = b.techs()
    parts["artifacts"] = b.artifacts(parts["units"], parts["buildings"], parts["techs"])
    parts["maps"] = b.maps()
    parts["scenarios"] = b.scenarios()
    parts["roles"] = b.roles(parts["resources"])
    if parts["techs"] and "research" not in parts["roles"]:
        b.warnings.append(Issue("<ruleset>", "roles.research",
                                "techs are defined but no resource has role 'research': nobody can research them"))
    research = parts["roles"].get("research")
    if research is not None:
        # The research resource is spent only on techs (TECH_AND_SETUP.md 1.1): research moves the whole stock into
        # the active tech every turn, so a unit or building priced in it could never be paid for while researching.
        for table, kind in (("units", "unit"), ("buildings", "building")):
            for eid, entity in parts[table].items():
                if entity.cost.get(research):
                    b.warnings.append(Issue("<ruleset>", f"{table}.{eid}.cost",
                                            f"{kind} {eid} costs the research resource {research}: it is spent only "
                                            "on techs (research takes the whole stock every turn)"))
    parts["base_stats"] = b.base_stats(parts["resources"], parts["units"], parts["buildings"], parts["techs"])
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
    move_default = registry.default_unit_move(any(parts[k] for k in ("branches", "techs", "artifacts")))
    stats = b.effective(parts["base_stats"], overrides, raw.file, move_default)
    faction_stats = {f: b.effective(parts["base_stats"], overrides + parts["faction_mods"][f], f, move_default)
                     for f in scenario.factions}
    if scenario.setup is not None and scenario.setup["artifacts"] != "none" and not parts["artifacts"]:
        b.warnings.append(Issue(raw.file, _json_path(raw.path, ["setup", "artifacts"]),
                                f"artifact mode {scenario.setup['artifacts']!r} but no artifacts are defined"))
    if b.errors:
        raise ContentError(list(b.errors))
    ruleset = Ruleset(
        packs=dict(sorted(packs.items())), resources=parts["resources"], roles=parts["roles"],
        province_types=parts["province_types"], units=parts["units"], buildings=parts["buildings"],
        factions={f: parts["factions"][f] for f in scenario.factions}, map=parts["maps"][scenario.map],
        scenario=scenario, stats=stats, faction_stats=faction_stats, warnings=tuple(b.warnings),
        branches=parts["branches"], techs=parts["techs"], artifacts=parts["artifacts"],
        base_stats=parts["base_stats"], override_mods=tuple(overrides),
        faction_mods={f: tuple(parts["faction_mods"][f]) for f in scenario.factions}, move_default=move_default)
    return replace(ruleset, hash=sha256_json(ruleset.to_dict()))


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


def legacy_pack_sets() -> list[list[Path]]:
    """Pack sets made of the default packs with one of them swapped for a frozen earlier version of the same pack
    id (content/legacy/*), newest directory name first."""
    if not LEGACY_PACKS_DIR.is_dir():
        return []
    defaults = default_pack_dirs()
    by_id = {_pack_id(p): i for i, p in enumerate(defaults)}
    out = []
    for legacy in sorted((p for p in LEGACY_PACKS_DIR.iterdir() if (p / "pack.yaml").is_file()), reverse=True):
        pid = _pack_id(legacy)
        if pid in by_id:
            dirs = list(defaults)
            dirs[by_id[pid]] = legacy
            out.append(dirs)
    return out


def _pack_id(pack_dir: Path) -> str | None:
    try:
        data = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data.get("id") if isinstance(data, dict) else None


def default_pack_dirs() -> list[Path]:
    """All pack directories under content/packs of this repository, ``core`` first."""
    dirs = sorted(p for p in DEFAULT_PACKS_DIR.iterdir() if (p / "pack.yaml").is_file())
    return sorted(dirs, key=lambda p: p.name != "core")
