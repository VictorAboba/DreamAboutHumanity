"""Trade (docs/TRADE.md, core 0.8.0, system `trade`): offers «даю X за Y» from one seat to another, answered by it.

A trade action is an op in the turn's diplomacy log, like a gift or a pact (engine/diplomacy.py delegates to
``apply_op`` here), so the live game and replay change the state the same way:

    {"op": "trade_offer", "id", "sender", "recipient", "give", "want", "later"?, "reply_to"?, "replaces"?,
     "fulfils"?, "heads"?, "turn", "phase"}
    {"op": "trade_answer", "id", "seat", "accept": bool, "result": "done" | "pending" | "failed" | None,
     "reason"?, "lack"?, "copies"?, "turn", "phase"}

The live game checks a request with ``make_offer`` / ``make_answer`` (they raise DiplomacyError and change nothing):
the proposer's whole own side (its free resources, its provinces with its own builds and orders, its reports), the
recipient's side only by public facts (the owner of a province, ruins, the capitals rule; no treasury is looked at,
an answer would tell its size). At an accept both sides are checked atomically: the recipient's lack is an error to
the recipient (the offer stays open), the proposer's lack ends the trade as ``failed`` (``proposer_lacks``); the
verdict goes into the op, so replay needs no orders books. A sold report's content (its facts, never its truth) goes
into the answer op too, so a copy needs no recon files.

A trade is a dict in ``GameState.trades`` (docs/TRADE.md 1.1): {id, a, b, give, want, later, reply_to, replaces,
fulfils, escrow, status, settle, proposed_turn, proposed_phase, answered_turn, answered_phase, settled_turn,
failed_reason, ended_turn[, replaced_by, countered_by, heads, copies, accept_order]}. Goods are {"resources": {res:
n}, "provinces": [pid], "reports": [rid]} (empty keys left out). Without a province a trade settles at the accept
(``settle: accept``); with one (``settle: resolution``) the accept moves both sides' resources out of the treasuries
into ``escrow`` and the trade is ``pending`` until step 4b of this turn's resolution (``settle``), where everything
passes at once or nothing does (``failed``: the province was lost or is in ruins; the escrow goes back).
``later`` {give?, want?, by_turn} is a promise the game records and never carries out.

Statuses: proposed -> done | pending -> done | failed; proposed -> failed | rejected (by the recipient) | withdrawn
(by the proposer) | replaced (an explicit ``replaces``) | countered (the recipient's ``reply_to``) | lapsed
(unanswered when its turn is resolved). Terminal trades older than 2 turns leave the state at step 0 of a resolution,
except those whose promise is still due; the journal keeps the whole history. Nothing here is public: only the two
seats know of an offer or a trade (docs/TRADE.md 16а, decision 10); a transferred province changes its owner as the
map shows any change of owner, without a mark that a trade did it.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from . import conquest as cq
from . import diplomacy as dip
from .content import Ruleset
from .model import GameState, Order, as_int
from .techs import stat_key

if TYPE_CHECKING:       # pragma: no cover
    from .conquest import ConquestStep

PROPOSED, PENDING, DONE, FAILED = "proposed", "pending", "done", "failed"
REJECTED, WITHDRAWN, REPLACED, COUNTERED, LAPSED = "rejected", "withdrawn", "replaced", "countered", "lapsed"
TERMINAL = frozenset({DONE, FAILED, REJECTED, WITHDRAWN, REPLACED, COUNTERED, LAPSED})
PROPOSER_LACKS, LOST, RUINED = "proposer_lacks", "lost", "ruined"
KEEP_TURNS = 2                  # terminal trades stay in the state (and list_trades) this many turns
MAX_RESOURCE_KINDS = 4          # per side of a trade (docs/TRADE.md 1.3)
MAX_PROVINCES = 1
MAX_REPORTS = 2
GOODS_KEYS = ("resources", "provinces", "reports")
STATUS_TEXT = {PROPOSED: "ждёт ответа", PENDING: "в залоге до расчёта", DONE: "исполнен", FAILED: "сорван",
               REJECTED: "отклонён", WITHDRAWN: "отозван", REPLACED: "заменён", COUNTERED: "встречное",
               LAPSED: "сгорел"}
FAILED_TEXT = {PROPOSER_LACKS: "у предложившего не хватило обещанного", LOST: "провинцию отбили до передачи",
               RUINED: "провинция стала Пепелищем"}
# Report events of trade: all private to the two seats (dip.PRIVATE_KINDS), none public (docs/TRADE.md 16а). A
# province_transferred is seen by the others as a plain change of owner (resolve._as_seen), never as a trade.
EVENT_KINDS = frozenset({"trade_proposed", "trade_rejected", "trade_withdrawn", "trade_settled", "trade_failed",
                         "trade_lapsed", "trade_accepted", "intel_copied"})
TRANSFERRED = "province_transferred"
STATE_LINE_LIMIT = 160          # the ОБМЕН line of get_state (docs/TRADE.md 9)
STATE_LINE_ITEMS = 2
LIST_LIMIT = 600                # list_trades text
COPY_HEAD_LIMIT = 80            # «КОПИЯ донесения Ник, конец Д хода T, заверено игрой» (docs/TRADE.md 2.3)
COPY_LIMIT = 1200
TRANSFER_MARK = cq.TRANSFER_MARK    # Province.status mark «получено не боем» (docs/TRADE.md 6.3)
_REPORT_RE = re.compile(r"^#?([rc])(\d{1,6})$", re.IGNORECASE)
# orders on a promised province its giver cannot give (docs/TRADE.md 1.4): they would build or decide on land that
# leaves in this resolution
PROMISED_KINDS = ("build", "recruit", "dispose", "discover")


def on(rs: Ruleset) -> bool:
    return rs.system("trade")


def provinces_on(rs: Ruleset) -> bool:
    """Provinces are traded only with conquest (the receiver gets an Occupation) and provinces_per_turn > 0."""
    return rs.system("conquest") and rs.stat("trade.provinces_per_turn") > 0


def reports_on(rs: Ruleset) -> bool:
    return rs.system("espionage") and bool(rs.stat("trade.reports"))


def via_capture(rs: Ruleset) -> bool:
    """Some objective of the ruleset counts only land taken in battle (`hold_foreign` with `via: capture`): only
    then is the mark «получено не боем» written (docs/TRADE.md 6.3)."""
    return cq.transfer_marked(rs)


@dataclass(frozen=True)
class Ctx:
    """What the live game knows beyond the state: a seat's reserve and orders (its book), its own reports, the turn
    limit. Replay never needs it (the verdicts are in the ops)."""
    reserved: Callable[[str], Mapping[str, int]]
    orders: Callable[[str], Sequence[Order]] = lambda seat: ()
    report: Callable[[str, str], Mapping[str, Any] | None] = lambda seat, rid: None
    turn_limit: int = 99


# ---------------------------------------------------------------- queries

def trade_by_id(state: GameState, trade_id: Any) -> dict[str, Any] | None:
    return next((t for t in state.trades if t["id"] == trade_id), None)


def pair_key(x: str, y: str) -> str:
    a, b = sorted((x, y))
    return f"{a}-{b}"


def open_offer(state: GameState, a: str, b: str) -> dict[str, Any] | None:
    """The open offer from `a` to `b` (one per ordered pair), if any."""
    return next((t for t in state.trades if t["status"] == PROPOSED and t["a"] == a and t["b"] == b), None)


def waiting_for(state: GameState, seat: str) -> list[dict[str, Any]]:
    """Open offers the seat has to answer, oldest first."""
    return [t for t in state.trades if t["status"] == PROPOSED and t["b"] == seat]


def mine(state: GameState, seat: str) -> list[dict[str, Any]]:
    return [t for t in state.trades if seat in (t["a"], t["b"])]


def other_of(trade: Mapping[str, Any], seat: str) -> str:
    return trade["b"] if trade["a"] == seat else trade["a"]


def moves_of(trade: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The province moves of a trade: [(pid, giver, receiver)], the proposer's first."""
    return ([(pid, trade["a"], trade["b"]) for pid in (trade["give"].get("provinces") or [])]
            + [(pid, trade["b"], trade["a"]) for pid in (trade["want"].get("provinces") or [])])


def promised(state: GameState) -> dict[str, tuple[str, str, str]]:
    """Provinces of pending trades: {pid: (giver, receiver, trade id)}."""
    out = {}
    for t in state.trades:
        if t["status"] == PENDING:
            for pid, giver, taker in moves_of(t):
                out[pid] = (giver, taker, t["id"])
    return out


def given_now(state: GameState, seat: str) -> int:
    """Provinces the seat gives in pending trades (they pass in this turn's resolution)."""
    return sum(1 for giver, _, _ in promised(state).values() if giver == seat)


def per_turn(state: GameState, rs: Ruleset, seat: str) -> int:
    return int(rs.stat("trade.provinces_per_turn", stat_key(state, rs, seat)))


def escrow_of(state: GameState, seat: str) -> dict[str, int]:
    """`trade_escrow`: the seat's resources held in pending trades (only for the treasury line, never a check:
    they are not in the treasury)."""
    out: dict[str, int] = {}
    for t in state.trades:
        if t["status"] == PENDING:
            for res, n in ((t.get("escrow") or {}).get(seat) or {}).items():
                out[res] = out.get(res, 0) + n
    return out


def promises(state: GameState, seat: str) -> list[dict[str, Any]]:
    """Promises of the seat's done trades still due ({trade, from, to, goods, by_turn}); a promise to or from a seat
    that is out is void and left out (docs/TRADE.md 3)."""
    out = []
    for t in state.trades:
        later = t.get("later")
        if t["status"] != DONE or not later or seat not in (t["a"], t["b"]) or later["by_turn"] < state.turn:
            continue
        if not (state.players[t["a"]].alive and state.players[t["b"]].alive):
            continue
        for side, giver, taker in (("give", t["a"], t["b"]), ("want", t["b"], t["a"])):
            if later.get(side):
                out.append({"trade": t["id"], "from": giver, "to": taker, "goods": later[side],
                            "by_turn": later["by_turn"]})
    return out


def order_problem(state: GameState, seat: str, order: Order) -> tuple[str, str] | None:
    """PROVINCE_PROMISED (docs/TRADE.md 1.4, 1.5.8): the giver's build, recruit, fate or find choice in a province
    a pending trade gives away; the receiver's move into a province a pending trade gives it (it passes in this
    resolution: a fight there would take it by force and void the trade)."""
    if not state.trades:
        return None
    pend = promised(state)
    if not pend:
        return None
    if order.kind == "move":
        hit = pend.get(order.dst or "")
        if hit is not None and hit[1] == seat:
            return ("PROVINCE_PROMISED", f"{order.dst} перейдёт к тебе обменом {hit[2]} в расчёте этого хода: входи "
                                         "в неё со следующего хода")
        return None
    if order.kind in PROMISED_KINDS:
        hit = pend.get(order.province or "")
        if hit is not None and hit[0] == seat:
            return ("PROVINCE_PROMISED", f"{order.province} уходит обменом {hit[2]} в расчёте этого хода: стройка, "
                                         "найм, судьба и выбор находки в ней закрыты")
    return None


def move_warning(state: GameState, seat: str, order: Order) -> str | None:
    """A move of the giver into a province it gives away: the units will leave it in the resolution."""
    if order.kind != "move" or not state.trades:
        return None
    hit = promised(state).get(order.dst or "")
    if hit is None or hit[0] != seat:
        return None
    return (f"{order.dst} уходит обменом {hit[2]} в расчёте: твои войска из неё уйдут в соседнюю твою (нет такой — "
            "распустятся)")


# ---------------------------------------------------------------- parsing

def _decode(raw: Any, what: str) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise dip.DiplomacyError("INVALID", f"{what} — объект, например {{\"alloy\": 10}}") from None
    return raw


def province_ref(rs: Ruleset, ref: Any) -> str | None:
    if not isinstance(ref, str) or not ref.strip():
        return None
    pid = ref.strip().upper()
    return pid if pid in rs.map.adjacency else None


def report_ref(ref: Any) -> str | None:
    """`r4`, `#r4`, `R4` -> `r4`; `c2` -> `c2` (a copy: never traded, the caller says so); else None."""
    m = _REPORT_RE.match(ref.strip()) if isinstance(ref, str) else None
    return f"{m.group(1).lower()}{int(m.group(2))}" if m else None


def _no_provinces(rs: Ruleset) -> dip.DiplomacyError:
    why = ("провинции передаются только при включённом завоевании" if not rs.system("conquest")
           else "провинции в этой партии не торгуются")
    return dip.DiplomacyError("NOT_AVAILABLE", f"{why}; обмен — ресурсами")


def _no_reports(rs: Ruleset) -> dip.DiplomacyError:
    why = "шпионаж выключен" if not rs.system("espionage") else "донесения в этой партии не торгуются"
    return dip.DiplomacyError("NOT_AVAILABLE", f"{why}: донесения разведки не продаются")


def parse_goods(rs: Ruleset, raw: Any, what: str, reports: bool = True) -> dict[str, Any]:
    """Goods {"resources": {res: n}, "provinces": [pid], "reports": [rid]} from the nested form or the flat one
    {"alloy": 10, "P07": 1, "r4": 1} (a key is a resource, a province or a report); a JSON string is decoded; nothing
    (None, "", {}) is an empty side. `reports`: False where no report may stand (the promise of `later`)."""
    raw = _decode(raw, what)
    if raw in (None, {}, []):
        return {}
    if not isinstance(raw, Mapping):
        raise dip.DiplomacyError("INVALID", f"{what} — объект {{ресурс: число}}, например {{\"alloy\": 10}}")
    resources: Any = {}
    provinces: list[Any] = []
    reps: list[Any] = []
    if set(raw) & set(GOODS_KEYS):
        unknown = sorted(str(k) for k in raw if k not in GOODS_KEYS)
        if unknown:
            raise dip.DiplomacyError("INVALID", f"{what}: ключи resources, provinces, reports — или плоская форма "
                                                f"{{\"alloy\": 10}}; лишнее: {', '.join(unknown)[:60]}")
        resources = raw.get("resources") or {}
        for key, out in (("provinces", provinces), ("reports", reps)):
            items = raw.get(key) or []
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, (list, tuple)):
                raise dip.DiplomacyError("INVALID", f"{what}.{key} — список, например [\"P07\"]")
            out.extend(items)
    else:
        flat = {}
        for key, n in raw.items():
            if dip._resource_ref(rs, key) is None and province_ref(rs, key) is not None:
                if n not in (1, True, "1"):
                    raise dip.DiplomacyError("INVALID", f"{what}: провинция — {{\"{key}\": 1}} или "
                                                        f"{{\"provinces\": [\"{key}\"]}}")
                provinces.append(key)
            elif dip._resource_ref(rs, key) is None and report_ref(key) is not None:
                reps.append(key)
            else:
                flat[key] = n
        resources = flat
    out: dict[str, Any] = {}
    if resources:
        out.update(_resources(rs, resources, what))
    if provinces:
        if not provinces_on(rs):
            raise _no_provinces(rs)
        pids = []
        for ref in provinces:
            pid = province_ref(rs, ref)
            if pid is None:
                raise dip.DiplomacyError("UNKNOWN_ID", f"{what}: нет провинции {str(ref)[:20]!r}")
            if pid not in pids:
                pids.append(pid)
        if len(pids) > MAX_PROVINCES:
            raise dip.DiplomacyError("INVALID", f"{what}: не больше {MAX_PROVINCES} провинции на сторону")
        out["provinces"] = pids
    if reps:
        if not reports:
            raise dip.DiplomacyError("INVALID", f"{what}: обещать можно ресурсы и провинции, не донесения")
        if not reports_on(rs):
            raise _no_reports(rs)
        rids = []
        for ref in reps:
            rid = report_ref(ref)
            if rid is None:
                raise dip.DiplomacyError("UNKNOWN_ID", f"{what}: донесение — \"r4\", а не {str(ref)[:20]!r}")
            if rid.startswith("c"):
                raise dip.DiplomacyError("INVALID", f"{what}: {rid} — купленная копия, её перепродать нельзя; "
                                                    "продаются только свои донесения r<k>")
            if rid not in rids:
                rids.append(rid)
        if len(rids) > MAX_REPORTS:
            raise dip.DiplomacyError("INVALID", f"{what}: не больше {MAX_REPORTS} донесений на сторону")
        out["reports"] = rids
    return out


def _resources(rs: Ruleset, raw: Any, what: str) -> dict[str, Any]:
    amounts = dip.parse_resources(rs, raw)
    if len(amounts) > MAX_RESOURCE_KINDS:
        raise dip.DiplomacyError("INVALID", f"{what}: не больше {MAX_RESOURCE_KINDS} видов ресурсов на сторону")
    return {"resources": amounts}


def parse_later(state: GameState, rs: Ruleset, raw: Any, turn_limit: int) -> dict[str, Any] | None:
    """`later` {give?, want?, by_turn}: a promise for the future (docs/TRADE.md 3) of resources and provinces, or
    None."""
    raw = _decode(raw, "later")
    if raw in (None, {}):
        return None
    if not rs.stat("trade.later"):
        raise dip.DiplomacyError("NOT_AVAILABLE", "отложенная часть later в этой партии выключена")
    if not isinstance(raw, Mapping) or set(raw) - {"give", "want", "by_turn"}:
        raise dip.DiplomacyError("INVALID", "later — {\"give\": {...}, \"want\": {...}, \"by_turn\": N}")
    by_turn = as_int(raw.get("by_turn"))
    if isinstance(by_turn, bool) or not isinstance(by_turn, int) or not state.turn <= by_turn <= turn_limit:
        raise dip.DiplomacyError("INVALID", f"later.by_turn — ход {state.turn}..{turn_limit}, до конца которого "
                                            "обещано")
    out: dict[str, Any] = {}
    for side in ("give", "want"):
        goods = parse_goods(rs, raw.get(side), f"later.{side}", reports=False)
        if goods:
            out[side] = goods
    if not out:
        raise dip.DiplomacyError("INVALID", "later: укажи give или want — что обещано")
    out["by_turn"] = by_turn
    return out


def _decision(decision: Any) -> bool:
    if isinstance(decision, str):
        decision = decision.strip().lower()
    if decision in ("accept", True, "yes", "да", "принять", "true"):
        return True
    if decision in ("reject", False, "no", "нет", "отклонить", "false"):
        return False
    raise dip.DiplomacyError("INVALID", "decision — accept или reject")


# ---------------------------------------------------------------- checks (no state change)

def _require_turn(state: GameState, seat: str, phase: str) -> None:
    if phase not in dip.NEGOTIATION_PHASES:
        raise dip.DiplomacyError("PHASE_LOCKED", "обмен — только в фазах переговоров П1 и П2")
    player = state.players.get(seat)
    if player is None or not player.alive:
        raise dip.DiplomacyError("PHASE_LOCKED", "место выбыло из игры")


def lacking(state: GameState, seat: str, goods: Mapping[str, Any],
            reserved: Mapping[str, int]) -> list[dict[str, Any]]:
    """What of `goods` the seat lacks in free (unreserved) resources: [{resource, free, need}]."""
    player = state.players[seat]
    out = []
    for res, n in (goods.get("resources") or {}).items():
        free = player.resources.get(res, 0) - reserved.get(res, 0)
        if n > free:
            out.append({"resource": res, "free": max(free, 0), "need": n})
    return out


def lack_text(rs: Ruleset, lack: Iterable[Mapping[str, Any]]) -> str:
    parts = []
    for x in lack:
        if "resource" in x:
            parts.append(f"{rs.resources[x['resource']].name}: свободно {x['free']}, нужно {x['need']}")
        else:
            parts.append(f"{x.get('province') or x.get('report')}: {x.get('text') or x.get('reason')}")
    return ", ".join(parts)


def exit_price(rs: Ruleset, phase: str, reserved: Mapping[str, int]) -> str:
    """What freeing a reserve would cost (docs/TRADE.md 1.2): no promise that it can be freed for nothing."""
    if not any(reserved.values()):
        return ""
    if phase == "negotiation_1":
        return "; остальное в резерве приказов, а в П1 приказы не правятся (PHASE_LOCKED)"
    from .orders import CP_CANCEL       # orders imports this module
    refund = round(rs.stat("p2.cancel_refund") * 100)
    return (f"; остальное в резерве приказов: в П2 отмена приказа стоит {CP_CANCEL} ОК и вернёт лишь {refund}% "
            "его резерва (остальное сгорит)")


def _order_exit(rs: Ruleset, phase: str) -> str:
    if phase == "negotiation_1":
        return "; в П1 приказы не правятся (PHASE_LOCKED)"
    from .orders import CP_CANCEL
    refund = round(rs.stat("p2.cancel_refund") * 100)
    return f"; в П2 отмена приказа стоит {CP_CANCEL} ОК и вернёт лишь {refund}% его резерва"


def home_of(state: GameState, pid: str) -> str | None:
    """The seat whose start capital (`Player.home`) the province is."""
    return next((s for s, p in sorted(state.players.items()) if p.home == pid), None)


def province_problem(state: GameState, rs: Ruleset, giver: str, receiver: str, pid: str, own: bool,
                     orders: Sequence[Order] = (), phase: str = "", trade_id: str | None = None
                     ) -> tuple[str, str] | None:
    """Why `giver` cannot give `pid` to `receiver` (docs/TRADE.md 2.2), or None. Public facts always (the owner,
    ruins, the value cap, the capitals rule); with `own` (the giver checks its own side) also its private ones: the
    province in another pending trade, its build in progress there, its build, recruit, fate or find order there in
    the book. `trade_id`: the trade being accepted (its own province is not «another» trade)."""
    prov = state.provinces[pid]
    nick = state.players[giver].nick
    if prov.owner != giver:
        return "INVALID", (f"{pid} — не твоя провинция" if own else f"{pid} не у {nick}: отдать её может только хозяин")
    if cq.is_ruined(prov, state.turn):
        return "INVALID", f"{pid} — Пепелище до хода {prov.status['ruined_until']}: её не передать"
    value = cq.conquest_value(rs, prov)
    cap = int(rs.stat("trade.province_max_value"))
    if value > cap:
        return "INVALID", f"{pid}: ценность {value} выше предела обмена {cap}"
    home = home_of(state, pid)
    if home is not None and home != receiver:
        who = "твою" if home == giver and own else f"{state.players[home].nick}"
        return "INVALID", (f"{pid} — стартовая столица {who}: её можно передать только самому этому месту"
                           if home != giver else f"{pid} — стартовая столица {'твоя' if own else nick}: её не отдать")
    if not own:
        return None
    hit = promised(state).get(pid)
    if hit is not None and hit[2] != trade_id:
        return "PROVINCE_PROMISED", f"{pid} уже обещана обменом {hit[2]}: одна провинция — в одном обмене"
    if any(b.province == pid and b.seat == giver for b in state.pending_builds):
        return "BUILD_IN_PROGRESS", (f"в {pid} идёт твоя стройка: провинцию с идущей стройкой не передать (доли "
                                     "сгорели бы); отдать — после конца стройки")
    order = next((o for o in orders if o.kind in PROMISED_KINDS and o.province == pid), None)
    if order is not None:
        return "BUILD_IN_PROGRESS", (f"в книге твой приказ {order.id} ({order.kind}) на {pid}: с ним провинцию не "
                                     f"передать{_order_exit(rs, phase)}")
    return None


def _per_turn_problem(state: GameState, rs: Ruleset, seat: str, n: int) -> tuple[str, str] | None:
    if not n:
        return None
    limit = per_turn(state, rs, seat)
    have = given_now(state, seat)
    if have + n > limit:
        return "LIMIT_EXCEEDED", f"за ход можно отдать обменами не больше {limit} провинц.; уже обещано {have}"
    return None


def _report_head(report: Mapping[str, Any]) -> dict[str, Any]:
    return {"mission": report.get("mission"), "target": report.get("target"), "turn": report.get("turn")}


def report_copy(report: Mapping[str, Any]) -> dict[str, Any]:
    """The certified content of a sold report: what its seller's report shows (facts with the seller's windows,
    never `truth`, no Shade)."""
    keep = ("id", "mission", "behavior", "target", "round", "rounds", "turn", "facts")
    return {k: copy.deepcopy(report[k]) for k in keep if k in report}


def _find_head(rs: Ruleset, state: GameState, seller: str, pid: str) -> str | None:
    """The head of a find in a sold province, only when the seller knows it (docs/TRADE.md 1.5.4)."""
    entry = state.discoveries.get(pid)
    if not entry or entry.get("state") not in ("site", "pending") or seller not in (entry.get("known") or []):
        return None
    d = rs.discoveries.get(entry.get("id") or "")
    name = d.name if d is not None else str(entry.get("id"))
    return f"находка «{name}»" + (" (ждёт выбора)" if entry.get("state") == "pending" else "")


def make_offer(state: GameState, rs: Ruleset, seat: str, to: Any, give: Any, want: Any, later: Any,
               reply_to: Any, replaces: Any, fulfils: Any, phase: str, ctx: Ctx) -> dict[str, Any]:
    """A new offer op, or {"already": trade} when the same offer to the same seat is already open."""
    _require_turn(state, seat, phase)
    recipient = dip._living_other(state, seat, to)
    nick = state.players[recipient].nick
    give_g, want_g = parse_goods(rs, give, "give"), parse_goods(rs, want, "want")
    later_g = parse_later(state, rs, later, ctx.turn_limit)
    if not (give_g or want_g or later_g):
        raise dip.DiplomacyError("INVALID", "пустой обмен: укажи give (что отдаёшь), want (что просишь) или later")
    if want_g.get("reports"):
        raise dip.DiplomacyError("INVALID", f"донесения — только в give (свои r<k>): номера донесений {nick} тебе не "
                                            "известны, договоритесь словами — он предложит своё")
    reply = _ref(reply_to)
    if reply is not None:
        t = trade_by_id(state, reply)
        if t is None or seat not in (t["a"], t["b"]):
            raise dip.DiplomacyError("UNKNOWN_ID", f"нет обмена {reply[:40]!r} для reply_to; list_trades() покажет "
                                                   "твои")
        if t["a"] != recipient or t["b"] != seat:
            raise dip.DiplomacyError("INVALID", f"reply_to — встречное на предложение тебе от {nick}; {t['id']} — "
                                                "не оно")
        if t["status"] != PROPOSED:
            raise dip.DiplomacyError("INVALID", f"{t['id']} уже не ждёт ответа ({STATUS_TEXT[t['status']]})")
    old = _ref(replaces)
    if old is not None:
        t = trade_by_id(state, old)
        if t is None or seat not in (t["a"], t["b"]):
            raise dip.DiplomacyError("UNKNOWN_ID", f"нет обмена {old[:40]!r} для replaces; list_trades() покажет "
                                                   "твои")
        if t["a"] != seat or t["b"] != recipient:
            raise dip.DiplomacyError("INVALID", f"replaces — твоё открытое предложение {nick}; {t['id']} — не оно")
        if t["status"] != PROPOSED:
            raise dip.DiplomacyError("INVALID", f"{t['id']} уже не ждёт ответа ({STATUS_TEXT[t['status']]}): "
                                                "предложи заново без replaces")
    existing = open_offer(state, seat, recipient)
    if existing is not None and existing["id"] != old:
        if (existing["give"], existing["want"], existing.get("later")) == (give_g, want_g, later_g):
            return {"already": existing}
        raise dip.DiplomacyError("OFFER_EXISTS", f"твоё предложение {existing['id']} для {nick} ещё ждёт ответа; "
                                                 "заменить — propose_trade(..., replaces=\"" + existing["id"]
                                                 + "\"), отозвать — respond_trade(id=\"" + existing["id"]
                                                 + "\", decision=\"reject\")")
    mark = _ref(fulfils)
    if mark is not None:
        t = trade_by_id(state, mark)
        if t is None or seat not in (t["a"], t["b"]):
            raise dip.DiplomacyError("UNKNOWN_ID", f"нет обмена {mark[:40]!r} для fulfils; list_trades() покажет "
                                                   "твои")
    heads: dict[str, Any] = {}
    # the proposer's own side in full (its private facts are its own), the recipient's by public facts only
    for pid in give_g.get("provinces") or []:
        problem = province_problem(state, rs, seat, recipient, pid, True, ctx.orders(seat), phase)
        if problem:
            raise dip.DiplomacyError(*problem)
        find = _find_head(rs, state, seat, pid)
        if find:
            heads[pid] = find
    problem = _per_turn_problem(state, rs, seat, len(give_g.get("provinces") or []))
    if problem:
        raise dip.DiplomacyError(*problem)
    for pid in want_g.get("provinces") or []:
        problem = province_problem(state, rs, recipient, seat, pid, False)
        if problem:
            raise dip.DiplomacyError(*problem)
    for rid in give_g.get("reports") or []:
        report = ctx.report(seat, rid)
        if report is None:
            raise dip.DiplomacyError("UNKNOWN_ID", f"у тебя нет донесения {rid} (get_intel покажет твои)")
        heads[rid] = _report_head(report)
    lack = lacking(state, seat, give_g, ctx.reserved(seat))
    if lack:
        raise dip.DiplomacyError("NOT_ENOUGH_RESOURCES", f"отдать нечем: {lack_text(rs, lack)}"
                                                         f"{exit_price(rs, phase, ctx.reserved(seat))}")
    key = pair_key(seat, recipient)
    op: dict[str, Any] = {"op": "trade_offer", "id": f"trade-{key}-{state.trade_seq.get(key, 0) + 1}",
                          "sender": seat, "recipient": recipient, "give": give_g, "want": want_g,
                          "turn": state.turn, "phase": phase}
    for name, value in (("later", later_g), ("reply_to", reply), ("replaces", old), ("fulfils", mark),
                        ("heads", heads or None)):
        if value is not None:
            op[name] = value
    return op


def _ref(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise dip.DiplomacyError("INVALID", "ID обмена — строка вида \"trade-A-B-1\"")
    return value.strip()


def settle_kind(trade: Mapping[str, Any]) -> str:
    """`resolution` when a province is in an immediate part, else `accept` (docs/TRADE.md 1.1)."""
    return "resolution" if moves_of(trade) else "accept"


def make_answer(state: GameState, rs: Ruleset, seat: str, trade_id: Any, decision: Any, phase: str,
                ctx: Ctx) -> dict[str, Any]:
    """An answer op: the recipient accepts or rejects, the proposer withdraws (reject)."""
    _require_turn(state, seat, phase)
    tid = _ref(trade_id) or ""
    t = trade_by_id(state, tid)
    if t is None or seat not in (t["a"], t["b"]):
        raise dip.DiplomacyError("UNKNOWN_ID", f"нет обмена {tid[:40]!r}; list_trades() покажет твои")
    accept = _decision(decision)
    if t["status"] == REPLACED:
        raise dip.DiplomacyError("UNKNOWN_ID", f"предложение {t['id']} заменено новым {t.get('replaced_by')}: "
                                               "отвечай на него (list_trades)")
    if t["status"] != PROPOSED:
        raise dip.DiplomacyError("INVALID", f"обмен {t['id']} не ждёт ответа ({STATUS_TEXT[t['status']]}); "
                                            "list_trades() покажет, что ждёт")
    op: dict[str, Any] = {"op": "trade_answer", "id": t["id"], "seat": seat, "accept": accept, "result": None,
                          "turn": state.turn, "phase": phase}
    if seat == t["a"]:
        if accept:
            raise dip.DiplomacyError("INVALID", f"{t['id']} предложил ты; отвечает {state.players[t['b']].nick}. "
                                                f"Отозвать — respond_trade(id=\"{t['id']}\", decision=\"reject\")")
        return op
    if not accept:
        return op
    a = t["a"]
    if not state.players[a].alive:
        raise dip.DiplomacyError("INVALID", "предложивший выбыл из игры")
    # the recipient's own side, in full: an error to it, the offer stays open
    own = ctx.reserved(seat)
    lack = lacking(state, seat, t["want"], own)
    if lack:
        raise dip.DiplomacyError("NOT_ENOUGH_RESOURCES", f"тебе отдать нечем: {lack_text(rs, lack)}"
                                                         f"{exit_price(rs, phase, own)}; предложение ждёт ответа")
    for pid in t["want"].get("provinces") or []:
        problem = province_problem(state, rs, seat, a, pid, True, ctx.orders(seat), phase, t["id"])
        if problem:
            raise dip.DiplomacyError(problem[0], f"{problem[1]}; предложение ждёт ответа")
    problem = _per_turn_problem(state, rs, seat, len(t["want"].get("provinces") or []))
    if problem:
        raise dip.DiplomacyError(problem[0], f"{problem[1]}; предложение ждёт ответа")
    # the proposer's side: a lack ends the trade (the recipient learns no detail)
    theirs = lacking(state, a, t["give"], ctx.reserved(a))
    for pid in t["give"].get("provinces") or []:
        problem = province_problem(state, rs, a, seat, pid, True, ctx.orders(a), phase, t["id"])
        if problem:
            theirs.append({"province": pid, "reason": problem[0], "text": problem[1]})
    problem = _per_turn_problem(state, rs, a, len(t["give"].get("provinces") or []))
    if problem:
        theirs.append({"province": ",".join(t["give"].get("provinces") or []), "reason": problem[0],
                       "text": problem[1]})
    copies = []
    for rid in t["give"].get("reports") or []:
        report = ctx.report(a, rid)
        if report is None:
            theirs.append({"report": rid, "reason": "UNKNOWN_ID", "text": "донесения больше нет"})
        else:
            copies.append({"source": rid, "report": report_copy(report)})
    if theirs:
        op.update(result=FAILED, reason=PROPOSER_LACKS, lack=theirs)
        return op
    op["result"] = PENDING if settle_kind(t) == "resolution" else DONE
    if copies:
        op["copies"] = copies
    return op


# ---------------------------------------------------------------- applying

def _check_stock(state: GameState, trade: Mapping[str, Any]) -> None:
    for giver, goods in ((trade["a"], trade["give"]), (trade["b"], trade["want"])):
        for res, n in (goods.get("resources") or {}).items():
            if state.players[giver].resources.get(res, 0) < n:
                raise dip.DiplomacyError("NOT_ENOUGH_RESOURCES", f"trade {trade['id']}: {giver} lacks {res}")


def issue_copies(state: GameState, trade: Mapping[str, Any], copies: Iterable[Mapping[str, Any]]
                 ) -> list[dict[str, Any]]:
    """Copies of the proposer's sold reports to the recipient (`Player.intel_copies`, ids c<k> of the holder's
    `spy_seq["copy"]`); the seller keeps its reports. Returns the copies made."""
    holder = state.players[trade["b"]]
    made = []
    for c in copies:
        holder.spy_seq["copy"] = holder.spy_seq.get("copy", 0) + 1
        entry = {"id": f"c{holder.spy_seq['copy']}", "from": trade["a"], "turn": c["report"].get("turn"),
                 "source": c["source"], "trade": trade["id"], "report": copy.deepcopy(dict(c["report"]))}
        holder.intel_copies.append(entry)
        made.append(entry)
    return made


def apply_op(state: GameState, op: Mapping[str, Any]) -> dict[str, Any]:
    """Apply one trade op in place; returns the trade it touched. Raises DiplomacyError when the op does not fit
    the state (a replay of a different game)."""
    kind = op.get("op")
    if kind == "trade_offer":
        if trade_by_id(state, op["id"]) is not None:
            raise dip.DiplomacyError("INVALID", f"trade {op['id']!r} exists")
        trade = {"id": op["id"], "a": op["sender"], "b": op["recipient"], "give": dict(op["give"]),
                 "want": dict(op["want"]), "later": op.get("later"), "reply_to": op.get("reply_to"),
                 "replaces": op.get("replaces"), "fulfils": op.get("fulfils"), "escrow": None, "status": PROPOSED,
                 "settle": "accept", "proposed_turn": op["turn"], "proposed_phase": op["phase"],
                 "answered_turn": None, "answered_phase": None, "settled_turn": None, "failed_reason": None,
                 "ended_turn": None}
        trade["settle"] = settle_kind(trade)
        if op.get("heads"):
            trade["heads"] = copy.deepcopy(dict(op["heads"]))
        for ref, status, mark in (("replaces", REPLACED, "replaced_by"), ("reply_to", COUNTERED, "countered_by")):
            if op.get(ref):
                old = trade_by_id(state, op[ref])
                if old is None or old["status"] != PROPOSED:
                    raise dip.DiplomacyError("INVALID", f"{ref} {op[ref]!r}: no open offer")
                old.update(status=status, ended_turn=op["turn"], **{mark: op["id"]})
        key = pair_key(op["sender"], op["recipient"])
        state.trade_seq[key] = state.trade_seq.get(key, 0) + 1
        state.trades.append(trade)
        return trade
    if kind != "trade_answer":
        raise dip.DiplomacyError("INVALID", f"unknown op {kind!r}")
    trade = trade_by_id(state, op.get("id"))
    if trade is None or trade["status"] != PROPOSED:
        raise dip.DiplomacyError("UNKNOWN_ID", f"no open trade {op.get('id')!r}")
    trade.update(answered_turn=op["turn"], answered_phase=op["phase"], ended_turn=op["turn"])
    a, b = trade["a"], trade["b"]
    if not op["accept"]:
        trade["status"] = WITHDRAWN if op["seat"] == a else REJECTED
    elif op["result"] == DONE:
        _check_stock(state, trade)
        dip.transfer_resources(state, a, b, (trade["give"].get("resources") or {}))
        dip.transfer_resources(state, b, a, (trade["want"].get("resources") or {}))
        issue_copies(state, trade, op.get("copies") or ())
        trade.update(status=DONE, settled_turn=op["turn"])
    elif op["result"] == PENDING:
        # docs/TRADE.md 1.4: both sides' resources leave the treasuries into the trade until step 4b
        _check_stock(state, trade)
        escrow = {}
        for giver, goods in ((a, trade["give"]), (b, trade["want"])):
            amounts = dict(goods.get("resources") or {})
            for res, n in amounts.items():
                state.players[giver].resources[res] -= n
            if amounts:
                escrow[giver] = amounts
        order = max((int(t.get("accept_order") or 0) for t in state.trades), default=0) + 1
        trade.update(status=PENDING, escrow=escrow or None, ended_turn=None, accept_order=order)
        if op.get("copies"):
            trade["copies"] = copy.deepcopy(list(op["copies"]))
    else:
        trade.update(status=FAILED, failed_reason=op.get("reason") or PROPOSER_LACKS)
    return trade


def lapse(state: GameState, turn: int) -> list[dict[str, Any]]:
    """Step 0 of a resolution: unanswered offers lapse; terminal trades older than KEEP_TURNS leave the state
    (a promise still due keeps its trade). Returns the lapsed trades."""
    lapsed = []
    for t in state.trades:
        if t["status"] == PROPOSED:
            t.update(status=LAPSED, ended_turn=turn)
            lapsed.append(t)
    state.trades = [t for t in state.trades
                    if t["status"] not in TERMINAL or turn - (t.get("ended_turn") or turn) < KEEP_TURNS
                    or (t.get("later") and t["status"] == DONE and t["later"]["by_turn"] >= turn)]
    return lapsed


# ---------------------------------------------------------------- step 4b: pending trades settle

def settle_all(step: ConquestStep) -> None:
    """Step 4b (docs/TRADE.md 1.5), after the fates of captures and the decisions, before the retreats: every
    pending trade in the order of its accept."""
    for t in sorted((t for t in step.s.trades if t["status"] == PENDING), key=lambda t: t.get("accept_order") or 0):
        settle(step, t)


def settle(step: ConquestStep, trade: dict[str, Any]) -> None:
    """One pending trade: every given province still its giver's (full or occupied by it, not taken in this
    resolution) and not in ruins -> everything passes at once; else `failed` (lost / ruined) and the escrow goes
    back. A province passes by the rule of a capture without a battle (SPY_AND_CONQUEST.md 3.1): to its origin or the
    one it was taken from -> a Return (full, its claim dropped); else an Occupation of the receiver (since this turn:
    no revolt check now). No `captured` event: no war points, no homes taken, no capture note; no claim for the
    giver. The giver's units there leave (retreats, full provinces first; neighbours after all the trade's moves),
    a waiting find choice was put back to `site` at step 1 and goes to the new holder after the battles."""
    s, rs, turn, r = step.s, step.rs, step.turn, step.r
    a, b, tid = trade["a"], trade["b"], trade["id"]
    moves = moves_of(trade)
    for pid, giver, _ in moves:
        prov = s.provinces[pid]
        reason = (LOST if prov.owner != giver or pid in step.captures else
                  RUINED if cq.is_ruined(prov, turn) else None)
        if reason is not None:
            for seat, amounts in sorted((trade.get("escrow") or {}).items()):
                stock = s.players[seat].resources
                for res, n in amounts.items():
                    stock[res] = stock.get(res, 0) + n
            trade.update(status=FAILED, failed_reason=reason, ended_turn=turn, escrow=None)
            trade.pop("copies", None)
            r.ev("trade_failed", [a, b], None, trade=tid, reason=reason, short_side=giver, lost_province=pid,
                 at="resolution")
            return
    mark = via_capture(rs)
    leaving = []
    for pid, giver, taker in moves:
        prov = s.provinces[pid]
        occ = cq.occupied(prov)
        back = s.origins.get(pid) == taker or (occ is not None and occ.get("from") == taker)
        if back:
            prov.status.pop("occupied", None)
        else:
            prov.status["occupied"] = {"by": taker, "from": occ["from"] if occ is not None else giver,
                                       "since": turn}
        step.drop_claims(taker, pid, "returned")
        units = {u: n for u, n in sorted(prov.units.items()) if n}
        prov.units = {}
        prov.fortified = False
        prov.owner = taker
        if mark:
            prov.status[TRANSFER_MARK] = {"from": giver, "to": taker, "turn": turn}
        if units:
            leaving.append((giver, pid, units))
        step.transferred.add(pid)
        step.changes.append({"province": pid, "from": giver, "to": taker,
                             "status": cq.STATUS_FULL if back else cq.STATUS_OCCUPIED})
        r.ev(TRANSFERRED, [giver, taker], pid, **{"from": giver, "to": taker}, trade=tid,
             status="return" if back else "occupation", units_withdrawn=units)
    for giver, pid, units in leaving:
        # neighbours after all the moves of this trade: no unit goes into a province just given away
        r.retreats.append((giver, sorted(rs.map.adjacency[pid]), units, pid, True))
    escrow = trade.get("escrow") or {}
    for giver, taker in ((a, b), (b, a)):
        stock = s.players[taker].resources
        for res, n in (escrow.get(giver) or {}).items():
            stock[res] = stock.get(res, 0) + n
    made = issue_copies(s, trade, trade.pop("copies", None) or ())
    trade.update(status=DONE, settled_turn=turn, ended_turn=turn, escrow=None)
    r.ev("trade_settled", [a, b], None, trade=tid, a=a, give=dict(trade["give"]), want=dict(trade["want"]),
         at="resolution", escrow=escrow, copies=[c["id"] for c in made])


# ---------------------------------------------------------------- report events (private to the two seats)

def op_event(op: Mapping[str, Any], state: GameState) -> dict[str, Any] | None:
    if op["op"] == "trade_offer":
        event = {"kind": "trade_proposed", "seats": sorted((op["sender"], op["recipient"])), "province": None,
                 "trade": op["id"], "sender": op["sender"], "give": dict(op["give"]), "want": dict(op["want"]),
                 "phase": op["phase"]}
        for name in ("later", "reply_to", "replaces"):
            if op.get(name):
                event[name] = op[name]
        return event
    trade = trade_by_id(state, op.get("id"))
    if trade is None:
        return None
    base = {"seats": sorted((trade["a"], trade["b"])), "province": None, "trade": trade["id"], "phase": op["phase"]}
    if not op["accept"]:
        return {"kind": "trade_withdrawn" if op["seat"] == trade["a"] else "trade_rejected", **base}
    if op["result"] == DONE:
        return {"kind": "trade_settled", **base, "a": trade["a"], "give": dict(trade["give"]),
                "want": dict(trade["want"]), "at": "accept"}
    if op["result"] == PENDING:
        return {"kind": "trade_accepted", **base, "a": trade["a"], "give": dict(trade["give"]),
                "want": dict(trade["want"])}
    return {"kind": "trade_failed", **base, "reason": op.get("reason") or PROPOSER_LACKS, "short_side": trade["a"]}


# ---------------------------------------------------------------- texts

def report_head_text(rs: Ruleset, state: GameState, rid: str, head: Mapping[str, Any] | None,
                     seat: str | None = None) -> str:
    """`r4: Чтение приказов → Хор, ход 5, найдено` — the certified head of a report on sale (docs/TRADE.md 2.3)."""
    if not head:
        return rid
    m = rs.missions.get(head.get("mission") or "")
    target = head.get("target")
    who = ("тебя" if target == seat else state.players[target].nick) if target in state.players else str(target)
    return f"{rid}: {m.name if m else head.get('mission')} → {who}, ход {head.get('turn')}, найдено"


def goods_text(rs: Ruleset, goods: Mapping[str, Any] | None, state: GameState | None = None,
               heads: Mapping[str, Any] | None = None, seat: str | None = None, names: bool = True) -> str:
    goods = goods or {}
    amounts = goods.get("resources") or {}
    parts = [f"{rs.resources[r].name if r in rs.resources else r} {n}" for r, n in amounts.items()]
    for pid in goods.get("provinces") or []:
        name = rs.map.provinces[pid].name if names and pid in rs.map.provinces else ""
        text = f"{pid} «{name}»" if name else pid
        if heads and heads.get(pid):
            text += f" ({heads[pid]})"
        parts.append(text)
    for rid in goods.get("reports") or []:
        parts.append(f"донесение {report_head_text(rs, state, rid, (heads or {}).get(rid), seat)}"
                     if state is not None and heads and heads.get(rid) else f"донесение {rid}")
    return ", ".join(parts)


def terms_text(rs: Ruleset, trade: Mapping[str, Any], seat: str | None, state: GameState | None = None,
               promise: bool = True, names: bool = True) -> str:
    """`даёшь Сплав 10, просишь Провиант 5` (the proposer's view), `даёт …, просит …` (the recipient's) and the
    promise part (`promise`). Without `names`: bare province and report ids (a short reply)."""
    own = seat == trade["a"]
    heads = trade.get("heads") if names else None
    give = goods_text(rs, trade["give"], state if names else None, heads, seat, names)
    want = goods_text(rs, trade["want"], state if names else None, heads, seat, names)
    parts = []
    if give:
        parts.append(f"{'даёшь' if own else 'даёт'} {give}")
    if want:
        parts.append(f"{'просишь' if own else 'просит'} {want}")
    later = trade.get("later")
    if later and promise:
        promised_ = []
        if later.get("give"):
            promised_.append(f"{'ты' if own else 'он'} {goods_text(rs, later['give'])}")
        if later.get("want"):
            promised_.append(f"{'он' if own else 'ты'} {goods_text(rs, later['want'])}")
        parts.append(f"обещание к ходу {later['by_turn']}: {'; '.join(promised_)}")
    return ", ".join(parts) or "—"


REPLY_LIMIT = 200        # docs/TRADE.md 10: the propose_trade reply (and respond_trade's, by the same principle)


def fit_reply(variants: list[str], limit: int = REPLY_LIMIT) -> str:
    """The first (richest) variant within `limit`; the last one cut with «…» if none fits."""
    for text in variants:
        if len(text) <= limit:
            return text
    return variants[-1][:limit - 1] + "…"


def province_notes(rs: Ruleset, state: GameState, trade: Mapping[str, Any], seat: str, short: bool = False) -> str:
    """What a province part means for the seat: «перейдёт в расчёте; у Хор — оккупация, подчинить — Провиант 5» or
    «твои войска уйдут в соседнюю твою». `short` (the replies, list_trades, docs/TRADE.md 10): «в расчёте» once,
    then per province «P07 → Хор: оккупация, подчинить: Провиант 5» and where the giver's units go."""
    notes = []
    for pid, giver, taker in moves_of(trade):
        prov = state.provinces[pid]
        occ = cq.occupied(prov)
        back = state.origins.get(pid) == taker or (occ is not None and occ.get("from") == taker)
        who = "у тебя" if taker == seat else f"у {state.players[taker].nick}"
        price = "" if back else _subjugate_price(rs, prov, stat_key(state, rs, taker))
        if short:
            to = "тебе" if taker == seat else state.players[taker].nick
            what = "своя земля" if back else "оккупация" + (f", подчинить: {price}" if price else "")
            notes.append(f"{pid} → {to}: {what}")
            if giver == seat and prov.unit_total():
                notes.append("твои войска уйдут в соседнюю твою")
            continue
        if back:
            notes.append(f"{pid} перейдёт в расчёте; {who} — сразу полноценная (своя земля)")
        else:
            notes.append(f"{pid} перейдёт в расчёте; {who} — оккупация"
                         + (f", подчинить со следующего хода — {price}" if price else ""))
        if giver == seat and prov.unit_total():
            notes.append("твои войска из неё уйдут в соседнюю твою (нет — распустятся)")
    if short and notes:
        return "в расчёте: " + "; ".join(notes)
    return "; ".join(notes)


def _subjugate_price(rs: Ruleset, prov: Any, key: Any) -> str:
    annex = next((k for k, d in cq.ordered_dispositions(rs) if d.behavior == cq.ANNEX), None)
    if annex is None:
        return ""
    cost = cq.disposition_cost(rs, annex, prov, key)
    if not cost:
        return ""
    return ", ".join(f"{rs.resources[r].name} {n}" for r, n in cost.items())


def settled_text(rs: Ruleset, trade: Mapping[str, Any], seat: str) -> str:
    """`+Провиант 25, −Сплав 10` for the seat."""
    got = trade["want"] if seat == trade["a"] else trade["give"]
    gave = trade["give"] if seat == trade["a"] else trade["want"]
    parts = [f"+{x}" for x in goods_text(rs, got).split(", ") if x] + \
            [f"−{x}" for x in goods_text(rs, gave).split(", ") if x]
    # a report is sold as a copy: the seller keeps it (docs/TRADE.md 2.3)
    text = ", ".join(x.replace("+донесение ", "+копия ").replace("−донесение ", "продана копия ") for x in parts)
    return text or "без ресурсов"


def escrow_text(rs: Ruleset, amounts: Mapping[str, int]) -> str:
    return ", ".join(f"{rs.resources[r].name if r in rs.resources else r} {n}" for r, n in amounts.items() if n)


def state_line(state: GameState, rs: Ruleset, seat: str, who: Callable[[str], str]) -> str:
    """`ОБМЕН: от Хор [trade-A-B-2]: Сплав 10 за Провиант 5 — respond_trade · твоё → Ник: ждёт · P05 уйдёт в
    расчёте` (≤ 160 chars, at most 2 items, then `+k (list_trades)`); "" without open or pending trades."""
    items = []
    for t in state.trades:
        if t["status"] not in (PROPOSED, PENDING) or seat not in (t["a"], t["b"]):
            continue
        if t["status"] == PENDING:
            moves = [f"{pid} {'уйдёт' if giver == seat else 'придёт'} в расчёте" for pid, giver, _ in moves_of(t)]
            items.append((f"[{t['id']}] " + ", ".join(moves), ", ".join(moves)))
        elif t["b"] == seat:
            give, want = goods_text(rs, t["give"]), goods_text(rs, t["want"])
            what = (f"{give} за {want}" if give and want else f"дарит {give}" if give
                    else f"просит {want}" if want else "обещание")
            items.append((f"от {who(t['a'])} [{t['id']}]: {what} — respond_trade", f"от {who(t['a'])} [{t['id']}]"))
        else:
            items.append((f"твоё → {who(t['b'])}: ждёт", f"твоё → {who(t['b'])}"))
    if not items:
        return ""
    for n in range(min(STATE_LINE_ITEMS, len(items)), 0, -1):
        for form in (0, 1):
            shown = [i[form] for i in items[:n]]
            more = f" · +{len(items) - n} (list_trades)" if len(items) > n else ""
            line = "ОБМЕН: " + " · ".join(shown) + more
            if len(line) <= STATE_LINE_LIMIT:
                return line
    return f"ОБМЕН: обменов {len(items)} (list_trades)"


DISPUTE_LINE_LIMIT = 160        # core 0.8.1 (decision 32): the СПОР line of get_state
# core 0.8.1 (TRADE.md 12 G7, review F3): trade's growth of one get_state text: the ОБМЕН line (<= 160) and the СПОР
# line (<= DISPUTE_LINE_LIMIT), each with its line break
STATE_GROWTH_LIMIT = 160 + DISPUTE_LINE_LIMIT + 2
DISPUTE_ITEMS = 3
DISPUTE_TAIL = " — можно предложить обмен: propose_trade (провинция, ресурсы, донесение)"


def _ties(state: GameState, pid: str, seat: str) -> bool:
    """Public facts that tie `seat` to the province: it started with it (origin), the province is occupied from it,
    or the seat holds a claim on it in force."""
    prov = state.provinces[pid]
    occ = cq.occupied(prov)
    return (state.origins.get(pid) == seat or (occ is not None and occ.get("from") == seat)
            or cq.claim_of(state, seat, pid, state.turn) is not None)


def disputes(state: GameState, rs: Ruleset, seat: str, rival: str | None = None) -> list[tuple[str, str]]:
    """Core 0.8.1 (decision 32, m10: two seats each held a province of the other all game and never traded):
    [(province, rival)] where the seat holds a province a living rival is tied to (`_ties`), or a living rival
    holds one the seat is tied to. Public facts only (owners, origins, occupations, claims are public); only in a
    trade game where provinces can change hands by trade. `rival`: that rival only."""
    if not on(rs) or not provinces_on(rs) or seat not in state.players or not state.players[seat].alive:
        return []
    out = []
    for pid, prov in sorted(state.provinces.items()):
        owner = prov.owner
        if owner is None:
            continue
        if owner == seat:
            others = [r for r, p in sorted(state.players.items())
                      if r != seat and p.alive and (rival is None or r == rival) and _ties(state, pid, r)]
            out += [(pid, r) for r in others[:1]]
        elif (rival is None or owner == rival) and state.players[owner].alive and _ties(state, pid, seat):
            out.append((pid, owner))
    return out


def dispute_line(state: GameState, rs: Ruleset, seat: str, who: Callable[[str], str]) -> str:
    """`СПОР: P07 (Бета), P03 (Гамма) — можно предложить обмен: propose_trade (провинция, ресурсы, донесение)` (≤ 160
    chars, at most 3 provinces, then `+k`); "" without a dispute."""
    found = disputes(state, rs, seat)
    if not found:
        return ""
    for n in range(min(DISPUTE_ITEMS, len(found)), 0, -1):
        more = f" +{len(found) - n}" if len(found) > n else ""
        line = "СПОР: " + ", ".join(f"{pid} ({who(r)})" for pid, r in found[:n]) + more + DISPUTE_TAIL
        if len(line) <= DISPUTE_LINE_LIMIT:
            return line
    return f"СПОР: провинций {len(found)}" + DISPUTE_TAIL


def state_growth(state_line: str, dispute: str) -> int:
    """What trade adds to one get_state text: its ОБМЕН and СПОР lines, each with its line break (gate G7)."""
    return sum(len(x) + 1 for x in (state_line, dispute) if x)


def dispute_note(state: GameState, rs: Ruleset, seat: str, rival: str) -> str:
    """The reply to a pact offer between two seats in a dispute: `спор о P07, P03: можно и обмен — propose_trade
    (провинция, ресурсы, донесение)`; "" without one."""
    pids = sorted({pid for pid, _ in disputes(state, rs, seat, rival)})
    if not pids:
        return ""
    shown = ", ".join(pids[:DISPUTE_ITEMS]) + (f" +{len(pids) - DISPUTE_ITEMS}" if len(pids) > DISPUTE_ITEMS else "")
    return f"спор о {shown}: можно и обмен — propose_trade (провинция, ресурсы, донесение)"


def promises_line(state: GameState, rs: Ruleset, seat: str, who: Callable[[str], str]) -> str:
    """ОБЕЩАНИЯ of get_state, only with trade.show_promises (docs/TRADE.md 3, a separate «memory vs honesty»
    series): `ОБЕЩАНИЯ: ты → Хор: Сплав 20 к ходу 6 · Ник → тебе: P07 к ходу 7` (≤ 160 chars)."""
    items = []
    for p in promises(state, seat):
        side = f"ты → {who(p['to'])}" if p["from"] == seat else f"{who(p['from'])} → тебе"
        items.append(f"{side}: {goods_text(rs, p['goods'])} к ходу {p['by_turn']}")
    if not items:
        return ""
    line = "ОБЕЩАНИЯ: " + " · ".join(items)
    return line if len(line) <= STATE_LINE_LIMIT else line[:STATE_LINE_LIMIT - 16] + "… (list_trades)"


def trade_json(trade: Mapping[str, Any], seat: str, nick: Callable[[str], str]) -> dict[str, Any]:
    out = {"id": trade["id"], "with": nick(other_of(trade, seat)), "mine": trade["a"] == seat,
           "give": trade["give"], "want": trade["want"], "status": trade["status"],
           "proposed_turn": trade["proposed_turn"], "settle": trade.get("settle", "accept")}
    for key in ("later", "reply_to", "replaces", "fulfils", "replaced_by", "countered_by", "settled_turn",
                "failed_reason", "heads", "escrow"):
        if trade.get(key):
            out[key] = trade[key]
    return out


def list_text(state: GameState, rs: Ruleset, seat: str, who: Callable[[str], str]) -> str:
    """list_trades: open offers, pending trades, the trades of the last turns, promises still due (≤ 600 chars)."""
    open_lines, done_lines = [], []
    for t in reversed(mine(state, seat)):
        other = who(other_of(t, seat))
        terms = terms_text(rs, t, seat, state)
        if t["status"] == PROPOSED and t["b"] == seat:
            open_lines.append(f"{t['id']}: от {other} — {terms}; respond_trade(id=\"{t['id']}\", "
                              "decision=\"accept\"|\"reject\") до конца хода")
        elif t["status"] == PROPOSED:
            open_lines.append(f"{t['id']}: ты → {other} — {terms}; ждёт ответа")
        elif t["status"] == PENDING:
            held = escrow_text(rs, (t.get("escrow") or {}).get(seat) or {})
            open_lines.append(f"{t['id']}: с {other} — принят, переход в расчёте: {province_notes(rs, state, t, seat)}"
                              + (f"; твоё в залоге: {held}" if held else ""))
        elif t["status"] == DONE:
            done_lines.append(f"{t['id']}: с {other} — исполнен (ход {t['settled_turn']}): "
                              f"{settled_text(rs, t, seat)}")
        else:
            extra = (f" → {t['replaced_by']}" if t.get("replaced_by") else
                     f" → {t['countered_by']}" if t.get("countered_by") else
                     f" ({FAILED_TEXT[t['failed_reason']]})" if t.get("failed_reason") in (LOST, RUINED) else "")
            done_lines.append(f"{t['id']}: с {other} — {STATUS_TEXT[t['status']]}{extra}")
    promise_lines = []
    for p in promises(state, seat):
        side = f"ты → {who(p['to'])}" if p["from"] == seat else f"{who(p['from'])} → тебе"
        promise_lines.append(f"обещано: {side}: {goods_text(rs, p['goods'])} к ходу {p['by_turn']} ({p['trade']})")
    lines = open_lines + promise_lines + done_lines
    if not lines:
        return "обменов нет; предложить — propose_trade(to, give, want)"
    out: list[str] = []
    for i, line in enumerate(lines):
        more = f"… ещё {len(lines) - i} (старые — в history)"
        if len("\n".join(out + [line])) > LIST_LIMIT - len(more) - 1 and i < len(lines) - 1:
            out.append(more)
            break
        if len("\n".join(out + [line])) > LIST_LIMIT:
            out.append(line[:max(0, LIST_LIMIT - len("\n".join(out)) - 2)] + "…")
            break
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- report copies (docs/TRADE.md 2.3)

def copy_of(state: GameState, seat: str, cid: str) -> dict[str, Any] | None:
    player = state.players.get(seat)
    return next((c for c in (player.intel_copies if player else []) if c["id"] == cid), None)


def copy_report(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The copy as a report (for estimate(defender="c1") and the texts): the seller's report under the copy's id."""
    return {**entry["report"], "id": entry["id"], "shade": "Тень", "copy_of": entry["source"],
            "seller": entry["from"]}


def copy_head(state: GameState, entry: Mapping[str, Any]) -> str:
    seller = state.players[entry["from"]].nick if entry["from"] in state.players else str(entry["from"])
    head = f"КОПИЯ донесения {seller}, конец Д хода {entry.get('turn')}, заверено игрой"
    return head[:COPY_HEAD_LIMIT]


def copy_text(entry: Mapping[str, Any], rs: Ruleset, state: GameState, full: bool = True) -> str:
    """`get_intel(report_id="c1")`: the head (≤ 80 chars) and the seller's report text as the seller reads it (its
    windows, not recomputed), with «у твоих границ» and the other «ты» forms naming the seller (≤ 1200 chars)."""
    from .espionage import report_text
    seller = entry["from"]
    nick = state.players[seller].nick if seller in state.players else str(seller)
    text = report_text(copy_report(entry), rs, state, seller, full=full)
    for old, new in (("у твоих границ", f"у границ {nick}"), ("НА ТЕБЯ:", f"НА {nick}:"),
                     ("ударов по тебе нет", f"ударов по {nick} нет"), ("удар по тебе", f"удар по {nick}"),
                     ("[ты]", f"[{nick}]"), ("твою столицу", f"столицу {nick}")):
        text = text.replace(old, new)
    # the seller named as «ты» by the report's naming (espionage._nick: «взята у ты», «→ ты»): the seller's nick
    text = re.sub(r"(?<![\w-])ты(?![\w-])", nick, text)
    if len(text) > COPY_LIMIT:
        text = text[:COPY_LIMIT - 1] + "…"
    return copy_head(state, entry) + "\n" + text


def copies_line(state: GameState, rs: Ruleset, seat: str) -> str:
    """The seat's bought copies in get_intel without a report id: `копии: c1 (Нико, ход 5: Чтение приказов → Хор)
    — get_intel(report_id="c1")`."""
    player = state.players.get(seat)
    items = []
    for c in (player.intel_copies if player else []):
        head = report_head_text(rs, state, c["source"], _report_head(c["report"]), seat).split(": ", 1)[-1]
        seller = state.players[c["from"]].nick if c["from"] in state.players else c["from"]
        items.append(f"{c['id']} ({seller}: {head})")
    if not items:
        return ""
    return "копии: " + "; ".join(items[-4:]) + (f"; +{len(items) - 4}" if len(items) > 4 else "") + \
        " — get_intel(report_id=\"c<k>\")"


def _event_pair(e: Mapping[str, Any], who: Any) -> str:
    return " и ".join(who(s) for s in e["seats"])


def _settled_event(e: Mapping[str, Any], rs: Ruleset, who: Any) -> str:
    give, want = goods_text(rs, e.get("give")), goods_text(rs, e.get("want"))
    when = " в расчёте" if e.get("at") == "resolution" else ""
    return (f"обмен {e['trade']} исполнен{when}: {who(e.get('a'))} отдал {give or 'ничего'}, получил "
            f"{want or 'ничего'}")


def _failed_event(e: Mapping[str, Any], rs: Ruleset, who: Any) -> str:
    if e.get("reason") in (LOST, RUINED):
        return (f"обмен {e['trade']} сорван в расчёте: {e.get('lost_province')} — {FAILED_TEXT[e['reason']]}; залог "
                "возвращён")
    return f"обмен {e['trade']} сорван: у предложившего ({who(e['short_side'])}) не хватило обещанного"


def _transferred_event(e: Mapping[str, Any], rs: Ruleset, who: Any) -> str:
    how = "возвращение, полноценная" if e.get("status") == "return" else "оккупация"
    units = e.get("units_withdrawn") or {}
    gone = ("; войска ушли: " + " ".join(f"{rs.short(u)}×{n}" for u, n in units.items())) if units else ""
    return f"{e['province']} передана обменом {e.get('trade')}: {who(e['from'])} → {who(e['to'])} ({how}){gone}"


EVENT_TEXT = {
    "trade_proposed": lambda e, rs, who: (f"предложен обмен {e['trade']}: {who(e['sender'])} даёт "
                                          f"{goods_text(rs, e['give']) or 'ничего'} за "
                                          f"{goods_text(rs, e['want']) or 'ничего'} ({_event_pair(e, who)})"),
    "trade_rejected": lambda e, rs, who: f"обмен {e['trade']} отклонён ({_event_pair(e, who)})",
    "trade_withdrawn": lambda e, rs, who: f"предложение обмена {e['trade']} отозвано ({_event_pair(e, who)})",
    "trade_settled": _settled_event,
    "trade_accepted": lambda e, rs, who: f"обмен {e['trade']} принят: переход в расчёте ({_event_pair(e, who)})",
    "trade_failed": _failed_event,
    "trade_lapsed": lambda e, rs, who: f"предложение обмена {e['trade']} не принято до конца хода ({_event_pair(e, who)})",
    TRANSFERRED: _transferred_event,
}
