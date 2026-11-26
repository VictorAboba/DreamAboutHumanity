"""Abandoned lands and finds (docs/ABANDONED_LANDS.md as amended by CONCEPT decision 25).

Everything here is a no-op unless the ruleset has the `discoveries` system on (`Ruleset.system("discoveries")`): no
Abandoned province, find or status ever appears, so games without it resolve and hash exactly as before.

State (`GameState`, both left out of state_hash while empty):
    abandoned [pid]                    public, sorted: the provinces of the starts nobody took (a label for ever)
    discoveries {pid: {id, state, known, pending?}}
        state hidden | site | pending  hidden: nobody entered yet; site: a mixed find revealed to its entrants, waiting
                                       for a holder; pending {seat, turn}: the holder decides with `discover` in the
                                       next turn (else default_choice). Spent finds are removed (the layout stays in
                                       the admin journal, `discoveries_placed`).
        known [seat]                   seats that know the find (entered it, or observed the province)
Province status: guard {reward, grow?: {every, add, cap, next}} (ruins' garrison, a lair), income_mul x (forever).

Decision 25: ANY army triggers a find (no size threshold): the hindrances are what make a sweep of cheap scouts a
loss (the validator values every find against small armies too, discovery_content.py). Mixed finds are decided in
one turn by the ordinary `discover` order, default_choice otherwise; the army does not freeze. Geography is not
equalised: the placement balances only how finds spread over the Abandoned land itself (budget and parity, no
compensation in ordinary neutral provinces).

Every roll of finds (placement, mixed outcomes, artifact ties) comes from the secret stream
`stream(f"{seed}|{salt}", ...)` (the admin salt `objectives_salt`); without a salt (tests, the simulator) the seed.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from . import conquest as cq
from .combat import Force, share_losses, unit_price
from .content import DiscoveryDef, Ruleset
from .discovery_content import choice_risk, loss_count
from .model import GameState, Order, Province, SeatOrders
from .rng import stream

if TYPE_CHECKING:       # pragma: no cover
    from .resolve import _Resolution

DISCOVERIES = "discoveries"
HIDDEN, SITE, PENDING = "hidden", "site", "pending"

# Report events only their seats see (resolve._PRIVATE_KINDS); seen in the public form by those who see the province
PRIVATE_KINDS = frozenset({"discovery_chosen", "discovery_units", "discovery_pending", "order_clipped"})
FOG_KINDS = frozenset({"discovery_found"})
# journal: the full forms of the resolution's events (game.py)
JOURNAL_KINDS = frozenset({"discovery_found", "discovery_chosen", "discovery_units", "discovery_pending",
                           "order_clipped", "guard_cleared", "lair_grew"})

NO_PENDING = "NO_PENDING"
BAD_CHOICE = "BAD_CHOICE"
REASONS = (NO_PENDING, BAD_CHOICE)
DISCOVER_EXAMPLE = '{"kind":"discover","province":"P16","choice":"<вариант>"}'


def on(rs: Ruleset) -> bool:
    return rs.system(DISCOVERIES)


def active(rs: Ruleset, state: GameState) -> bool:
    """The game has Abandoned lands or finds (the rules section, the wake-up line, the view's blocks)."""
    return on(rs) and bool(state.abandoned or state.discoveries)


def secret_seed(seed: int | str, salt: int | str | None) -> int | str:
    return seed if salt is None else f"{seed}|{salt}"


def statuses_active(rs: Ruleset) -> bool:
    """Province statuses (ravage, ruins: timers, marks, ruin_ended) work with conquest or with discoveries on."""
    return rs.system("conquest") or on(rs)


# ---------------------------------------------------------------- geometry

def distances(rs: Ruleset, start: str) -> dict[str, int]:
    out = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in rs.map.adjacency[node]:
            if nxt not in out:
                out[nxt] = out[node] + 1
                queue.append(nxt)
    return out


def rings(rs: Ruleset, capitals: Mapping[str, str]) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Ring r(p) = the BFS distance to the nearest living seat's capital, and the zone Z(p): every seat at it."""
    dist = {seat: distances(rs, cap) for seat, cap in sorted(capitals.items())}
    ring: dict[str, int] = {}
    zone: dict[str, list[str]] = {}
    for pid in sorted(rs.map.provinces):
        values = {seat: d.get(pid, 10 ** 6) for seat, d in dist.items()}
        if not values:
            continue
        r = min(values.values())
        ring[pid] = r
        zone[pid] = sorted(s for s, v in values.items() if v == r)
    return ring, zone


def _by_ring(table: Mapping[str, Any], r: int) -> Any:
    """The entry of ring `r`: its own, else the one of the largest ring below it («дальше как 3»)."""
    keys = sorted((int(k) for k in table), reverse=True)
    for k in keys:
        if k <= r:
            return table[str(k)]
    return None


# ---------------------------------------------------------------- setup and placement (ABANDONED_LANDS.md 2, 5)

def setup_abandoned(rs: Ruleset, state: GameState, unused: Sequence[Any], capitals: Mapping[str, str],
                    seed: int | str, salt: int | str | None) -> None:
    """At game creation: the provinces of `unused` starts become Abandoned (statuses of «Души снижены»), the old
    capitals ruins under a guard with its reward, and the finds are laid out."""
    cfg = rs.scenario.discoveries
    abandoned = sorted(pid for start in unused for pid in (start.capital, *start.provinces))
    state.abandoned = abandoned
    ruins = {start.capital for start in unused}
    for pid in abandoned:
        prov = state.provinces[pid]
        if cfg["ruined_turns"] > 0:
            prov.status["ruined_until"] = 1 + int(cfg["ruined_turns"])
        elif cfg["ravaged_turns"] > 0:
            prov.status["ravaged_until"] = 1 + int(cfg["ravaged_turns"])
        if cfg["income_mul"] < 1.0:
            prov.status["income_mul"] = float(cfg["income_mul"])
        if pid in ruins:
            garrison = cfg["garrison"]
            prov.units = dict(garrison["units"])
            if garrison["units"] or garrison["reward"]:
                prov.status["guard"] = {"reward": [dict(e) for e in garrison["reward"]]}
    finds = place(rs, state, capitals, seed, salt)["finds"]
    state.discoveries = {pid: {"id": fid, "state": HIDDEN, "known": []} for pid, fid in sorted(finds.items())}


def _orbit_rotation(rs: Ruleset, capitals: Mapping[str, str]) -> dict[str, str] | None:
    """fair_mirror (5.6): the smallest power R^j (j >= 1) of the map's rotation that maps the occupied starts onto
    themselves while moving them, or None."""
    rot = rs.map.rotation
    if not rot:
        return None
    occupied = set(capitals.values())
    power = dict(rot)
    for _ in range(1, len(rs.map.starts)):
        if {power[c] for c in occupied} == occupied and any(power[c] != c for c in occupied):
            return power
        power = {k: rot[v] for k, v in power.items()}
    return None


def _orbit(rot: Mapping[str, str], pid: str) -> list[str]:
    out = [pid]
    nxt = rot[pid]
    while nxt != pid and len(out) <= len(rot):
        out.append(nxt)
        nxt = rot[nxt]
    return sorted(out)


def place(rs: Ruleset, state: GameState, capitals: Mapping[str, str], seed: int | str,
          salt: int | str | None) -> dict[str, Any]:
    """The layout of finds (5): candidates without ring 1 (and without the ruins unless `ruins_find`), k =
    ceil(density x candidates) + extra in ordinary neutral provinces; up to `tries` attempts from the secret stream,
    the first one whose budgets differ by <= fair_tolerance and whose zone counts differ by <= 1 is taken, else the
    best (`accepted` False: the admin's `discoveries_unfair`). No compensation step (decision 25 c).

    Pure: reads the map, the occupied starts (`capitals`), the pool and base prices, never factions, so the
    provisional turn 1 of a setup phase and the final one get the same layout."""
    cfg = rs.scenario.discoveries
    secret = secret_seed(seed, salt)
    ring, zone = rings(rs, capitals)
    starts_all = {pid for s in rs.map.starts for pid in (s.capital, *s.provinces)}
    ruins = {s.capital for s in rs.map.starts if s.capital in state.abandoned}
    players = len(capitals)
    pool = sorted((d for d in rs.discoveries.values() if d.fits(players)), key=lambda d: d.id)
    abandoned = [p for p in state.abandoned if ring.get(p, 0) >= 2 and (p not in ruins or cfg["ruins_find"])]
    ordinary = [p for p in sorted(rs.map.provinces) if p not in starts_all and ring.get(p, 0) >= 2
                and state.provinces[p].owner is None]
    k = min(len(abandoned), math.ceil(cfg["density"] * len(abandoned) - 1e-9))
    k_extra = min(len(ordinary), int(cfg["extra"]))
    info: dict[str, Any] = {"finds": {}, "rings": {p: ring[p] for p in sorted(set(abandoned) | set(ordinary))},
                            "zones": {p: zone[p] for p in sorted(set(abandoned) | set(ordinary))},
                            "budgets": {s: 0.0 for s in sorted(capitals)}, "counts": {s: 0 for s in sorted(capitals)},
                            "spread": 0.0, "attempt": None, "accepted": True, "mirror": None}
    if not pool or (k == 0 and k_extra == 0):
        return info
    mirror = _orbit_rotation(rs, capitals) if cfg["fair_mirror"] else None
    if cfg["fair_mirror"]:
        info["mirror"] = "applied" if mirror is not None else "skipped"

    def allowed(pid: str) -> list[DiscoveryDef]:
        rarities = _by_ring(cfg["rings"], ring[pid]) or ()
        return [d for d in pool if f"{d.kind}:{d.rarity}" in rarities and (pid not in ruins or d.kind == "boon")]

    def draw(rng: Any, chosen: list[str]) -> dict[str, str]:
        used: set[str] = set()
        out: dict[str, str] = {}
        for pid in chosen:
            options = allowed(pid)
            fresh = [d for d in options if d.id not in used] or options
            if not fresh:
                continue
            total = sum(d.weight for d in fresh)
            x = rng.random() * total
            pick = fresh[-1]
            for d in fresh:
                x -= d.weight
                if x < 0:
                    pick = d
                    break
            used.add(pick.id)
            out[pid] = pick.id
        return out

    def measure(finds: Mapping[str, str]) -> tuple[dict[str, float], dict[str, int]]:
        budgets = {s: 0.0 for s in sorted(capitals)}
        counts = {s: 0 for s in sorted(capitals)}
        for pid, fid in finds.items():
            seats = zone[pid]
            for s in seats:
                counts[s] += 1
            r = ring[pid]
            weight = _by_ring(cfg["w"], r) if r <= cfg["max_ring"] else None
            if weight:
                for s in seats:
                    budgets[s] += weight * rs.discoveries[fid].power / len(seats)
        return {s: round(v, 4) for s, v in budgets.items()}, counts

    best: tuple[Any, ...] | None = None
    for attempt in range(int(cfg["tries"])):
        rng = stream(secret, 0, DISCOVERIES, f"place|{attempt}")
        if mirror is not None:
            reps_a = sorted({_orbit(mirror, p)[0] for p in abandoned})
            reps_o = sorted({_orbit(mirror, p)[0] for p in ordinary})
            rng.shuffle(reps_a)
            rng.shuffle(reps_o)
            size = len(_orbit(mirror, reps_a[0])) if reps_a else 1
            chosen = reps_a[:min(len(reps_a), math.ceil(k / size - 1e-9))]
            chosen += reps_o[:min(len(reps_o), math.ceil(k_extra / max(size, 1) - 1e-9))]
            base = draw(rng, chosen)
            finds = {q: fid for pid, fid in base.items() for q in _orbit(mirror, pid)}
        else:
            cand_a, cand_o = list(abandoned), list(ordinary)
            rng.shuffle(cand_a)
            rng.shuffle(cand_o)
            finds = draw(rng, cand_a[:k] + cand_o[:k_extra])
        budgets, counts = measure(finds)
        spread = (max(budgets.values()) - min(budgets.values())) if budgets else 0.0
        parity = (max(counts.values()) - min(counts.values())) if counts else 0
        ok = spread <= cfg["fair_tolerance"] + 1e-9 and parity <= 1
        key = (not ok, round(spread, 4), parity, attempt)
        if best is None or key < best[0]:
            best = (key, finds, budgets, counts, spread, attempt, ok)
        if ok:
            break
    assert best is not None
    _, finds, budgets, counts, spread, attempt, ok = best
    info.update(finds=dict(sorted(finds.items())), budgets=budgets, counts=counts, spread=round(spread, 4),
                attempt=attempt, accepted=ok)
    return info


def capitals_of(state: GameState) -> dict[str, str]:
    """The living seats' start capitals as the placement read them (Player.home is set when a system is on)."""
    return {seat: p.home for seat, p in sorted(state.players.items()) if p.home}


# ---------------------------------------------------------------- effects

def _price(rs: Ruleset, units: Mapping[str, int]) -> int:
    return sum(unit_price(rs, u) * n for u, n in units.items())


def _shares(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    """`total` split over seats in proportion to `weights` (largest remainder, ties by seat id)."""
    seats = sorted(weights)
    pools = [max(0, weights[s]) for s in seats]
    if sum(pools) <= 0:
        pools = [1] * len(seats)
    whole = sum(pools)
    base = [p * total // whole for p in pools]
    order = sorted(range(len(seats)), key=lambda i: (-(pools[i] * total % whole), seats[i]))
    for i in order[:total - sum(base)]:
        base[i] += 1
    return dict(zip(seats, base))


def losses_for(rs: Ruleset, units: Mapping[str, int], frac: float, low: int) -> dict[str, int]:
    """`lose_units {frac, min}` on an army: cheapest units first until the price taken reaches frac x the army's
    price, at least `low` units (discovery_content.loss_count, the validator's rule)."""
    flat = sorted((unit_price(rs, u), u) for u, n in units.items() for _ in range(n))
    k = loss_count([p for p, _ in flat], frac, low)
    return dict(Counter(u for _, u in flat[:k]))


def _give_resource(state: GameState, seat: str, res: str, n: int) -> int:
    """Add `n` (may be negative: never below 0); returns the change made."""
    stock = state.players[seat].resources
    before = stock.get(res, 0)
    stock[res] = max(0, before + n)
    return stock[res] - before


def _give_artifact(rs: Ruleset, state: GameState, seat: str, art: str) -> dict[str, Any]:
    """The artifact, or 15 x power of the build resource when the seat has it or one it excludes (3)."""
    player = state.players[seat]
    a = rs.artifacts.get(art)
    if a is None:
        return {}
    clash = art in player.artifacts or any(art in rs.artifacts[x].exclusive_with or x in a.exclusive_with
                                           for x in player.artifacts if x in rs.artifacts)
    if clash:
        res = rs.roles["build"]
        n = 15 * a.power
        _give_resource(state, seat, res, n)
        return {"artifact_swap": {"artifact": art, "resources": {res: n}}}
    player.artifacts.append(art)
    return {"artifact": art}


def _value(rs: Ruleset, gained: Mapping[str, Any]) -> float:
    """The realized value (P) of what a seat got or lost from a find."""
    v = sum(n for n in (gained.get("resources") or {}).values()) / 15
    v += _price(rs, gained.get("units") or {}) / 20
    v -= _price(rs, gained.get("lost") or {}) / 20
    if gained.get("artifact") in rs.artifacts:
        v += rs.artifacts[gained["artifact"]].power
    for n in ((gained.get("artifact_swap") or {}).get("resources") or {}).values():
        v += n / 15
    return round(v, 2)


FIND_MARKS = cq.FIND_MARKS


def _set_status(prov: Province, status: str, turns: int, turn: int) -> dict[str, int]:
    """A find's Ravage/Ashes. When it lengthens the mark, the value is also noted in `status.find_marks` so that the
    views show it only where the province is seen (ABANDONED_LANDS.md 10); a conquest disposition drops the note."""
    key = "ravaged_until" if status == "ravaged" else "ruined_until"
    before = int(prov.status.get(key, 0))
    prov.status[key] = max(before, turn + turns)
    if prov.status[key] > before:
        prov.status.setdefault(FIND_MARKS, {})[key] = prov.status[key]
    return {key: prov.status[key]}


def public_status(prov: Province, status: Mapping[str, Any] | None, visible: bool) -> dict[str, Any] | None:
    """`status` (a public `statuses_for` entry) without the marks a find set, unless the province is seen."""
    if status is None:
        return None
    marks = (prov.status or {}).get(FIND_MARKS) or {}
    if visible or not marks:
        return dict(status)
    out = {k: v for k, v in status.items() if not (k in marks and marks[k] == prov.status.get(k))}
    return out or None


# ---------------------------------------------------------------- the resolution (ABANDONED_LANDS.md 7-9)

class DiscoveryStep:
    """Finds in a resolution: step 1 (mixed choices, before the snapshot of the books, with clipping of moves),
    3c (finds triggered by the armies that entered), after the battles (units of boons, rewards of guards, pending
    choices), step 7 (lairs grow) and eliminations."""

    def __init__(self, r: _Resolution):
        self.r = r
        self.s = r.s
        self.rs = r.rs
        self.turn = r.turn
        self.secret = r.secret
        self.grants: list[tuple[str, str, str, int, str | None]] = []   # (seat, pid, unit, n, first part's origin)

    def rng(self, entity: str) -> Any:
        return stream(self.secret, self.turn, DISCOVERIES, entity)

    # -- step 1: the choices of mixed finds
    def choose(self, alive: Sequence[str], books: dict[str, SeatOrders]) -> dict[str, SeatOrders]:
        """Apply every pending choice (the holder's `discover` order, else default_choice) before any order of this
        turn; clip moves out of the province that the outcome left short of units. Returns the books without the
        discover orders (they are spent here)."""
        s = self.s
        out: dict[str, SeatOrders] = {}
        orders: dict[tuple[str, str], Order] = {}
        for seat in alive:
            book = books.get(seat, SeatOrders())
            for o in book.orders:
                if o.kind != "discover":
                    continue
                entry = s.discoveries.get(o.province or "")
                pending = (entry or {}).get("pending") or {}
                if entry is None or entry.get("state") != PENDING or pending.get("seat") != seat:
                    self.r.ev("order_rejected", [seat], None, order=o.to_dict(), reason=NO_PENDING)
                elif (seat, o.province) in orders:
                    self.r.ev("order_rejected", [seat], None, order=o.to_dict(), reason="INVALID")
                else:
                    orders[(seat, o.province)] = o
            out[seat] = SeatOrders([o for o in book.orders if o.kind != "discover"], book.cp_spent, dict(book.forfeit))
        for seat, book in books.items():
            out.setdefault(seat, book)
        # core 0.8.0 (docs/TRADE.md 1.5.4): the choice in a province a pending trade gives away waits for the new
        # holder: back to `site` now, after the battles it is the receiver's (as at a capture)
        from .trade import promised
        promised_now = set(promised(s)) if s.trades else set()
        for pid in sorted(s.discoveries):
            entry = s.discoveries[pid]
            if entry.get("state") != PENDING:
                continue
            seat = entry["pending"]["seat"]
            d = self.rs.discoveries.get(entry["id"])
            prov = s.provinces[pid]
            if pid in promised_now and prov.owner == seat:
                entry["state"] = SITE
                entry.pop("pending", None)
                order = orders.get((seat, pid))
                if order is not None:
                    self.r.ev("order_rejected", [seat], None, order=order.to_dict(), reason="PROVINCE_PROMISED")
                continue
            if d is None or seat not in alive or prov.owner != seat:
                entry["state"] = SITE
                entry.pop("pending", None)
                continue
            order = orders.get((seat, pid))
            given = order.choice if order is not None and d.choice(order.choice) is not None else None
            if order is not None and given is None:
                self.r.ev("order_rejected", [seat], None, order=order.to_dict(), reason=BAD_CHOICE)
            cid = given or d.default_choice
            choice = d.choice(cid) or d.choices[0]
            u = self.rng(f"{pid}|{d.id}").random()
            acc = 0.0
            index = len(choice["outcomes"]) - 1
            for i, o in enumerate(choice["outcomes"]):
                acc += o["p"]
                if u < acc:
                    index = i
                    break
            outcome = choice["outcomes"][index]
            got = self.apply_own(seat, pid, outcome["effects"])
            del s.discoveries[pid]
            self.r.ev("discovery_chosen", [seat], pid, seat=seat, id=d.id, choice=choice["id"],
                      by_default=given is None, outcome=index, p=outcome["p"], u=round(u, 6), **got,
                      value=_value(self.rs, got))
            if got.get("lost"):
                out[seat] = self.clip(seat, pid, out.get(seat, SeatOrders()))
        return out

    def apply_own(self, seat: str, pid: str, effects: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Effects of a choice's outcome (or a reward) for the holder of `pid`: everything in full, units in pid."""
        s, rs = self.s, self.rs
        prov = s.provinces[pid]
        got: dict[str, Any] = {}
        for e in effects:
            kind = e["type"]
            if kind == "add_resource":
                n = _give_resource(s, seat, e["resource"], e["amount"])
                got.setdefault("resources", {})[e["resource"]] = got.get("resources", {}).get(e["resource"], 0) + n
            elif kind == "grant_units":
                prov.units[e["unit"]] = prov.units.get(e["unit"], 0) + e["count"]
                got.setdefault("units", {})[e["unit"]] = got.get("units", {}).get(e["unit"], 0) + e["count"]
            elif kind == "grant_building":
                if e["building"] not in prov.buildings:
                    prov.buildings = sorted([*prov.buildings, e["building"]])
                    got["building"] = e["building"]
            elif kind == "grant_artifact":
                got.update(_give_artifact(rs, s, seat, e["artifact"]))
            elif kind == "lose_units":
                lost = losses_for(rs, prov.units, float(e["frac"]), int(e.get("min", 1)))
                for unit, n in lost.items():
                    prov.units[unit] -= n
                    if prov.units[unit] <= 0:
                        del prov.units[unit]
                lost_all = Counter(got.get("lost") or {})
                lost_all.update(lost)
                if lost_all:
                    got["lost"] = dict(sorted(lost_all.items()))
            elif kind == "set_status":
                got.setdefault("status", {}).update(_set_status(prov, e["status"], e["turns"], self.turn))
        return got

    def clip(self, seat: str, pid: str, book: SeatOrders) -> SeatOrders:
        """Moves out of `pid` that the units left there no longer cover are cut down to what is there, the latest
        entries of the book first (`order_clipped`); a move left with no units is dropped."""
        have = Counter(self.s.provinces[pid].units)
        moves = [o for o in book.orders if o.kind == "move" and o.src == pid]
        need = Counter()
        for o in moves:
            need.update(o.units or {})
        excess = {u: n - have[u] for u, n in need.items() if n > have[u]}
        if not excess:
            return book
        changed: dict[str, dict[str, int]] = {}
        for o in reversed(moves):
            units = dict(changed.get(o.id, o.units or {}))
            for u in sorted(excess):
                cut = min(excess[u], units.get(u, 0))
                if cut:
                    units[u] -= cut
                    excess[u] -= cut
                    if not units[u]:
                        del units[u]
            if units != (o.units or {}):
                changed[o.id] = units
        orders = []
        for o in book.orders:
            if o.id not in changed:
                orders.append(o)
                continue
            after = changed[o.id]
            self.r.ev("order_clipped", [seat], pid, seat=seat, order=o.id, before=dict(o.units or {}), after=after)
            if after:
                orders.append(Order.from_dict({**o.to_dict(), "units": after}))
        return SeatOrders(orders, book.cp_spent, dict(book.forfeit))

    # -- step 3c: the armies that entered
    def trigger(self, arrivals: dict[str, list[Force]]) -> None:
        s, rs = self.s, self.rs
        for pid in sorted(arrivals):
            entry = s.discoveries.get(pid)
            if entry is None or entry.get("state") != HIDDEN:
                continue
            forces: dict[str, list[Force]] = {}
            for f in arrivals[pid]:
                if f.owner is not None and f.total() > 0:
                    forces.setdefault(f.owner, []).append(f)
            if not forces:
                continue
            d = rs.discoveries.get(entry["id"])
            if d is None:
                continue
            prices = {seat: sum(_price(rs, f.units) for f in fs) for seat, fs in sorted(forces.items())}
            seats = sorted(forces)
            if d.kind == "mixed":
                entry["state"] = SITE
                entry["known"] = sorted(set(entry.get("known") or []) | set(seats))
                self.r.ev("discovery_found", seats, pid, id=d.id, find_kind=d.kind, entrants=seats, shares={},
                          province_effects={}, value={x: 0.0 for x in seats})
                continue
            shares, province = self.apply_entry(d, pid, forces, prices)
            arrivals[pid][:] = [f for f in arrivals[pid] if f.total() > 0]     # a wiped army fights nothing
            del s.discoveries[pid]
            self.r.ev("discovery_found", seats, pid, id=d.id, find_kind=d.kind, entrants=seats, shares=shares,
                      province_effects=province, value={x: _value(rs, shares.get(x) or {}) for x in seats})

    def apply_entry(self, d: DiscoveryDef, pid: str, forces: Mapping[str, list[Force]],
                    prices: Mapping[str, int]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """A boon or hindrance as armies enter (7.3): army hindrances to each army in full, seat boons in shares by
        army price, a negative resource effect to each seat in full, province effects once."""
        s, rs = self.s, self.rs
        prov = s.provinces[pid]
        shares: dict[str, dict[str, Any]] = {seat: {} for seat in sorted(forces)}
        province: dict[str, Any] = {}

        def add(seat: str, key: str, item: str, n: int) -> None:
            if n:
                bucket = shares[seat].setdefault(key, {})
                bucket[item] = bucket.get(item, 0) + n

        for e in d.effects:
            kind = e["type"]
            if kind == "add_resource":
                if e["amount"] > 0:
                    for seat, n in _shares(e["amount"], prices).items():
                        add(seat, "resources", e["resource"], _give_resource(s, seat, e["resource"], n))
                else:
                    for seat in sorted(forces):
                        add(seat, "resources", e["resource"], _give_resource(s, seat, e["resource"], e["amount"]))
            elif kind == "grant_units":
                for seat, n in _shares(e["count"], prices).items():
                    if n:
                        first = sorted(p for f in forces[seat] for p in f.parts)
                        self.grants.append((seat, pid, e["unit"], n, first[0] if first else None))
                        add(seat, "units", e["unit"], n)
            elif kind == "grant_artifact":
                top = max(prices.values())
                best = sorted(seat for seat, p in prices.items() if p == top)
                seat = best[0] if len(best) == 1 else best[int(self.rng(f"{pid}|artifact").random() * len(best))]
                shares[seat].update(_give_artifact(rs, s, seat, e["artifact"]))
            elif kind == "lose_units":
                for seat, fs in sorted(forces.items()):
                    merged: Counter[str] = Counter()
                    for f in fs:
                        merged.update(f.units)
                    lost = losses_for(rs, merged, float(e["frac"]), int(e.get("min", 1)))
                    for unit, n in sorted(lost.items()):
                        holders = [f for f in fs if f.units.get(unit)]
                        for f, k in zip(holders, share_losses([f.units[unit] for f in holders], n)):
                            if k:
                                f.remove({unit: k})
                        add(seat, "lost", unit, n)
            elif kind == "grant_building":
                if e["building"] not in prov.buildings:
                    prov.buildings = sorted([*prov.buildings, e["building"]])
                    province["building"] = e["building"]
            elif kind == "spawn_neutral":
                if prov.owner is None:
                    for unit, n in e["units"].items():
                        prov.units[unit] = prov.units.get(unit, 0) + n
                    # a guard already there (the ruins' garrison with its Treasury under `ruins_find`) keeps its
                    # reward: the find's reward is added to it, never replaces it (ABANDONED_LANDS.md 2.3, 9)
                    old = prov.status.get("guard") or {}
                    guard: dict[str, Any] = {"reward": [*(dict(x) for x in old.get("reward") or ()),
                                                        *(dict(x) for x in e.get("reward") or ())]}
                    if e.get("grow"):
                        guard["grow"] = {**e["grow"], "next": self.turn + int(e["grow"]["every"])}
                    elif old.get("grow"):
                        guard["grow"] = dict(old["grow"])
                    prov.status["guard"] = guard
                    province["neutrals"] = dict(sorted(e["units"].items()))
            elif kind == "set_status":
                province.setdefault("status", {}).update(_set_status(prov, e["status"], e["turns"], self.turn))
        return shares, province

    # -- after the battles (7.4, 9)
    def after_battles(self) -> None:
        s = self.s
        for seat, pid, unit, n, origin in self.grants:
            if seat not in s.players or not s.players[seat].alive:
                continue
            target = None
            for cand in (pid, origin, s.players[seat].home):
                if cand is not None and cand in s.provinces and s.provinces[cand].owner == seat:
                    target = cand
                    break
            if target is None:
                owned = s.owned(seat)
                target = owned[0].id if owned else None
            if target is None:
                continue
            prov = s.provinces[target]
            prov.units[unit] = prov.units.get(unit, 0) + n
            self.r.ev("discovery_units", [seat], target, seat=seat, units={unit: n}, source=pid)
        self.grants = []
        for pid, prov in sorted(s.provinces.items()):
            guard = prov.status.get("guard") if prov.status else None
            if guard is None or prov.owner is None:
                continue
            got = self.apply_own(prov.owner, pid, guard.get("reward") or ())
            del prov.status["guard"]
            self.r.ev("guard_cleared", [prov.owner], pid, by=prov.owner, lair=bool(guard.get("grow")), **got,
                      value=_value(self.rs, got))
        for pid in sorted(s.discoveries):
            entry = s.discoveries[pid]
            owner = s.provinces[pid].owner
            if entry.get("state") == SITE and owner is not None and s.players[owner].alive:
                entry["state"] = PENDING
                entry["pending"] = {"seat": owner, "turn": self.turn}
                entry["known"] = sorted(set(entry.get("known") or []) | {owner})
                self.r.ev("discovery_pending", [owner], pid, seat=owner, id=entry["id"])

    # -- step 7: lairs grow (9)
    def timers(self) -> None:
        for pid, prov in sorted(self.s.provinces.items()):
            guard = prov.status.get("guard") if prov.status else None
            grow = (guard or {}).get("grow")
            if not grow or prov.owner is not None or not prov.unit_total() or self.turn < grow["next"]:
                continue
            room = int(grow["cap"]) - prov.unit_total()
            added: dict[str, int] = {}
            for unit, n in sorted(grow["add"].items()):
                k = max(0, min(n, room))
                if k:
                    prov.units[unit] = prov.units.get(unit, 0) + k
                    added[unit] = k
                    room -= k
            grow["next"] = self.turn + int(grow["every"])
            if added:
                self.r.ev("lair_grew", [], pid, units=added, total=prov.unit_total(), cap=int(grow["cap"]))

    # -- eliminations (7.4)
    def eliminated(self, seats: Iterable[str]) -> None:
        gone = set(seats)
        if not gone:
            return
        for entry in self.s.discoveries.values():
            if entry.get("state") == PENDING and entry["pending"]["seat"] in gone:
                entry["state"] = SITE
                entry.pop("pending", None)
            entry["known"] = [x for x in entry.get("known") or [] if x not in gone]


# ---------------------------------------------------------------- orders (8)

def move_warning(state: GameState, rs: Ruleset, seat: str, order: Order) -> str | None:
    """A move into an Abandoned province the seat does not hold: entering may set off a find. Said for every such
    province, with a find or without (fog: the warning must not tell where finds lie); a hidden find the seat knows
    (Observation) is named. A warning, never a refusal; only the validate/submit answer carries it."""
    if order.kind != "move" or order.feint or order.dst not in state.abandoned or not on(rs):
        return None
    if state.provinces[order.dst].owner == seat:
        return None
    entry = known_find(state, seat, order.dst)
    if entry is not None and entry.get("state") != HIDDEN:
        return None                                         # an opened mixed find does not go off again
    d = rs.discoveries.get(entry["id"]) if entry is not None else None
    what = f"находка «{d.name}» (сила {d.power:+.1f})" if d is not None else "возможна находка"
    return (f"{order.dst} — Заброшенная: {what} при входе, до боя; помеха может снять юниты, мелкую армию — "
            'целиком (rules(topic="discoveries"))')


def discover_problem(state: GameState, rs: Ruleset, seat: str, order: Order) -> tuple[str, str] | None:
    entry = state.discoveries.get(order.province or "")
    pending = (entry or {}).get("pending") or {}
    if entry is None or entry.get("state") != PENDING or pending.get("seat") != seat:
        mine = sorted(p for p, e in state.discoveries.items()
                      if e.get("state") == PENDING and (e.get("pending") or {}).get("seat") == seat)
        return NO_PENDING, (f"в {order.province} у тебя нет находки на выбор"
                            + (f"; ждут выбора: {', '.join(mine)}" if mine else ""))
    d = rs.discoveries.get(entry["id"])
    if d is None or d.choice(order.choice) is None:
        options = ", ".join(c["id"] for c in d.choices) if d is not None else "—"
        return BAD_CHOICE, f"вариант {order.choice!r} не подходит; варианты: {options}"
    return None


# ---------------------------------------------------------------- fog and views (10, 13)

def public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """discovery_found for those who only see the province: where, who entered, the kind of the find and the visible
    consequences there (neutrals, a status, a building); no name, no gains (a name would tell the artifact)."""
    out = {k: event[k] for k in ("kind", "seats", "province") if k in event}
    out.update(entrants=list(event.get("entrants") or []), find_kind=event.get("find_kind"),
               province_effects=dict(event.get("province_effects") or {}), public=True)
    return out


def known_find(state: GameState, seat: str, pid: str) -> dict[str, Any] | None:
    entry = state.discoveries.get(pid)
    if entry is None or seat not in (entry.get("known") or []):
        return None
    return entry


def lair_tag(prov: Province) -> str | None:
    guard = prov.status.get("guard") if prov.status else None
    if guard is None:
        return None
    grow = guard.get("grow")
    if grow:
        return f"[логово {prov.unit_total()}/{grow['cap']}]"
    return "[стража]"


def is_ruins(rs: Ruleset, state: GameState, pid: str) -> bool:
    """The ruins: an Abandoned old capital (its guard is the scenario's garrison)."""
    return pid in state.abandoned and pid in rs.map.capitals


def _guard_seen(rs: Ruleset, state: GameState, pid: str, visible: bool) -> bool:
    """The ruins' guard is public (the scenario's garrison); any guard a find spawned (a guarded boon, a guard, a
    lair) only where the province is seen (ABANDONED_LANDS.md 9, 10)."""
    prov = state.provinces[pid]
    guard = prov.status.get("guard") if prov.status else None
    return guard is not None and (visible or (is_ruins(rs, state, pid) and not guard.get("grow")))


def province_tags(rs: Ruleset, state: GameState, seat: str | None, pid: str, visible: bool = True) -> list[str]:
    """[руины]/[заброш.], [стража]/[логово n/cap] (a lair where seen), [находка «X»] (known to `seat` only)."""
    if not on(rs):
        return []
    tags = []
    prov = state.provinces[pid]
    if pid in state.abandoned:
        tags.append("[руины]" if pid in rs.map.capitals else "[заброш.]")
    tag = lair_tag(prov) if _guard_seen(rs, state, pid, visible) else None
    if tag:
        tags.append(tag)
    entry = known_find(state, seat, pid) if seat is not None else None
    if entry is not None:
        d = rs.discoveries.get(entry["id"])
        tags.append(f"[находка «{d.name if d else entry['id']}»]")
    return tags


def province_json(rs: Ruleset, state: GameState, seat: str | None, pid: str, visible: bool = True) -> dict[str, Any]:
    """Public marks of a province (a lair only where seen) plus the find known to `seat` ({id, state})."""
    if not on(rs):
        return {}
    out: dict[str, Any] = {}
    prov = state.provinces[pid]
    if pid in state.abandoned:
        out["abandoned"] = True
        if pid in rs.map.capitals:
            out["ruins"] = True
    guard = prov.status.get("guard") if _guard_seen(rs, state, pid, visible) else None
    if guard is not None:
        out["guard"] = {"lair": bool(guard.get("grow"))} | ({"cap": guard["grow"]["cap"]} if guard.get("grow") else {})
    entry = known_find(state, seat, pid) if seat is not None else None
    if entry is not None:
        out["discovery"] = {"id": entry["id"], "state": entry["state"]}
    return out


def _effect_text(rs: Ruleset, e: Mapping[str, Any]) -> str:
    kind = e["type"]
    if kind == "add_resource":
        name = rs.resources[e["resource"]].name if e["resource"] in rs.resources else e["resource"]
        return f"{e['amount']:+d} {name}"
    if kind == "grant_units":
        return f"{rs.short(e['unit'])}×{e['count']}"
    if kind == "grant_building":
        return rs.buildings[e["building"]].name if e["building"] in rs.buildings else e["building"]
    if kind == "grant_artifact":
        return f"артефакт «{rs.artifacts[e['artifact']].name}»" if e["artifact"] in rs.artifacts else "артефакт"
    if kind == "lose_units":
        low = int(e.get("min", 1))
        return f"−{float(e['frac']):.0%} армии" + (f" (мин. {low})" if low > 1 else "")
    if kind == "set_status":
        return ("разорение" if e["status"] == "ravaged" else "пепелище") + f" {e['turns']} х."
    if kind == "spawn_neutral":
        text = "нейтралы " + " ".join(f"{rs.short(u)}×{n}" for u, n in e["units"].items())
        grow = e.get("grow")
        if grow:
            add = " ".join(f"{rs.short(u)}×{n}" for u, n in grow["add"].items())
            text += f", +{add} раз в {grow['every']} х. до {grow['cap']}"
        if e.get("reward"):       # the reward of a guarded find: whoever destroys the neutrals and takes the province
            text += f" → награда {effects_text(rs, e['reward'])}"
        return text
    return kind


def effects_text(rs: Ruleset, effects: Iterable[Mapping[str, Any]]) -> str:
    return ", ".join(_effect_text(rs, e) for e in effects) or "ничего"


def choice_text(rs: Ruleset, d: DiscoveryDef, c: Mapping[str, Any]) -> str:
    outs = c["outcomes"]
    body = effects_text(rs, outs[0]["effects"]) if len(outs) == 1 else \
        " / ".join(f"{o['p']:.0%} {effects_text(rs, o['effects'])}" for o in outs)
    return f"{c['id']} ({body}, EV {d.ev.get(c['id'], 0):.1f})"


PENDING_LINES = 2


def pending_lines(rs: Ruleset, state: GameState, seat: str) -> list[str]:
    """НАХОДКА lines (13.1, 8): the seat's pending choices, at most PENDING_LINES, then «+k»."""
    if not on(rs):
        return []
    mine = [(pid, e) for pid, e in sorted(state.discoveries.items())
            if e.get("state") == PENDING and (e.get("pending") or {}).get("seat") == seat]
    lines = []
    for pid, e in mine[:PENDING_LINES]:
        d = rs.discoveries.get(e["id"])
        if d is None:
            continue
        options = " | ".join(choice_text(rs, d, c) for c in d.choices)
        example = f'{{"kind":"discover","province":"{pid}","choice":"{d.choices[0]["id"]}"}}'
        line = f"НАХОДКА {pid} «{d.name}»: {options}; без приказа — {d.default_choice}. {example}"
        if len(line) > 300:
            line = f"НАХОДКА {pid} «{d.name}»: {', '.join(c['id'] for c in d.choices)} (describe(\"{d.name}\")); " \
                   f"без приказа — {d.default_choice}. {example}"
        lines.append(line)
    if len(mine) > PENDING_LINES:
        lines.append(f"НАХОДКИ: +{len(mine) - PENDING_LINES}")
    return lines


def pending_json(rs: Ruleset, state: GameState, seat: str) -> list[dict[str, Any]]:
    """The seat's pending choices for `get_state(format=json)`: province, find, its choices with EV and risk (best -
    worst outcome, the validator's reckoning), the default."""
    out = []
    for pid, e in sorted(state.discoveries.items()):
        if e.get("state") != PENDING or (e.get("pending") or {}).get("seat") != seat:
            continue
        d = rs.discoveries.get(e["id"])
        entry: dict[str, Any] = {"province": pid, "id": e["id"]}
        if d is not None:
            entry.update(name=d.name, default=d.default_choice,
                         choices=[{"id": c["id"], "ev": round(d.ev.get(c["id"], 0.0), 2),
                                   "risk": choice_risk(c, rs.units, rs.buildings, rs.artifacts)} for c in d.choices])
        out.append(entry)
    return out


def _amounts(rs: Ruleset, amounts: Mapping[str, int] | None) -> str:
    return ", ".join(f"{n:+d} {rs.resources[r].name if r in rs.resources else r}" for r, n in (amounts or {}).items())


def _units(rs: Ruleset, units: Mapping[str, int] | None) -> str:
    return " ".join(f"{rs.short(u)}×{n}" for u, n in sorted((units or {}).items()) if n)


def _gain_text(rs: Ruleset, got: Mapping[str, Any]) -> str:
    parts = []
    if got.get("resources"):
        parts.append(_amounts(rs, got["resources"]))
    if got.get("units"):
        parts.append(_units(rs, got["units"]))
    if got.get("lost"):
        parts.append("потери " + _units(rs, got["lost"]))
    if got.get("artifact"):
        a = rs.artifacts.get(got["artifact"])
        parts.append(f"артефакт «{a.name if a else got['artifact']}»")
    if got.get("artifact_swap"):
        parts.append("вместо повторного артефакта " + _amounts(rs, got["artifact_swap"].get("resources")))
    if got.get("building"):
        parts.append(rs.buildings[got["building"]].name if got["building"] in rs.buildings else got["building"])
    if got.get("status"):
        parts.append(", ".join("разорена" if k == "ravaged_until" else f"пепелище до х{v}"
                               for k, v in got["status"].items()))
    return "; ".join(parts) or "ничего"


def _province_effects_text(rs: Ruleset, eff: Mapping[str, Any]) -> str:
    parts = []
    if eff.get("neutrals"):
        parts.append("нейтралы: " + _units(rs, eff["neutrals"]))
    if eff.get("status"):
        parts.append(", ".join("разорена" if k == "ravaged_until" else f"пепелище до х{v}"
                               for k, v in eff["status"].items()))
    if eff.get("building"):
        parts.append(rs.buildings[eff["building"]].name if eff["building"] in rs.buildings else eff["building"])
    return "; ".join(parts)


_KIND_TEXT = {"boon": "плюшка", "hindrance": "помеха", "mixed": "смешанная"}


def found_text(e: Mapping[str, Any], rs: Ruleset, who: Any) -> str:
    """«P13: находка «Тайник Сплава» — +30 Сплава»; with partners «… с Б — +18 Сплава (доля)»; the public form names
    only the kind and the visible consequences."""
    pid = e["province"]
    tail = _province_effects_text(rs, e.get("province_effects") or {})
    if e.get("public"):
        kind = _KIND_TEXT.get(e.get("find_kind"))
        text = f"{pid}: находка у {', '.join(who(s) for s in e.get('entrants') or [])}" + (f" — {kind}" if kind else "")
        return text + (f" ({tail})" if tail else "")
    d = rs.discoveries.get(e.get("id") or "")
    name = d.name if d else e.get("id")
    mine = [s for s in e.get("entrants") or [] if who(s) == "ТЫ"]
    others = [who(s) for s in e.get("entrants") or [] if who(s) != "ТЫ"]
    text = f"{pid}: находка «{name}»" + (f" с {', '.join(others)}" if others and mine else "")
    if e.get("find_kind") == "mixed":
        return text + " — смешанная, выбор у того, кто удержит провинцию"
    seat = mine[0] if mine else None
    got = (e.get("shares") or {}).get(seat) if seat else None
    body = _gain_text(rs, got or {}) if seat else "; ".join(
        f"{who(s)}: {_gain_text(rs, g)}" for s, g in sorted((e.get("shares") or {}).items()))
    if seat and len(e.get("entrants") or []) > 1:
        body += " (доля)"
    reward = _guard_reward_text(rs, d) if (e.get("province_effects") or {}).get("neutrals") else ""
    if reward:
        # a guarded boon gives nothing on entry: its worth is the guard's reward
        guards = tail.replace("нейтралы: ", "стража: ", 1)
        return text + (f" — {body}" if body != "ничего" else "") + f"; {guards}; {reward}"
    return text + f" — {body}" + (f"; {tail}" if tail else "")


def chosen_text(e: Mapping[str, Any], rs: Ruleset, who: Any) -> str:
    d = rs.discoveries.get(e.get("id") or "")
    name = d.name if d else e.get("id")
    how = "по умолчанию " if e.get("by_default") else ""
    return f"{e['province']}: «{name}» — {how}{e.get('choice')}: {_gain_text(rs, e)}"


def _guard_name(rs: Ruleset, e: Mapping[str, Any]) -> str:
    """«логово зачищено» / «стража руин уничтожена» (an old capital: the garrison) / «стража находки уничтожена».
    Read from the province, so the event (journal) stays as it was."""
    if e.get("lair"):
        return "логово зачищено"
    return "стража руин уничтожена" if e.get("province") in rs.map.capitals else "стража находки уничтожена"


def _guard_reward_text(rs: Ruleset, d: DiscoveryDef | None) -> str:
    """The reward of a find's guard («награда тому, кто их уничтожит и возьмёт провинцию: +45 Сплав»), or ''."""
    reward = [x for e in (d.effects if d else ()) if e["type"] == "spawn_neutral" for x in e.get("reward") or ()]
    return f"награда тому, кто их уничтожит и возьмёт провинцию: {effects_text(rs, reward)}" if reward else ""


DISCOVERY_EVENT_TEXT = {
    "discovery_found": found_text,
    "discovery_chosen": chosen_text,
    "discovery_units": lambda e, rs, who: f"{e['province']}: {_units(rs, e.get('units'))} из находки в {e.get('source')}",
    "discovery_pending": lambda e, rs, who: (f"{e['province']}: находка «"
                                             f"{rs.discoveries[e['id']].name if e.get('id') in rs.discoveries else e.get('id')}» "
                                             "ждёт твоего выбора — приказ discover в Д или П2 следующего хода "
                                             "(строка НАХОДКА), иначе вариант по умолчанию"),
    "order_clipped": lambda e, rs, who: (f"{e['province']}: приказ {e.get('order')} обрезан находкой — "
                                         f"{sum((e.get('before') or {}).values())} → "
                                         f"{sum((e.get('after') or {}).values())}"),
    "guard_cleared": lambda e, rs, who: (f"{e['province']}: {_guard_name(rs, e)}"
                                         f" ({who(e.get('by'))})" + (f" — {_gain_text(rs, e)}" if
                                                                     e.get("resources") or e.get("units") else "")),
    "lair_grew": lambda e, rs, who: f"{e['province']}: логово выросло до {e.get('total')}/{e.get('cap')}",
}


def journal_layout(rs: Ruleset, state: GameState, seed: int | str, salt: int | str | None) -> dict[str, Any] | None:
    """The layout for the admin journal (`discoveries_placed`), recomputed from the same inputs as at creation."""
    if not active(rs, state):
        return None
    info = place(rs, state, capitals_of(state), seed, salt)
    info["abandoned"] = list(state.abandoned)
    return info
