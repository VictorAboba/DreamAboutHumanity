"""Bot-side view of the game state.

`parse_state` is the single adapter between the server's `GET /v1/state?format=json` payload and the bots'
decision logic. Every guess about JSON field names lives here, so a contract mismatch is fixed in one place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

# Numbers of the `core` pack (0.3.0) the bots plan with (SLICE.md section 3, TECH_AND_SETUP.md section 8); the
# server stays the authority (faction modifiers, e.g. cheaper buildings, only make the bots plan conservatively).
# tests/test_bots_logic.py checks they match content/packs/core.
UNIT_COST_ALLOY: dict[str, int] = {"assault": 12, "armor": 25, "artillery": 15, "militia": 8, "outriders": 10,
                                   "dreamwalker": 14, "mortar": 22, "titan": 45,
                                   "throne_warden": 14, "faceless": 13, "ash_cantor": 12, "devourer": 10,
                                   "rift_corsair": 12}
UNIT_UPKEEP_FOOD: dict[str, int] = {"assault": 1, "armor": 1, "artillery": 1, "militia": 1, "outriders": 1,
                                    "dreamwalker": 1, "mortar": 1, "titan": 2,
                                    "throne_warden": 1, "faceless": 1, "ash_cantor": 1, "devourer": 2,
                                    "rift_corsair": 1}
# Costs beyond alloy (short resource id -> amount); units not listed cost only alloy.
UNIT_COST_OTHER: dict[str, dict[str, int]] = {}    # costs beyond alloy (none in core 0.3.0: creed is for techs)
# The faction's unique unit (stage 2): faction short id -> unit short id.
UNIQUE_UNITS: dict[str, str] = {"litany": "throne_warden", "synclite": "faceless", "ash_choir": "ash_cantor",
                                "growth": "devourer", "captains": "rift_corsair"}
BUILDING_COST_ALLOY: dict[str, int] = {"mine": 10, "farm": 8, "barracks": 30, "temple": 15, "smithy": 25,
                                       "citadel": 40, "warehouse": 20, "hq": 35, "monument": 10, "dream_spire": 60}
# Stage 4 (docs/SCORING.md 2.3, 10): great works of core 0.5.0. Provisions beyond alloy, build turns, points and the
# province types allowed (None: any); tests/test_bots_logic.py checks them, the simulator re-reads them from its pack.
BUILDING_COST_FOOD: dict[str, int] = {"monument": 25, "dream_spire": 100}
GREAT_WORKS: dict[str, tuple[int, int, tuple[str, ...] | None]] = {
    "monument": (2, 3, None), "dream_spire": (4, 7, ("agri", "forge", "shrine", "waste"))}
# Core 0.6.0 (decision 26): points a broken pact costs its breaker in a scored game (score.betrayal.per_break of the
# scored scenarios); tests/test_bots_logic.py checks it, the simulator re-reads it from its pack.
BETRAYAL_PENALTY = 3
RESEARCH_BUILDING = "temple"  # +research income
RECRUIT_BUILDINGS: frozenset[str] = frozenset({"barracks", "hq"})
FARM_SITE = "agri"  # province type a farm prefers
BUILD_RESOURCE = "alloy"
FOOD_RESOURCE = "provisions"

# Stage 3 (docs/SPY_AND_CONQUEST.md sections 5, 7): the numbers of core 0.4.0 the bots plan espionage and conquest
# with; tests/test_bots_logic.py checks them against content/packs/core, the simulator re-reads them from its pack.
SHADE_COST_ALLOY = 20      # core 0.6.0 (was 25); every seat also holds one Shade from the start
SHADE_UPKEEP_FOOD = 1
# mission short id -> (whisper needed, rounds_max, alloy once, provisions per round)
MISSIONS: dict[str, tuple[int, int, int, int]] = {"observe": (1, 5, 0, 1), "ledger": (1, 3, 0, 1),
                                                  "secret": (1, 3, 0, 2), "read_orders": (2, 3, 0, 2),
                                                  "sabotage": (2, 4, 8, 1)}
# disposition short id -> provisions per point of conquest value (before the faction multiplier)
DISPOSITION_FOOD: dict[str, int] = {"subjugate": 5, "exterminate": 0, "raid": 0, "liberate": 0}
CONQUEST_VALUE: dict[str, int] = {"capital": 3}     # province type short id -> value (others 1)
# faction short id -> disposition short id -> cost multiplier
DISPOSITION_COST_MUL: dict[str, dict[str, float]] = {"growth": {"subjugate": 1.2}}


def short_id(full_id: str) -> str:
    """`core:assault` -> `assault`; short IDs pass through unchanged."""
    return full_id.rsplit(":", 1)[-1]


@dataclass(frozen=True)
class Province:
    id: str
    name: str
    type: str  # as sent by the server (used verbatim in estimate requests)
    owner: str | None
    units: dict[str, int] | None  # short unit id -> count; None when hidden by fog of war
    buildings: tuple[str, ...]  # short building ids
    neighbors: tuple[str, ...]
    fortified: bool
    status: Mapping[str, Any] = field(default_factory=dict)  # public conquest status (occupied, ruined_until, ...)
    origin: str | None = None  # the seat that started with it (conquest games)
    building_now: tuple[str, ...] = ()  # short ids building here (own provinces; others: great works only)
    # stage 5 (docs/ABANDONED_LANDS.md 13): public marks of the Abandoned lands and the find this seat knows
    abandoned: bool = False
    ruins: bool = False
    guard: Mapping[str, Any] | None = None  # {lair, cap?}: neutral guard (ruins) or lair, where shown
    discovery: Mapping[str, Any] | None = None  # {id (short), state}: a find this seat knows (Observation, entry)

    @property
    def occupied_by(self) -> str | None:
        occ = self.status.get("occupied") if self.status else None
        return str(occ.get("by")) if isinstance(occ, Mapping) and occ.get("by") else None

    @property
    def kind(self) -> str:
        return short_id(self.type)

    @property
    def unit_count(self) -> int:
        return sum(self.units.values()) if self.units else 0


@dataclass(frozen=True)
class StateView:
    turn: int
    phase: str
    me: frozenset[str]  # seat and nick: whatever the server may use as `owner`
    resources: dict[str, int]  # short resource id -> amount
    command_points: int
    provinces: dict[str, Province]
    id_prefix: str  # e.g. "core:" when the server uses full IDs, else ""
    seat: str = ""  # own seat id as the server reports it ("you.seat")
    turn_limit: int = 0  # 0 when the server did not say
    players: tuple[str, ...] = ()  # seats of living players (self included)
    pacts: tuple[dict[str, Any], ...] = ()  # active pacts: {seats: [a, b], id?: own pacts only, ...}
    pact_proposals: tuple[dict[str, Any], ...] = ()  # own open proposals: {id, from, to, turns}
    net_income: dict[str, int] | None = None  # short resource id -> income minus upkeep; None if not sent
    faction: str = ""  # own faction, short id ("" when not sent)
    techs: tuple[str, ...] | None = None  # own researched techs (short ids); None: the game has no techs
    researching: str | None = None  # own active tech (short id)
    artifacts: tuple[str, ...] = ()  # own artifacts (short ids)
    # stage 3: None when the system is off in this game
    espionage: Mapping[str, Any] | None = None  # you.espionage: whisper, mirrors, shades_max, active_limit, shades
    conquest_policy: str | None = None
    conquest: bool = False
    claims: tuple[Mapping[str, Any], ...] = ()
    scores: Mapping[str, int] = field(default_factory=dict)  # seat -> public score
    # stage 4: seat -> {source: points or None (hidden by fog)} for the others (players[].score_known); empty when the
    # server sends none (the score is then exact for all)
    score_known: Mapping[str, Mapping[str, int | None]] = field(default_factory=dict)
    # stage 4 (docs/SCORING.md 8, 10): None / empty with the default scoring (land only)
    score_parts: Mapping[str, int] | None = None  # own turn score by source (you.score_parts)
    objectives: tuple[Mapping[str, Any], ...] = ()  # own secret objectives (you.objectives)
    war: Mapping[str, int] | None = None  # you.war: points, cap_left
    # stage 5 (docs/ABANDONED_LANDS.md 8, 13): own pending choices of mixed finds
    # [{province, id (short), name, default, choices: [{id, ev, risk}]}]; empty without them
    pending_finds: tuple[Mapping[str, Any], ...] = ()

    def scoring(self, source: str) -> bool:
        """The score source is on in this game (it appears in the own breakdown)."""
        return self.score_parts is not None and source in self.score_parts

    def open_objectives(self) -> list[Mapping[str, Any]]:
        """Own objectives not met right now."""
        return [o for o in self.objectives if not o.get("met_now")]

    @property
    def whisper(self) -> int:
        return int((self.espionage or {}).get("whisper", 0))

    @property
    def shades(self) -> list[Mapping[str, Any]]:
        return list((self.espionage or {}).get("shades") or [])

    def turns_left(self) -> int:
        """Turns still to play, this one included (0 when the server did not say the limit)."""
        return max(0, self.turn_limit - self.turn + 1) if self.turn_limit else 0

    def free_food(self) -> int:
        """Provisions free to spend: the stock less two turns of a food deficit (never starve the army for it)."""
        deficit = -min(0, (self.net_income or {}).get(FOOD_RESOURCE, 0))
        return max(0, self.resource(FOOD_RESOURCE) - 2 * deficit)

    def disposition_food(self, disposition: str, province: Province) -> int:
        """Provisions a disposition of `province` costs this seat (rounded half up, as the engine does)."""
        base = DISPOSITION_FOOD.get(disposition, 0) * CONQUEST_VALUE.get(province.kind, 1)
        mul = DISPOSITION_COST_MUL.get(self.faction, {}).get(disposition, 1.0)
        return int(math.floor(base * mul + 0.5))

    def full_provinces(self) -> list[Province]:
        """Own provinces where building and recruiting work: not in Occupation, not ruins (conquest games)."""
        return [p for p in self.own_provinces() if p.occupied_by is None and not (p.status or {}).get("ruined_until")]

    def own_occupations(self) -> list[Province]:
        """Own provinces held in Occupation (taken from a seat, no fate decided yet)."""
        return [p for p in self.own_provinces() if p.occupied_by is not None and p.occupied_by in self.me]

    @property
    def research_enabled(self) -> bool:
        return self.techs is not None

    @property
    def unique_unit(self) -> str | None:
        """The own faction's unique unit (short id), if the bots know one."""
        return UNIQUE_UNITS.get(self.faction)

    def affordable(self, unit: str, alloy: int) -> int:
        """How many `unit` `alloy` and the other stocks pay for."""
        counts = [alloy // UNIT_COST_ALLOY[unit]] if UNIT_COST_ALLOY.get(unit) else []
        counts += [self.resource(r) // n for r, n in UNIT_COST_OTHER.get(unit, {}).items() if n]
        return max(0, min(counts)) if counts else 0

    def is_mine(self, province: Province) -> bool:
        return province.owner is not None and province.owner in self.me

    def own_provinces(self) -> list[Province]:
        return [p for _, p in sorted(self.provinces.items()) if self.is_mine(p)]

    def neighbors(self, province: Province) -> list[Province]:
        return [self.provinces[n] for n in sorted(province.neighbors) if n in self.provinces]

    def qualify(self, short: str) -> str:
        """Short content ID -> the form the server expects in orders."""
        return f"{self.id_prefix}{short}"

    def resource(self, short: str) -> int:
        return self.resources.get(short, 0)

    def pact_with(self, other: str | None) -> dict[str, Any] | None:
        """Own active pact with `other` (a seat), if any."""
        if other is None:
            return None
        return next((p for p in self.pacts if other in p.get("seats", ()) and self.me & set(p.get("seats", ()))),
                    None)

    def is_partner(self, other: str | None) -> bool:
        return self.pact_with(other) is not None

    def feedable(self, unit: str, turns: int) -> int | None:
        """How many more `unit` the food stock and net food income keep fed for `turns` turns (None: unknown)."""
        if self.net_income is None or turns <= 0:
            return None
        per_unit = UNIT_UPKEEP_FOOD.get(unit, 1) or 1
        budget = self.resource(FOOD_RESOURCE) + turns * self.net_income.get(FOOD_RESOURCE, 0)
        return max(0, budget // (per_unit * turns))


def farm_site(view: StateView) -> Province | None:
    """Where a new farm goes (an agri world first), or None when every own province has one."""
    sites = [p for p in view.full_provinces() if "farm" not in p.buildings]
    preferred = [p for p in sites if p.kind == FARM_SITE]
    return (preferred or sites or [None])[0]


def parse_state(payload: dict[str, Any], me: set[str] | frozenset[str]) -> StateView:
    """Adapt a `/v1/state?format=json` response (with or without the `{ok, state}` envelope) to a StateView."""
    state = payload.get("state", payload)
    player = _player_block(state)
    raw_provinces = _province_records(state.get("provinces", {}))
    edges = _edge_pairs(state.get("edges", []))
    abandoned = {str(p) for p in state.get("abandoned") or []}

    seen_ids: list[str] = []
    provinces: dict[str, Province] = {}
    for record in raw_provinces:
        pid = str(record["id"])
        owner = _owner(record.get("owner"))
        units = _units(record.get("units"), owner)
        buildings = [str(b) for b in record.get("buildings") or []]
        neighbors = {str(n) for n in record.get("neighbors") or record.get("adjacent") or []}
        neighbors |= {b for a, b in edges if a == pid} | {a for a, b in edges if b == pid}
        ptype = str(record.get("type", ""))
        seen_ids += [ptype, *buildings, *(units or {})]
        provinces[pid] = Province(
            id=pid,
            name=str(record.get("name", pid)),
            type=ptype,
            owner=owner,
            units={short_id(u): n for u, n in units.items()} if units is not None else None,
            buildings=tuple(short_id(b) for b in buildings),
            neighbors=tuple(sorted(neighbors)),
            fortified=bool(record.get("fortified", False)),
            status=dict(record.get("status") or {}),
            origin=str(record["origin"]) if record.get("origin") else None,
            building_now=tuple(short_id(str(b.get("building"))) for b in record.get("building_now") or []
                               if isinstance(b, dict) and b.get("building")),
            abandoned=bool(record.get("abandoned")) or pid in abandoned,
            ruins=bool(record.get("ruins")),
            guard=dict(record["guard"]) if isinstance(record.get("guard"), dict) else None,
            discovery=({**record["discovery"], "id": short_id(str(record["discovery"].get("id")))}
                       if isinstance(record.get("discovery"), dict) else None),
        )

    resources = _resources(player.get("resources", state.get("resources", {})))
    seen_ids += list(resources)
    players = tuple(str(p.get("seat")) for p in state.get("players") or []
                    if isinstance(p, dict) and p.get("alive", True))
    return StateView(
        turn=int(state.get("turn", 0)),
        phase=str(state.get("phase", "")),
        me=frozenset(me),
        resources={short_id(r): v for r, v in resources.items()},
        command_points=int(player.get("command_points", player.get("cp", state.get("command_points", 0)))),
        provinces=provinces,
        id_prefix=_prefix(seen_ids),
        seat=str(player.get("seat", "")),
        turn_limit=int(state.get("turn_limit") or 0),
        players=players,
        pacts=tuple(dict(p) for p in state.get("pacts") or [] if isinstance(p, dict)),
        pact_proposals=tuple(dict(p) for p in state.get("pact_proposals") or [] if isinstance(p, dict)),
        net_income=_net_income(player.get("net_income")),
        faction=short_id(str(player.get("faction") or "")),
        techs=tuple(short_id(str(t)) for t in player["techs"]) if isinstance(player.get("techs"), list) else None,
        researching=_researching(player.get("researching")),
        artifacts=tuple(short_id(str(a)) for a in player.get("artifacts") or []),
        espionage=dict(player["espionage"]) if isinstance(player.get("espionage"), dict) else None,
        conquest_policy=short_id(str(player["conquest_policy"])) if player.get("conquest_policy") else None,
        conquest="conquest_policy" in player or "claims" in state,
        claims=tuple(dict(c) for c in state.get("claims") or [] if isinstance(c, dict)),
        scores={str(p.get("seat")): int(p.get("score") or 0) for p in state.get("players") or []
                if isinstance(p, dict) and p.get("seat") is not None},
        score_known={str(p.get("seat")): {str(k): (None if v is None else int(v)) for k, v in p["score_known"].items()}
                     for p in state.get("players") or [] if isinstance(p, dict) and isinstance(p.get("score_known"), dict)},
        score_parts=({str(k): int(v or 0) for k, v in player["score_parts"].items()}
                     if isinstance(player.get("score_parts"), dict) else None),
        objectives=tuple(dict(o) for o in player.get("objectives") or [] if isinstance(o, dict)),
        war=dict(player["war"]) if isinstance(player.get("war"), dict) else None,
        pending_finds=tuple({**e, "id": short_id(str(e.get("id")))} for e in state.get("discoveries_pending") or []
                            if isinstance(e, dict) and e.get("province")),
    )


def _researching(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("tech")
    return short_id(str(raw)) if raw else None


def _net_income(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    return {short_id(str(r)): int(v) for r, v in raw.items() if isinstance(v, (int, float))}


def _player_block(state: dict[str, Any]) -> dict[str, Any]:
    for key in ("you", "player", "me"):
        block = state.get(key)
        if isinstance(block, dict):
            return block
    return state


def _province_records(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        return [{"id": pid, **record} for pid, record in raw.items()]
    return list(raw)


def _edge_pairs(raw: Any) -> list[tuple[str, str]]:
    pairs = []
    for edge in raw:
        a, b = list(edge.values())[:2] if isinstance(edge, dict) else edge[:2]
        pairs.append((str(a), str(b)))
    return pairs


def _owner(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("seat") or raw.get("nick")
    return str(raw) if raw not in (None, "") else None


def _units(raw: Any, owner: str | None) -> dict[str, int] | None:
    if raw is None or isinstance(raw, str):  # missing or a "?" placeholder: hidden
        return None
    if any(isinstance(v, dict) for v in raw.values()):  # nested by owner
        nested = raw.get(owner) if owner in raw else {}
        if not nested:
            nested = {}
            for per_owner in raw.values():
                for unit, n in per_owner.items():
                    nested[unit] = nested.get(unit, 0) + int(n)
        raw = nested
    return {str(u): int(n) for u, n in raw.items() if int(n) > 0}


def _resources(raw: dict[str, Any]) -> dict[str, int]:
    result = {}
    for rid, value in raw.items():
        if isinstance(value, dict):
            value = value.get("amount", value.get("value", 0))
        result[str(rid)] = int(value)
    return result


def _prefix(ids: list[str]) -> str:
    for full_id in ids:
        if ":" in full_id:
            return full_id.rsplit(":", 1)[0] + ":"
    return ""
