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
from .model import ACTIONS, P2, GameState, Order, SeatOrders, as_int, canonical_json, sha256_json
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

# Report events only their sides see (resolve._PRIVATE_KINDS); the victim of a `sabotage` sees it without its author.
PRIVATE_KINDS = frozenset({"sabotage", "strike_cancelled", "feint", "mission_started", "mission_end"})

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
    first round is in this turn's recon), not for one given in negotiation_2 (first round next turn)."""
    return rs.scenario.turn_limit - turn + (1 if phase_added == ACTIONS else 0)


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
                 width: float) -> list[int]:
    """An honest interval [lo, hi] with the truth inside (2.4): w = max(2, ceil(width*n)); the offset is the observer's
    lasting error, from a stream keyed by (spy seat, target, facet, key) only (no turn, Shade or round)."""
    n = max(0, int(n))
    w = max(2, math.ceil(width * n - 1e-9))
    u = stream(seed, 0, "spy.range", f"{spy_seat}|{target}|{facet}|{key}").random()
    r = math.floor(u * (min(w, n) + 1))
    lo = n - r
    return [lo, lo + w]


# ---------------------------------------------------------------- orders (orders.py calls these)

def target_problem(state: GameState, rs: Ruleset, seat: str, mission: MissionDef,
                   target: str | None) -> tuple[str, str] | None:
    if mission.target == "seat":
        player = state.players.get(target or "")
        if player is None:
            return "BAD_TARGET", f"цель {target!r} — не игрок (нужен ник или место)"
        if target == seat or not player.alive:
            return "BAD_TARGET", "цель — живой другой игрок"
        return None
    if target not in state.provinces:
        return "BAD_TARGET", f"цель {target!r} — не провинция"
    owner = state.provinces[target].owner
    if owner == seat:
        return "BAD_TARGET", f"{target} — твоя провинция"
    if mission.target == "owned_province" and owner is None:
        return "BAD_TARGET", f"{target} нейтральная: «{mission.name}» — только против провинции игрока"
    return None


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
        return "UNKNOWN_ID", f"нет Тени {order.shade!r}; свои Тени — list_spies"
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
        return "BAD_ROUNDS", (f"срок «{mission.name}» — {mission.rounds_min}..{top} раундов (не больше оставшихся "
                              f"ходов), получено {order.rounds!r}")
    busy = {sh["id"] for sh in player.shades if is_active(sh)}
    recalled = {o.shade for o in others if o.kind == "recall"}
    new = {o.shade for o in others if o.kind == "spy" and o.shade not in busy}
    limit = active_limit(rs, key)
    if len(busy - recalled) + len(new) + 1 > limit:
        return "TOO_MANY_MISSIONS", f"на заданиях одновременно не больше {limit} Теней (1 + Шёпот/2)"
    return None


def order_cost(rs: Ruleset, key: StatKey, order: Order) -> dict[str, int]:
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
    return out


def _book_entries(book: SeatOrders) -> list[tuple[dict[str, Any], bool, str]]:
    """(entry, is_feint, order id) in the canonical order (kind, then fields): the order of the book reveals nothing;
    the id never reaches the reader (it only links a feint's exposure to its `feint` event in the journal)."""
    items = [(_entry(o), o.kind == "move" and o.feint, o.id) for o in book.orders if o.kind not in SKIPPED_IN_READ]
    return sorted(items, key=lambda it: (it[0]["kind"], canonical_json(it[0])))


def _observe(snap: GameState, rs: Ruleset, seed: int | str, spy: str, pid: str,
             width: float) -> tuple[dict[str, Any], dict[str, Any]]:
    prov = snap.provinces[pid]
    truth = {"province": pid, "owner": prov.owner, "units": {u: n for u, n in sorted(prov.units.items()) if n},
             "buildings": sorted(prov.buildings),
             "building_now": [[b.building, b.turns_left] for b in snap.pending_builds if b.province == pid],
             "fortified": prov.fortified, "status": cq.statuses_for(snap).get(pid, {})}
    facts = {**truth, "units": {u: honest_range(seed, spy, pid, "units", u, n, width)
                                for u, n in truth["units"].items()}}
    return facts, truth


def _ledger(snap: GameState, rs: Ruleset, seed: int | str, spy: str, target: str, book: SeatOrders,
            width: float) -> tuple[dict[str, Any], dict[str, Any]]:
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
    facts = {**truth, "stock": {r: honest_range(seed, spy, target, "stock", r, n, width) for r, n in stock.items()}}
    if research:
        facts["progress"] = honest_range(seed, spy, target, "research", research, truth["progress"], width)
    return facts, truth


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

        for kind in ("hire_shade", "recall", "spy"):
            for seat in alive:
                for o in (o for o in valid[seat] if o.kind == kind):
                    player = s.players[seat]
                    if kind == "hire_shade":
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
        for seat, shade in active:
            _round(s, snap, rs, seed, rep, books, seat, shade)
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


def _round(s: GameState, snap: GameState, rs: Ruleset, seed: int | str, rep: dict[str, Any],
           books: Mapping[str, SeatOrders], seat: str, shade: dict[str, Any]) -> None:
    m = shade["mission"]
    turn = s.turn
    mission = rs.missions.get(m["type"])
    shade_uid = uid(seat, shade["id"])
    base = {"seat": seat, "shade": shade["id"], "mission": m["type"], "target": m["target"],
            "round": m["played"] + 1, "rounds": m["rounds"]}
    place = None if mission is None else check_place(snap, rs, mission, m["target"])
    if mission is None or place is None or _void(snap, rs, seat, mission, m["target"]):
        _end(rep, seat, shade, VOID)
        rep["rounds"].append({**base, "outcome": VOIDED})
        return
    key = stat_key(snap, rs, seat)
    victim = snap.provinces[place].owner
    S, C, D = whisper(rs, key), local_mirrors(snap, rs, place), difficulty(rs, m["type"], key)
    V, K, R = int(shade.get("xp", 0)), min(m["played"], _int(rs, "spy.cover_max", key)), 0
    p_ok, p_det = chances(rs, key, S=S, C=C, D=D, V=V, K=K, R=R)
    u_det = stream(seed, turn, "spy.detect", shade_uid).random()
    rec = {**base, "place": place, "victim": victim, "K": K, "R": R, "S": S, "C_loc": C, "V": V, "D": D,
           "p_det": p_det, "u_det": round(u_det, 6), "detected": u_det < p_det}
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
            marks.append({"turn": turn, "by": seat, "shade": shade["id"], **effects})
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
    target = m["target"]
    extra: dict[str, Any] = {}
    if mission.behavior == OBSERVE:
        facts, truth = _observe(snap, rs, seed, seat, target, width)
    elif mission.behavior == LEDGER:
        facts, truth = _ledger(snap, rs, seed, seat, target, books.get(target, SeatOrders()), width)
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

def estimate_mission(state: GameState, rs: Ruleset, seat: str, mission_ref: Any, target: Any, rounds: Any,
                     shade: Any = None, assume_mirrors: Any = None) -> dict[str, Any]:
    """The exact chances of a mission (2.3; no Monte-Carlo): per round (success, detection), surviving the term and
    at least one report (a strike for a loud mission). Another seat's C is hidden: `assume_mirrors` (default: the
    ruleset's base) stands for it; the army on the spot counts if seen, and is assumed when not seen."""
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
        army = prov.unit_total() > 0 if place in seen else True
        if place not in seen:
            notes.append("армия на месте не видна: считается, что есть")
        if army:
            C += _int(rs, "spy.detect.army", None)
    else:
        C = 0
    S, D = whisper(rs, key), difficulty(rs, mid, key)
    cover_max = _int(rs, "spy.cover_max", key)
    per, alive_clean, survive_caught, any_hit = [], 1.0, 0.0, 0.0
    for i in range(n):
        p_ok, p_det = chances(rs, key, S=S, C=C, D=D, V=V, K=min(i, cover_max))
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
    text = (f"{mission.name} → {place or tgt}, S{S} против C_лок {C}, D{D}, V{V}: раунды (успех/поимка %): "
            f"{rounds_txt}; дожить {round(survive * 100)}%, ≥1 {what} {round(any_hit * 100)}%")
    if notes:
        text += " (" + "; ".join(notes) + ")"
    return {"mission": mid, "target": tgt, "place": place, "rounds": n, "S": S, "C_loc": C, "D": D, "V": V,
            "per_round": [{"success": o, "detect": d} for o, d in per], "survive": round(survive, 4),
            "any": round(any_hit, 4), "text": text[:ESTIMATE_CHARS]}


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
    return kind


def _orders_body(rs: Ruleset, state: GameState, facts: Mapping[str, Any], me: str, full: bool) -> list[str]:
    """read_orders: moves grouped by source, exposed false entries and moves at the reader's borders first."""
    mine = {p.id for p in state.owned(me)} if me in state.players else set()
    near = mine | {n for m in mine for n in rs.map.adjacency.get(m, ())}
    groups: dict[str, list[Mapping[str, Any]]] = {}
    others: list[str] = []
    for e in facts.get("entries", []):
        if e["kind"] == "move":
            groups.setdefault(e["from"], []).append(e)
        else:
            others.append(_entry_text(rs, state, e, me))
    lines: list[tuple[int, str]] = []
    for src, moves in sorted(groups.items()):
        parts = []
        for i, mv in enumerate(moves):
            head = f"{src}→{mv['to']}" if i == 0 else f"→{mv['to']}"
            mark = " [ЛОЖНАЯ ЗАПИСЬ]" if mv.get("exposed") else ""
            brk = " [break]" if mv.get("pact") == "break" else ""
            parts.append(f"{head} {_units(rs, mv['units'])}{brk}{mark}")
        rank = 0 if any(mv.get("exposed") for mv in moves) else (1 if any(mv["to"] in near for mv in moves) else 2)
        lines.append((rank, ", ".join(parts)))
    lines.sort(key=lambda it: it[0])
    items = [t for _, t in lines] + others
    shown = items if full else items[:COMPACT_ENTRIES]
    out = ["; ".join(shown) if shown else "записей нет"]
    if len(items) > len(shown):
        out[0] += f"; +{len(items) - len(shown)} ещё"
    free = facts.get("free_at_borders") or {}
    tail = f"ОК хода {facts.get('command_points')}, новая атака {facts.get('new_attack_cp')} ОК"
    if free:
        tail += ". Свободны у твоих границ: " + "; ".join(f"{pid} {_units(rs, u)}" for pid, u in sorted(free.items()))
    out.append(tail)
    return out


def report_text(report: Mapping[str, Any], rs: Ruleset, state: GameState, me: str, full: bool = False) -> str:
    """One report (2.4): compact (<= 400 chars) or full (<= 1200, get_intel(report_id))."""
    facts = report["facts"]
    behavior = report.get("behavior")
    head = (f"#{report['id']} {report['shade']} → {target_label(rs, state, report['target'], me)} · "
            f"{_mission_name(rs, report['mission'])} · раунд {report['round']}/{report['rounds']} · снимок: конец Д "
            f"хода {report['turn']}")
    if behavior == READ_ORDERS:
        head += " (в П2 могли измениться)"
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
            body += "; статус: " + ", ".join(sorted(status))
        lines.append(body)
    elif behavior == LEDGER:
        body = f"запасы: {_res(rs, facts.get('stock') or {})}"
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
    else:
        lines += _orders_body(rs, state, facts, me, full)
    text = "\n".join(lines)
    limit = FULL_CHARS if full else COMPACT_CHARS
    return text if len(text) <= limit else text[:limit - 1] + "…"


def capture_text(rs: Ruleset, state: GameState, c: Mapping[str, Any], me: str) -> str:
    owner = _nick(state, c.get("owner"), me) if c.get("owner") else "не установлен"
    return (f"В {c['province']} поймана Тень (хозяин: {owner}), задание: "
            f"{_mission_name(rs, c.get('mission'))} {target_label(rs, state, c.get('target'), me)}")


def _outcome_text(rs: Ruleset, state: GameState, r: Mapping[str, Any], me: str) -> str:
    if r["outcome"] == LOST:
        return f"{r['shade']}: нет связи"
    what = {FOUND: f"найдено #{r.get('report_id')}", NOTHING: "ничего",
            HIT: f"окно найдено: удар по {r['target']} в расчёте этого хода",
            VOIDED: "цели нет, задание снято"}[r["outcome"]]
    name = _mission_name(rs, r["mission"]).split()[0]
    return f"{r['shade']} {name} → {target_label(rs, state, r['target'], me)} {r['round']}/{r['rounds']}: {what}"


def own_rounds(rep: Mapping[str, Any] | None, seat: str) -> list[dict[str, Any]]:
    return [r for r in (rep or {}).get("rounds", []) if r["seat"] == seat]


def state_line(rep: Mapping[str, Any] | None, rs: Ruleset, state: GameState, seat: str) -> str | None:
    """ДОНЕСЕНИЯ in get_state during negotiation_2 (2.7): a Shade per item (at most 3), captures at the seat."""
    if rep is None:
        return None
    rounds = own_rounds(rep, seat)
    items = [_outcome_text(rs, state, r, seat) for r in rounds[:STATE_SHADES]]
    if len(rounds) > STATE_SHADES:
        items.append(f"+{len(rounds) - STATE_SHADES} (list_spies)")
    for c in (rep.get("captures") or {}).get(seat, []):
        items.append(f"ПОЙМАНА У ТЕБЯ: {c['province']} ({_mission_name(rs, c['mission'])} "
                     f"{target_label(rs, state, c['target'], seat)})")
    return "ДОНЕСЕНИЯ: " + " · ".join(items) if items else None


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
    if lost:
        parts.append(f"без связи {lost}")
    if caught_here:
        parts.append(f"поймано у тебя: {caught_here}")
    if rejected:
        parts.append(f"отклонено приказов шпионажа: {rejected}")
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
    lines = [_outcome_text(rs, state, r, seat) for r in own_rounds(rep, seat)
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
