"""Conquest (docs/SPY_AND_CONQUEST.md section 3): province statuses, dispositions, claims, the resolution step 4b.

Everything here is a no-op unless the ruleset has the `conquest` system on (`Ruleset.system("conquest")`): no status,
claim or origin ever appears, so games without conquest resolve and hash exactly as before.

Province status (`Province.status`, public except `sabotaged` and `looted_turn` details):
    occupied {by, from, since}   held by force: no income, no score, no building effects, no builds or recruits
    ruined_until T               ruins until turn T (exclusive): no income, no score, no builds or recruits
    ravaged_until T              ravaged until turn T (exclusive): no income
    looted_turn T                last turn the province gave loot (survives a change of owner)
    sabotaged [{...}]            espionage: strikes in this turn's resolution (income x `income_mul` there only)
A province is *full* without `occupied` and without ruins (`is_full`).

Claims (`GameState.claims`, public): [{seat, province, until_turn}] — the attack of the claim's holder on that
province is x (1 + conquest.claim_bonus) in battles of turns <= until_turn.
"""

from __future__ import annotations

import copy
import math
from collections import Counter
from typing import TYPE_CHECKING, Any, Mapping

from .content import Ruleset, StatKey
from .model import GameState, Order, Province

if TYPE_CHECKING:       # pragma: no cover
    from .resolve import _Resolution

CONQUEST = "conquest"
ANNEX, RAZE, RAID, RELEASE = "annex", "raze", "raid", "release"
# behaviours that cannot be applied to ruins (SPY_AND_CONQUEST.md 3.3: "Нельзя подчинить или разграбить Пепелище")
NOT_ON_RUINS = (ANNEX, RAID)

# captured-event `mode` and final `status` words (the public form keeps `status` only)
MODES = ("free", "reclaimed", "occupied", "directive", "policy")
STATUS_FULL, STATUS_OCCUPIED, STATUS_RUINED, STATUS_RETURNED = "full", "occupied", "ruined", "returned"
DISPOSITION_STATUS = {ANNEX: "subjugated", RAZE: "ruined", RAID: "raided_returned", RELEASE: "released"}

# Report events only their sides see (resolve._PRIVATE_KINDS); public to everyone (in the public form); public form
PRIVATE_KINDS = frozenset({"policy_set", "policy_unaffordable", "dispose_failed"})
PUBLIC_KINDS = frozenset({"disposition", "occupation_revolt", "claim_created", "claim_ended", "ruin_ended"})
_PUBLIC_DROP = {"captured": ("mode",), "disposition": ("cost", "loot", "loot_reason", "source", "disposition")}


# core 0.8.0 (docs/SPY_V2.md 7): in a ruleset where sabotage can raise a revolt, the public revolt tells no cause
_REVOLT_DROP = ("reason", "rebels", "lost")


def public_event(event: Mapping[str, Any], hide_revolt: bool = False) -> dict[str, Any]:
    """The form of a conquest event a seat that is not its side sees: no prices, loot or source (a directive or a
    policy would tell that the taker had the price free), a capture without its `mode`; with `hide_revolt` (a
    ruleset with sabotage `unrest_units`) a revolt without its reason, rebels and lost units."""
    drop = _REVOLT_DROP if hide_revolt and event["kind"] == "occupation_revolt" else _PUBLIC_DROP.get(event["kind"], ())
    return {k: v for k, v in event.items() if k not in drop}


# ---------------------------------------------------------------- statuses

def occupied(prov: Province) -> dict[str, Any] | None:
    return prov.status.get("occupied")


def is_ruined(prov: Province, turn: int) -> bool:
    return prov.status.get("ruined_until", 0) > turn


def is_ravaged(prov: Province, turn: int) -> bool:
    return prov.status.get("ravaged_until", 0) > turn


def is_full(prov: Province, turn: int) -> bool:
    """A full province: not occupied and not in ruins (ravaged and sabotaged ones are full)."""
    return not prov.status or (occupied(prov) is None and not is_ruined(prov, turn))


def buildings_work(prov: Province, turn: int) -> bool:
    """Its buildings act (income, province and owner modifiers, recruitment): only in a full province."""
    return is_full(prov, turn)


def unrest_mark(prov: Province, turn: int) -> Mapping[str, Any] | None:
    """This turn's strike mark with the highest `unrest_units` (core 0.8.0 sabotage), the first by uid; or None."""
    marks = [mk for mk in strike_marks(prov, turn) if mk.get("unrest_units") is not None]
    return min(marks, key=lambda mk: (-int(mk["unrest_units"]), str(mk.get("uid") or ""))) if marks else None


def strike_marks(prov: Province, turn: int) -> list[Mapping[str, Any]]:
    """This turn's sabotage marks on a province (`status.sabotaged` is a list: one mark per successful strike)."""
    raw = prov.status.get("sabotaged") if prov.status else None
    if isinstance(raw, Mapping):
        raw = [raw]
    return [m for m in (raw or []) if isinstance(m, Mapping) and m.get("turn") == turn]


def income_factor(prov: Province, turn: int, strikes: bool = False) -> float:
    """Multiplier of a province's income: 0 while occupied, in ruins or ravaged. A sabotage strike's `income_mul`
    (the lowest of this turn's marks) counts only with `strikes` - in the resolution's economy step; the views of
    negotiation_2 never apply a hidden strike (SPY_AND_CONQUEST.md 2.6)."""
    if not prov.status:
        return 1.0
    if not is_full(prov, turn) or is_ravaged(prov, turn):
        return 0.0
    # stage 5 (ABANDONED_LANDS.md 2.2): an Abandoned province's lasting x income_mul, times a strike's
    lasting = float(prov.status.get("income_mul", 1.0))
    if strikes:
        muls = [float(m["income_mul"]) for m in strike_marks(prov, turn) if "income_mul" in m]
        if muls:
            return min(muls) * lasting
    return lasting


def scores_province(prov: Province, turn: int) -> bool:
    """The province gives points to its owner: not occupied, not in ruins (ravaged and sabotaged ones count)."""
    return is_full(prov, turn)


def living_capital(state: GameState, seat: str) -> str | None:
    """The seat's start capital (`Player.home`) if the seat holds it full itself, else None."""
    player = state.players.get(seat)
    home = player.home if player is not None else None
    if home is None or home not in state.provinces:
        return None
    prov = state.provinces[home]
    return home if prov.owner == seat and is_full(prov, state.turn) else None


def check_place(state: GameState, seat: str) -> str | None:
    """The province a mission against a seat is checked in (SPY_AND_CONQUEST.md 1.3): its living capital, else its
    first full province by id, else its first occupied one by id; None for a seat with no land."""
    capital = living_capital(state, seat)
    if capital is not None:
        return capital
    owned = state.owned(seat)
    full = [p.id for p in owned if is_full(p, state.turn)]
    if full:
        return full[0]
    held = [p.id for p in owned if occupied(p) is not None]
    return held[0] if held else (owned[0].id if owned else None)


# ---------------------------------------------------------------- claims

def claim_of(state: GameState, seat: str | None, pid: str, turn: int) -> dict[str, Any] | None:
    """The seat's claim on the province in force in `turn`."""
    if seat is None:
        return None
    return next((c for c in state.claims if c["seat"] == seat and c["province"] == pid and c["until_turn"] >= turn),
                None)


def claim_mul(state: GameState, ruleset: Ruleset, seat: str | None, pid: str, turn: int,
              key: StatKey = None) -> float:
    """Attack multiplier of `seat` attacking `pid`: 1 + conquest.claim_bonus with a claim in force, else 1."""
    if not ruleset.system(CONQUEST) or claim_of(state, seat, pid, turn) is None:
        return 1.0
    return 1.0 + ruleset.stat("conquest.claim_bonus", key)


# ---------------------------------------------------------------- prices and loot

def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5 + 1e-9))


def conquest_value(ruleset: Ruleset, prov: Any) -> int:
    """Value of a province (anything with `.type`: a Province or the map's province) by its type (default 1)."""
    return ruleset.province_types[prov.type].conquest_value


def _scaled(ruleset: Ruleset, amounts: Mapping[str, int], value: int, mul: float) -> dict[str, int]:
    out = {r: _round_half_up(n * value * mul) for r, n in sorted(amounts.items())}
    return {r: n for r, n in out.items() if n > 0}


def disposition_cost(ruleset: Ruleset, disposition: str, prov: Any, key: StatKey = None) -> dict[str, int]:
    """Price of a disposition for a province: cost x conquest_value x disposition.<id>.cost_mul, half up."""
    d = ruleset.dispositions[disposition]
    return _scaled(ruleset, d.cost, conquest_value(ruleset, prov), ruleset.stat(f"disposition.{disposition}.cost_mul",
                                                                                 key))


def disposition_loot(ruleset: Ruleset, disposition: str, prov: Any, key: StatKey = None) -> dict[str, int]:
    """Loot of a disposition: loot x conquest_value x disposition.<id>.loot_mul, half up (made by the engine, never
    taken from the victim's stock)."""
    d = ruleset.dispositions[disposition]
    return _scaled(ruleset, d.loot, conquest_value(ruleset, prov), ruleset.stat(f"disposition.{disposition}.loot_mul",
                                                                                key))


def loot_block(ruleset: Ruleset, prov: Province, turn: int, key: StatKey = None) -> str | None:
    """Why the province gives no loot now: "ravaged" / "ruined" at the start of the resolution, or "cooldown" (it
    gave loot in the last conquest.loot_cooldown turns); None: it gives loot."""
    if is_ravaged(prov, turn):
        return "ravaged"
    if is_ruined(prov, turn):
        return "ruined"
    looted = prov.status.get("looted_turn")
    if looted is not None and turn - looted <= int(ruleset.stat("conquest.loot_cooldown", key)):
        return "cooldown"
    return None


def resolve_disposition_ref(ruleset: Ruleset, ref: Any) -> str | None:
    """Full disposition id for a full or short id or a name (ru or en), any case."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    full = ruleset.resolve("disposition", ref) or ruleset.resolve("disposition", ref.lower())
    if full:
        return full
    label = ref.lower().rstrip(".")
    hits = [k for k, d in ruleset.dispositions.items() if label in (d.name.lower(), d.name_en.lower())]
    return hits[0] if len(hits) == 1 else None


def seat_ref(state: GameState, ref: Any) -> str | None:
    """A seat id or a nick -> the seat id."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    if ref in state.players:
        return ref
    return next((s for s, p in state.players.items() if p.nick == ref), None)


def dispose_problem(state: GameState, ruleset: Ruleset, seat: str, order: Order) -> tuple[str, str] | None:
    """Why a `dispose` cannot be given (phase independent); None if it can. Reasons: UNKNOWN_ID, BAD_TARGET (a
    neutral province), NOT_OCCUPIED (an own full province), RUINED, NO_RECIPIENT, INVALID."""
    pid = order.province
    if pid not in state.provinces:
        return "UNKNOWN_ID", f"неизвестная провинция {pid!r}"
    prov = state.provinces[pid]
    d = ruleset.dispositions.get(order.disposition or "")
    if d is None:
        return "UNKNOWN_ID", f"неизвестная судьба {order.disposition!r}; варианты: {disposition_list(ruleset)}"
    if prov.owner is None:
        return "BAD_TARGET", f"{pid} нейтральная: судьбу решают только для провинций игроков (взятая у нейтралов — " \
                             "сразу твоя)"
    if prov.owner == seat:
        occ = occupied(prov)
        if occ is None or occ.get("by") != seat:
            return "NOT_OCCUPIED", f"{pid} — твоя полноценная провинция: судьба нужна только оккупированной"
    if d.behavior in NOT_ON_RUINS and is_ruined(prov, state.turn):
        return "RUINED", f"{pid} — пепелище до хода {prov.status['ruined_until']}: «{d.name}» нельзя"
    if order.to is not None:
        if d.behavior != RELEASE:
            release = ", ".join(f"{k.split(':')[-1]} ({x.name})" for k, x in ordered_dispositions(ruleset)
                                if x.behavior == RELEASE)
            return "INVALID", f"to (получатель) — только у судьбы {release or 'освобождения'}"
        to = state.players.get(order.to)
        if to is None or not to.alive or order.to == seat:
            shown = to.nick if to is not None and to.nick else order.to
            return "NO_RECIPIENT", f"получатель {shown!r} — не живой другой игрок"
    return None


def ordered_dispositions(ruleset: Ruleset) -> list[tuple[str, Any]]:
    """Dispositions in the order of their behaviours (annex, raze, raid, release), then by id."""
    order = {b: i for i, b in enumerate((ANNEX, RAZE, RAID, RELEASE))}
    return sorted(ruleset.dispositions.items(), key=lambda kv: (order.get(kv[1].behavior, 9), kv[0]))


def disposition_list(ruleset: Ruleset) -> str:
    return ", ".join(f"{k.split(':')[-1]} ({d.name})" for k, d in ordered_dispositions(ruleset))


# core 0.8.0 (docs/TRADE.md 6.3): Province.status mark {from, to, turn} of land got without a battle (a trade, a
# release), written only when an objective reads it (`hold_foreign` with `via: capture`); a capture removes it
TRANSFER_MARK = "transferred"


def transfer_marked(ruleset: Ruleset) -> bool:
    return any(o.kind == "hold_foreign" and o.params.get("via") == "capture" for o in ruleset.objectives.values())


# ---------------------------------------------------------------- resolution step 4b

class ConquestStep:
    """Step 4b of a resolution with conquest on (SPY_AND_CONQUEST.md 3, 4): fates of captured provinces, decisions
    on older occupations, claims, retreats, revolts, refunds of unused reserves and the builds filter."""

    def __init__(self, r: _Resolution):
        self.r = r
        self.s = r.s
        self.rs = r.rs
        self.turn = r.turn
        # pid -> {by, from, parts, status (at the start of the resolution)}: captures of steps 3-4
        self.captures: dict[str, dict[str, Any]] = {}
        # seat -> [(order, paid)] dispose orders whose price is held since step 1
        self.held: dict[str, list[tuple[Order, Counter[str]]]] = {}
        self.used: set[tuple[str, str]] = set()          # (seat, order id) of dispose orders carried out
        self.changes: list[dict[str, Any]] = []          # public ownership changes, from explicit events
        self.transferred: set[str] = set()               # core 0.8.0: provinces a trade gave in this resolution

    # -- step 1
    def hold(self, seat: str, order: Order, paid: Counter[str]) -> None:
        self.held.setdefault(seat, []).append((order, paid))

    def set_policy(self, seat: str, order: Order) -> None:
        player = self.s.players[seat]
        if player.conquest_policy != order.disposition:
            player.conquest_policy = order.disposition
            self.r.ev("policy_set", [seat], None, seat=seat, disposition=order.disposition)

    # -- steps 3-4
    def capture(self, pid: str, by: str | None, prev_owner: str | None, parts: Mapping[str, Mapping[str, int]],
                status: Mapping[str, Any]) -> None:
        self.captures[pid] = {"by": by, "from": prev_owner, "parts": copy.deepcopy(dict(parts)),
                              "status": copy.deepcopy(dict(status))}

    # -- step 4b
    def run(self) -> None:
        for pid in sorted(self.captures):
            self.fresh(pid, self.captures[pid])
        self.decisions()
        if self.s.trades:
            from . import trade     # core 0.8.0 (docs/TRADE.md 1.5): pending trades pass before the retreats
            trade.settle_all(self)
        self.r.apply_retreats()
        self.revolts()
        self.refunds()
        self.s.pending_builds = [b for b in self.s.pending_builds if self.s.provinces[b.province].owner == b.seat]

    def alive(self, seat: str | None) -> bool:
        return seat is not None and seat in self.s.players and self.s.players[seat].alive

    def key(self, seat: str | None) -> StatKey:
        return self.r.factions.get(seat)

    def own_land(self, seat: str, pid: str) -> bool:
        """Is `pid` the seat's own land for a free return: its start region or the province was taken from it."""
        occ = occupied(self.s.provinces[pid])
        return self.s.origins.get(pid) == seat or (occ is not None and occ.get("from") == seat)

    def fresh(self, pid: str, cap: dict[str, Any]) -> None:
        prov = self.s.provinces[pid]
        by, frm, before = cap["by"], cap["from"], cap["status"]
        self.drop_claims(by, pid, "returned")      # any return of the province to its claim holder ends the claim
        was_occ = before.get("occupied")
        was_full = was_occ is None and before.get("ruined_until", 0) <= self.turn
        if frm is None:
            self.captured(pid, frm, by, "free", self.final_status(prov))
            prov.status.pop("occupied", None)
            return
        if self.s.origins.get(pid) == by or (was_occ is not None and was_occ.get("from") == by):
            prov.status.pop("occupied", None)
            self.captured(pid, frm, by, "reclaimed", self.final_status(prov))
            if was_full and self.alive(frm):
                self.add_claim(frm, pid)        # the one who held it full lost it, as any loss of a full province
            return
        occ_from = was_occ["from"] if was_occ is not None else frm
        mode, done = "occupied", False
        directive = self.find_order(by, pid)
        if directive is not None:
            order, paid = directive
            d = self.rs.dispositions[order.disposition]
            if d.behavior in NOT_ON_RUINS and is_ruined(prov, self.turn):
                self.r.ev("dispose_failed", [by], pid, seat=by, disposition=order.disposition, reason="RUINED")
            else:
                self.used.add((by, order.id))
                self.captured(pid, frm, by, "directive", None)
                self.execute(pid, by, order.disposition, "directive", order.to, paid, occ_from, cap["parts"])
                done, mode = True, "directive"
        if not done:
            policy = self.s.players[by].conquest_policy if by in self.s.players else None
            if policy is not None and policy in self.rs.dispositions:
                d = self.rs.dispositions[policy]
                price = Counter(disposition_cost(self.rs, policy, prov, self.key(by)))
                stock = self.s.players[by].resources
                if d.behavior in NOT_ON_RUINS and is_ruined(prov, self.turn):
                    self.r.ev("policy_unaffordable", [by], pid, seat=by, disposition=policy, reason="ruined")
                elif any(stock.get(res, 0) < n for res, n in price.items()):
                    self.r.ev("policy_unaffordable", [by], pid, seat=by, disposition=policy, reason="resources",
                              cost=dict(price))
                else:
                    for res, n in price.items():
                        stock[res] -= n
                    self.captured(pid, frm, by, "policy", None)
                    self.execute(pid, by, policy, "policy", None, price, occ_from, cap["parts"])
                    done, mode = True, "policy"
        if not done:
            prov.status["occupied"] = {"by": by, "from": occ_from, "since": self.turn}
            self.captured(pid, frm, by, "occupied", STATUS_OCCUPIED)
        if was_full and self.alive(frm) and prov.owner != frm:
            self.add_claim(frm, pid)

    def final_status(self, prov: Province) -> str:
        if occupied(prov) is not None:
            return STATUS_OCCUPIED
        return STATUS_RUINED if is_ruined(prov, self.turn) else STATUS_FULL

    def captured(self, pid: str, frm: str | None, by: str | None, mode: str, status: str | None) -> None:
        """The capture event (full form: with `mode`); its status is filled in when the fate is known."""
        # a capture ends the mark «получено не боем» (docs/TRADE.md 6.3): the province now counts as taken
        self.s.provinces[pid].status.pop(TRANSFER_MARK, None)
        self.r.ev("captured", [frm, by], pid, **{"from": frm, "to": by}, mode=mode, status=status)
        self.changes.append({"province": pid, "from": frm, "to": by, "status": status})
        self._last = (self.r.events[-1], self.changes[-1])

    def _set_capture_status(self, status: str) -> None:
        event, change = self._last
        if event["status"] is None:
            event["status"] = status
            change["status"] = status

    def find_order(self, seat: str | None, pid: str) -> tuple[Order, Counter[str]] | None:
        for order, paid in self.held.get(seat or "", []):
            if order.province == pid and (seat, order.id) not in self.used:
                return order, paid
        return None

    def execute(self, pid: str, seat: str, did: str, source: str, to: str | None, paid: Counter[str],
                occ_from: str | None, parts: Mapping[str, Mapping[str, int]] | None) -> None:
        """Carry out a disposition of `seat` on its (just taken or occupied) province `pid`, paid already."""
        prov = self.s.provinces[pid]
        d = self.rs.dispositions[did]
        key = self.key(seat)
        loot: dict[str, int] = {}
        reason = None
        if d.loot:
            reason = loot_block(self.rs, prov, self.turn, key)
            if reason is None:
                loot = disposition_loot(self.rs, did, prov, key)
                if loot:
                    player = self.s.players[seat]
                    for res, n in loot.items():
                        player.resources[res] = player.resources.get(res, 0) + n
                    prov.status["looted_turn"] = self.turn
        owner_after: str | None = seat
        leaving = False
        if d.behavior == ANNEX:
            prov.status.pop("occupied", None)
        elif d.behavior == RAZE:
            prov.status.pop("occupied", None)
            prov.buildings = []
            self.s.pending_builds = [b for b in self.s.pending_builds if b.province != pid]
            prov.status["ruined_until"] = self.turn + int(d.params["ruin_turns"])
            drop_find_mark(prov, "ruined_until")
        elif d.behavior == RAID:
            owner_after = occ_from if self.alive(occ_from) else None
            prov.status.pop("occupied", None)
            prov.status["ravaged_until"] = self.turn + int(d.params["ravage_turns"])
            drop_find_mark(prov, "ravaged_until")
            leaving = True
        else:   # release
            owner_after = self.recipient(pid, seat, to, occ_from)
            back = owner_after is not None and (owner_after == occ_from or self.s.origins.get(pid) == owner_after)
            if owner_after is None or back:
                prov.status.pop("occupied", None)
            else:
                prov.status["occupied"] = {"by": owner_after, "from": occ_from, "since": self.turn}
            leaving = True
        if leaving:
            self.leave(pid, seat, parts)
            prov.owner = owner_after
            prov.units = {}
            if owner_after is not None:
                self.drop_claims(owner_after, pid, "returned")
            if d.behavior == RELEASE and owner_after is not None and transfer_marked(self.rs):
                # core 0.8.0 (docs/TRADE.md 6.3): a release to another seat is land got without a battle too
                prov.status[TRANSFER_MARK] = {"from": seat, "to": owner_after, "turn": self.turn}
        status = DISPOSITION_STATUS[d.behavior]
        if source != "decision":
            self._set_capture_status({ANNEX: STATUS_FULL, RAZE: STATUS_RUINED}.get(d.behavior, STATUS_RETURNED))
        event: dict[str, Any] = {"seat": seat, "disposition": did, "behavior": d.behavior, "cost": dict(paid),
                                 "loot": loot, "to": to, "owner_after": owner_after, "source": source,
                                 "status": status}
        if reason is not None:
            event["loot_reason"] = reason
        self.r.ev("disposition", [seat, owner_after], pid, **event)
        if owner_after != seat:
            self.changes.append({"province": pid, "from": seat, "to": owner_after, "status": status})

    def recipient(self, pid: str, seat: str, to: str | None, occ_from: str | None) -> str | None:
        """Who a released province goes to: `to` (alive, another seat), else the previous owner, else the origin,
        else nobody (neutral)."""
        for cand in (to, occ_from, self.s.origins.get(pid)):
            if cand is not None and cand != seat and self.alive(cand):
                return cand
        return None

    def leave(self, pid: str, seat: str, parts: Mapping[str, Mapping[str, int]] | None) -> None:
        """The taker's troops leave a province a raid or a release gave away: a fresh capture's parts each to its
        own source (else the first own province next to it); troops of an older occupation to the first own
        neighbour, full ones first (else they are lost)."""
        prov = self.s.provinces[pid]
        near = sorted(self.rs.map.adjacency[pid])
        if parts and sum(n for units in parts.values() for n in units.values()) == prov.unit_total():
            for origin, units in sorted(parts.items()):
                if units:
                    self.r.retreats.append((seat, [origin] + [n for n in near if n != origin], dict(units), pid,
                                            False))
        elif prov.units:
            self.r.retreats.append((seat, near, dict(prov.units), pid, True))

    def decisions(self) -> None:
        """Dispose orders on provinces the seat held occupied at the start of the resolution (by province id)."""
        todo = []
        for seat, items in sorted(self.held.items()):
            for order, paid in items:
                if (seat, order.id) in self.used:
                    continue
                prov = self.s.provinces[order.province]
                occ = occupied(prov)
                if prov.owner == seat and occ is not None and occ.get("by") == seat and order.province not in \
                        self.captures:
                    todo.append((order.province, seat, order, paid))
        for pid, seat, order, paid in sorted(todo, key=lambda t: (t[0], t[1])):
            if (seat, order.id) in self.used or occupied(self.s.provinces[pid]) is None:
                continue
            self.used.add((seat, order.id))
            occ_from = occupied(self.s.provinces[pid]).get("from")
            self.execute(pid, seat, order.disposition, "decision", order.to, paid, occ_from, None)

    def revolts(self) -> None:
        """An occupation held since before this resolution with no troops of the occupant left becomes neutral.
        Core 0.8.0 (docs/SPY_V2.md 4.5b): a sabotage strike there with `unrest_units` makes it revolt with up to that
        many units left after the battles; they are lost (no war points for them) and the strike's
        `revolt_garrison` stays as rebels without an owner (a revolt with no units at all is the old empty one)."""
        for pid, prov in sorted(self.s.provinces.items()):
            occ = occupied(prov)
            if occ is None or occ.get("by") != prov.owner or occ.get("since", self.turn) >= self.turn:
                continue
            mark = unrest_mark(prov, self.turn) if self.r.espionage is not None else None
            if mark is not None and 0 < prov.unit_total() <= int(mark["unrest_units"]):
                seat = prov.owner
                lost = dict(sorted((u, n) for u, n in prov.units.items() if n))
                rebels = dict(mark.get("revolt_garrison") or {})
                prov.owner = None
                prov.units = rebels
                prov.status.pop("occupied", None)
                self.r.ev("occupation_revolt", [seat], pid, seat=seat, reason="sabotage", lost=lost, rebels=rebels)
                self.changes.append({"province": pid, "from": seat, "to": None, "status": "revolt"})
                continue
            if prov.unit_total() == 0:
                seat = prov.owner
                prov.owner = None
                prov.units = {}
                prov.status.pop("occupied", None)
                self.r.ev("occupation_revolt", [seat], pid, seat=seat, reason="no_garrison")
                self.changes.append({"province": pid, "from": seat, "to": None, "status": "revolt"})

    def refunds(self) -> None:
        """Unused dispose reserves come back in full (no capture, a partner held the province, a failed fate)."""
        for seat, items in sorted(self.held.items()):
            player = self.s.players[seat]
            for order, paid in items:
                if (seat, order.id) not in self.used:
                    for res, n in paid.items():
                        player.resources[res] = player.resources.get(res, 0) + n

    # -- claims
    def add_claim(self, seat: str, pid: str) -> None:
        turns = int(self.rs.stat("conquest.claim_turns", self.key(seat)))
        if turns <= 0:
            return
        until = self.turn + turns
        claim = next((c for c in self.s.claims if c["seat"] == seat and c["province"] == pid), None)
        if claim is None:
            self.s.claims.append({"seat": seat, "province": pid, "until_turn": until})
        else:
            claim["until_turn"] = until
        self.r.ev("claim_created", [seat], pid, seat=seat, until_turn=until)

    def drop_claims(self, seat: str | None, pid: str | None, reason: str) -> None:
        keep = []
        for c in self.s.claims:
            if (seat is None or c["seat"] == seat) and (pid is None or c["province"] == pid):
                self.r.ev("claim_ended", [c["seat"]], c["province"], seat=c["seat"], reason=reason)
            else:
                keep.append(c)
        self.s.claims = keep


FIND_MARKS = "find_marks"   # Province.status note of the Ravage/Ashes a find set (shown only where seen)


def drop_find_mark(prov: Province, key: str) -> None:
    """A conquest disposition set `key` publicly: it is no longer a find's mark."""
    marks = prov.status.get(FIND_MARKS)
    if marks and key in marks:
        del marks[key]
        if not marks:
            del prov.status[FIND_MARKS]


def timers(r: _Resolution) -> None:
    """Step 7: ruins and ravage that end before the next turn are lifted, expired claims end, strikes are cleared."""
    s, turn = r.s, r.turn
    for pid, prov in sorted(s.provinces.items()):
        if not prov.status:
            continue
        if "ruined_until" in prov.status and prov.status["ruined_until"] <= turn + 1:
            del prov.status["ruined_until"]
            r.ev("ruin_ended", [prov.owner], pid, seat=prov.owner)
        if "ravaged_until" in prov.status and prov.status["ravaged_until"] <= turn + 1:
            del prov.status["ravaged_until"]
        prov.status.pop("sabotaged", None)
        marks = prov.status.get(FIND_MARKS)
        if marks is not None:   # stage 5: notes of marks a find set (engine/discoveries.py), dropped once stale
            for k in [k for k, v in marks.items() if prov.status.get(k) != v]:
                del marks[k]
            if not marks:
                del prov.status[FIND_MARKS]
    keep = []
    for c in s.claims:
        if c["until_turn"] <= turn:
            r.ev("claim_ended", [c["seat"]], c["province"], seat=c["seat"], reason="expired")
        else:
            keep.append(c)
    s.claims = keep


def eliminated(r: _Resolution, seats: list[str]) -> None:
    """Claims of eliminated seats end."""
    if not seats:
        return
    keep = []
    for c in r.s.claims:
        if c["seat"] in seats:
            r.ev("claim_ended", [c["seat"]], c["province"], seat=c["seat"], reason="eliminated")
        else:
            keep.append(c)
    r.s.claims = keep


def statuses_for(state: GameState, turn: int | None = None) -> dict[str, dict[str, Any]]:
    """Public statuses of provinces {pid: {occupied?, ruined_until?, ravaged_until?, income_mul?}} (no sabotage, no
    loot mark; a guard is shown by engine/discoveries.py)."""
    turn = state.turn if turn is None else turn
    out = {}
    for pid, prov in sorted(state.provinces.items()):
        entry = {}
        if occupied(prov) is not None:
            entry["occupied"] = dict(occupied(prov))
        if is_ruined(prov, turn):
            entry["ruined_until"] = prov.status["ruined_until"]
        if is_ravaged(prov, turn):
            entry["ravaged_until"] = prov.status["ravaged_until"]
        if prov.status.get("income_mul") is not None:
            entry["income_mul"] = prov.status["income_mul"]      # stage 5: a lasting multiplier (Abandoned land)
        if entry:
            out[pid] = entry
    return out
