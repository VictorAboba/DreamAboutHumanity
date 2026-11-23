"""Gifts and pacts (CONCEPT.md section 7, user decision 14): pure state operations shared by the live game and replay.

A diplomatic action is an *op* (a JSON dict). The live game checks a request with ``make_*`` (which raises
DiplomacyError and does not change the state), applies the op with ``apply_op`` and keeps the turn's ops; replay
applies the same ops in the same order before resolving the turn, so a game with gifts and pacts replays exactly.

    {"op": "gift", "sender", "recipient", "resources": {res: n}, "turn", "phase"}
    {"op": "propose", "id", "sender", "recipient", "turns": int | None, "turn", "phase"}
    {"op": "respond", "id", "seat", "accept": bool, "turn", "phase"}
    {"op": "leave", "id", "seat", "turn", "phase"}       # an active pact: notice; a proposal: its proposer withdraws

A pact is a dict in ``GameState.pacts``: {id, a (proposer), b (recipient), turns, status, proposed_turn,
proposed_phase, since_turn, until_turn, leaving, ends_before_turn, ended_turn, ended_reason, broken_by}.
Statuses: proposed -> active | rejected | withdrawn | lapsed (unanswered when its turn is resolved);
active -> left | broken | expired | ended (a side was eliminated).

Timing: a pact accepted in turn T with ``turns = N`` protects the resolutions of turns T..T+N-1 (``until_turn``) and
expires after the last of them; ``turns = None`` is indefinite. Leaving is public at once and takes effect at the
start of a resolution the partner had a whole actions phase to prepare for: left in negotiation_1 of turn T -> it no
longer protects in the resolution of turn T; left in negotiation_2 of turn T -> it still protects in turn T and ends
at the start of the resolution of turn T+1 (``ends_before_turn``).
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .content import Ruleset
from .model import GameState, as_int

PROPOSED, ACTIVE = "proposed", "active"
REJECTED, WITHDRAWN, LAPSED = "rejected", "withdrawn", "lapsed"
LEFT, BROKEN, EXPIRED, ENDED = "left", "broken", "expired", "ended"
NEGOTIATION_PHASES = ("negotiation_1", "negotiation_2")
MAX_PACT_TURNS = 100


class DiplomacyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- queries

def pact_by_id(state: GameState, pact_id: Any) -> dict[str, Any] | None:
    return next((p for p in state.pacts if p["id"] == pact_id), None)


def pair_of(pact: Mapping[str, Any]) -> frozenset[str]:
    return frozenset((pact["a"], pact["b"]))


def partner_of(pact: Mapping[str, Any], seat: str) -> str:
    return pact["b"] if pact["a"] == seat else pact["a"]


def active_pacts(state: GameState) -> list[dict[str, Any]]:
    return [p for p in state.pacts if p["status"] == ACTIVE]


def in_force(pact: Mapping[str, Any], turn: int) -> bool:
    """The pact protects in the resolution of `turn` (an active pact whose notice has not taken effect yet)."""
    return pact["status"] == ACTIVE and not (pact.get("ends_before_turn") is not None
                                             and pact["ends_before_turn"] <= turn)


def pact_between(state: GameState, x: str | None, y: str | None, turn: int | None = None) -> dict[str, Any] | None:
    """The active pact of two seats; with `turn`, only one that is in force in that turn's resolution."""
    if x is None or y is None or x == y:
        return None
    for p in active_pacts(state):
        if pair_of(p) == {x, y} and (turn is None or in_force(p, turn)):
            return p
    return None


def public_pact(pact: Mapping[str, Any]) -> dict[str, Any]:
    """What everyone may know about an active pact: the sides and its terms, not its id (an id would count the
    pair's private proposals)."""
    return {"seats": sorted((pact["a"], pact["b"])), "turns": pact["turns"], "since_turn": pact["since_turn"],
            "until_turn": pact["until_turn"], "leaving": pact.get("leaving"),
            "ends_before_turn": pact.get("ends_before_turn")}


# ---------------------------------------------------------------- checks (no state change)

def _living_other(state: GameState, seat: str, other: Any) -> str:
    if not isinstance(other, str) or other not in state.players:
        raise DiplomacyError("UNKNOWN_ID", f"нет игрока {str(other)[:40]!r}")
    if other == seat:
        raise DiplomacyError("INVALID", "нельзя самому себе")
    if not state.players[other].alive:
        raise DiplomacyError("INVALID", f"{state.players[other].nick} выбыл из игры")
    return other


def _require_turn(state: GameState, seat: str, phase: str) -> None:
    if phase not in NEGOTIATION_PHASES:
        raise DiplomacyError("PHASE_LOCKED", "дипломатия (подношения и пакты) — только в фазах переговоров П1 и П2")
    player = state.players.get(seat)
    if player is None or not player.alive:
        raise DiplomacyError("PHASE_LOCKED", "место выбыло из игры")


def parse_resources(ruleset: Ruleset, raw: Any) -> dict[str, int]:
    """{resource: n} with n a positive integer; resource ids may be full, short or the resource's name."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if not isinstance(raw, Mapping) or not raw:
        raise DiplomacyError("INVALID", "resources — объект {ресурс: число}, например {\"alloy\": 10}")
    out: dict[str, int] = {}
    for ref, n in raw.items():
        rid = _resource_ref(ruleset, ref)
        if rid is None:
            known = ", ".join(f"{r} ({d.name})" for r, d in ruleset.resources.items())
            raise DiplomacyError("UNKNOWN_ID", f"нет ресурса {str(ref)[:40]!r}; ресурсы: {known}")
        n = n if isinstance(n, bool) else as_int(n)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n >= 2 ** 31:
            raise DiplomacyError("INVALID", f"количество {ref!r} — целое число ≥ 1")
        out[rid] = out.get(rid, 0) + n
    return dict(sorted(out.items()))


def _resource_ref(ruleset: Ruleset, ref: Any) -> str | None:
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    full = ruleset.resolve("resource", ref) or ruleset.resolve("resource", ref.lower())
    if full:
        return full
    hits = [r for r, d in ruleset.resources.items() if ref.lower() in (d.name.lower(), d.short.lower())]
    return hits[0] if len(hits) == 1 else None


def make_gift(state: GameState, ruleset: Ruleset, seat: str, to: Any, resources: Any, phase: str,
              reserved: Mapping[str, int]) -> dict[str, Any]:
    """A gift from the seat's free (unreserved) resources. Raises DiplomacyError."""
    _require_turn(state, seat, phase)
    recipient = _living_other(state, seat, to)
    amounts = parse_resources(ruleset, resources)
    player = state.players[seat]
    for res, n in amounts.items():
        free = player.resources.get(res, 0) - reserved.get(res, 0)
        if n > free:
            raise DiplomacyError("NOT_ENOUGH_RESOURCES", f"{ruleset.resources[res].name}: свободно {max(free, 0)} "
                                                         f"(остальное зарезервировано приказами), нужно {n}")
    return {"op": "gift", "sender": seat, "recipient": recipient, "resources": amounts, "turn": state.turn,
            "phase": phase}


def _parse_turns(turns: Any) -> int | None:
    if turns is None or turns == "" or (isinstance(turns, str) and turns.strip().lower() in ("null", "none")):
        return None
    turns = turns if isinstance(turns, bool) else as_int(turns)
    if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= MAX_PACT_TURNS:
        raise DiplomacyError("INVALID", f"turns — число ходов 1..{MAX_PACT_TURNS} или null (бессрочно)")
    return turns


def make_proposal(state: GameState, seat: str, to: Any, turns: Any, phase: str) -> dict[str, Any]:
    _require_turn(state, seat, phase)
    recipient = _living_other(state, seat, to)
    n = _parse_turns(turns)
    pair = {seat, recipient}
    for p in state.pacts:
        # a pact whose leave notice already took effect (left in P1 of this turn) no longer protects anyone: the
        # pair may sign a new one at once; it ends at the start of this resolution
        if pair_of(p) == pair and p["status"] == ACTIVE and in_force(p, state.turn):
            if p.get("leaving"):
                raise DiplomacyError("PACT_EXISTS", f"пакт с {state.players[recipient].nick} ({p['id']}) ещё "
                                                    f"действует: выход объявлен, конец перед расчётом хода "
                                                    f"{p['ends_before_turn']}; новый пакт можно предложить с хода "
                                                    f"{p['ends_before_turn']}")
            raise DiplomacyError("PACT_EXISTS", f"пакт с {state.players[recipient].nick} уже действует ({p['id']})")
        if pair_of(p) == pair and p["status"] == PROPOSED:
            raise DiplomacyError("PACT_EXISTS", f"предложение пакта {p['id']} между вами уже ждёт ответа "
                                                f"(respond_pact или leave_pact, чтобы отозвать своё)")
    x, y = sorted(pair)
    k = 1 + sum(1 for p in state.pacts if pair_of(p) == pair)
    return {"op": "propose", "id": f"pact-{x}-{y}-{k}", "sender": seat, "recipient": recipient, "turns": n,
            "turn": state.turn, "phase": phase}


def make_response(state: GameState, seat: str, pact_id: Any, decision: Any, phase: str) -> dict[str, Any]:
    _require_turn(state, seat, phase)
    pact = pact_by_id(state, pact_id)
    if pact is None or seat not in (pact["a"], pact["b"]):
        raise DiplomacyError("UNKNOWN_ID", f"нет пакта {str(pact_id)[:40]!r}; list_pacts() покажет твои")
    if pact["status"] != PROPOSED:
        raise DiplomacyError("INVALID", f"пакт {pact['id']} не ждёт ответа (статус {pact['status']})")
    if pact["b"] != seat:
        raise DiplomacyError("INVALID", "отвечает тот, кому предложили; своё предложение можно отозвать leave_pact")
    if isinstance(decision, str):
        decision = decision.strip().lower()
    if decision in ("accept", True, "yes", "да", "принять"):
        accept = True
    elif decision in ("reject", False, "no", "нет", "отклонить"):
        accept = False
    else:
        raise DiplomacyError("INVALID", "decision — accept или reject")
    if accept and not state.players[pact["a"]].alive:
        raise DiplomacyError("INVALID", "предложивший выбыл из игры")
    return {"op": "respond", "id": pact["id"], "seat": seat, "accept": accept, "turn": state.turn, "phase": phase}


def make_leave(state: GameState, seat: str, pact_id: Any, phase: str) -> dict[str, Any]:
    _require_turn(state, seat, phase)
    pact = pact_by_id(state, pact_id)
    if pact is None or seat not in (pact["a"], pact["b"]):
        raise DiplomacyError("UNKNOWN_ID", f"нет пакта {str(pact_id)[:40]!r}; list_pacts() покажет твои")
    if pact["status"] == PROPOSED:
        if pact["a"] != seat:
            raise DiplomacyError("INVALID", "это предложение тебе: ответь respond_pact(id, \"reject\")")
    elif pact["status"] != ACTIVE:
        raise DiplomacyError("INVALID", f"пакт {pact['id']} уже не действует (статус {pact['status']})")
    elif pact.get("leaving"):
        raise DiplomacyError("INVALID", f"из пакта {pact['id']} уже объявлен выход "
                                        f"(конец перед расчётом хода {pact['ends_before_turn']})")
    return {"op": "leave", "id": pact["id"], "seat": seat, "turn": state.turn, "phase": phase}


# ---------------------------------------------------------------- applying

def apply_op(state: GameState, op: Mapping[str, Any]) -> dict[str, Any] | None:
    """Apply one op to the state in place; returns the pact it touched (None for a gift). Raises DiplomacyError
    when the op does not fit the state (a replay of a different game)."""
    kind = op.get("op")
    if kind == "gift":
        giver, taker = state.players[op["sender"]], state.players[op["recipient"]]
        for res, n in op["resources"].items():
            if giver.resources.get(res, 0) < n:
                raise DiplomacyError("NOT_ENOUGH_RESOURCES", f"gift of {n} {res}: not enough")
            giver.resources[res] -= n
            taker.resources[res] = taker.resources.get(res, 0) + n
        return None
    if kind == "propose":
        pact = {"id": op["id"], "a": op["sender"], "b": op["recipient"], "turns": op["turns"], "status": PROPOSED,
                "proposed_turn": op["turn"], "proposed_phase": op["phase"], "since_turn": None, "until_turn": None,
                "leaving": None, "ends_before_turn": None, "ended_turn": None, "ended_reason": None,
                "broken_by": []}
        state.pacts.append(pact)
        return pact
    pact = pact_by_id(state, op.get("id"))
    if pact is None:
        raise DiplomacyError("UNKNOWN_ID", f"no pact {op.get('id')!r}")
    if kind == "respond":
        if op["accept"]:
            pact.update(status=ACTIVE, since_turn=op["turn"],
                        until_turn=None if pact["turns"] is None else op["turn"] + pact["turns"] - 1)
        else:
            pact.update(status=REJECTED, ended_turn=op["turn"])
        return pact
    if kind == "leave":
        if pact["status"] == PROPOSED:
            pact.update(status=WITHDRAWN, ended_turn=op["turn"])
        else:
            pact.update(leaving=op["seat"],
                        ends_before_turn=op["turn"] + (1 if op["phase"] == "negotiation_2" else 0))
        return pact
    raise DiplomacyError("INVALID", f"unknown op {kind!r}")


def apply_ops(state: GameState, ops: Iterable[Mapping[str, Any]]) -> None:
    for op in ops:
        apply_op(state, op)


# Events of diplomacy in the turn report. Kinds only their two seats see; and fields only the two seats (and the admin)
# see in a public event: gift amounts and pact ids (an id would count the pair's private proposals).
PRIVATE_KINDS = frozenset({"pact_proposed", "pact_rejected", "pact_withdrawn", "pact_lapsed"})
PUBLIC_KINDS = frozenset({"gift", "pact_signed", "pact_leave", "pact_broken", "pact_expired", "pact_ended"})
PRIVATE_FIELDS = ("resources", "pact")


def op_event(op: Mapping[str, Any], state: GameState) -> dict[str, Any] | None:
    """The turn-report event of an op (full form, with the private fields)."""
    if op["op"] == "gift":
        return {"kind": "gift", "seats": sorted((op["sender"], op["recipient"])), "province": None,
                "sender": op["sender"], "recipient": op["recipient"], "resources": dict(op["resources"]),
                "phase": op["phase"]}
    pact = pact_by_id(state, op.get("id"))
    if pact is None:
        return None
    base = {"seats": sorted((pact["a"], pact["b"])), "province": None, "pact": pact["id"], "phase": op["phase"]}
    if op["op"] == "propose":
        return {"kind": "pact_proposed", **base, "sender": op["sender"], "turns": op["turns"]}
    if op["op"] == "respond":
        return {"kind": "pact_signed" if op["accept"] else "pact_rejected", **base, "turns": pact["turns"]}
    if op["op"] == "leave":
        if op["turn"] == pact.get("ended_turn") and pact["status"] == WITHDRAWN:
            return {"kind": "pact_withdrawn", **base}
        return {"kind": "pact_leave", **base, "seat": op["seat"],
                "ends_before_turn": op["turn"] + (1 if op["phase"] == "negotiation_2" else 0)}
    return None


def public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """A public diplomacy event as a non-party sees it."""
    return {k: v for k, v in event.items() if k not in PRIVATE_FIELDS}
