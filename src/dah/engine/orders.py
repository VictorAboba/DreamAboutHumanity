"""Order parsing and validation, resource/unit reservations and P2 edits priced in command points (CP)."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, replace
from itertools import count as _counter
from typing import Any, Callable, Iterable, Mapping

from .content import Ruleset, StatKey
from .diplomacy import pact_between
from .model import ACTIONS, BREAK, HONOR, INT_BOUND, P2, GameState, Order, SeatOrders, as_int, sha256_json
from .techs import (ALREADY_RESEARCHED, NOT_AVAILABLE, building_problem, move_problem, province_stat,
                    resolve_tech_ref, stat_key, tech_problem, unit_problem)

KINDS = ("move", "recruit", "build", "fortify", "research")
PACT_FLAGS = (HONOR, BREAK)
FREE_PHASES = (ACTIONS,)    # orders are given in actions (free) and edited in negotiation_2 (priced in CP)
P1 = "negotiation_1"
P1_LOCKED_HINT = "в П1 приказы можно только проверить через validate_orders; подавать — в фазе Д"
CP_CANCEL = 1
CP_REDUCE = 1
CP_FORTIFY = 1

NOT_ADJACENT = "NOT_ADJACENT"
NOT_OWNER = "NOT_OWNER"
NOT_ENOUGH_UNITS = "NOT_ENOUGH_UNITS"
NOT_ENOUGH_RESOURCES = "NOT_ENOUGH_RESOURCES"
NO_RECRUIT_BUILDING = "NO_RECRUIT_BUILDING"
ALREADY_BUILT = "ALREADY_BUILT"
PHASE_LOCKED = "PHASE_LOCKED"
NOT_ENOUGH_CP = "NOT_ENOUGH_CP"
UNKNOWN_ID = "UNKNOWN_ID"
INVALID = "INVALID"          # malformed order or patch (not in SLICE's list; needed for bad input)
# stage 2 (TECH_AND_SETUP.md, from engine/techs.py): NOT_AVAILABLE (a unit, building or tech locked by techs,
# faction or a missing building), ALREADY_RESEARCHED (research of a tech the seat has)
STAGE2_REASONS = (NOT_AVAILABLE, ALREADY_RESEARCHED)


class OrderRejected(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class Check:
    ok: bool
    reason: str | None = None
    message: str = ""
    cp_cost: int = 0
    order: Order | None = None
    warning: str | None = None      # accepted, but probably not what the player wants (a pact bounce)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "cp_cost": self.cp_cost}
        if self.reason:
            out["reason"] = self.reason
            out["message"] = self.message
        if self.warning:
            out["warning"] = self.warning
        if self.order is not None:
            out["order"] = self.order.to_dict()
        return out


@dataclass(frozen=True)
class SubmitResult:
    book: SeatOrders
    results: list[Check]                    # one per submitted item, in order

    @property
    def accepted(self) -> list[Order]:
        return [c.order for c in self.results if c.ok and c.order is not None]

    @property
    def rejected(self) -> list[Check]:
        return [c for c in self.results if not c.ok]


@dataclass(frozen=True)
class EditResult:
    ok: bool
    book: SeatOrders
    reason: str | None = None
    message: str = ""
    cp_cost: int = 0
    order: Order | None = None              # the amended order (amend only)
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "cp_cost": self.cp_cost}
        if self.reason:
            out.update(reason=self.reason, message=self.message)
        if self.warning:
            out["warning"] = self.warning
        if self.order is not None:
            out["order"] = self.order.to_dict()
        return out


# ---------------------------------------------------------------- parsing

def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise OrderRejected(INVALID, f"поле {key!r} обязательно (строка)")
    return value


_as_int = as_int
_INT_BOUND = INT_BOUND


def _positive_int(value: Any, key: str) -> int:
    value = _as_int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OrderRejected(INVALID, f"поле {key!r} должно быть целым числом ≥ 1")
    return value


def _label(text: str) -> str:
    return text.strip().lower().rstrip(".")


def resolve_unit_ref(ruleset: Ruleset, ref: Any) -> str | None:
    """Full unit id for a full or short id, a state label (штурм) or a name (Штурмовики), any case."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    full = ruleset.resolve("unit", ref) or ruleset.resolve("unit", ref.lower())
    if full:
        return full
    label = _label(ref.split(":", 1)[-1])
    hits = [uid for uid, u in ruleset.units.items() if label in (_label(u.short), _label(u.name))]
    return hits[0] if len(hits) == 1 else None


def _shown(value: Any, limit: int = 120) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


FLAT_MOVE_EXAMPLE = '{"kind":"move","from":"P01","to":"P04","unit":"assault","count":2}'
STRING_ORDERS_EXAMPLE = 'orders="[{\\"kind\\":\\"move\\",\\"from\\":\\"P01\\",\\"to\\":\\"P04\\",\\"units\\":{\\"assault\\":2}}]"'
FLAT_PATCH_EXAMPLE = 'patch={"unit":"assault","count":1}'
STRING_PATCH_EXAMPLE = 'patch="{\\"units\\":{\\"assault\\":1}}"'


def _empty_units_message(raw: Any, received: Mapping[str, Any] | None, patch: bool) -> str:
    got = "поле 'units' не задано" if raw is None else f"поле 'units' пустое: получено units={_shown(raw)}"
    if received is not None:
        got += f" (весь {'patch' if patch else 'приказ'}: {_shown(dict(received), 200)})"
    if patch:
        return (f"{got}. Нужно {{юнит: число}}. Если клиент теряет вложенный объект — плоская форма: "
                f"{FLAT_PATCH_EXAMPLE}, или передай patch одной JSON-строкой: {STRING_PATCH_EXAMPLE}")
    return (f"{got}. Нужно {{юнит: число}}. Если клиент теряет вложенный объект — плоская форма: "
            f"{FLAT_MOVE_EXAMPLE}, или передай orders одной JSON-строкой: {STRING_ORDERS_EXAMPLE}")


def _decode_units(raw: Any) -> Any:
    """{"assault": 2} as is; also a JSON string of it, or a list of {"unit", "count"} / ["assault", 2] pairs."""
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if isinstance(raw, list):
        converted: dict[Any, Any] = {}
        for item in raw:
            if isinstance(item, Mapping) and isinstance(item.get("unit"), str):
                ref, n = item["unit"], item.get("count", item.get("n"))
            elif isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
                ref, n = item
            else:
                return raw
            n = _as_int(n)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:     # each entry, before any summing
                raise OrderRejected(INVALID, f"число юнитов {str(ref)[:40]!r} должно быть целым ≥ 0: "
                                             f"получено {_shown(item)}")
            converted[ref] = converted.get(ref, 0) + n
        return converted
    return raw


def _no_positive_count(raw: Any) -> bool:
    """Nested units that carry nothing: missing, empty, or every count zero (keys kept, values lost)."""
    if raw is None or (isinstance(raw, (Mapping, list, str)) and not raw):
        return True
    return isinstance(raw, Mapping) and all(_as_int(n) == 0 and not isinstance(n, bool) for n in raw.values())


def _move_units(ruleset: Ruleset, data: Mapping[str, Any], patch: bool = False) -> dict[str, int]:
    """Units of a move or a move patch: nested {"units": {...}} (also a list of pairs or a JSON string) or flat
    {"unit": "assault", "count": 2}. Both together only if they agree; null fields count as absent (some
    clients fill every schema property with null), and a stray count next to nested units is ignored."""
    flat, count = data.get("unit"), data.get("count")
    raw = _decode_units(data.get("units"))
    if flat is None:
        if count is None or not _no_positive_count(raw):
            return _units(ruleset, data.get("units"), data, patch)
        flat = ""      # count without unit and without nested units: refused below with the flat example
    example = FLAT_PATCH_EXAMPLE if patch else FLAT_MOVE_EXAMPLE
    if not isinstance(flat, str) or not flat.strip():
        raise OrderRejected(INVALID, f"плоская форма: 'unit' — строка с юнитом, 'count' — число ≥ 1; получено "
                                     f"unit={_shown(flat)}, count={_shown(count)}. Пример: {example}")
    flat_units = _units(ruleset, {flat: _positive_int(count, "count")})
    if _no_positive_count(raw):
        return flat_units       # flat wins over a lost nested object (empty, or keys with zero counts)
    nested = _units(ruleset, raw, data, patch)
    if nested != flat_units:
        raise OrderRejected(INVALID, f"указаны и units={_shown(data.get('units'))}, и unit/count "
                                     f"({_shown(flat)}×{_shown(data.get('count'))}), и они расходятся: оставь одно")
    return nested


def _units(ruleset: Ruleset, raw: Any, received: Mapping[str, Any] | None = None,
           patch: bool = False) -> dict[str, int]:
    given, raw = raw, _decode_units(raw)
    if raw is None or (isinstance(raw, (Mapping, list, str)) and not raw):
        raise OrderRejected(INVALID, _empty_units_message(given, received, patch))
    if not isinstance(raw, Mapping):
        raise OrderRejected(INVALID, f"поле 'units' — словарь {{юнит: число}}, а получено units={_shown(raw)}. "
                                     f"Плоская форма: {FLAT_PATCH_EXAMPLE if patch else FLAT_MOVE_EXAMPLE}")
    out: dict[str, int] = {}
    for ref, n in raw.items():
        full = resolve_unit_ref(ruleset, ref)
        if full is None:
            known = ", ".join(f"{uid} ({u.short})" for uid, u in ruleset.units.items())
            raise OrderRejected(UNKNOWN_ID, f"неизвестный юнит {str(ref)[:40]!r}; юниты: {known}")
        n = _as_int(n)
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise OrderRejected(INVALID, f"число юнитов {ref!r} должно быть целым ≥ 0")
        if n:
            out[full] = out.get(full, 0) + n
    if not out:
        raise OrderRejected(INVALID, f"в приказе нет ни одного юнита: получено units={_shown(raw)}")
    return dict(sorted(out.items()))


def _pact_flag(value: Any) -> str:
    """`pact` of a move: "honor" (default; null too) or "break"."""
    if value is None:
        return HONOR
    flag = value.strip().lower() if isinstance(value, str) else value
    if flag not in PACT_FLAGS:
        raise OrderRejected(INVALID, f"pact — \"honor\" (соблюдать пакт, по умолчанию) или \"break\" (нарушить: "
                                     f"бой с союзником, пакт публично разрывается); получено {_shown(value)}")
    return flag


def decode_json_arg(value: Any) -> Any:
    """A value sent as a JSON string (an order, a list of orders, a patch) becomes its object; anything else,
    including a malformed string, is returned as is for the caller to refuse."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def parse_order(ruleset: Ruleset, data: Mapping[str, Any], order_id: str, phase: str) -> Order:
    """Normalize a JSON order into the canonical Order. Raises OrderRejected.

    Lenient on input, canonical on output: unit ids may be full, short or state labels; a move's units may be
    nested ({"units": {"assault": 2}}, also a list of pairs or a JSON string) or flat ({"unit": "assault",
    "count": 2}); the order itself may be a JSON string. The Order is always the nested form.
    """
    data = decode_json_arg(data)
    if not isinstance(data, Mapping):
        raise OrderRejected(INVALID, f"приказ должен быть JSON-объектом, получено {_shown(data)}")
    kind = data.get("kind")
    if isinstance(kind, str):
        kind = kind.strip().lower()
    if kind not in KINDS:
        raise OrderRejected(INVALID, f"kind должен быть одним из {', '.join(KINDS)}")
    added = P2 if phase == P2 else ACTIONS
    if kind == "research":
        return _research_order(ruleset, data, order_id, added)
    if kind == "move":
        return Order(order_id, kind, added, src=_require_str(data, "from"), dst=_require_str(data, "to"),
                     units=_move_units(ruleset, data), pact=_pact_flag(data.get("pact")))
    province = _require_str(data, "province")
    if kind == "recruit":
        unit = resolve_unit_ref(ruleset, _require_str(data, "unit"))
        if unit is None:
            raise OrderRejected(UNKNOWN_ID, f"неизвестный юнит {data.get('unit')!r}")
        return Order(order_id, kind, added, province=province, unit=unit,
                     count=_positive_int(data.get("count"), "count"))
    if kind == "build":
        building = ruleset.resolve("building", _require_str(data, "building"))
        if building is None:
            raise OrderRejected(UNKNOWN_ID, f"неизвестное здание {data.get('building')!r}")
        return Order(order_id, kind, added, province=province, building=building)
    return Order(order_id, kind, added, province=province)


RESEARCH_EXAMPLE = '{"kind":"research","tech":"<id технологии>"}'


def _research_order(ruleset: Ruleset, data: Mapping[str, Any], order_id: str, added: str) -> Order:
    ref = data.get("tech")
    tech = resolve_tech_ref(ruleset, ref)
    if tech is None:
        if not ruleset.techs:
            raise OrderRejected(NOT_AVAILABLE, "в этой партии нет технологий")
        if not isinstance(ref, str) or not ref.strip():
            raise OrderRejected(INVALID, f"поле 'tech' обязательно: {RESEARCH_EXAMPLE}")
        raise OrderRejected(UNKNOWN_ID, f"неизвестная технология {str(ref)[:40]!r}; список — list_techs")
    return Order(order_id, "research", added, tech=tech)


# ---------------------------------------------------------------- reservations

def order_cost(ruleset: Ruleset, faction: StatKey, order: Order, state: GameState | None = None) -> Counter[str]:
    """Resources an order holds. `faction` is the seat's stat key (techs.stat_key; a faction id is the slice's
    case); with `state`, a recruit gets its province's `province.recruit_discount`, applied per unit and rounded
    in the seat's favour (the unit price is rounded down), so one order of N costs the same as N orders of one."""
    if order.kind == "recruit":
        assert order.unit is not None and order.count is not None
        unit = ruleset.unit_cost(order.unit, faction)
        if state is not None and order.province in state.provinces:
            discount = province_stat(state, ruleset, order.province, "province.recruit_discount")
            if discount > 0:
                # Per unit, the discount is rounded up (the price down), so batching never changes the price.
                unit = {r: n - math.ceil(n * discount - 1e-9) for r, n in unit.items()}
        return Counter({r: n * order.count for r, n in unit.items() if n * order.count > 0})
    if order.kind == "build":
        assert order.building is not None
        return Counter(ruleset.building_cost(order.building, faction))
    return Counter()


def reserved_resources(ruleset: Ruleset, faction: StatKey, book: SeatOrders,
                       state: GameState | None = None) -> dict[str, int]:
    """Resources held by recruit/build orders plus reserve already forfeited in P2. Pass the seat's stat key and
    the state (`seat_reserved` does both) for techs, artifacts and recruit discounts to count."""
    total: Counter[str] = Counter(book.forfeit)
    for o in book.orders:
        total.update(order_cost(ruleset, faction, o, state))
    return dict(sorted((k, v) for k, v in total.items() if v))


def seat_reserved(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders) -> dict[str, int]:
    """reserved_resources with the seat's stat key and recruit discounts."""
    return reserved_resources(ruleset, stat_key(state, ruleset, seat), book, state)


def reserved_units(orders: Iterable[Order], province: str) -> Counter[str]:
    total: Counter[str] = Counter()
    for o in orders:
        if o.kind == "move" and o.src == province:
            total.update(o.units or {})
    return total


def orders_hash(book: SeatOrders) -> str:
    return sha256_json([o.to_dict() for o in book.orders])


def cp_left(state: GameState, seat: str, book: SeatOrders) -> int:
    return state.players[seat].command_points - book.cp_spent


# ---------------------------------------------------------------- legality (phase independent)

def _rules_problem(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders, order: Order,
                   exclude: str | None = None) -> tuple[str, str] | None:
    player = state.players.get(seat)
    if player is None or not player.alive:
        return PHASE_LOCKED, "место выбыло из игры"
    others = [o for o in book.orders if exclude is None or o.id != exclude]
    key = stat_key(state, ruleset, seat)
    if order.kind in ruleset.gated_orders and f"order:{order.kind}" not in ruleset.unlocks(key):
        return NOT_AVAILABLE, f"приказ {order.kind} ещё не открыт (нужна технология или артефакт)"
    if order.kind == "research":
        assert order.tech is not None
        if any(o.kind == "research" for o in others):
            return INVALID, "приказ research уже есть: одна активная технология (отмени прежний приказ)"
        return tech_problem(state, ruleset, seat, order.tech)
    target = order.src if order.kind == "move" else order.province
    if target not in state.provinces:
        return UNKNOWN_ID, f"неизвестная провинция {target!r}"
    prov = state.provinces[target]
    if prov.owner != seat:
        return NOT_OWNER, f"{target} — не твоя провинция"
    if order.kind == "move":
        if order.dst not in state.provinces:
            return UNKNOWN_ID, f"неизвестная провинция {order.dst!r}"
        far = move_problem(state, ruleset, seat, target, order.dst, (order.units or {}).keys(), key)
        if far is not None:
            return NOT_ADJACENT, far
        busy = reserved_units(others, target)
        for unit, n in (order.units or {}).items():
            free = prov.units.get(unit, 0) - busy.get(unit, 0)
            if n > free:
                return NOT_ENOUGH_UNITS, f"в {target} свободно {ruleset.short(unit)}×{max(free, 0)}, нужно {n}"
        return None
    if order.kind == "fortify":
        if any(o.kind == "fortify" and o.province == target for o in others):
            return INVALID, f"{target} уже укрепляется"
        return None
    if order.kind == "recruit":
        if not any(ruleset.buildings[b].recruit for b in prov.buildings):
            return NO_RECRUIT_BUILDING, f"в {target} нет здания для найма"
        locked = unit_problem(state, ruleset, seat, order.unit, target, key)
        if locked:
            return NOT_AVAILABLE, locked
    if order.kind == "build":
        b = order.building
        locked = building_problem(state, ruleset, seat, b, key)
        if locked:
            return NOT_AVAILABLE, locked
        pending = any(p.province == target and p.building == b for p in state.pending_builds)
        ordered = any(o.kind == "build" and o.province == target and o.building == b for o in others)
        if b in prov.buildings or pending or ordered:
            return ALREADY_BUILT, f"{ruleset.short(b)} в {target} уже есть или строится"
    need = order_cost(ruleset, key, order, state)
    held = reserved_resources(ruleset, key, replace(book, orders=others), state)
    for res, n in sorted(need.items()):
        free = player.resources.get(res, 0) - held.get(res, 0)
        if n > free:
            return NOT_ENOUGH_RESOURCES, f"{ruleset.resources[res].name}: свободно {free}, нужно {n}"
    return None


def _new_order_cp(ruleset: Ruleset, faction: StatKey, order: Order, phase: str) -> tuple[str, str] | int:
    if phase in FREE_PHASES:
        return 0
    if phase == P1:
        return PHASE_LOCKED, P1_LOCKED_HINT
    if phase != P2:
        return PHASE_LOCKED, "приказы сейчас не принимаются"
    if order.kind in ("recruit", "build"):
        return PHASE_LOCKED, "найм и стройка в П2 запрещены"
    if order.kind == "research":
        return PHASE_LOCKED, "исследование выбирают только в фазе Д"
    if order.kind == "fortify":
        return CP_FORTIFY
    return int(ruleset.stat("p2.cost.new_attack", faction))


def _check(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders, order: Order, phase: str) -> Check:
    if seat not in state.players:
        return Check(False, UNKNOWN_ID, f"неизвестное место {seat!r}", order=order)
    priced = _new_order_cp(ruleset, stat_key(state, ruleset, seat), order, phase)
    if isinstance(priced, tuple):
        return Check(False, *priced, order=order)
    problem = _rules_problem(state, ruleset, seat, book, order)
    if problem:
        return Check(False, *problem, cp_cost=priced, order=order)
    if priced > cp_left(state, seat, book):
        return Check(False, NOT_ENOUGH_CP, f"нужно {priced} ОК, осталось {cp_left(state, seat, book)}",
                     cp_cost=priced, order=order)
    return Check(True, cp_cost=priced, order=order, warning=pact_warning(state, seat, order))


def pact_warning(state: GameState, seat: str, order: Order) -> str | None:
    """A move into a pact partner's province: with "honor" it bounces back (no fight); with "break" it is the
    public breach of the pact. Said as a warning, never a refusal."""
    if order.kind != "move" or order.dst not in state.provinces:
        return None
    owner = state.provinces[order.dst].owner
    pact = pact_between(state, seat, owner, state.turn)
    if pact is None or owner is None:
        return None
    nick = state.players[owner].nick
    if order.pact == BREAK:
        return (f"{order.dst} — провинция союзника по пакту ({nick}): pact=break — это нарушение, этот ход "
                f"будет биться, пакт публично разорвётся после расчёта; прочие встречи с {nick} в этом расчёте "
                f"останутся без боя")
    return (f"{order.dst} — провинция союзника по пакту ({nick}): с pact=honor войска вернутся в {order.src} без "
            f"боя; чтобы атаковать, нужен pact=\"break\" (публичное нарушение пакта)")


# ---------------------------------------------------------------- public operations

def _temp_ids(taken: Iterable[str]) -> Callable[[], str]:
    """Ids for dry runs, distinct from ids already in the book."""
    used = set(taken)
    candidates = (f"tmp{n}" for n in _counter(1))
    return lambda: next(c for c in candidates if c not in used)


def submit_orders(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders,
                  items: Iterable[Mapping[str, Any]], phase: str, mode: str = "append",
                  id_factory: Callable[[], str] | None = None) -> SubmitResult:
    """Validate items one by one (each sees the reservations of the previous ones) and return the new book.

    The input book is not modified. Dry-run validation = call this and use ``results`` only.
    ``replace_all`` drops existing orders first; it is free outside P2 and PHASE_LOCKED in P2
    (in P2 use append / cancel_order / amend_order so that every change is priced).
    """
    ids = id_factory or _temp_ids(o.id for o in book.orders)
    items = list(items)
    if mode not in ("append", "replace_all"):
        return SubmitResult(book, [Check(False, INVALID, "mode: append или replace_all") for _ in items])
    if mode == "replace_all" and phase == P2:
        msg = "в П2 нельзя заменить все приказы: используй append, cancel_order, amend_order"
        return SubmitResult(book, [Check(False, PHASE_LOCKED, msg) for _ in items])
    work = SeatOrders([], 0, {}) if mode == "replace_all" else SeatOrders(list(book.orders), book.cp_spent,
                                                                           dict(book.forfeit))
    results = []
    for item in items:
        try:
            order = parse_order(ruleset, item, ids(), phase)
        except OrderRejected as exc:
            results.append(Check(False, exc.reason, exc.message))
            continue
        check = _check(state, ruleset, seat, work, order, phase)
        if check.ok:
            work.orders.append(order)
            work.cp_spent += check.cp_cost
        results.append(check)
    return SubmitResult(work, results)


def check_order(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders,
                item: Mapping[str, Any], phase: str) -> Check:
    """Validate one new order against an existing book (no change)."""
    return submit_orders(state, ruleset, seat, book, [item], phase).results[0]


def _forfeit(ruleset: Ruleset, faction: StatKey, removed: Counter[str]) -> Counter[str]:
    refund = ruleset.stat("p2.cancel_refund", faction)
    return Counter({r: n - math.floor(n * refund) for r, n in removed.items() if n - math.floor(n * refund)})


def _edit_phase_problem(state: GameState, seat: str, phase: str) -> tuple[str, str] | None:
    if seat not in state.players:
        return UNKNOWN_ID, f"неизвестное место {seat!r}"
    if phase == P1:
        return PHASE_LOCKED, P1_LOCKED_HINT
    if phase not in FREE_PHASES and phase != P2:
        return PHASE_LOCKED, "приказы сейчас не меняются"
    if not state.players[seat].alive:
        return PHASE_LOCKED, "место выбыло из игры"
    return None


def cancel_order(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders, order_id: str,
                 phase: str) -> EditResult:
    """Free outside P2; in P2 costs 1 CP and only ``p2.cancel_refund`` of the reserve comes back. A research order
    cannot be cancelled in P2 (PHASE_LOCKED): the research choice is made only in Д."""
    problem = _edit_phase_problem(state, seat, phase)
    if problem:
        return EditResult(False, book, *problem)
    order = book.find(order_id)
    if order is None:
        return EditResult(False, book, UNKNOWN_ID, f"нет приказа {order_id!r}")
    if phase == P2 and order.kind == "research":
        # The research choice is fixed after Д: cancelling it in П2 would switch back to the previous tech.
        return EditResult(False, book, PHASE_LOCKED, "исследование выбирают только в фазе Д")
    rest = [o for o in book.orders if o.id != order_id]
    if phase != P2:
        return EditResult(True, replace(book, orders=rest))
    if CP_CANCEL > cp_left(state, seat, book):
        return EditResult(False, book, NOT_ENOUGH_CP, "нужно 1 ОК", CP_CANCEL)
    faction = stat_key(state, ruleset, seat)
    forfeit = Counter(book.forfeit) + _forfeit(ruleset, faction, order_cost(ruleset, faction, order, state))
    return EditResult(True, SeatOrders(rest, book.cp_spent + CP_CANCEL, dict(sorted(forfeit.items()))),
                      cp_cost=CP_CANCEL)


def amend_order(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders, order_id: str,
                patch: Mapping[str, Any], phase: str) -> EditResult:
    """Change ``units``/``to``/``pact`` of a move or ``count`` of a recruit.

    In P2: reducing (or turning "break" back to "honor") costs 1 CP; a new target, more units or turning "honor"
    into "break" (the betrayal) costs ``p2.cost.new_attack`` and makes the move hasty (phase_added =
    negotiation_2); increasing a recruit is PHASE_LOCKED.
    """
    problem = _edit_phase_problem(state, seat, phase)
    if problem:
        return EditResult(False, book, *problem)
    old = book.find(order_id)
    if old is None:
        return EditResult(False, book, UNKNOWN_ID, f"нет приказа {order_id!r}")
    patch = decode_json_arg(patch)
    if not isinstance(patch, Mapping):
        return EditResult(False, book, INVALID, f"patch должен быть объектом, получено {_shown(patch)}")
    patch = {k: v for k, v in patch.items() if v is not None}     # null-filling clients: null = not changed
    allowed = {"move": {"units", "to", "pact"}, "recruit": {"count"}, "research": {"tech"}}.get(old.kind, set())
    accepted = allowed | ({"unit", "count"} if old.kind == "move" else set())   # move: the flat form too
    extra = set(patch) - accepted
    if not patch or extra:
        what = ", ".join(sorted(allowed)) or "ничего"
        if old.kind == "move":
            what += f" (или плоско unit+count: {FLAT_PATCH_EXAMPLE})"
        return EditResult(False, book, INVALID, f"у приказа {old.kind} можно менять: {what}")
    faction = stat_key(state, ruleset, seat)
    try:
        if old.kind == "move":
            moved = any(k in patch for k in ("units", "unit", "count"))
            new = replace(old, units=_move_units(ruleset, patch, patch=True) if moved else old.units,
                          dst=_require_str(patch, "to") if "to" in patch else old.dst,
                          pact=_pact_flag(patch["pact"]) if "pact" in patch else old.pact)
        elif old.kind == "research":
            new = replace(old, tech=_research_order(ruleset, patch, old.id, old.phase_added).tech)
        else:
            new = replace(old, count=_positive_int(patch["count"], "count"))
    except OrderRejected as exc:
        return EditResult(False, book, exc.reason, exc.message)
    cost, forfeit = 0, Counter[str]()
    if phase == P2:
        try:
            cost, hasty = _amend_price(ruleset, faction, old, new)
        except OrderRejected as exc:
            return EditResult(False, book, exc.reason, exc.message)
        if hasty:
            new = replace(new, phase_added=P2)
        removed = order_cost(ruleset, faction, old, state) - order_cost(ruleset, faction, new, state)
        forfeit = _forfeit(ruleset, faction, removed)
    work = SeatOrders([new if o.id == order_id else o for o in book.orders], book.cp_spent,
                      dict(Counter(book.forfeit) + forfeit))
    problem = _rules_problem(state, ruleset, seat, replace(work, forfeit=dict(book.forfeit)), new, exclude=order_id)
    if problem:
        return EditResult(False, book, *problem, cp_cost=cost)
    if cost > cp_left(state, seat, book):
        return EditResult(False, book, NOT_ENOUGH_CP, f"нужно {cost} ОК, осталось {cp_left(state, seat, book)}",
                          cost)
    work.cp_spent += cost
    return EditResult(True, work, cp_cost=cost, order=new, warning=pact_warning(state, seat, new))


def _amend_price(ruleset: Ruleset, faction: StatKey, old: Order, new: Order) -> tuple[int, bool]:
    """(CP cost, becomes hasty) of a P2 amendment. Raises OrderRejected."""
    if old.kind == "research":
        raise OrderRejected(PHASE_LOCKED, "исследование выбирают только в фазе Д")
    if old.kind == "recruit":
        assert old.count is not None and new.count is not None
        if new.count > old.count:
            raise OrderRejected(PHASE_LOCKED, "увеличить найм в П2 нельзя")
        if new.count == old.count:
            raise OrderRejected(INVALID, "ничего не изменилось")
        return CP_REDUCE, False
    old_u, new_u = Counter(old.units or {}), Counter(new.units or {})
    betrayal = old.pact == HONOR and new.pact == BREAK
    if new.dst != old.dst or betrayal or any(new_u[u] > old_u[u] for u in new_u):
        return int(ruleset.stat("p2.cost.new_attack", faction)), True
    if new_u == old_u and new.pact == old.pact:
        raise OrderRejected(INVALID, "ничего не изменилось")
    return CP_REDUCE, False


def executable_orders(state: GameState, ruleset: Ruleset, seat: str,
                      book: SeatOrders) -> tuple[list[Order], list[tuple[Order, str]]]:
    """Re-check a book at resolution time (no phase/CP rules): (valid orders, [(order, reason)])."""
    valid = SeatOrders([], 0, dict(book.forfeit))
    rejected = []
    for order in book.orders:
        problem = _rules_problem(state, ruleset, seat, valid, order)
        if problem:
            rejected.append((order, problem[0]))
        else:
            valid.orders.append(order)
    return valid.orders, rejected

