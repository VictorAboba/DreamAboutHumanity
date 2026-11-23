"""Bot-side view of the game state.

`parse_state` is the single adapter between the server's `GET /v1/state?format=json` payload and the bots'
decision logic. Every guess about JSON field names lives here, so a contract mismatch is fixed in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Numbers of the `core` pack the bots plan with (SLICE.md section 3); the server stays the authority.
UNIT_COST_ALLOY: dict[str, int] = {"assault": 12, "armor": 25, "artillery": 20}
UNIT_UPKEEP_FOOD: dict[str, int] = {"assault": 1, "armor": 2, "artillery": 1}
BUILDING_COST_ALLOY: dict[str, int] = {"mine": 20, "farm": 15, "barracks": 30}
RECRUIT_BUILDINGS: frozenset[str] = frozenset({"barracks"})
BUILD_RESOURCE = "alloy"
FOOD_RESOURCE = "provisions"


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


def parse_state(payload: dict[str, Any], me: set[str] | frozenset[str]) -> StateView:
    """Adapt a `/v1/state?format=json` response (with or without the `{ok, state}` envelope) to a StateView."""
    state = payload.get("state", payload)
    player = _player_block(state)
    raw_provinces = _province_records(state.get("provinces", {}))
    edges = _edge_pairs(state.get("edges", []))

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
        )

    resources = _resources(player.get("resources", state.get("resources", {})))
    seen_ids += list(resources)
    return StateView(
        turn=int(state.get("turn", 0)),
        phase=str(state.get("phase", "")),
        me=frozenset(me),
        resources={short_id(r): v for r, v in resources.items()},
        command_points=int(player.get("command_points", player.get("cp", state.get("command_points", 0)))),
        provinces=provinces,
        id_prefix=_prefix(seen_ids),
    )


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
