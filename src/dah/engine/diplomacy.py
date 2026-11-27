"""Gifts and pacts (CONCEPT.md section 7, user decision 14): pure state operations shared by the live game and replay.

A diplomatic action is an *op* (a JSON dict). The live game checks a request with ``make_*`` (which raises
DiplomacyError and does not change the state), applies the op with ``apply_op`` and keeps the turn's ops; replay
applies the same ops in the same order before resolving the turn, so a game with gifts and pacts replays exactly.

    {"op": "gift", "sender", "recipient", "resources": {res: n}, "turn", "phase"}
    {"op": "propose", "id", "sender", "recipient", "turns": int | None, "turn", "phase"[, "renews": pact id]}
    {"op": "respond", "id", "seat", "accept": bool, "turn", "phase"}
    {"op": "leave", "id", "seat", "turn", "phase"}       # an active pact: notice; a proposal: its proposer withdraws

A pact is a dict in ``GameState.pacts``: {id, a (proposer), b (recipient), turns, status, proposed_turn,
proposed_phase, since_turn, until_turn, leaving, ends_before_turn, ended_turn, ended_reason, broken_by}.
Statuses: proposed -> active | rejected | withdrawn | lapsed (unanswered when its turn is resolved);
active -> left | broken | expired | ended (a side was eliminated).

Renewal: while an active pact with a fixed end (``until_turn``) is in force and nobody has given notice, either side
may propose to renew it: ``propose`` to the partner makes a proposal with ``renews = <pact id>`` (a pact entry of its
own, answered, withdrawn and lapsing like any proposal). Accepted, it is merged into the pact it renews: the pact's
``until_turn`` grows by the renewal's ``turns`` (``None``: the pact becomes indefinite), so the renewal takes effect
exactly when the current term ends, with no gap and no overlap; the pact keeps its id and gets ``renewals`` [{id,
turns, turn, phase, from_turn}]; the renewal entry ends as ``renewed``. Leaving the pact cancels a pending renewal
(``cancelled``) and ends the pact by the usual notice rule, accepted renewals included. An indefinite pact cannot be
renewed. The extra keys appear only in games that renew, so older games hash as before.

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
RENEWED, CANCELLED = "renewed", "cancelled"      # a renewal proposal: merged into its pact | dropped by a leave
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
    pair's private proposals). A renewed pact adds `renewed` (how many times); its `until_turn` is the renewed end."""
    out = {"seats": sorted((pact["a"], pact["b"])), "turns": pact["turns"], "since_turn": pact["since_turn"],
           "until_turn": pact["until_turn"], "leaving": pact.get("leaving"),
           "ends_before_turn": pact.get("ends_before_turn")}
    if pact.get("renewals"):
        out["renewed"] = len(pact["renewals"])
    return out


def pending_renewal(state: GameState, pact_id: Any) -> dict[str, Any] | None:
    """The open renewal proposal of a pact, if any."""
    return next((p for p in state.pacts if p["status"] == PROPOSED and p.get("renews") == pact_id), None)


def renewable(pact: Mapping[str, Any], turn: int) -> bool:
    """An active pact with a fixed end, in force in `turn`, nobody leaving: it may be renewed."""
    return in_force(pact, turn) and pact["until_turn"] is not None and not pact.get("leaving")


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


def make_proposal(state: GameState, seat: str, to: Any, turns: Any, phase: str,
                  leave_price: Any = None) -> dict[str, Any]:
    """A new pact proposal; to the partner of an active pact with a fixed end, a proposal to renew it (`renews`).
    `leave_price(pact)` -> points a leave of that pact would cost the seat now (core 0.8.3, decision 34: the game
    passes scoring.leave_cost); a refusal that advises a leave names the price."""
    _require_turn(state, seat, phase)
    recipient = _living_other(state, seat, to)
    n = _parse_turns(turns)
    pair = {seat, recipient}
    nick = state.players[recipient].nick
    renews = None
    for p in state.pacts:
        # a pact whose leave notice already took effect (left in P1 of this turn) no longer protects anyone: the
        # pair may sign a new one at once; it ends at the start of this resolution
        if pair_of(p) == pair and p["status"] == ACTIVE and in_force(p, state.turn):
            if p.get("leaving"):
                raise DiplomacyError("PACT_EXISTS", f"пакт с {nick} ({p['id']}) ещё действует, но выход объявлен "
                                                    f"(конец перед расчётом хода {p['ends_before_turn']}): "
                                                    "продлить его нельзя; новый пакт можно предложить с хода "
                                                    f"{p['ends_before_turn']}")
            if p["until_turn"] is None:
                fine = leave_price(dict(p, leaving=seat, ends_before_turn=state.turn)) if leave_price else 0
                price = f"; выход из бессрочного пакта — −{fine} к твоему счёту" if fine else ""
                raise DiplomacyError("PACT_EXISTS", f"пакт с {nick} ({p['id']}) бессрочный: продлевать нечего, он "
                                                    "действует, пока кто-то не выйдет. Другие условия — выйди "
                                                    f"(leave_pact(id=\"{p['id']}\")) и предложи новый{price}")
            renewal = pending_renewal(state, p["id"])
            if renewal is not None:
                if renewal["a"] == seat:
                    raise DiplomacyError("PACT_EXISTS", f"твоё предложение продлить пакт {p['id']} ({renewal['id']}) "
                                                        "уже ждёт ответа; отозвать — "
                                                        f"leave_pact(id=\"{renewal['id']}\")")
                raise DiplomacyError("PACT_EXISTS", f"{nick} уже предлагает продлить пакт {p['id']}: прими "
                                                    f"respond_pact(id=\"{renewal['id']}\", decision=\"accept\") "
                                                    "или отклони (decision=\"reject\")")
            renews = p["id"]
        if pair_of(p) == pair and p["status"] == PROPOSED and not p.get("renews"):
            if p["a"] == seat:
                raise DiplomacyError("PACT_EXISTS", f"твоё предложение пакта {p['id']} для {nick} уже ждёт ответа; "
                                                    f"отозвать — leave_pact(id=\"{p['id']}\")")
            raise DiplomacyError("PACT_EXISTS", f"{nick} уже предлагает тебе пакт {p['id']}: прими "
                                                f"respond_pact(id=\"{p['id']}\", decision=\"accept\") или отклони "
                                                "(decision=\"reject\") — своё предлагать не нужно")
    x, y = sorted(pair)
    k = 1 + sum(1 for p in state.pacts if pair_of(p) == pair)
    op = {"op": "propose", "id": f"pact-{x}-{y}-{k}", "sender": seat, "recipient": recipient, "turns": n,
          "turn": state.turn, "phase": phase}
    if renews is not None:
        op["renews"] = renews
    return op


def make_response(state: GameState, seat: str, pact_id: Any, decision: Any, phase: str) -> dict[str, Any]:
    _require_turn(state, seat, phase)
    pact = pact_by_id(state, pact_id)
    if pact is None or seat not in (pact["a"], pact["b"]):
        raise DiplomacyError("UNKNOWN_ID", f"нет пакта {str(pact_id)[:40]!r}; list_pacts() покажет твои")
    if pact["status"] != PROPOSED:
        raise DiplomacyError("INVALID", f"пакт {pact['id']} не ждёт ответа (статус {pact['status']}): "
                                        "list_pacts() покажет, что ждёт ответа")
    if pact["b"] != seat:
        raise DiplomacyError("INVALID", f"предложение {pact['id']} сделал ты; отвечает тот, кому предложили. "
                                        f"Отозвать своё — leave_pact(id=\"{pact['id']}\")")
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
    if accept and pact.get("renews"):
        current = pact_by_id(state, pact["renews"])
        if current is None or current["status"] != ACTIVE or not renewable(current, state.turn):
            raise DiplomacyError("INVALID", f"продлевать нечего: пакт {pact['renews']} уже не действует или из него "
                                            "объявлен выход")
    return {"op": "respond", "id": pact["id"], "seat": seat, "accept": accept, "turn": state.turn, "phase": phase}


def make_leave(state: GameState, seat: str, pact_id: Any, phase: str) -> dict[str, Any]:
    _require_turn(state, seat, phase)
    pact = pact_by_id(state, pact_id)
    if pact is None or seat not in (pact["a"], pact["b"]):
        raise DiplomacyError("UNKNOWN_ID", f"нет пакта {str(pact_id)[:40]!r}; list_pacts() покажет твои")
    if pact["status"] == PROPOSED:
        if pact["a"] != seat:
            raise DiplomacyError("INVALID", f"это предложение тебе: ответь respond_pact(id=\"{pact['id']}\", "
                                            "decision=\"reject\")")
    elif pact["status"] == RENEWED:
        raise DiplomacyError("INVALID", f"продление {pact['id']} уже вошло в пакт {pact['renews']}; выйти из пакта — "
                                        f"leave_pact(id=\"{pact['renews']}\")")
    elif pact["status"] != ACTIVE:
        raise DiplomacyError("INVALID", f"пакт {pact['id']} уже не действует (статус {pact['status']})")
    elif pact.get("leaving"):
        raise DiplomacyError("INVALID", f"из пакта {pact['id']} уже объявлен выход "
                                        f"(конец перед расчётом хода {pact['ends_before_turn']})")
    return {"op": "leave", "id": pact["id"], "seat": seat, "turn": state.turn, "phase": phase}


# ---------------------------------------------------------------- applying

def transfer_resources(state: GameState, sender: str, recipient: str, amounts: Mapping[str, int]) -> None:
    """Move resources from one seat to another (a gift, a side of a trade). Raises DiplomacyError when the sender
    has not enough (nothing moves then)."""
    giver, taker = state.players[sender], state.players[recipient]
    for res, n in amounts.items():
        if giver.resources.get(res, 0) < n:
            raise DiplomacyError("NOT_ENOUGH_RESOURCES", f"transfer of {n} {res}: not enough")
    for res, n in amounts.items():
        giver.resources[res] -= n
        taker.resources[res] = taker.resources.get(res, 0) + n


def apply_op(state: GameState, op: Mapping[str, Any]) -> dict[str, Any] | None:
    """Apply one op to the state in place; returns the pact (or trade) it touched (None for a gift). Raises
    DiplomacyError when the op does not fit the state (a replay of a different game). Trade ops (`trade_*`,
    docs/TRADE.md) are applied by engine/trade.py."""
    kind = op.get("op")
    if isinstance(kind, str) and kind.startswith("trade_"):
        from . import trade      # trade builds on this module
        return trade.apply_op(state, op)
    if kind == "gift":
        transfer_resources(state, op["sender"], op["recipient"], op["resources"])
        return None
    if kind == "propose":
        pact = {"id": op["id"], "a": op["sender"], "b": op["recipient"], "turns": op["turns"], "status": PROPOSED,
                "proposed_turn": op["turn"], "proposed_phase": op["phase"], "since_turn": None, "until_turn": None,
                "leaving": None, "ends_before_turn": None, "ended_turn": None, "ended_reason": None,
                "broken_by": []}
        if op.get("renews"):
            pact["renews"] = op["renews"]
        state.pacts.append(pact)
        return pact
    pact = pact_by_id(state, op.get("id"))
    if pact is None:
        raise DiplomacyError("UNKNOWN_ID", f"no pact {op.get('id')!r}")
    if kind == "respond":
        if op["accept"] and pact.get("renews"):
            current = pact_by_id(state, pact["renews"])
            if current is None or current["until_turn"] is None:
                raise DiplomacyError("INVALID", f"renewal of {pact['renews']!r}: no pact with a fixed end")
            start = current["until_turn"] + 1
            until = None if pact["turns"] is None else current["until_turn"] + pact["turns"]
            current["until_turn"] = until
            current.setdefault("renewals", []).append({"id": pact["id"], "turns": pact["turns"], "turn": op["turn"],
                                                       "phase": op["phase"], "from_turn": start})
            pact.update(status=RENEWED, since_turn=start, until_turn=until)
        elif op["accept"]:
            pact.update(status=ACTIVE, since_turn=op["turn"],
                        until_turn=None if pact["turns"] is None else op["turn"] + pact["turns"] - 1)
        else:
            pact.update(status=REJECTED, ended_turn=op["turn"])
        return pact
    if kind == "leave":
        if pact["status"] == PROPOSED:
            pact.update(status=WITHDRAWN, ended_turn=op["turn"])
        else:
            renewal = pending_renewal(state, pact["id"])
            if renewal is not None:       # a leave cancels the pending renewal; accepted ones end with the pact
                renewal.update(status=CANCELLED, ended_turn=op["turn"], ended_reason="left")
            pact.update(leaving=op["seat"],
                        ends_before_turn=op["turn"] + (1 if op["phase"] == "negotiation_2" else 0))
        return pact
    raise DiplomacyError("INVALID", f"unknown op {kind!r}")


def apply_ops(state: GameState, ops: Iterable[Mapping[str, Any]]) -> None:
    for op in ops:
        apply_op(state, op)


# Events of diplomacy in the turn report. Kinds only their two seats see; and fields only the two seats (and the admin)
# see in a public event: gift amounts and pact ids (an id would count the pair's private proposals).
# Core 0.8.0 (docs/TRADE.md 5, 16а): every trade event is private to its two seats, an executed trade too.
PRIVATE_KINDS = frozenset({"pact_proposed", "pact_rejected", "pact_withdrawn", "pact_lapsed",
                           "trade_proposed", "trade_rejected", "trade_withdrawn", "trade_settled", "trade_failed",
                           "trade_lapsed", "trade_accepted", "intel_copied"})
PUBLIC_KINDS = frozenset({"gift", "pact_signed", "pact_renewed", "pact_leave", "pact_broken", "pact_expired",
                          "pact_ended"})
PRIVATE_FIELDS = ("resources", "pact", "renewal")


def op_event(op: Mapping[str, Any], state: GameState) -> dict[str, Any] | None:
    """The turn-report event of an op (full form, with the private fields)."""
    if str(op.get("op", "")).startswith("trade_"):
        from . import trade
        return trade.op_event(op, state)
    if op["op"] == "gift":
        return {"kind": "gift", "seats": sorted((op["sender"], op["recipient"])), "province": None,
                "sender": op["sender"], "recipient": op["recipient"], "resources": dict(op["resources"]),
                "phase": op["phase"]}
    pact = pact_by_id(state, op.get("id"))
    if pact is None:
        return None
    base = {"seats": sorted((pact["a"], pact["b"])), "province": None, "pact": pact["id"], "phase": op["phase"]}
    if op["op"] == "propose":
        event = {"kind": "pact_proposed", **base, "sender": op["sender"], "turns": op["turns"]}
        if op.get("renews"):
            event["renews"] = op["renews"]
        return event
    if op["op"] == "respond":
        if op["accept"] and pact.get("renews"):
            # public: the pair renewed its pact and until when; private: which pact and which proposal
            return {"kind": "pact_renewed", **base, "pact": pact["renews"], "renewal": pact["id"],
                    "turns": pact["turns"], "from_turn": pact["since_turn"], "until_turn": pact["until_turn"]}
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
