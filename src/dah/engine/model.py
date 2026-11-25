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
    # stage 3 (SPY_AND_CONQUEST.md 1.3; engine/conquest.py): {"occupied": {by, from, since}, "ruined_until",
    # "ravaged_until", "looted_turn", "sabotaged": {...}}; left out of state_hash while empty
    status: dict[str, Any] = field(default_factory=dict)

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
    # stage 3 (SPY_AND_CONQUEST.md 1.3): left out of state_hash while empty
    home: str | None = None                                 # start capital; set at turn 1 when a stage-3 system is on
    shades: list[dict[str, Any]] = field(default_factory=list)   # own Shades (espionage)
    spy_seq: dict[str, int] = field(default_factory=dict)        # per-seat counters: shade, report
    conquest_policy: str | None = None                      # default disposition of captures (kept between turns)
    # stage 4 (SCORING.md 3): left out of state_hash while empty. tally {war_value: {victim: int}, homes_taken: [pid]}
    # only grows; objectives [{id, target?}] are the seat's secret objectives (dealt at the start)
    tally: dict[str, Any] = field(default_factory=dict)
    objectives: list[dict[str, Any]] = field(default_factory=list)


PLAYER_STAGE2_FIELDS = ("techs", "research", "researching", "artifacts")
PLAYER_STAGE3_FIELDS = ("home", "shades", "spy_seq", "conquest_policy")
PLAYER_SCORE_FIELDS = ("tally", "objectives")
PROVINCE_STAGE3_FIELDS = ("status",)
STATE_STAGE3_FIELDS = ("claims", "origins", "abandoned", "discoveries")


@dataclass
class PendingBuild:
    seat: str
    province: str
    building: str
    turns_left: int
    # core 0.7.0 (decision 27, `payment: parts`): the whole price fixed at the order (its shares come from it) and
    # whether the last build step found the share unpaid; both left out of state_hash while empty, so builds paid in
    # full hash as before
    cost: dict[str, int] = field(default_factory=dict)
    stalled: bool = False


PENDING_BUILD_NEW_FIELDS = ("cost", "stalled")


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
    # stage 3 (SPY_AND_CONQUEST.md 1.3, 3.5), left out of state_hash while empty: public claims [{seat, province,
    # until_turn}], and the start regions {province: seat} (filled at turn 1 only when conquest is on)
    claims: list[dict[str, Any]] = field(default_factory=list)
    origins: dict[str, str] = field(default_factory=dict)
    # stage 5 (ABANDONED_LANDS.md 11, engine/discoveries.py), left out of state_hash while empty: the public list of
    # Abandoned provinces, and the finds {pid: {id, state, known, pending?}} (secret: never in a seat's view)
    abandoned: list[str] = field(default_factory=list)
    discoveries: dict[str, dict[str, Any]] = field(default_factory=dict)

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
            claims=[dict(c) for c in data.get("claims") or []],
            origins=dict(data.get("origins") or {}),
            abandoned=list(data.get("abandoned") or []),
            discoveries={k: dict(v) for k, v in (data.get("discoveries") or {}).items()},
        )

    def owned(self, seat: str) -> list[Province]:
        return [p for _, p in sorted(self.provinces.items()) if p.owner == seat]


def state_hash(state: GameState) -> str:
    data = state.to_dict()
    data.pop("seq")
    if not data.get("pacts"):
        data.pop("pacts", None)
    for key in STATE_STAGE3_FIELDS:
        if not data.get(key):
            data.pop(key, None)
    for player in data["players"].values():
        for key in PLAYER_STAGE2_FIELDS + PLAYER_STAGE3_FIELDS + PLAYER_SCORE_FIELDS:
            if not player.get(key):
                player.pop(key, None)
    for build in data["pending_builds"]:
        for key in PENDING_BUILD_NEW_FIELDS:
            if not build.get(key):
                build.pop(key, None)
    for prov in data["provinces"].values():
        for key in PROVINCE_STAGE3_FIELDS:
            if not prov.get(key):
                prov.pop(key, None)
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
    # stage 3 (SPY_AND_CONQUEST.md 1.4): written by to_dict only for their own kinds (`feint` only when true), so
    # orders_hash of older books does not change
    feint: bool = False              # move: a false entry (espionage)
    shade: str | None = None         # spy, recall
    mission: str | None = None       # spy
    target: str | None = None        # spy: a province or a seat
    rounds: int | None = None        # spy
    disposition: str | None = None   # dispose; policy (None: no policy)
    to: str | None = None            # dispose (release): the recipient seat
    choice: str | None = None        # discover (stage 5): the variant of a mixed find

    @property
    def hasty(self) -> bool:
        return self.kind == "move" and self.phase_added == P2

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "phase_added": self.phase_added}
        if self.kind == "move":
            out.update({"from": self.src, "to": self.dst, "units": dict(self.units or {}), "pact": self.pact})
            if self.feint:
                out["feint"] = True
        elif self.kind == "recruit":
            out.update({"province": self.province, "unit": self.unit, "count": self.count})
        elif self.kind == "build":
            out.update({"province": self.province, "building": self.building})
        elif self.kind == "research":
            out["tech"] = self.tech
        elif self.kind == "hire_shade":
            pass
        elif self.kind == "spy":
            out.update({"shade": self.shade, "mission": self.mission, "target": self.target, "rounds": self.rounds})
        elif self.kind == "recall":
            out["shade"] = self.shade
        elif self.kind == "dispose":
            out.update({"province": self.province, "disposition": self.disposition})
            if self.to is not None:
                out["to"] = self.to
        elif self.kind == "policy":
            out["disposition"] = self.disposition
        elif self.kind == "discover":
            out.update({"province": self.province, "choice": self.choice})
        else:       # fortify, ward
            out["province"] = self.province
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Order:
        recipient = data["kind"] == "dispose"       # "to" is a dispose's recipient, a move's target otherwise
        return cls(
            id=data["id"], kind=data["kind"], phase_added=data.get("phase_added", ACTIONS),
            src=data.get("from"), dst=None if recipient else data.get("to"),
            units=dict(data["units"]) if data.get("units") is not None else None,
            province=data.get("province"), unit=data.get("unit"), count=data.get("count"),
            building=data.get("building"), pact=data.get("pact") or HONOR, tech=data.get("tech"),
            feint=bool(data.get("feint", False)), shade=data.get("shade"), mission=data.get("mission"),
            target=data.get("target"), rounds=data.get("rounds"), disposition=data.get("disposition"),
            to=data.get("to") if recipient else None,
            choice=data.get("choice") if data["kind"] == "discover" else None,
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
