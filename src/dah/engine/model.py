"""Serializable game state and orders. Everything round-trips through canonical JSON."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

PHASES = ("setup", "negotiation_1", "actions", "negotiation_2", "resolution", "game_over")
SETUP = "setup"          # before turn 1 when the scenario has a setup (TECH_AND_SETUP.md section 2)
P2 = "negotiation_2"
ACTIONS = "actions"
HONOR = "honor"
BREAK = "break"


INT_BOUND = 2 ** 31


def as_int(value: Any) -> Any:
    """"2" and 2.0 count as 2 (ASCII digits, integral floats, below 2**31); anything else is returned as is
    for the caller to refuse."""
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit() and len(text) <= 10 and int(text) < INT_BOUND:
            return int(text)
    elif isinstance(value, float) and value.is_integer() and abs(value) < INT_BOUND:
        return int(value)
    return value


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass
class Province:
    id: str
    name: str
    type: str                       # full province type id
    owner: str | None               # seat or None (neutral)
    units: dict[str, int] = field(default_factory=dict)   # owner's units, full unit id -> count
    buildings: list[str] = field(default_factory=list)
    fortified: bool = False         # fortified during the last resolved turn

    def unit_total(self) -> int:
        return sum(self.units.values())


@dataclass
class Player:
    seat: str
    nick: str
    faction: str
    resources: dict[str, int] = field(default_factory=dict)
    alive: bool = True
    command_points: int = 0
    flags: list[str] = field(default_factory=list)
    # stage 2 (TECH_AND_SETUP.md): left out of state_hash while empty, so slice games hash as before
    techs: list[str] = field(default_factory=list)          # researched, in order of completion
    research: dict[str, int] = field(default_factory=dict)  # tech -> points put in so far (kept when switching)
    researching: str | None = None                          # the active tech (persists until done or switched)
    artifacts: list[str] = field(default_factory=list)      # chosen at setup (hidden from others until game end)


PLAYER_STAGE2_FIELDS = ("techs", "research", "researching", "artifacts")


@dataclass
class PendingBuild:
    seat: str
    province: str
    building: str
    turns_left: int


@dataclass
class GameState:
    turn: int
    phase: str
    provinces: dict[str, Province]
    players: dict[str, Player]
    pending_builds: list[PendingBuild] = field(default_factory=list)
    seq: dict[str, int] = field(default_factory=dict)   # per-seat order id counters (excluded from state_hash)
    result: dict[str, Any] | None = None   # {"winners": [...], "reason": ..., "scores": {...}} at game over
    # Pacts and pact proposals, oldest first (engine/diplomacy.py); excluded from state_hash while empty, so states
    # of games without diplomacy hash as they did before pacts existed.
    pacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GameState:
        return cls(
            turn=data["turn"],
            phase=data["phase"],
            provinces={k: Province(**v) for k, v in data["provinces"].items()},
            players={k: Player(**v) for k, v in data["players"].items()},
            pending_builds=[PendingBuild(**b) for b in data.get("pending_builds", [])],
            seq=dict(data.get("seq") or {}),
            result=data.get("result"),
            pacts=[dict(p) for p in data.get("pacts") or []],
        )

    def owned(self, seat: str) -> list[Province]:
        return [p for _, p in sorted(self.provinces.items()) if p.owner == seat]


def state_hash(state: GameState) -> str:
    data = state.to_dict()
    data.pop("seq")
    if not data.get("pacts"):
        data.pop("pacts", None)
    for player in data["players"].values():
        for key in PLAYER_STAGE2_FIELDS:
            if not player.get(key):
                player.pop(key, None)
    return sha256_json(data)


def issue_order_id(state: GameState, seat: str) -> str:
    """Server-side id issuing from the seat's own counter, so ids reveal nothing about other seats' orders."""
    state.seq[seat] = state.seq.get(seat, 0) + 1
    return f"o{state.seq[seat]}"


@dataclass
class Order:
    id: str
    kind: str                        # move | recruit | build | fortify | research
    phase_added: str = ACTIONS       # actions | negotiation_2
    src: str | None = None           # move: from
    dst: str | None = None           # move: to
    units: dict[str, int] | None = None
    province: str | None = None      # recruit/build/fortify
    unit: str | None = None
    count: int | None = None
    building: str | None = None
    pact: str = HONOR                # move: "honor" (keep pacts) | "break" (fight a pact partner, ending the pact)
    tech: str | None = None          # research: the tech to make active

    @property
    def hasty(self) -> bool:
        return self.kind == "move" and self.phase_added == P2

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "phase_added": self.phase_added}
        if self.kind == "move":
            out.update({"from": self.src, "to": self.dst, "units": dict(self.units or {}), "pact": self.pact})
        elif self.kind == "recruit":
            out.update({"province": self.province, "unit": self.unit, "count": self.count})
        elif self.kind == "build":
            out.update({"province": self.province, "building": self.building})
        elif self.kind == "research":
            out["tech"] = self.tech
        else:
            out["province"] = self.province
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Order:
        return cls(
            id=data["id"], kind=data["kind"], phase_added=data.get("phase_added", ACTIONS),
            src=data.get("from"), dst=data.get("to"),
            units=dict(data["units"]) if data.get("units") is not None else None,
            province=data.get("province"), unit=data.get("unit"), count=data.get("count"),
            building=data.get("building"), pact=data.get("pact") or HONOR, tech=data.get("tech"),
        )


@dataclass
class SeatOrders:
    """One seat's orders for the current turn plus P2 accounting."""
    orders: list[Order] = field(default_factory=list)
    cp_spent: int = 0
    forfeit: dict[str, int] = field(default_factory=dict)   # reserve lost to P2 cancels/reductions

    def to_dict(self) -> dict[str, Any]:
        return {"orders": [o.to_dict() for o in self.orders], "cp_spent": self.cp_spent,
                "forfeit": dict(self.forfeit)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SeatOrders:
        return cls(orders=[Order.from_dict(o) for o in data.get("orders", [])],
                   cp_spent=data.get("cp_spent", 0), forfeit=dict(data.get("forfeit", {})))

    def find(self, order_id: str) -> Order | None:
        return next((o for o in self.orders if o.id == order_id), None)
