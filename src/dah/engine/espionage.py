"""Espionage, stage 3a (docs/SPY_AND_CONQUEST.md sections 1.5, 2, 4): Shades, missions, the recon step, sabotage.

Everything here is a no-op unless the ruleset has the `espionage` system on (`Ruleset.system("espionage")`): no Shade
or report ever appears, so games without espionage resolve and hash exactly as before.

Shade (`Player.shades`, private to its seat):
    {"id": "s1", "xp": V, "status": "idle" | "mission", "hired_turn": T,
     "mission": None | {"type": mission id, "target": seat | province, "rounds": N, "played": k, "successes": n,
                        "first_turn": T, "phase": actions | negotiation_2, "report_ids": [...], "ended"?: reason}}
A mission with `ended` (done | empty | struck | recalled | void) keeps its Shade on `mission` until step 1b of the next
resolution, which makes it `idle` (and gives experience). A caught Shade is removed by the recon step at once.

The recon step (`recon`) runs when actions close: it carries out and pays for the actions book's `hire_shade`, `recall`
and `spy` entries (`orders.recon_consumed`), then plays one round of every active mission, ordered by the Shade's
stable uid `f"{seat}/{shade}"`. Each check rolls its own stream `stream(seed, T, "spy.<check>", uid)`, so a new Shade
or mission of one seat never moves another's rolls. Reports carry `facts` (what the owner sees: honest intervals,
false entries masked) next to `truth` (the same selection, exact).
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from . import conquest as cq
from .content import MissionDef, Ruleset, StatKey
from .model import ACTIONS, P2, GameState, Order, Province, SeatOrders, as_int, canonical_json, sha256_json
from .rng import stream
from .techs import province_stat, stat_key

if TYPE_CHECKING:       # pragma: no cover
    from .resolve import _Resolution

ESPIONAGE = "espionage"
IDLE, MISSION = "idle", "mission"
# why a mission ended (journal `mission_end.reason`)
DONE, EMPTY, STRUCK, RECALLED, VOID, CAUGHT = "done", "empty", "struck", "recalled", "void", "caught"
# a round's outcome for the Shade's owner
FOUND, NOTHING, LOST, HIT, VOIDED = "found", "nothing", "lost", "struck", "void"
OBSERVE, LEDGER, READ_ORDERS, SABOTAGE = "observe_province", "read_ledger", "read_orders", "sabotage"
READ_OBJECTIVE = "read_objective"     # core 0.6.0 (decision 26): the target's secret objectives

# Report events only their sides see (resolve._PRIVATE_KINDS); the victim of a `sabotage` sees it without its author.
PRIVATE_KINDS = frozenset({"sabotage", "strike_cancelled", "feint", "mission_started", "mission_end",
                           # core 0.8.0 (docs/SPY_V2.md 7): what a strike did and its point, to the spy only
                           "sabotage_effect", "intel_point",
                           # to the build's owner: its build stands this turn by sabotage, no author
                           "build_sabotaged"})

COMPACT_ENTRIES = 6            # read_orders: entry lines in the compact text, then "+k ещё"
COMPACT_CHARS = 400            # one compact report
FULL_CHARS = 1200              # one report in full (get_intel(report_id))
INTEL_CHARS = 1500             # get_intel without a report id
SPIES_CHARS = 600              # list_spies
ESTIMATE_CHARS = 400           # estimate_mission
STATE_SHADES = 3               # ДОНЕСЕНИЯ in get_state: at most this many Shades, then "+k (list_spies)"
SKIPPED_IN_READ = ("research", "hire_shade", "recall")   # entries read_orders does not show (2.4)

_SHADE_RE = re.compile(r"^[sS]?(\d{1,6})$")


# ---------------------------------------------------------------- shades and stats

def shade_id(ref: Any) -> str | None:
    """"s1", "1", 1, "S1" -> "s1"; anything else -> None."""
    ref = as_int(ref)
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return f"s{ref}" if ref >= 1 else None
    if isinstance(ref, str):
        m = _SHADE_RE.match(ref.strip())
        if m and int(m.group(1)) >= 1:
            return f"s{int(m.group(1))}"
    return None


def shade_of(state: GameState, seat: str, sid: str | None) -> dict[str, Any] | None:
    player = state.players.get(seat)
    if player is None or sid is None:
        return None
    return next((sh for sh in player.shades if sh["id"] == sid), None)


def uid(seat: str, sid: str) -> str:
    return f"{seat}/{sid}"


def is_active(shade: Mapping[str, Any]) -> bool:
    """On a mission that has not ended."""
    return shade.get("status") == MISSION and bool(shade.get("mission")) and not shade["mission"].get("ended")


def _int(rs: Ruleset, path: str, key: StatKey) -> int:
    return int(rs.stat(path, key))


def whisper(rs: Ruleset, key: StatKey) -> int:
    return _int(rs, "spy.whisper", key)


def mirrors(rs: Ruleset, key: StatKey) -> int:
    return _int(rs, "spy.mirrors", key)


def _amounts(rs: Ruleset, prefix: str, key: StatKey) -> dict[str, int]:
    out = {r: _int(rs, f"{prefix}.{r}", key) for r in rs.resources}
    return {r: n for r, n in out.items() if n > 0}


def shade_cost(rs: Ruleset, key: StatKey) -> dict[str, int]:
    return _amounts(rs, "spy.shade.cost", key)


def shade_upkeep(rs: Ruleset, key: StatKey) -> dict[str, int]:
    return _amounts(rs, "spy.shade.upkeep", key)


def difficulty(rs: Ruleset, mission: str, key: StatKey) -> int:
    return _int(rs, f"spy.mission.{mission}.difficulty", key)


def max_rounds(rs: Ruleset, mission: str, key: StatKey) -> int:
    return _int(rs, f"spy.mission.{mission}.max_rounds", key) + _int(rs, "spy.rounds_bonus", key)


def active_limit(rs: Ruleset, key: StatKey) -> int:
    """Shades on missions at once: spy.active_base + S // 2 (CONCEPT: 1 + S/2)."""
    return _int(rs, "spy.active_base", key) + whisper(rs, key) // 2


def mission_cost(rs: Ruleset, mission: str | None, rounds: int | None) -> dict[str, int]:
    """The price of a mission for the whole term: `cost` once plus `cost_per_round` x rounds."""
    m = rs.missions.get(mission or "")
    if m is None or not isinstance(rounds, int) or rounds < 1:
        return {}
    total: Counter[str] = Counter(m.cost)
    for res, n in m.cost_per_round.items():
        total[res] += n * rounds
    return {r: n for r, n in sorted(total.items()) if n > 0}


def rounds_left(rs: Ruleset, turn: int, phase_added: str) -> int:
    """The longest term a mission may have: the turns left, this one included for a mission given in actions (its
    first round is in this turn's recon), not for one given in negotiation_2 (first round next turn). Under a hidden
    end (core 0.8.2) the turns up to the latest possible one: the game may end before them (`rounds_note`)."""
    return rs.scenario.turn_limit - turn + (1 if phase_added == ACTIONS else 0)


def rounds_note(rs: Ruleset) -> str:
    """What the term limit counts (review F4): the turns left, or under a hidden end the turns up to the latest
    possible one, rounds after the real end lost with their price (paid for the whole term up front)."""
    sc = rs.scenario
    if not sc.end_hidden:
        return "не больше оставшихся ходов"
    return (f"не больше ходов до самого позднего конца — хода {sc.turn_limit}; партия может кончиться раньше, "
            "раунды после конца пропадут вместе с их ценой")


def resolve_mission_ref(rs: Ruleset, ref: Any) -> str | None:
    """Full mission id for a full or short id or a name (ru or en), any case."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    full = rs.resolve("mission", ref) or rs.resolve("mission", ref.lower())
    if full:
        return full
    label = ref.lower().rstrip(".")
    hits = [k for k, m in rs.missions.items() if label in (m.name.lower(), m.name_en.lower())]
    return hits[0] if len(hits) == 1 else None


def mission_list(rs: Ruleset) -> str:
    return ", ".join(f"{k.split(':')[-1]} ({m.name})" for k, m in sorted(rs.missions.items()))


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def chances(rs: Ruleset, key: StatKey, *, S: int, C: int, D: int, V: int, K: int, R: int = 0) -> tuple[float, float]:
    """(success, detection) of one round (CONCEPT 5; the spy's stat key gives the coefficients)."""
    st = lambda name: rs.stat(name, key)       # noqa: E731
    p_ok = _clamp(st("spy.success.base") + st("spy.success.skill") * (S - D) + st("spy.success.xp") * V
                  + st("spy.success.cover") * K, st("spy.success.min"), st("spy.success.max"))
    p_det = _clamp(st("spy.detect.base") + st("spy.detect.difficulty") * D + st("spy.detect.skill") * (C - S)
                   + st("spy.detect.repeat") * R - st("spy.detect.xp") * V - st("spy.detect.cover") * K,
                   st("spy.detect.min"), st("spy.detect.max"))
    return round(p_ok, 6), round(p_det, 6)


def attribution_chance(rs: Ruleset, victim_key: StatKey, C: int, S: int) -> float:
    """Does the victim learn whose Shade it caught: its counter-intelligence raises it (the victim's stat key)."""
    st = lambda name: rs.stat(name, victim_key)       # noqa: E731
    return round(_clamp(st("spy.attribution.base") + st("spy.attribution.skill") * (C - S),
                        st("spy.attribution.min"), st("spy.attribution.max")), 6)


def expose_chance(rs: Ruleset, key: StatKey, S: int, C: int) -> float:
    st = lambda name: rs.stat(name, key)       # noqa: E731
    return round(_clamp(st("spy.feint_expose.base") + st("spy.feint_expose.skill") * (S - C), 0.0,
                        st("spy.feint_expose.max")), 6)


def local_mirrors(state: GameState, rs: Ruleset, place: str) -> int:
    """C_loc of a province (2.3): its owner's C + province.mirrors (no building effects in occupations and ruins) +
    spy.detect.army if the owner has units there; 0 for a neutral province."""
    prov = state.provinces[place]
    if prov.owner is None:
        # stage 5 (ABANDONED_LANDS.md 10): Observation of Abandoned land is not free; the same for every Abandoned
        # province, with a find or without (a failure must not tell which)
        if rs.system("discoveries") and place in state.abandoned:
            return int(rs.scenario.discoveries.get("observe_mirrors", 0))
        return 0
    key = stat_key(state, rs, prov.owner)
    c = mirrors(rs, key) + int(province_stat(state, rs, place, "province.mirrors", key))
    if prov.unit_total():
        c += _int(rs, "spy.detect.army", key)
    return c


def check_place(state: GameState, rs: Ruleset, mission: MissionDef, target: str) -> str | None:
    """Where a round is checked: the target province, or for a seat the rule of 1.3 (conquest.check_place)."""
    if mission.target == "seat":
        return cq.check_place(state, target)
    return target if target in state.provinces else None


def honest_range(seed: int | str, spy_seat: str, target: str, facet: str, key: str, n: int,
                 width: float, width_min: int = 2) -> list[int]:
    """An honest interval [lo, hi] with the truth inside (2.4): w = max(width_min, ceil(width*n)) (width_min is
    spy.intel.width_min, 2 before core 0.8.0); the offset is the observer's lasting error, from a stream keyed by
    (spy seat, target, facet, key) only (no turn, Shade or round)."""
    n = max(0, int(n))
    w = max(int(width_min), math.ceil(width * n - 1e-9))
    u = stream(seed, 0, "spy.range", f"{spy_seat}|{target}|{facet}|{key}").random()
    r = math.floor(u * (min(w, n) + 1))
    lo = n - r
    return [lo, lo + w]


# ---------------------------------------------------------------- orders (orders.py calls these)

def seat_targets(state: GameState, seat: str) -> str:
    """«Альфа (A), Гамма (C)»: the living rivals a seat-target mission may name, by nick or by letter."""
    return ", ".join(f"{p.nick} ({s})" for s, p in sorted(state.players.items()) if s != seat and p.alive) or "нет"


def target_problem(state: GameState, rs: Ruleset, seat: str, mission: MissionDef,
                   target: str | None) -> tuple[str, str] | None:
    if mission.target == "seat":
        player = state.players.get(target or "")
        if player is None:
            # core 0.8.1 (m10): a nick or a seat letter; a wrong one names the valid targets
            return "UNKNOWN_ID", f"нет игрока {str(target)[:40]!r}; цели: {seat_targets(state, seat)}"
        if target == seat or not player.alive:
            return "BAD_TARGET", f"цель — живой другой игрок: {seat_targets(state, seat)}"
        return None
    if target not in state.provinces:
        if cq.seat_ref(state, target) is not None:
            return "BAD_TARGET", f"цель «{mission.name}» — провинция (id вида P05), а не игрок {str(target)[:40]!r}"
        return "BAD_TARGET", f"цель {str(target)[:40]!r} — не провинция"
    owner = state.provinces[target].owner
    if owner == seat:
        return "BAD_TARGET", f"{target} — твоя провинция"
    if mission.target == "owned_province" and owner is None:
        return "BAD_TARGET", f"{target} нейтральная: «{mission.name}» — только против провинции игрока"
    return None


def own_shades_text(player: Any) -> str:
    """Core 0.8.1 (m10: «нет Тени 's1'» after its loss read like a bad target): the seat's Shades now."""
    if not player.shades:
        return "у тебя сейчас нет Теней (потерянная не возвращается; нанять — hire_shade)"
    return "твои Тени: " + ", ".join(f"{sh['id']} ({'свободна' if sh.get('status') == IDLE else 'на задании'})"
                                     for sh in player.shades) + " — list_spies"


def _struck_now(state: GameState, seat: str, shade: Mapping[str, Any]) -> bool:
    """The Shade's sabotage struck in this turn's recon and its mark still stands (a P2 recall cancels it)."""
    m = shade.get("mission") or {}
    if m.get("ended") != STRUCK:
        return False
    return any(mk.get("by") == seat and mk.get("shade") == shade.get("id")
               for prov in state.provinces.values() for mk in cq.strike_marks(prov, state.turn))


def order_problem(state: GameState, rs: Ruleset, seat: str, order: Order, others: list[Order],
                  key: StatKey) -> tuple[str, str] | None:
    """Why a hire_shade / spy / recall cannot be given (phase independent; resources are checked by orders.py)."""
    player = state.players[seat]
    if order.kind == "ward":
        return ward_problem(state, seat, order, others)
    if order.kind == "hire_shade":
        if cq.living_capital(state, seat) is None:
            return "NO_CAPITAL", "Тень нанимают только при своей полноценной стартовой столице"
        if any(o.kind == "hire_shade" for o in others):
            return "INVALID", "одна Тень за ход: найм уже в приказах"
        limit = _int(rs, "spy.shades_max", key)
        if len(player.shades) + 1 > limit:
            return "SHADE_LIMIT", f"Теней {len(player.shades)}/{limit}: больше держать нельзя"
        return None
    shade = shade_of(state, seat, order.shade)
    if shade is None:
        return "UNKNOWN_ID", f"нет Тени {order.shade!r}; {own_shades_text(player)}"
    if order.kind == "recall":
        if any(o.kind == "recall" and o.shade == order.shade for o in others):
            return "INVALID", f"отзыв {order.shade} уже в приказах"
        if shade.get("status") != MISSION:
            return "INVALID", f"{order.shade} не на задании"
        if not is_active(shade) and not _struck_now(state, seat, shade):
            return "INVALID", f"задание {order.shade} уже закончено: отзывать нечего"
        return None
    # spy
    if shade.get("status") != IDLE or any(o.kind == "spy" and o.shade == order.shade for o in others):
        return "SHADE_BUSY", f"{order.shade} занята: задание уже идёт или в приказах"
    if order.phase_added == P2 and shade.get("hired_turn") == state.turn:
        return "SHADE_BUSY", f"{order.shade} нанята в этом ходу: задания она получает со следующего Д"
    mission = rs.missions.get(order.mission or "")
    if mission is None:
        return "UNKNOWN_ID", f"неизвестная миссия {order.mission!r}; миссии: {mission_list(rs)}"
    S = whisper(rs, key)
    if S < mission.whisper:
        return "MISSION_LOCKED", f"«{mission.name}» требует Шёпот {mission.whisper}, у тебя {S}"
    missing = [t for t in mission.requires_tech if t not in player.techs]
    if missing:
        names = ", ".join(rs.techs[t].name if t in rs.techs else t for t in missing)
        return "MISSION_LOCKED", f"«{mission.name}» требует технологию: {names}"
    problem = target_problem(state, rs, seat, mission, order.target)
    if problem:
        return problem
    top = min(max_rounds(rs, order.mission, key), rounds_left(rs, state.turn, order.phase_added))
    if not isinstance(order.rounds, int) or not mission.rounds_min <= order.rounds <= top:
        return "BAD_ROUNDS", (f"срок «{mission.name}» — {mission.rounds_min}..{top} раундов ({rounds_note(rs)}), "
                              f"получено {order.rounds!r}")
    busy = {sh["id"] for sh in player.shades if is_active(sh)}
    recalled = {o.shade for o in others if o.kind == "recall"}
    new = {o.shade for o in others if o.kind == "spy" and o.shade not in busy}
    limit = active_limit(rs, key)
    if len(busy - recalled) + len(new) + 1 > limit:
        return "TOO_MANY_MISSIONS", f"на заданиях одновременно не больше {limit} Теней (1 + Шёпот/2)"
    return None


def ward_problem(state: GameState, seat: str, order: Order, others: list[Order]) -> tuple[str, str] | None:
    """Core 0.8.0 (docs/SPY_V2.md 4.6): a Ward stands in the seat's own full province, one per province."""
    pid = order.province or ""
    prov = state.provinces.get(pid)
    if prov is None:
        return "UNKNOWN_ID", f"неизвестная провинция {pid!r}"
    if prov.owner != seat:
        return "NOT_OWNER", f"{pid} — не твоя провинция: заставу ставят только в своей"
    if cq.occupied(prov) is not None:
        return "OCCUPIED", f"{pid} оккупирована: заставу ставят только в полноценной провинции"
    if cq.is_ruined(prov, state.turn):
        return "RUINED", f"{pid} — пепелище: заставу ставят только в полноценной провинции"
    if any(o.kind == "ward" and o.province == pid for o in others):
        return "INVALID", f"застава в {pid} уже в приказах (одна на провинцию)"
    return None


def ward_cost(rs: Ruleset, key: StatKey) -> dict[str, int]:
    return _amounts(rs, "spy.ward.cost", key)


def ward_bonus(rs: Ruleset, key: StatKey) -> int:
    return _int(rs, "spy.ward.bonus", key)


def order_cost(rs: Ruleset, key: StatKey, order: Order) -> dict[str, int]:
    if order.kind == "ward":
        return ward_cost(rs, key)
    if order.kind == "hire_shade":
        return shade_cost(rs, key)
    if order.kind == "spy":
        return mission_cost(rs, order.mission, order.rounds)
    return {}


# ---------------------------------------------------------------- reports

def _entry(order: Order) -> dict[str, Any]:
    """A book entry as read_orders shows it: no id, no phase, no feint mark, no Shade."""
    out = order.to_dict()
    for k in ("id", "phase_added", "feint", "shade"):
        out.pop(k, None)
    if order.kind == "discover":
        out.pop("choice", None)      # the variant would name the find (ABANDONED_LANDS.md 10)
    return out


def _book_entries(book: SeatOrders) -> list[tuple[dict[str, Any], bool, str]]:
    """(entry, is_feint, order id) in the canonical order (kind, then fields): the order of the book reveals nothing;
    the id never reaches the reader (it only links a feint's exposure to its `feint` event in the journal)."""
    items = [(_entry(o), o.kind == "move" and o.feint, o.id) for o in book.orders if o.kind not in SKIPPED_IN_READ]
    return sorted(items, key=lambda it: (it[0]["kind"], canonical_json(it[0])))


def planned_here(book: SeatOrders | None, pid: str) -> dict[str, Any]:
    """Observation's block «в расчёте этого хода» (core 0.8.0, docs/SPY_V2.md 4.1): the recruits in this province and
    whether it is fortified, from its owner's actions book. Never moves (they stay for Read Orders, decision 22)."""
    recruit: Counter[str] = Counter()
    fortify = False
    for o in (book.orders if book is not None else ()):
        if o.kind == "recruit" and o.province == pid and o.unit and isinstance(o.count, int) and o.count > 0:
            recruit[o.unit] += o.count
        elif o.kind == "fortify" and o.province == pid:
            fortify = True
    return {"recruit": dict(sorted(recruit.items())), "fortify": fortify}


def _observe(snap: GameState, rs: Ruleset, seed: int | str, spy: str, pid: str, width: float,
             mission: MissionDef | None = None, books: Mapping[str, SeatOrders] | None = None,
             width_min: int = 2) -> tuple[dict[str, Any], dict[str, Any]]:
    prov = snap.provinces[pid]
    params = mission.params if mission is not None else {}
    truth = {"province": pid, "owner": prov.owner, "units": {u: n for u, n in sorted(prov.units.items()) if n},
             "buildings": sorted(prov.buildings),
             "building_now": [[b.building, b.turns_left] for b in snap.pending_builds if b.province == pid],
             "fortified": prov.fortified, "status": cq.statuses_for(snap).get(pid, {})}
    if rs.system("discoveries") and (snap.abandoned or snap.discoveries):
        # a find is revealed, never triggered (ABANDONED_LANDS.md 10): exact, or None («находок нет»)
        entry = snap.discoveries.get(pid)
        d = rs.discoveries.get(entry["id"]) if entry else None
        truth["discovery"] = {"id": d.id, "name": d.name, "kind": d.kind} if d is not None else None
    if params.get("planned"):
        truth["planned"] = planned_here((books or {}).get(prov.owner or ""), pid)
    if params.get("exact"):
        return copy.deepcopy(truth), truth
    facts = {**truth, "units": {u: honest_range(seed, spy, pid, "units", u, n, width, width_min)
                                for u, n in truth["units"].items()}}
    return facts, truth


def _ledger(snap: GameState, rs: Ruleset, seed: int | str, spy: str, target: str, book: SeatOrders,
            width: float, mission: MissionDef | None = None,
            width_min: int = 2) -> tuple[dict[str, Any], dict[str, Any]]:
    from .orders import reserved_resources
    player = snap.players[target]
    key = stat_key(snap, rs, target)
    held = reserved_resources(rs, key, book, snap)
    stock = {r: max(0, player.resources.get(r, 0) - held.get(r, 0)) for r in rs.resources}
    research = next((o.tech for o in book.orders if o.kind == "research"), None) or player.researching
    truth = {"seat": target, "stock": stock, "techs": list(player.techs), "researching": research,
             "progress": player.research.get(research, 0) if research else None, "artifacts": list(player.artifacts),
             "whisper": whisper(rs, key), "mirrors": mirrors(rs, key), "shades": len(player.shades)}
    if not rs.default_scoring:
        # the target's exact turn score by source, without its secret objective (SCORING.md 5, decision 24)
        from .scoring import score_parts
        truth["score_parts"] = score_parts(snap, rs)[target]
    if mission is not None and mission.params.get("army_total"):
        # core 0.8.0 (docs/SPY_V2.md 4.2): the target's units in all its provinces at the close of actions
        truth["army_total"] = sum(p.unit_total() for p in snap.owned(target))
    facts = {**truth, "stock": {r: honest_range(seed, spy, target, "stock", r, n, width, width_min)
                                for r, n in stock.items()}}
    if research:
        facts["progress"] = honest_range(seed, spy, target, "research", research, truth["progress"], width,
                                         width_min)
    if "army_total" in truth:
        facts["army_total"] = honest_range(seed, spy, target, "army", "total", truth["army_total"], width, width_min)
    return facts, truth


def _objective(snap: GameState, rs: Ruleset, target: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """read_objective (core 0.6.0, decision 26): the target's secret objectives as at the close of actions — id,
    the rival it names (if any), points and progress. Exact (an objective is a fact, not a count to blur): the one
    payoff of a Shade talk cannot give. The objective never changes, so one report is enough; progress is a snapshot."""
    from .scoring import objective_status
    goals = [{k: o[k] for k in ("id", "target", "points", "met", "have", "need")}
             for o in objective_status(snap, rs, target)]
    truth = {"seat": target, "objectives": goals}
    return truth, copy.deepcopy(truth)


def _read_orders(snap: GameState, rs: Ruleset, seed: int | str, turn: int, spy: str, target: str, book: SeatOrders,
                 shade_uid: str, S: int, C: int, key: StatKey) -> tuple[dict[str, Any], dict[str, Any], list[dict]]:
    entries = _book_entries(book)
    p = expose_chance(rs, key, S, C)
    facts_entries, truth_entries, feints = [], [], []
    for i, (entry, feint, oid) in enumerate(entries):
        shown = dict(entry)
        truth_entries.append({**entry, "feint": True} if feint else dict(entry))
        if feint:
            u = stream(seed, turn, "spy.feint", f"{shade_uid}|{i}").random()
            exposed = u < p
            if exposed:
                shown["exposed"] = True
            feints.append({"index": i, "order_id": oid, "from": entry["from"], "to": entry["to"], "p": p, "u": round(u, 6),
                           "exposed": exposed})
        facts_entries.append(shown)
    tkey = stat_key(snap, rs, target)
    mine = {p.id for p in snap.owned(spy)}
    moving: dict[str, Counter[str]] = {}
    for o in book.orders:
        if o.kind == "move" and o.src:
            moving.setdefault(o.src, Counter()).update(o.units or {})
    free: dict[str, dict[str, int]] = {}
    for pid in sorted({n for m in mine for n in rs.map.adjacency[m]}):
        prov = snap.provinces[pid]
        if prov.owner != target:
            continue
        left = {u: n - moving.get(pid, Counter())[u] for u, n in sorted(prov.units.items())}
        left = {u: n for u, n in left.items() if n > 0}
        if left:
            free[pid] = left
    common = {"seat": target, "command_points": snap.players[target].command_points,
              "new_attack_cp": _int(rs, "p2.cost.new_attack", tkey), "free_at_borders": free}
    return ({**common, "entries": facts_entries}, {**common, "entries": truth_entries}, feints)


# ---------------------------------------------------------------- the recon step

def recon(state: GameState, books_d: Mapping[str, SeatOrders | Mapping[str, Any]], ruleset: Ruleset,
          seed: int | str) -> tuple[GameState, dict[str, Any]]:
    """The recon step at the close of actions (SPY_AND_CONQUEST.md 1.5, 2.3). Pure: returns (state', report).

    `state` is the state at the close of actions (turn start plus negotiation_1 ops), `books_d` the books then. The
    new state has the Shades hired, missions started and played, caught Shades removed, the recon-consumed entries
    paid for and sabotage marks set; its phase is negotiation_2. With espionage off nothing changes but the phase.
    """
    from .orders import _rules_problem, order_cost as _order_cost, recon_consumed
    rs = ruleset
    snap = copy.deepcopy(state)
    snap.phase = ACTIONS             # the snapshot of the close of actions: reports read it, never the new state
    s = copy.deepcopy(snap)
    turn = s.turn
    rep: dict[str, Any] = {"turn": turn, "rounds": [], "reports": {}, "captures": {}, "lost": {}, "strikes": [],
                           "hired": [], "started": [], "ended": [], "rejected": {}, "feints": []}
    if rs.system(ESPIONAGE):
        alive = [seat for seat, p in sorted(s.players.items()) if p.alive]
        books = {seat: _as_book(books_d.get(seat)) for seat in alive}
        keys = {seat: stat_key(snap, rs, seat) for seat in alive}
        valid: dict[str, list[Order]] = {seat: [] for seat in alive}
        for seat in alive:
            for o in books[seat].orders:
                if o.kind == "move" and o.feint:
                    rep["feints"].append({"seat": seat, "order_id": o.id, "from": o.src, "to": o.dst})
                if not recon_consumed(o):
                    continue
                problem = _rules_problem(snap, rs, seat, books[seat], o, exclude=o.id)
                if problem:
                    rep["rejected"].setdefault(seat, []).append({"order": o.to_dict(), "reason": problem[0],
                                                                 "message": problem[1]})
                else:
                    valid[seat].append(o)

        def pay(seat: str, o: Order) -> dict[str, int]:
            cost = dict(_order_cost(rs, keys[seat], o, snap))
            res = s.players[seat].resources
            for r, n in cost.items():
                res[r] = res.get(r, 0) - n
            return cost

        wards: dict[str, int] = {}          # core 0.8.0 (docs/SPY_V2.md 4.6): province -> C_loc bonus this recon
        for kind in ("hire_shade", "recall", "spy", "ward"):
            for seat in alive:
                for o in (o for o in valid[seat] if o.kind == kind):
                    player = s.players[seat]
                    if kind == "ward":
                        cost = pay(seat, o)
                        bonus = ward_bonus(rs, keys[seat])
                        wards[str(o.province)] = bonus
                        rep.setdefault("wards", []).append({"seat": seat, "province": o.province, "bonus": bonus,
                                                            "cost": cost})
                    elif kind == "hire_shade":
                        cost = pay(seat, o)
                        player.spy_seq["shade"] = player.spy_seq.get("shade", 0) + 1
                        sid = f"s{player.spy_seq['shade']}"
                        player.shades.append({"id": sid, "xp": 0, "status": IDLE, "hired_turn": turn,
                                              "mission": None})
                        rep["hired"].append({"seat": seat, "shade": sid, "cost": cost})
                    elif kind == "recall":
                        shade = shade_of(s, seat, o.shade)
                        if shade is not None and is_active(shade):
                            _end(rep, seat, shade, RECALLED)
                    else:
                        shade = shade_of(s, seat, o.shade)
                        cost = pay(seat, o)
                        start_mission(shade, o, turn, ACTIONS)
                        rep["started"].append({"seat": seat, "shade": o.shade, "mission": o.mission,
                                               "target": o.target, "rounds": o.rounds, "phase": ACTIONS,
                                               "first_turn": turn, "cost": cost})
        active = sorted(((seat, sh) for seat in alive for sh in s.players[seat].shades
                         if is_active(sh) and sh["mission"]["first_turn"] <= turn),
                        key=lambda it: uid(it[0], it[1]["id"]))
        places = _round_places(snap, rs, active)
        for seat, shade in active:
            _round(s, snap, rs, seed, rep, books, seat, shade, places, wards)
        # core 0.7.0 (decision 27): points for successful missions against living rivals (scoring source `intel`);
        # core 0.8.0: by the mission's `intel` kind, none against a pact partner (docs/SPY_V2.md 5.2)
        from .scoring import credit_intel
        denied: list[dict[str, Any]] = []
        points = credit_intel(s, rs, rep["rounds"], rs.missions, denied)
        if points:
            rep["intel_points"] = points
        if denied:
            rep["intel_denied"] = denied
    s.phase = P2
    rep["hash"] = sha256_json({k: v for k, v in rep.items() if k != "hash"})
    return s, rep


def _as_book(value: Any) -> SeatOrders:
    if value is None:
        return SeatOrders()
    if isinstance(value, SeatOrders):
        return value
    return SeatOrders.from_dict(value)


def start_mission(shade: dict[str, Any], order: Order, turn: int, phase: str) -> None:
    shade["status"] = MISSION
    shade["mission"] = {"type": order.mission, "target": order.target, "rounds": order.rounds, "played": 0,
                        "successes": 0, "first_turn": turn if phase == ACTIONS else turn + 1, "phase": phase,
                        "report_ids": []}


def _end(rep: dict[str, Any] | None, seat: str, shade: dict[str, Any], reason: str) -> dict[str, Any]:
    m = shade["mission"]
    m["ended"] = reason
    entry = {"seat": seat, "shade": shade["id"], "mission": m["type"], "target": m["target"], "reason": reason,
             "reports": list(m.get("report_ids") or [])}
    if rep is not None:
        rep["ended"].append(entry)
    return entry


def _void(state: GameState, rs: Ruleset, seat: str, mission: MissionDef, target: str) -> bool:
    """The target is gone (2.3): the seat is out, the province became the spy's own or (owned_province) neutral."""
    if mission.target == "seat":
        player = state.players.get(target)
        return player is None or not player.alive
    prov = state.provinces.get(target)
    if prov is None or prov.owner == seat:
        return True
    return mission.target == "owned_province" and prov.owner is None


def void_cause(snap: GameState, rs: Ruleset, seat: str, mission: MissionDef | None, target: str) -> str:
    """Core 0.8.1 (review F3): why a mission is void, on the recon snapshot that voided it, stored in its round
    record (`why`) so that a later text does not rebuild it from a state the resolution has moved on: `rules` (no
    such mission), `out` (the target seat is out), `no_land` (the seat has no land to check in), `own` / `neutral`
    (the province became the spy's own / neutral), `gone` (no such province)."""
    if mission is None:
        return "rules"
    if mission.target == "seat":
        player = snap.players.get(target)
        return "out" if player is None or not player.alive else "no_land"
    prov = snap.provinces.get(target)
    if prov is None:
        return "gone"
    return "own" if prov.owner == seat else "neutral"


def _round_places(snap: GameState, rs: Ruleset, active: Iterable[tuple[str, Mapping[str, Any]]]
                  ) -> dict[str, tuple[str, str]] | None:
    """Core 0.8.0 (docs/SPY_V2.md 3.2, spy.detect.repeat_on): the check place of every Shade playing a round in this
    recon step, {uid: (seat, place)}; a void mission plays no round and is left out. None while R is off."""
    if not int(rs.stat("spy.detect.repeat_on")):
        return None
    out: dict[str, tuple[str, str]] = {}
    for seat, shade in active:
        m = shade["mission"]
        mission = rs.missions.get(m["type"])
        place = None if mission is None else check_place(snap, rs, mission, m["target"])
        if mission is not None and place is not None and not _void(snap, rs, seat, mission, m["target"]):
            out[uid(seat, shade["id"])] = (seat, place)
    return out


def repeat_count(places: Mapping[str, tuple[str, str]] | None, seat: str, place: str, me: str) -> int:
    """R of a round: the seat's other Shades checked at the same place in this recon step (another seat's never
    count); 0 while R is off."""
    if places is None:
        return 0
    return sum(1 for u, (st, pl) in places.items() if u != me and st == seat and pl == place)


def _round(s: GameState, snap: GameState, rs: Ruleset, seed: int | str, rep: dict[str, Any],
           books: Mapping[str, SeatOrders], seat: str, shade: dict[str, Any],
           places: Mapping[str, tuple[str, str]] | None = None, wards: Mapping[str, int] | None = None) -> None:
    m = shade["mission"]
    turn = s.turn
    mission = rs.missions.get(m["type"])
    shade_uid = uid(seat, shade["id"])
    base = {"seat": seat, "shade": shade["id"], "mission": m["type"], "target": m["target"],
            "round": m["played"] + 1, "rounds": m["rounds"]}
    place = None if mission is None else check_place(snap, rs, mission, m["target"])
    if mission is None or place is None or _void(snap, rs, seat, mission, m["target"]):
        _end(rep, seat, shade, VOID)
        rep["rounds"].append({**base, "outcome": VOIDED, "why": void_cause(snap, rs, seat, mission, m["target"])})
        return
    key = stat_key(snap, rs, seat)
    victim = snap.provinces[place].owner
    ward = (wards or {}).get(place, 0)
    S, C, D = whisper(rs, key), local_mirrors(snap, rs, place) + ward, difficulty(rs, m["type"], key)
    V, K = int(shade.get("xp", 0)), min(m["played"], _int(rs, "spy.cover_max", key))
    R = repeat_count(places, seat, place, shade_uid)
    p_ok, p_det = chances(rs, key, S=S, C=C, D=D, V=V, K=K, R=R)
    u_det = stream(seed, turn, "spy.detect", shade_uid).random()
    rec = {**base, "place": place, "victim": victim, "K": K, "R": R, "S": S, "C_loc": C, "V": V, "D": D,
           "p_det": p_det, "u_det": round(u_det, 6), "detected": u_det < p_det}
    if ward:
        rec["ward"] = ward
    if rec["detected"]:
        victim_key = stat_key(snap, rs, victim) if victim is not None else None
        p_a = attribution_chance(rs, victim_key, C, S)
        u_a = stream(seed, turn, "spy.attrib", shade_uid).random()
        attributed = victim is not None and u_a < p_a
        s.players[seat].shades = [sh for sh in s.players[seat].shades if sh["id"] != shade["id"]]
        ended = _end(rep, seat, shade, CAUGHT)
        rep["lost"].setdefault(seat, []).append(shade["id"])
        capture = {"victim": victim, "owner": seat, "shade": shade["id"], "mission": m["type"], "target": m["target"],
                   "province": place, "attributed": attributed, "p_attrib": p_a, "u_attrib": round(u_a, 6),
                   "reports": ended["reports"]}
        rep.setdefault("caught", []).append(capture)
        if victim is not None and victim != seat:
            rep["captures"].setdefault(victim, []).append(
                {"province": place, "mission": m["type"], "target": m["target"],
                 "owner": seat if attributed else None})
        rep["rounds"].append({**rec, "outcome": LOST})
        return
    u_ok = stream(seed, turn, "spy.success", shade_uid).random()
    success = u_ok < p_ok
    rec.update(p_ok=p_ok, u_ok=round(u_ok, 6), success=success)
    m["played"] += 1
    outcome = NOTHING
    if success:
        m["successes"] += 1
        if mission.loud:
            pid = m["target"] if m["target"] in s.provinces else place
            effects = {k: mission.params[k] for k in ("deny_fortify", "defense_mul", "income_mul")
                       if k in mission.params}
            marks = list(cq.strike_marks(s.provinces[pid], turn))     # strikes of several Shades all stand
            mark = {"turn": turn, "by": seat, "shade": shade["id"], **effects}
            # core 0.8.0 (docs/SPY_V2.md 4.5): the stop of a build and the revolt of an occupation, only when the
            # pack gives them (older marks unchanged)
            mark.update({k: copy.deepcopy(mission.params[k]) for k in STRIKE_EXTRA if k in mission.params})
            if mission.intel == "effect":
                # core 0.8.0 (docs/SPY_V2.md 5.2): what the end of the resolution needs to credit the strike's
                # effect: the mission's uid (the first strike scores), the victim and a pact on the recon snapshot
                from .diplomacy import pact_between
                victim_now = snap.provinces[pid].owner
                mark.update(uid=shade_uid, mission=m["type"], victim=victim_now,
                            pact=pact_between(snap, seat, victim_now, turn) is not None)
            marks.append(mark)
            s.provinces[pid].status["sabotaged"] = marks
            rep["strikes"].append({"seat": seat, "shade": shade["id"], "province": pid,
                                   "victim": snap.provinces[pid].owner, "effects": effects})
            _end(rep, seat, shade, STRUCK)
            outcome = HIT
        else:
            report = _report(s, snap, rs, seed, books, seat, shade, mission, key, S, C)
            m["report_ids"].append(report["id"])
            rep["reports"].setdefault(seat, []).append(report)
            rec["report_id"] = report["id"]
            outcome = FOUND
    if not m.get("ended") and m["played"] >= m["rounds"]:
        _end(rep, seat, shade, DONE if m["successes"] else EMPTY)
    rep["rounds"].append({**rec, "outcome": outcome})


def _report(s: GameState, snap: GameState, rs: Ruleset, seed: int | str, books: Mapping[str, SeatOrders], seat: str,
            shade: dict[str, Any], mission: MissionDef, key: StatKey, S: int, C: int) -> dict[str, Any]:
    m = shade["mission"]
    player = s.players[seat]
    player.spy_seq["report"] = player.spy_seq.get("report", 0) + 1
    rid = f"r{player.spy_seq['report']}"
    width = rs.stat("spy.intel.width", key)
    width_min = _int(rs, "spy.intel.width_min", key)
    target = m["target"]
    extra: dict[str, Any] = {}
    if mission.behavior == OBSERVE:
        facts, truth = _observe(snap, rs, seed, seat, target, width, mission, books, width_min)
        entry = s.discoveries.get(target) if facts.get("discovery") else None
        if entry is not None and seat not in entry.get("known", []):
            entry["known"] = sorted({*entry.get("known", []), seat})     # the find is known to the observer now
    elif mission.behavior == LEDGER:
        facts, truth = _ledger(snap, rs, seed, seat, target, books.get(target, SeatOrders()), width, mission,
                               width_min)
    elif mission.behavior == READ_OBJECTIVE:
        facts, truth = _objective(snap, rs, target)
    else:       # read_orders
        facts, truth, feints = _read_orders(snap, rs, seed, s.turn, seat, target, books.get(target, SeatOrders()),
                                            uid(seat, shade["id"]), S, C, key)
        extra = {"feints": feints}
    return {"id": rid, "seat": seat, "shade": shade["id"], "mission": m["type"], "behavior": mission.behavior,
            "target": target, "round": m["played"], "rounds": m["rounds"], "turn": s.turn, "facts": facts,
            "truth": truth, **extra}


def recon_summary(rep: Mapping[str, Any]) -> dict[str, Any]:
    """The numbers of the journal's `recon` event."""
    rounds = [r for r in rep.get("rounds", []) if r.get("outcome") != VOIDED]
    return {"missions": len(rounds), "successes": sum(1 for r in rounds if r.get("success")),
            "captures": sum(1 for r in rounds if r.get("detected"))}


def informativeness(report: Mapping[str, Any]) -> float:
    """1 - width / value averaged over the report's intervals (1.0 without intervals): for the journal (8)."""
    vals: list[float] = []

    def walk(f: Any, t: Any) -> None:
        if isinstance(f, list) and len(f) == 2 and all(isinstance(x, int) for x in f) and isinstance(t, int):
            vals.append(max(0.0, 1 - (f[1] - f[0]) / t) if t else 0.0)
        elif isinstance(f, Mapping) and isinstance(t, Mapping):
            for k in f:
                walk(f[k], t.get(k))

    walk(report.get("facts"), report.get("truth"))
    return round(sum(vals) / len(vals), 4) if vals else 1.0


# ---------------------------------------------------------------- the resolution (SPY_AND_CONQUEST.md 4)

class EspionageStep:
    """Espionage in a resolution: step 1 (missions and recalls given in negotiation_2), 1b (Shades of ended missions
    go idle), 2b (sabotage strikes), the end (eliminations, the marks are cleared)."""

    def __init__(self, r: _Resolution):
        self.r = r
        self.s = r.s
        self.rs = r.rs
        self.turn = r.turn
        self.recalls: list[tuple[str, str]] = []
        self.defense: dict[str, float] = {}          # pid -> defense multiplier of this resolution's strike

    # -- step 1
    def order(self, seat: str, o: Order) -> None:
        if o.kind == "spy":
            shade = shade_of(self.s, seat, o.shade)
            if shade is None:
                return
            start_mission(shade, o, self.turn, P2)
            self.r.ev("mission_started", [seat], None, seat=seat, shade=o.shade, mission=o.mission, target=o.target,
                      rounds=o.rounds, phase=P2, first_turn=self.turn + 1)
        elif o.kind == "recall" and o.shade:
            self.recalls.append((seat, o.shade))

    # -- step 1b
    def idle(self) -> None:
        for seat, sid in self.recalls:
            shade = shade_of(self.s, seat, sid)
            if shade is None or shade.get("status") != MISSION:
                continue
            if is_active(shade):
                self.r.ev("mission_end", [seat], None, **_end(None, seat, shade, RECALLED))
            for pid, prov in sorted(self.s.provinces.items()):
                marks = cq.strike_marks(prov, self.turn)
                mine = [m for m in marks if m.get("by") == seat and m.get("shade") == sid]
                if not mine:
                    continue
                rest = [m for m in marks if m not in mine]           # only this Shade's strike is cancelled
                if rest:
                    prov.status["sabotaged"] = rest
                else:
                    del prov.status["sabotaged"]
                self.r.ev("strike_cancelled", [seat], pid, seat=seat, shade=sid, victim=prov.owner)
        for seat, player in sorted(self.s.players.items()):
            xp_max = _int(self.rs, "spy.xp_max", self.r.factions.get(seat))
            for shade in player.shades:
                m = shade.get("mission")
                if shade.get("status") == MISSION and m and m.get("ended"):
                    if m["ended"] != VOID and m.get("successes"):
                        shade["xp"] = min(xp_max, int(shade.get("xp", 0)) + 1)
                    shade["status"], shade["mission"] = IDLE, None

    # -- step 2b
    def strikes(self, fortified: set[str]) -> None:
        for pid, prov in sorted(self.s.provinces.items()):
            # several strikes on one province: deny fortify if any, the lowest defense and income multipliers;
            # one event per author
            for mark in cq.strike_marks(prov, self.turn):
                effects = {k: mark[k] for k in ("deny_fortify", "defense_mul", "income_mul") if k in mark}
                if effects.get("deny_fortify"):
                    fortified.discard(pid)
                self.defense[pid] = min(self.defense.get(pid, 1.0), float(effects.get("defense_mul", 1.0)))
                self.r.ev("sabotage", [prov.owner, mark.get("by")], pid, seat=mark.get("by"), victim=prov.owner,
                          shade=mark.get("shade"), effects=effects)

    def defense_mul(self, pid: str) -> float:
        return self.defense.get(pid, 1.0)

    # -- after step 6 (core 0.8.0, docs/SPY_V2.md 5.2 p. 4): the points of strikes that did something
    def effects(self) -> None:
        """A loud `effect` mission scores only when its strike did something in this resolution: a battle in the
        province where the victim defended and lost a unit or the province (any attacker), a build stopped
        (`build_sabotaged`) or a revolt of the occupation (`occupation_revolt` by sabotage). One effect, one point: in
        a province the first successful strike by mission uid that may score takes it; the spy always learns what its
        strike did (`sabotage_effect`, private)."""
        from . import scoring as sc_
        events = self.r.events
        for pid, prov in sorted(self.s.provinces.items()):
            marks = sorted((mk for mk in cq.strike_marks(prov, self.turn) if mk.get("uid")),
                           key=lambda mk: str(mk["uid"]))
            if not marks:
                continue
            victim = marks[0].get("victim")
            found = strike_effects(events, pid, victim)
            taken = False
            seen: set[tuple[str, Any, str]] = set()
            for mk in marks:
                seat, mission = mk.get("by"), str(mk.get("mission"))
                out: dict[str, Any] = {"seat": seat, "shade": mk.get("shade"), "victim": victim, "effects": found}
                for e in events:
                    if e["kind"] == "build_sabotaged" and e.get("province") == pid:
                        out["building"] = e.get("building")
                        break
                why = None
                point = None
                if not found:
                    why = sc_.DENY_NO_EFFECT
                elif taken:
                    why = sc_.DENY_TAKEN
                elif (seat, victim, mission) in seen:
                    why = sc_.DENY_TAKEN
                else:
                    point, why = sc_.credit_intel_effect(self.s, self.rs, seat, victim, mission, bool(mk.get("pact")))
                if point is not None:
                    taken = True
                    seen.add((seat, victim, mission))
                    point["effect"] = found[0]
                    point["effects"] = list(found)
                    # `kind` would clash with the event's kind: the report event says `intel_kind` (the journal
                    # entry `kind`, as at the recon step)
                    self.r.ev("intel_point", [seat], pid, **{k: v for k, v in point.items() if k != "kind"},
                              intel_kind=point["kind"])
                if sc_.on(self.rs, "intel"):
                    out["intel_point"] = point is not None
                    if why is not None:
                        out["reason"] = why
                self.r.ev("sabotage_effect", [seat], pid, **out)

    # -- the end
    def end(self, eliminated: Iterable[str]) -> None:
        gone = set(eliminated)
        for seat in sorted(gone):
            self.s.players[seat].shades = []
        for seat, player in sorted(self.s.players.items()):
            for shade in player.shades:
                m = shade.get("mission")
                if not is_active(shade):
                    continue
                mission = self.rs.missions.get(m["type"])
                if mission is not None and mission.target == "seat" and m["target"] in gone:
                    self.r.ev("mission_end", [seat], None, **_end(None, seat, shade, VOID))
        for prov in self.s.provinces.values():
            prov.status.pop("sabotaged", None)


STRIKE_EFFECTS = ("battle", "stall", "revolt")
# sabotage parameters a strike mark carries besides its effects on this resolution (docs/SPY_V2.md 4.5, 13)
STRIKE_EXTRA = ("stall_build", "stall_max", "unrest_units", "revolt_garrison")


def stall_mark(prov: Province, turn: int) -> Mapping[str, Any] | None:
    """The first (by mission uid) of this turn's strike marks on the province that stops its builds, or None."""
    marks = [mk for mk in cq.strike_marks(prov, turn) if mk.get("stall_build")]
    return min(marks, key=lambda mk: str(mk.get("uid") or "")) if marks else None


def unrest_in(rs: Ruleset) -> bool:
    """A mission of the ruleset can make an occupation revolt: the public form of a revolt drops its reason (7)."""
    return any("unrest_units" in m.params for m in rs.missions.values()) if rs.system(ESPIONAGE) else False


def strike_effects(events: Iterable[Mapping[str, Any]], pid: str, victim: str | None) -> list[str]:
    """What a strike on `pid` did in this resolution (docs/SPY_V2.md 5.2 p. 4), in STRIKE_EFFECTS order: `battle` —
    the victim defended there and lost a unit or the province (an attack that costs the victim nothing does not
    count); `stall` — a build there stopped by the sabotage; `revolt` — the occupation there revolted by it."""
    out = set()
    for e in events:
        if e.get("province") != pid:
            continue
        kind = e["kind"]
        if kind == "battle" and e.get("stage") == "defense":
            for side in e.get("sides") or ():
                if side.get("owner") == victim and victim is not None and (
                        sum((side.get("losses") or {}).values()) > 0 or not side.get("won")):
                    out.add("battle")
        elif kind == "captured" and victim is not None and e.get("from") == victim:
            out.add("battle")
        elif kind == "build_sabotaged":
            out.add("stall")
        elif kind == "occupation_revolt" and e.get("reason") == "sabotage":
            out.add("revolt")
    return [k for k in STRIKE_EFFECTS if k in out]


def upkeep(state: GameState, rs: Ruleset, seat: str, key: StatKey) -> dict[str, int]:
    """Upkeep of the seat's Shades (spy.shade.upkeep x Shades)."""
    player = state.players.get(seat)
    if player is None or not player.shades:
        return {}
    return {r: n * len(player.shades) for r, n in shade_upkeep(rs, key).items()}


def own_view(state: GameState, rs: Ruleset, seat: str) -> dict[str, Any]:
    """`you.espionage` of the JSON state (programs, bots): the seat's own S, C, limits and Shades, nothing of others."""
    player = state.players[seat]
    key = stat_key(state, rs, seat)
    shades = []
    for sh in player.shades:
        m = sh.get("mission")
        entry: dict[str, Any] = {"id": sh["id"], "xp": sh.get("xp", 0), "status": sh.get("status"),
                                 "hired_turn": sh.get("hired_turn")}
        if m:
            entry["mission"] = {k: m.get(k) for k in ("type", "target", "rounds", "played", "successes",
                                                      "first_turn", "ended")}
        shades.append(entry)
    return {"whisper": whisper(rs, key), "mirrors": mirrors(rs, key), "shades_max": _int(rs, "spy.shades_max", key),
            "active_limit": active_limit(rs, key), "shades": shades}


def intel_json(rep: Mapping[str, Any] | None, seat: str) -> dict[str, Any]:
    """get_intel(format=json): what the seat's own texts show, as data: its reports' `facts` (never `truth`), its
    Shades' round outcomes, Shades caught at the seat (the owner only when attributed) and its own lost Shades."""
    if rep is None:
        return {"turn": None, "reports": [], "rounds": [], "caught": [], "lost": []}
    reports = [{k: r[k] for k in ("id", "shade", "mission", "behavior", "target", "round", "rounds", "turn", "facts")}
               for r in (rep.get("reports") or {}).get(seat, [])]
    rounds = [{"shade": r["shade"], "mission": r["mission"], "target": r["target"], "outcome": r["outcome"],
               **({"report_id": r["report_id"]} if r.get("report_id") else {})} for r in own_rounds(rep, seat)]
    return {"turn": rep.get("turn"), "reports": reports, "rounds": rounds,
            "caught": [dict(c) for c in (rep.get("captures") or {}).get(seat, [])],
            "lost": list((rep.get("lost") or {}).get(seat, []))}


def sabotage_seen(event: Mapping[str, Any], seat: str) -> dict[str, Any]:
    """A sabotage event as a seat sees it: its victim does not learn the author (no attribution for strikes)."""
    if event.get("seat") == seat:
        return dict(event)
    out = {k: v for k, v in event.items() if k not in ("seat", "shade")}
    if "seats" in out:
        out["seats"] = [x for x in out["seats"] if x == seat]   # the author's seat would name it
    return out


# ---------------------------------------------------------------- estimate

def report_defender(report: Mapping[str, Any], turn: int, rid: str | None = None,
                    persists: bool = False) -> dict[str, Any]:
    """estimate(defender="r3") (docs/SPY_V2.md 6.1) from an Observation report (the caller checks the kind): the
    units (an older report's interval: its upper end) plus the recruits of its block «в расчёте этого хода», the
    province, whether it fortifies, and the header of the answer. The server and the simulator's bots share it.
    `persists` (combat.fortify_persists): the fortification the report shows («укр.») is kept while the owner holds
    units there, so it counts as well as a fortify of the block."""
    facts = report["facts"]
    units: dict[str, int] = {}
    ranged = False
    for u, n in (facts.get("units") or {}).items():
        if isinstance(n, list):            # an older Observation's interval: its upper end
            n, ranged = n[-1], True
        units[u] = units.get(u, 0) + int(n)
    planned = facts.get("planned") or {}
    for u, n in (planned.get("recruit") or {}).items():
        units[u] = units.get(u, 0) + int(n)
    age = turn - int(report["turn"])
    kept = bool(persists and facts.get("fortified") and sum(units.values()) > 0)
    fortified = bool(planned.get("fortify")) or kept
    head = (f"по донесению {rid or report.get('id')}: состав конца Д хода {report['turn']} без подходов и уходов; "
            + ("укрепление держится (укр. в донесении)" + ("" if age <= 0 else ", если войска не уходили")
               if kept and not planned.get("fortify") else "в П2 возможны укрепление (1 ОК)")
            + " и подход наспех")
    if planned.get("recruit"):
        head += "; найм этого хода учтён"
    if ranged:
        head += "; числа — верх интервалов"
    if age > 0:
        head += f"; устарело на {age} ход{'' if age == 1 else 'а' if age < 5 else 'ов'}"
    return {"units": units, "province": facts["province"], "fortified": fortified, "header": head}


def own_repeat(state: GameState, rs: Ruleset, seat: str, place: str | None, book: SeatOrders | None,
               me: str | None) -> int:
    """R as the engine will count it (docs/SPY_V2.md 3.2), from what the seat knows: its other Shades whose mission
    (active, or a `spy` entry of this turn's book) is checked at `place`; Shades recalled in the book and the Shade
    `me` itself do not count. 0 while R is off."""
    if place is None or not int(rs.stat("spy.detect.repeat_on")):
        return 0
    player = state.players[seat]
    orders = list(book.orders) if book is not None else []
    recalled = {o.shade for o in orders if o.kind == "recall"}
    found: set[str] = set()
    for sh in player.shades:
        if is_active(sh):
            m = sh["mission"]
            mission = rs.missions.get(m["type"])
            if mission is not None and check_place(state, rs, mission, m["target"]) == place:
                found.add(sh["id"])
    for o in orders:
        if o.kind == "spy" and o.shade:
            mission = rs.missions.get(o.mission or "")
            tgt = o.target or ""
            if mission is not None and (mission.target != "seat" or tgt in state.players) \
                    and check_place(state, rs, mission, tgt) == place:
                found.add(o.shade)
    return len(found - recalled - {me})


def estimate_mission(state: GameState, rs: Ruleset, seat: str, mission_ref: Any, target: Any, rounds: Any,
                     shade: Any = None, assume_mirrors: Any = None, book: SeatOrders | None = None) -> dict[str, Any]:
    """The exact chances of a mission (2.3; no Monte-Carlo): per round (success, detection), surviving the term and
    at least one report (a strike for a loud mission). Another seat's C is hidden: `assume_mirrors` (default: the
    ruleset's base) stands for it; the army on the spot counts if seen, and is assumed when not seen. Core 0.8.0
    (docs/SPY_V2.md 3.2, 5.3): R from the seat's own Shades at the same place (its `book` of this turn included) and
    what a success would score, with the reason (`intel_points`, `intel_reason`)."""
    mid = resolve_mission_ref(rs, mission_ref)
    if mid is None:
        raise ValueError(f"неизвестная миссия {mission_ref!r}; миссии: {mission_list(rs)}")
    mission = rs.missions[mid]
    key = stat_key(state, rs, seat)
    tgt = target if isinstance(target, str) else ""
    if mission.target == "seat":
        tgt = cq.seat_ref(state, tgt) or tgt
    problem = target_problem(state, rs, seat, mission, tgt)
    if problem:
        raise ValueError(problem[1])
    notes = []
    phase = P2 if state.phase == P2 else ACTIONS
    top = min(max_rounds(rs, mid, key), rounds_left(rs, state.turn, phase))
    n = as_int(rounds) if rounds is not None else mission.rounds_min
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"rounds — целое число, получено {rounds!r}")
    if n > top or n < mission.rounds_min:
        n = max(mission.rounds_min, min(n, top))
        notes.append(f"срок обрезан до {n} (границы {mission.rounds_min}..{top})")
    sh = shade_of(state, seat, shade_id(shade)) if shade is not None else None
    V = int(sh.get("xp", 0)) if sh else 0
    place = check_place(state, rs, mission, tgt)
    from .resolve import visible_provinces
    seen = visible_provinces(state, rs, seat)
    if assume_mirrors is None:
        C = mirrors(rs, None)
        notes.append(f"чужие Зеркала скрыты: принято C{C}")
    else:
        C = as_int(assume_mirrors)
        if isinstance(C, bool) or not isinstance(C, int):
            raise ValueError(f"assume_mirrors — целое число, получено {assume_mirrors!r}")
    if place is not None and state.provinces[place].owner is not None:
        prov = state.provinces[place]
        if prov.buildings and place in seen:
            C += int(province_stat(state, rs, place, "province.mirrors", None))
        # core 0.7.0 army fog (fog.army_presence): whether another seat's army stands there is known beyond sight too
        presence = bool(rs.stat("fog.army_presence")) and prov.owner != seat
        army = prov.unit_total() > 0 if place in seen or presence else True
        if place not in seen and not presence:
            notes.append("армия на месте не видна: считается, что есть")
        if army:
            C += _int(rs, "spy.detect.army", None)
    else:
        C = 0
    S, D = whisper(rs, key), difficulty(rs, mid, key)
    R = own_repeat(state, rs, seat, place, book, sh["id"] if sh else None)
    cover_max = _int(rs, "spy.cover_max", key)
    per, alive_clean, survive_caught, any_hit = [], 1.0, 0.0, 0.0
    for i in range(n):
        p_ok, p_det = chances(rs, key, S=S, C=C, D=D, V=V, K=min(i, cover_max), R=R)
        per.append((p_ok, p_det))
        any_hit += alive_clean * (1 - p_det) * p_ok
        survive_caught += alive_clean * p_det
        alive_clean *= (1 - p_det) * (1 - p_ok)
    if mission.loud:
        survive = 1 - survive_caught
    else:
        survive = math.prod(1 - d for _, d in per)
    rounds_txt = ", ".join(f"{round(o * 100)}/{round(d * 100)}" for o, d in per)
    what = "удар" if mission.loud else "донесение"
    rr = f", R{R}" if R else ""
    text = (f"{mission.name} → {place or tgt}, S{S} против C_лок {C}, D{D}, V{V}{rr}: раунды (успех/поимка %): "
            f"{rounds_txt}; дожить {round(survive * 100)}%, ≥1 {what} {round(any_hit * 100)}%")
    if mission.behavior == OBSERVE and mission.params.get("planned") and place in seen:
        # G3: the province is in sight already
        text += f"; {place} в твоём обзоре: войска видны и так, донесение добавит только найм и укрепление этого хода"
    points = intel_payoff(state, rs, seat, mission, tgt, place, book, sh["id"] if sh else None)
    if points is not None:
        if points[0]:
            text += f"; успех: +{points[0]} очк. разведки (осталось {points[1]})" + (
                f", {points[2]}" if points[2] else "")
        elif points[2].startswith("очко"):
            text += f"; {points[2]}"
        else:
            text += f"; очков разведки не даст ({points[2]})"
    if notes:
        text += " (" + "; ".join(notes) + ")"
    out = {"mission": mid, "target": tgt, "place": place, "rounds": n, "S": S, "C_loc": C, "D": D, "V": V, "R": R,
           "per_round": [{"success": o, "detect": d} for o, d in per], "survive": round(survive, 4),
           "any": round(any_hit, 4), "text": text[:ESTIMATE_CHARS]}
    if points is not None:
        out["intel_points"] = points[0]
        if points[2]:
            out["intel_reason"] = points[2]
    return out


def intel_payoff(state: GameState, rs: Ruleset, seat: str, mission: MissionDef, target: str,
                 place: str | None, book: SeatOrders | None = None,
                 me: str | None = None) -> tuple[int, int, str] | None:
    """Core 0.7.0 (decision 27): what a success of this mission adds to the scoring source `intel`: (points, points
    left, reason or condition); None while the source is off. Only a living rival seat (the target seat, or the owner
    of the target province) counts (scoring.credit_intel). Core 0.8.0 (docs/SPY_V2.md 5.3, G3): by the mission's
    `intel` kind; a loud `effect` mission promises its point only on condition, and none while the seat's other
    scoring `spy` entries of this `book` (Shade `me` aside) fill what is left of the cap, or while its own strike with
    a smaller uid hits the same province first."""
    from . import scoring as sc_
    if not sc_.on(rs, "intel"):
        return None
    key = stat_key(state, rs, seat)
    left = sc_.intel_left(state, rs, seat, key)
    rival = target if mission.target == "seat" else (state.provinces[place].owner if place in state.provinces
                                                       else None)
    player = state.players.get(rival) if rival is not None else None
    if player is None or rival == seat or not player.alive:
        return 0, left, "цель — не соперник"
    kind = sc_.intel_kind(mission)
    if kind == sc_.NO_INTEL:
        return 0, left, "не даёт очков (сведения)"
    why = sc_.intel_block(state, rs, seat, rival, mission.id, kind, key)
    short = mission.id.split(":")[-1]
    texts = {sc_.DENY_PACT: "партнёр по пакту: очков не даст", sc_.DENY_ONCE: f"очко за {short} уже получено",
             sc_.DENY_TARGET: f"очко за {short} против этого места уже получено"}
    if why is not None:
        return 0, left, texts.get(why, sc_.DENY_TEXT.get(why, why))
    per = min(left, int(rs.stat("score.intel.per_success", key)))
    if kind != sc_.EFFECT:
        # core 0.8.1 (decision 32): a deep mission scores against a pact partner too; say so, the 0.8.0 rule did not
        from .diplomacy import pact_between
        partner = sc_.pact_deep(rs, key) and pact_between(state, seat, rival, state.turn) is not None
        return per, left, "партнёр по пакту: очко засчитают, пакт шпионаж не нарушает" if partner else ""
    others = [o for o in (book.orders if book is not None else ()) if o.kind == "spy" and o.shade != me]
    scoring = 0
    for o in others:
        m = rs.missions.get(o.mission or "")
        k = sc_.intel_kind(m)
        if m is None or k in (sc_.NO_INTEL, sc_.EFFECT):
            continue
        tgt = o.target if m.target == "seat" else (state.provinces[o.target].owner
                                                   if o.target in state.provinces else None)
        if isinstance(tgt, str) and tgt in state.players and tgt != seat \
                and sc_.intel_block(state, rs, seat, tgt, m.id, k, key) is None:
            scoring += 1
    if scoring >= left:
        return 0, left, "очко, если останется место в пределе (другие задания этого хода могут занять его раньше)"
    mine = uid(seat, me) if me else None
    if any(o.target == target and rs.missions.get(o.mission or "") is not None
           and rs.missions[o.mission or ""].loud and (mine is None or uid(seat, o.shade or "") < mine)
           for o in others):
        return 0, left, "очко — первому удару по этой провинции (эффект засчитают ему)"
    return per, left, "только если удар что-то сделает: бой с потерями жертвы, остановка стройки, бунт"


# ---------------------------------------------------------------- texts (Russian, compact)

def _nick(state: GameState, seat: str | None, me: str | None = None) -> str:
    if seat is None:
        return "нейтр."
    if seat == me:
        return "ты"
    player = state.players.get(seat)
    return player.nick if player is not None else seat


def _mission_name(rs: Ruleset, mid: str | None) -> str:
    m = rs.missions.get(mid or "")
    return m.name if m is not None else str(mid)


def target_label(rs: Ruleset, state: GameState, target: str | None, me: str | None = None) -> str:
    if target in state.players:
        return _nick(state, target, me)
    return str(target)


def _span(value: Any) -> str:
    if isinstance(value, list) and len(value) == 2:
        return f"{value[0]}–{value[1]}"
    return str(value)


def _units(rs: Ruleset, units: Mapping[str, Any]) -> str:
    return " ".join(f"{rs.short(u)}×{_span(n)}" for u, n in sorted(units.items())) or "пусто"


def _res(rs: Ruleset, amounts: Mapping[str, Any]) -> str:
    return ", ".join(f"{rs.resources[r].name} {_span(n)}" for r, n in amounts.items() if r in rs.resources)


def _entry_text(rs: Ruleset, state: GameState, e: Mapping[str, Any], me: str | None) -> str:
    kind = e["kind"]
    if kind == "recruit":
        return f"найм {e['province']} {rs.short(e['unit'])}×{e['count']}"
    if kind == "build":
        return f"стр. {e['province']} {rs.short(e['building'])}"
    if kind == "fortify":
        return f"укреп. {e['province']}"
    if kind == "spy":
        return f"шпионаж {_mission_name(rs, e.get('mission'))} → {target_label(rs, state, e.get('target'), me)}"
    if kind == "dispose":
        d = rs.dispositions.get(e.get("disposition") or "")
        to = f" → {_nick(state, e['to'], me)}" if e.get("to") else ""
        return f"судьба {e['province']} → {d.name.lower() if d else e.get('disposition')}{to}"
    if kind == "policy":
        d = rs.dispositions.get(e.get("disposition") or "")
        return f"политика {d.name.lower() if d else 'нет'}"
    if kind == "discover":
        return f"запись о находке в {e.get('province')}"
    if kind == "ward":            # core 0.8.0 (docs/SPY_V2.md 4.6): seen in the book after the fact
        return f"застава {e.get('province')}"
    return kind


def _orders_body(rs: Ruleset, state: GameState, facts: Mapping[str, Any], me: str, full: bool) -> list[str]:
    """read_orders (core 0.8.0, docs/SPY_V2.md 4.4, G4): first the pact breaks (moves with pact "break": «удар по
    союзнику X без выхода (−3): …», «удар по тебе …» when the reader is the partner), then «НА ТЕБЯ:» the other
    moves into the reader's provinces (or «ударов по тебе нет»), then the rest: moves grouped by source (exposed
    false entries and moves at the reader's borders first), the other entries, the target's command points."""
    from . import scoring as sc_
    mine = {p.id for p in state.owned(me)} if me in state.players else set()
    near = mine | {n for m in mine for n in rs.map.adjacency.get(m, ())}
    fine = int(rs.stat("score.betrayal.per_break")) if sc_.betrayal_on(rs) else 0
    breaks: list[str] = []
    at_me: list[str] = []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    others: list[str] = []
    for e in facts.get("entries", []):
        if e["kind"] != "move":
            others.append(_entry_text(rs, state, e, me))
            continue
        mark = " [ЛОЖНАЯ ЗАПИСЬ]" if e.get("exposed") else ""
        if e.get("pact") == "break":
            owner = state.provinces[e["to"]].owner if e["to"] in state.provinces else None
            who = "по тебе" if owner == me else f"по союзнику {_nick(state, owner, me)}"
            breaks.append(f"удар {who} без выхода{f' (−{fine})' if fine else ''}: {e['from']}→{e['to']} "
                          f"{_units(rs, e['units'])}{mark}")
        elif e["to"] in mine:
            at_me.append(f"{e['from']}→{e['to']} {_units(rs, e['units'])}{mark}")
        else:
            groups.setdefault(e["from"], []).append(e)
    lines: list[tuple[int, str]] = []
    for src, moves in sorted(groups.items()):
        parts = []
        for i, mv in enumerate(moves):
            head = f"{src}→{mv['to']}" if i == 0 else f"→{mv['to']}"
            mark = " [ЛОЖНАЯ ЗАПИСЬ]" if mv.get("exposed") else ""
            parts.append(f"{head} {_units(rs, mv['units'])}{mark}")
        rank = 0 if any(mv.get("exposed") for mv in moves) else (1 if any(mv["to"] in near for mv in moves) else 2)
        lines.append((rank, ", ".join(parts)))
    lines.sort(key=lambda it: it[0])
    out = []
    if breaks:
        out.append("НАРУШЕНИЕ ПАКТА: " + "; ".join(breaks))
    if mine:
        out.append("НА ТЕБЯ: " + "; ".join(at_me) if at_me else "ударов по тебе нет")
    items = [t for _, t in lines] + others
    shown = items if full else items[:COMPACT_ENTRIES]
    if shown or not (breaks or at_me):
        rest = "; ".join(shown) if shown else "записей нет"
        if len(items) > len(shown):
            rest += f"; +{len(items) - len(shown)} ещё"
        out.append(rest)
    free = facts.get("free_at_borders") or {}
    tail = f"ОК хода {facts.get('command_points')}, новая атака {facts.get('new_attack_cp')} ОК"
    if free:
        tail += ". Свободны у твоих границ: " + "; ".join(f"{pid} {_units(rs, u)}" for pid, u in sorted(free.items()))
    out.append(tail)
    return out


def planned_text(rs: Ruleset, planned: Mapping[str, Any]) -> str:
    """«Найм здесь: штурм.×2; Укрепление: да», or «найма и укрепления нет»."""
    parts = []
    if planned.get("recruit"):
        parts.append("Найм здесь: " + " ".join(f"{rs.short(u)}×{n}" for u, n in planned["recruit"].items()))
    if planned.get("fortify"):
        parts.append("Укрепление: да")
    return "; ".join(parts) or "найма и укрепления нет"


def _status_text(state: GameState, status: Mapping[str, Any], me: str | None) -> str:
    """An Observation's province status in the words of the province line: оккуп. у X, пепелище до хN, разорена,
    доход ×0.5 (an unknown key as is)."""
    parts = []
    for key in sorted(status):
        value = status[key]
        if key == "occupied":
            frm = (value or {}).get("from")
            parts.append(f"оккуп. (взята у {_nick(state, frm, me)})" if frm else "оккуп.")
        elif key == "ruined_until":
            parts.append(f"пепелище до х{value}")
        elif key == "ravaged_until":
            parts.append(f"разорена до х{value}")
        elif key == "income_mul":
            parts.append(f"доход ×{float(value):g}")
        else:
            parts.append(str(key))
    return ", ".join(parts)


def report_text(report: Mapping[str, Any], rs: Ruleset, state: GameState, me: str, full: bool = False) -> str:
    """One report (2.4): compact (<= 400 chars) or full (<= 1200, get_intel(report_id))."""
    facts = report["facts"]
    behavior = report.get("behavior")
    head = (f"#{report['id']} {report['shade']} → {target_label(rs, state, report['target'], me)} · "
            f"{_mission_name(rs, report['mission'])} · раунд {report['round']}/{report['rounds']} · снимок: конец Д "
            f"хода {report['turn']}")
    if behavior == READ_ORDERS:
        head += " (в П2 могли измениться)"
    elif behavior == OBSERVE and "planned" in facts:
        # core 0.8.0 (docs/SPY_V2.md 4.1): what the snapshot cannot show (CONCEPT 4: P2 fortify and hasty moves)
        head += " без подходов и уходов; в П2 возможны укрепление (1 ОК) и подход наспех"
    lines = [head]
    if behavior == OBSERVE:
        body = f"{facts['province']} [{_nick(state, facts.get('owner'), me)}] {_units(rs, facts.get('units') or {})}"
        if facts.get("fortified"):
            body += " укр."
        if facts.get("buildings"):
            body += "; здания: " + ", ".join(rs.short(b) for b in facts["buildings"])
        if facts.get("building_now"):
            body += "; стройка: " + ", ".join(f"{rs.short(b)} ({n})" for b, n in facts["building_now"])
        status = facts.get("status") or {}
        if status:
            body += "; статус: " + _status_text(state, status, me)
        if "discovery" in facts:
            found = facts["discovery"]
            body += f"; находка «{found['name']}»" if found else "; находок нет"
        if "planned" in facts:
            body += "; в расчёте этого хода: " + planned_text(rs, facts["planned"])
        lines.append(body)
    elif behavior == LEDGER:
        body = f"запасы: {_res(rs, facts.get('stock') or {})}"
        if "army_total" in facts:
            body += f"; армия всего: {_span(facts['army_total'])}"
        techs = [rs.techs[t].name if t in rs.techs else t for t in facts.get("techs") or []]
        body += f"; тех. {len(techs)}" + (f": {', '.join(techs)}" if techs and full else "")
        research = facts.get("researching")
        if research:
            name = rs.techs[research].name if research in rs.techs else research
            body += f"; исследуется «{name}» {_span(facts.get('progress'))}"
        if facts.get("artifacts"):
            body += "; артефакты: " + ", ".join(rs.artifacts[a].name if a in rs.artifacts else a
                                               for a in facts["artifacts"])
        body += f"; S{facts.get('whisper')} C{facts.get('mirrors')}; Теней {facts.get('shades')}"
        if facts.get("score_parts") is not None:
            from .scoring import LABELS
            parts = facts["score_parts"]
            body += (f"; счёт {sum(parts.values())} [" + " ".join(f"{LABELS[k]} {v}" for k, v in parts.items())
                     + "] без цели")
        lines.append(body)
    elif behavior == READ_OBJECTIVE:
        lines.append(_objective_body(rs, state, facts, me))
    else:
        lines += _orders_body(rs, state, facts, me, full)
    text = "\n".join(lines)
    limit = FULL_CHARS if full else COMPACT_CHARS
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _objective_body(rs: Ruleset, state: GameState, facts: Mapping[str, Any], me: str) -> str:
    """«цель «Хозяин кузниц» +5: в конце держать ≥2 провинц. типа «Кузня»; сейчас: нет (1/2)»."""
    from .scoring import objective_text
    items = []
    for o in facts.get("objectives") or []:
        obj = rs.objectives.get(o["id"])
        cond = objective_text(rs, state, o)
        if o.get("target") == me and me in state.players:       # objective_text names the rival by nick
            cond = cond.replace(f"столицу {state.players[me].nick}", "твою столицу")
        need = o.get("need") or 1
        now = "да" if o.get("met") else ("нет" if need <= 1 else f"нет ({o.get('have')}/{need})")
        pts = o.get("points", 0) * rs.weight("objectives")
        items.append(f"цель «{obj.name if obj else o['id']}» +{pts}: {cond}; сейчас: {now}")
    return "; ".join(items) or "тайной цели нет"


def capture_text(rs: Ruleset, state: GameState, c: Mapping[str, Any], me: str) -> str:
    owner = _nick(state, c.get("owner"), me) if c.get("owner") else "не установлен"
    return (f"В {c['province']} поймана Тень (хозяин: {owner}), задание: "
            f"{_mission_name(rs, c.get('mission'))} {target_label(rs, state, c.get('target'), me)}")


def miss_text(r: Mapping[str, Any]) -> str:
    """Core 0.8.1 (decision 32, m10: read_orders failed 6 of 6 at 45% and the agent saw only «ничего»): why a played
    round brought nothing — the success roll failed, with its chance."""
    p_ok = r.get("p_ok")
    return "успех не выпал" + (f" (шанс {round(float(p_ok) * 100)}%)" if isinstance(p_ok, (int, float)) else "")


VOID_TEXT = {"rules": "задания нет в правилах, снято", "out": "цель выбыла, задание снято",
             "no_land": "у цели нет земли для проверки, задание снято",
             "own": "провинция стала твоей, задание снято", "neutral": "провинция стала ничьей, задание снято",
             "gone": "цели нет, задание снято"}


def void_text(rs: Ruleset, state: GameState, seat: str, mission_id: Any, target: Any, why: Any = None) -> str:
    """Why a mission was dropped without a round (2.3), as its owner sees it. `why`: the cause stored in the round
    record on the snapshot that voided it (core 0.8.1, void_cause); a record without it (an earlier report) is read
    from `state`, as before."""
    if isinstance(why, str) and why in VOID_TEXT:
        return VOID_TEXT[why]
    mission = rs.missions.get(mission_id) if isinstance(mission_id, str) else None
    if mission is None:
        return "задания нет в правилах, снято"
    if mission.target == "seat":
        return "цель выбыла, задание снято"
    prov = state.provinces.get(target) if isinstance(target, str) else None
    if prov is not None and prov.owner == seat:
        return "провинция стала твоей, задание снято"
    if prov is not None and prov.owner is None:
        return "провинция стала ничьей, задание снято"
    return "цели нет, задание снято"


def ended_empty(rep: Mapping[str, Any] | None, seat: str, shade: Any) -> bool:
    """Core 0.8.1 (review F1): the seat's Shade ended its mission in this recon step with nothing found at all
    (EMPTY); a mission that found something in an earlier round ends DONE, even when its last round was empty."""
    return any(e.get("seat") == seat and e.get("shade") == shade and e.get("reason") == EMPTY
               for e in (rep or {}).get("ended") or [])


def _outcome_text(rs: Ruleset, state: GameState, r: Mapping[str, Any], me: str, compact: bool = False,
                  rep: Mapping[str, Any] | None = None) -> str:
    if r["outcome"] == LOST:
        return f"{r['shade']}: нет связи"
    if r["outcome"] == NOTHING and compact:
        # ДОНЕСЕНИЯ of get_state (its +400 budget): the success chance only (review F4: «шанс успеха», not a bare
        # «шанс» next to «ничего»); get_intel and list_spies say the rest
        p_ok = r.get("p_ok")
        what = "ничего" + (f" (шанс успеха {round(float(p_ok) * 100)}%)" if isinstance(p_ok, (int, float)) else "")
    elif r["outcome"] == NOTHING:
        # core 0.8.1 (decision 32): the reason and the chance; only a mission that ended with nothing found at all
        # says it came back empty (review F1: not the last round of a mission that found something earlier)
        empty = ended_empty(rep, me, r.get("shade")) if rep is not None else False
        what = ("вернулась ни с чем: " if empty else "ничего: ") + miss_text(r)
    elif r["outcome"] == VOIDED:
        what = void_text(rs, state, me, r.get("mission"), r.get("target"), r.get("why"))
    else:
        what = {FOUND: f"найдено #{r.get('report_id')}",
                HIT: f"окно найдено: удар по {r['target']} в расчёте этого хода"}[r["outcome"]]
    name = _mission_name(rs, r["mission"]).split()[0]
    return f"{r['shade']} {name} → {target_label(rs, state, r['target'], me)} {r['round']}/{r['rounds']}: {what}"


def own_rounds(rep: Mapping[str, Any] | None, seat: str) -> list[dict[str, Any]]:
    return [r for r in (rep or {}).get("rounds", []) if r["seat"] == seat]


def state_line(rep: Mapping[str, Any] | None, rs: Ruleset, state: GameState, seat: str) -> str | None:
    """ДОНЕСЕНИЯ in get_state during negotiation_2 (2.7): a Shade per item (at most 3), captures at the seat."""
    if rep is None:
        return None
    rounds = own_rounds(rep, seat)
    items = [_outcome_text(rs, state, r, seat, compact=True) for r in rounds[:STATE_SHADES]]
    if len(rounds) > STATE_SHADES:
        items.append(f"+{len(rounds) - STATE_SHADES} (list_spies)")
    for c in (rep.get("captures") or {}).get(seat, []):
        items.append(f"ПОЙМАНА У ТЕБЯ: {c['province']} ({_mission_name(rs, c['mission'])} "
                     f"{target_label(rs, state, c['target'], seat)})")
    gained = intel_gained(rep, seat)
    if gained:
        items.append(f"разведка +{gained} очк.")
    return "ДОНЕСЕНИЯ: " + " · ".join(items) if items else None


def intel_gained(rep: Mapping[str, Any] | None, seat: str) -> int:
    """Core 0.7.0: the intel points the seat got in this recon step."""
    return sum(int(p["points"]) for p in (rep or {}).get("intel_points") or [] if p["seat"] == seat)


def idle_shades(state: GameState, seat: str) -> list[str]:
    """Ids of the seat's own idle Shades."""
    player = state.players.get(seat)
    return [sh["id"] for sh in (player.shades if player else []) if sh.get("status") == IDLE]


def idle_reminder(n: int) -> str:
    """Core 0.7.0 (decision 27): the one-line reminder of phase() and the turn-start wake (phase info `idle_shades`)."""
    return (f"У тебя {n} свободн{'ая Тень' if n == 1 else 'ые Тени' if n < 5 else 'ых Теней'} — задание spy в Д "
            "(list_spies, estimate_mission)")


def idle_line(state: GameState, seat: str) -> str | None:
    """get_state in П1 and Д (core 0.6.0, m7: agents left their Shades unused): the seat's idle Shades, if any."""
    idle = idle_shades(state, seat)
    if not idle:
        return None
    return f"ТЕНИ: свободн{'а' if len(idle) == 1 else 'ы'} {', '.join(idle)} — задание spy в Д; миссии: list_spies"


def notice_text(rep: Mapping[str, Any], seat: str) -> str | None:
    """The one inbox entry of the recon step for a seat it touched (2.7); None if it touched nothing."""
    rounds = own_rounds(rep, seat)
    caught_here = len((rep.get("captures") or {}).get(seat, []))
    rejected = len((rep.get("rejected") or {}).get(seat, []))
    if not rounds and not caught_here and not rejected:
        return None
    found = sum(1 for r in rounds if r["outcome"] == FOUND)
    lost = sum(1 for r in rounds if r["outcome"] == LOST)
    strikes = [r["target"] for r in rounds if r["outcome"] == HIT]
    played = len([r for r in rounds if r["outcome"] != VOIDED])
    parts = [f"донесений {played} (найдено {found})"]
    if strikes:
        parts.append("удар: " + ", ".join(strikes))
    missed = [r for r in rounds if r["outcome"] == NOTHING]
    if missed:      # core 0.8.1 (decision 32): an empty round is a failed success roll, with its chance
        parts.append("пусто: " + ", ".join(f"{r['shade']} — {miss_text(r)}" for r in missed))
    if lost:
        parts.append(f"без связи {lost}")
    if caught_here:
        parts.append(f"поймано у тебя: {caught_here}")
    if rejected:
        parts.append(f"отклонено приказов шпионажа: {rejected}")
    gained = intel_gained(rep, seat)
    if gained:
        parts.append(f"разведка +{gained} очк.")
    return f"Разведсрез хода {rep['turn']}: {'; '.join(parts)} — get_intel / list_spies"


def intel_text(rep: Mapping[str, Any] | None, rs: Ruleset, state: GameState, seat: str, shade: str | None = None,
               report_id: str | None = None) -> str:
    """get_intel: the seat's reports of one recon step, compact (<= 1500 chars), or one report in full."""
    if rep is None:
        return "донесений нет: разведсрез этого хода ещё не прошёл"
    reports = [r for r in (rep.get("reports") or {}).get(seat, []) if shade is None or r["shade"] == shade]
    if report_id is not None:
        hit = next((r for r in reports if r["id"] == report_id), None)
        if hit is None:
            return f"донесения {report_id} нет в ходе {rep['turn']}"
        return report_text(hit, rs, state, seat, full=True)
    lines = [_outcome_text(rs, state, r, seat, rep=rep) for r in own_rounds(rep, seat)
             if (shade is None or r["shade"] == shade) and r["outcome"] != FOUND]
    out: list[str] = []
    for i, r in enumerate(reports):
        text = report_text(r, rs, state, seat)
        if sum(len(x) + 1 for x in out) + len(text) > INTEL_CHARS - 120:
            rest = len(reports) - i
            out.append(f"+{rest}: get_intel(report_id=\"{r['id']}\")")
            break
        out.append(text)
    body = out + lines
    text = "\n".join(body) if body else f"ход {rep['turn']}: донесений нет"
    return text if len(text) <= INTEL_CHARS else text[:INTEL_CHARS - 1] + "…"


# Why a mission ended, as list_spies shows it (the stored codes stay English)
END_TEXT = {DONE: "срок вышел", EMPTY: "ничего не найдено", STRUCK: "удар нанесён", RECALLED: "отозвана",
            VOID: "снято", CAUGHT: "поймана"}


def _price_text(rs: Ruleset, m: MissionDef) -> str:
    """` 8 Сплав+1 Провиант/р`: the once price and the price per round of a mission (6.1), '' when free."""
    once = "+".join(f"{n} {rs.resources[r].short}" for r, n in sorted(m.cost.items()) if n > 0)
    per = "+".join(f"{n} {rs.resources[r].short}" for r, n in sorted(m.cost_per_round.items()) if n > 0)
    parts = [x for x in (once, f"{per}/р" if per else "") if x]
    return " " + "+".join(parts) if parts else ""


def spies_text(state: GameState, rs: Ruleset, seat: str, rep: Mapping[str, Any] | None = None) -> str:
    """list_spies (6.1): S and C, Shades (missions, cover, experience, last report), missions, captures at the seat."""
    player = state.players[seat]
    key = stat_key(state, rs, seat)
    on = sum(1 for sh in player.shades if is_active(sh))
    cost = ", ".join(f"{n} {rs.resources[r].name}" for r, n in shade_cost(rs, key).items()) or "бесплатно"
    upkeep_txt = ", ".join(f"+{n} {rs.resources[r].name}" for r, n in shade_upkeep(rs, key).items())
    head = (f"S{whisper(rs, key)} C{mirrors(rs, key)} · Теней {len(player.shades)}/{_int(rs, 'spy.shades_max', key)}"
            f", на заданиях {on}/{active_limit(rs, key)} · найм {cost}")
    if upkeep_txt:
        head += f", {upkeep_txt}/ход"
    lines = [head]
    cover_max = _int(rs, "spy.cover_max", key)
    for sh in player.shades:
        m = sh.get("mission")
        if m and sh.get("status") == MISSION:
            last = f" — последнее: #{m['report_ids'][-1]}" if m.get("report_ids") else ""
            end = f" [{END_TEXT.get(m['ended'], m['ended'])}]" if m.get("ended") else ""
            if m.get("ended") == EMPTY:
                # core 0.8.1 (decision 32): why it came back empty, with the chance of its last round
                mine = [r for r in own_rounds(rep, seat) if r["shade"] == sh["id"] and r["outcome"] == NOTHING]
                end = f" [{END_TEXT[EMPTY]}: {miss_text(mine[-1]) if mine else 'успех не выпал'}]"
            lines.append(f"{sh['id']} {_mission_name(rs, m['type'])} → {target_label(rs, state, m['target'], seat)} "
                         f"{m['played']}/{m['rounds']} K{min(m['played'], cover_max)} V{sh.get('xp', 0)}{last}{end}")
        else:
            lines.append(f"{sh['id']} свободна V{sh.get('xp', 0)}")
    for sid in ((rep or {}).get("lost") or {}).get(seat, []):
        lines.append(f"{sid}: нет связи, потеряна")
    lines.append("миссии: " + "; ".join(
        f"{k.split(':')[-1]} D{difficulty(rs, k, key)} {m.rounds_min}–{max_rounds(rs, k, key)} S{m.whisper}"
        f"{_price_text(rs, m)}" for k, m in sorted(rs.missions.items())))
    for c in ((rep or {}).get("captures") or {}).get(seat, []):
        owner = f"Тень {_nick(state, c['owner'], seat)}" if c.get("owner") else "хозяин не установлен"
        lines.append(f"ПОЙМАНА У ТЕБЯ: {c['province']}, {owner} ({_mission_name(rs, c['mission'])} "
                     f"{target_label(rs, state, c['target'], seat)})")
    text = "\n".join(lines)
    return text if len(text) <= SPIES_CHARS else text[:SPIES_CHARS - 1] + "…"
