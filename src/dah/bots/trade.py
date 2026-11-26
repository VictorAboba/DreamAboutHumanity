"""Trade of the bots (docs/TRADE.md 12.1): pure decisions over a StateView plus the bot's TradeMemory, so the same code
plays over HTTP and in the simulator.

- Value of a resource: `1 + max(0, (need - stock) / need)`, need = two turns of it (provisions: the army's upkeep;
  alloy: NEED_ALLOY; others NEED_OTHER). Below its floor (provisions: FOOD_FLOOR_TURNS turns of upkeep; alloy:
  ALLOY_FLOOR) a bot gives nothing. The bot's `margin` (0.05-0.25, by its seed) is added to what it asks for.
- builder: accepts resource trades worth at least what it gives (floors kept); accepts a province next to its land
  with an own army beside it, provisions for the subjugation and a price of at most PROVINCE_MAX_PRICE; never gives
  its own; once a turn (negotiation_1), with a resource above SURPLUS and another below SHORT, offers a pact partner
  OFFER_GIVE of the surplus for OFFER_WANT of the shortage; keeps every promise.
- diplomat: as the builder, and its offers are «half now, half by T+LATER_TURNS» (`later`); keeps its promises except
  those due in the last BREAK_TURNS turns (the baseline of broken promises, like its pact breaks); buys a province of
  its open `hold_type` objective from a pact partner for PROVINCE_PRICE alloy; sells a province to a pact partner for
  at least PROVINCE_PRICE (never a capital or a province with a build).
- With a Shade (builder, diplomat): once a turn in negotiation_1 it offers a report of the last recon step on a seat
  to a neighbour of that seat for REPORT_PRICES alloy (in negotiation_1 the buyer's treasury is not yet held by
  orders); a builder or diplomat buys a report on a neighbour for at most REPORT_MAX_PRICE. An accept the free stock
  cannot pay is turned into a reject (no offer is left to lapse).
- rush: never offers, never promises; accepts only a clear gain: resources worth >= RUSH_GAIN x what it gives, or a
  fresh report on a neighbour for at most RUSH_REPORT_MAX_PRICE alloy.
- Balance core 0.8.0 (docs/TRADE.md «Баланс core 0.8.0»): the thresholds below were set by the simulator (spec 12.1
  started from surplus 60 / short 15, 20 for 15, alloy floor 15, reports 8-10 alloy).
- passive: nothing (offers to it lapse).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping

from dah.bots import score as bs
from dah.bots.client import ApiError
from dah.bots.view import BUILD_RESOURCE, FOOD_RESOURCE, Province, StateView, short_id

log = logging.getLogger(__name__)

FOOD_FLOOR_TURNS = 3
ALLOY_FLOOR = 12
NEED_ALLOY = 20
NEED_OTHER = 10
SURPLUS = 35
SHORT = 20
OFFER_GIVE = 14
OFFER_WANT = 8
PROVINCE_MAX_PRICE = 15     # the builder pays at most this (value) for a province
PROVINCE_PRICE = 12         # alloy: the diplomat's price for a province of its hold_type (simulator: --trade-knob)
REPORT_PRICES = (4, 5, 6)
REPORT_MAX_PRICE = 6
RUSH_GAIN = 1.3
RUSH_REPORT_MAX_PRICE = 6   # alloy: the rush buys a fresh report on a neighbour for at most this (0: never)
LATER_TURNS = 2
BREAK_TURNS = 2
POLICIES = ("builder", "diplomat", "rush")


@dataclass
class TradeMemory:
    """What a bot keeps of its trades between phases."""

    paid: set[str] = field(default_factory=set)          # trade ids whose promise of ours is settled (kept or broken)
    offered_turn: int | None = None                      # last turn a resource offer was made
    sold: set[str] = field(default_factory=set)          # report ids offered for sale
    asked: set[str] = field(default_factory=set)         # provinces asked for
    report_turn: int | None = None                       # last turn a report was offered
    promises: Counter[str] = field(default_factory=Counter)   # kept | broken (the simulator's tally)


# ---------------------------------------------------------------- values

def goods(side: Mapping[str, Any] | None) -> tuple[dict[str, int], list[str], list[str]]:
    """(resources by short id, provinces, reports) of one side of a trade as get_state shows it."""
    side = side or {}
    res = {short_id(str(r)): int(n) for r, n in (side.get("resources") or {}).items()}
    return res, [str(p) for p in side.get("provinces") or []], [str(r) for r in side.get("reports") or []]


def need(view: StateView, res: str) -> int:
    if res == FOOD_RESOURCE:
        return max(1, 2 * bs.upkeep(view))
    return NEED_ALLOY if res == BUILD_RESOURCE else NEED_OTHER


def floor(view: StateView, res: str) -> int:
    if res == FOOD_RESOURCE:
        return FOOD_FLOOR_TURNS * bs.upkeep(view)
    return ALLOY_FLOOR if res == BUILD_RESOURCE else 0


def value(view: StateView, res: str) -> float:
    n = need(view, res)
    return 1 + max(0.0, (n - view.resource(res)) / n)


def worth(view: StateView, amounts: Mapping[str, int]) -> float:
    return sum(n * value(view, r) for r, n in amounts.items())


def keeps_floor(view: StateView, pay: Mapping[str, int]) -> bool:
    return all(view.resource(r) - n >= floor(view, r) for r, n in pay.items())


def seat_of(view: StateView, nick: Any) -> str:
    return view.nicks.get(str(nick), str(nick))


def partners(view: StateView) -> list[str]:
    out = []
    for pact in view.pacts:
        seats = [str(s) for s in pact.get("seats", ())]
        if view.me & set(seats):
            out += [s for s in seats if s not in view.me]
    living = set(view.players) if view.players else set(out)
    return sorted(set(out) & living)


def borders(view: StateView, seat: str) -> bool:
    """`seat` owns a province next to one of ours."""
    return any(n.owner == seat for p in view.own_provinces() for n in view.neighbors(p))


def neighbours_of(view: StateView, seat: str) -> list[str]:
    """Living seats other than us and `seat` that own a province next to one of `seat`'s."""
    out = set()
    for p in view.provinces.values():
        if p.owner != seat:
            continue
        for n in view.neighbors(p):
            if n.owner and n.owner != seat and not view.is_mine(n):
                out.add(n.owner)
    living = set(view.players) if view.players else out
    return sorted(out & living)


# ---------------------------------------------------------------- answers

def province_buyable(view: StateView, prov: Province | None, pay: Mapping[str, int]) -> bool:
    """A province offered to us: next to our land, an own army beside it, provisions for its subjugation (beyond the
    floor) and a price of at most PROVINCE_MAX_PRICE."""
    if prov is None or view.is_mine(prov) or sum(pay.values()) > PROVINCE_MAX_PRICE:
        return False
    beside = [n for n in view.neighbors(prov) if view.is_mine(n)]
    if not any(n.unit_count for n in beside):
        return False
    food = view.resource(FOOD_RESOURCE) - pay.get(FOOD_RESOURCE, 0) - floor(view, FOOD_RESOURCE)
    return food >= view.disposition_food("subjugate", prov)


def province_sellable(view: StateView, prov: Province | None) -> bool:
    """The diplomat's own province it may sell: not a capital, no build in it, not its last province."""
    return (prov is not None and view.is_mine(prov) and prov.kind != "capital" and not prov.building_now
            and len(view.own_provinces()) > 2)


def accepts(view: StateView, trade: Mapping[str, Any], policy: str, trusts_later: bool = True) -> bool:
    """Whether the bot of `policy` accepts an offer made to it (`trade` as get_state shows it)."""
    got, got_p, got_r = goods(trade.get("give"))
    pay, pay_p, pay_r = goods(trade.get("want"))
    later = trade.get("later") or {}
    promised, _, _ = goods(later.get("give"))       # the proposer's promise to us
    owed, owed_p, _ = goods(later.get("want"))      # our promise
    other = seat_of(view, trade.get("with"))
    if pay_r or owed_p or not keeps_floor(view, pay):
        return False
    if policy == "rush":
        if got_r and not (got or got_p or pay_p or owed or promised):
            # a cheap fresh report on a neighbour is its clear gain too (balance core 0.8.0, docs/TRADE.md)
            heads = trade.get("heads") or {}
            return (not set(pay) - {BUILD_RESOURCE} and sum(pay.values()) <= RUSH_REPORT_MAX_PRICE
                    and all(report_useful(view, heads.get(r)) for r in got_r))
        if got_p or got_r or pay_p or owed:
            return False
        return worth(view, got) > 0 and worth(view, got) >= RUSH_GAIN * worth(view, pay)
    if policy not in POLICIES:
        return False
    if pay_p:
        if policy != "diplomat" or got_p or got_r or other not in partners(view):
            return False
        return all(province_sellable(view, view.provinces.get(p)) for p in pay_p) and \
            sum(got.values()) >= PROVINCE_PRICE * len(pay_p)
    if got_p:
        return all(province_buyable(view, view.provinces.get(p), pay) for p in got_p)
    if got_r:
        heads = trade.get("heads") or {}
        if set(pay) - {BUILD_RESOURCE} or sum(pay.values()) > REPORT_MAX_PRICE:
            return False
        return all(report_useful(view, heads.get(r)) for r in got_r)
    gain = worth(view, got) + (worth(view, promised) if trusts_later else 0.0)
    return gain > 0 and gain >= worth(view, pay) + worth(view, owed)


def report_useful(view: StateView, head: Mapping[str, Any] | None) -> bool:
    """A report head {mission, target, turn} about a neighbour (a seat, or a province next to our land)."""
    if not head or head.get("turn") is None or int(head["turn"]) < view.turn - 1:
        return False
    target = str(head.get("target"))
    prov = view.provinces.get(target)
    if prov is not None:
        return any(view.is_mine(n) for n in view.neighbors(prov)) and not view.is_mine(prov)
    return target not in view.me and borders(view, target)


def answer(bot: Any, view: StateView, api: Any) -> int:
    """Answer every open offer made to the bot; returns how many were answered."""
    policy = bot.trade_policy
    done = 0
    for t in view.trades or ():
        if t.get("mine") or t.get("status") != "proposed":
            continue
        decision = "accept" if accepts(view, t, policy, policy != "rush") else "reject"
        try:
            api.respond_trade(str(t["id"]), decision)
            done += 1
        except ApiError as exc:     # an accept the free stock does not pay (the rest is reserved): reject it
            log.info("bot %s: answer to %s refused: %s", bot.seat, t.get("id"), exc)
            if decision == "accept":
                try:
                    api.respond_trade(str(t["id"]), "reject")
                    done += 1
                except ApiError as again:
                    log.info("bot %s: reject of %s refused: %s", bot.seat, t.get("id"), again)
    return done


# ---------------------------------------------------------------- promises and offers

def keep_promises(bot: Any, view: StateView, api: Any) -> None:
    """Our promises (`later`) of done trades: paid with a gift from the turn after the trade (by its due turn at the
    latest); the diplomat breaks those due in the last BREAK_TURNS turns."""
    mem: TradeMemory = bot.trade_mem
    for t in view.trades or ():
        later = t.get("later") or {}
        if t.get("status") != "done" or not later or t["id"] in mem.paid:
            continue
        mine, _, _ = goods(later.get("give") if t.get("mine") else later.get("want"))
        if not mine:
            continue
        due = int(later.get("by_turn") or 0)
        if view.turn < int(t.get("settled_turn") or view.turn) + 1 and view.turn < due:
            continue
        if bot.trade_policy == "diplomat" and view.turn_limit and due > view.turn_limit - BREAK_TURNS:
            mem.paid.add(t["id"])
            mem.promises["broken"] += 1
            continue
        if view.turn > due:
            mem.paid.add(t["id"])
            mem.promises["broken"] += 1
            continue
        try:
            api.send_gift(seat_of(view, t.get("with")), {view.qualify(r): n for r, n in mine.items()})
        except ApiError as exc:     # reserved or the gift limit: try again next phase, until the due turn
            log.info("bot %s: promise of %s not paid yet: %s", bot.seat, t["id"], exc)
            continue
        mem.paid.add(t["id"])
        mem.promises["kept"] += 1


def _open_to(view: StateView, seat: str) -> bool:
    return any(t.get("mine") and t.get("status") == "proposed" and seat_of(view, t.get("with")) == seat
               for t in view.trades or ())


def resource_offer(bot: Any, view: StateView) -> tuple[str, dict[str, Any]] | None:
    """(partner, propose_trade kwargs) of the once-a-turn surplus offer, or None."""
    res = sorted(view.resources)
    surplus = [r for r in res if view.resource(r) > SURPLUS and view.resource(r) - OFFER_GIVE >= floor(view, r)]
    short = [r for r in res if view.resource(r) < SHORT and r not in surplus]
    if not surplus or not short:
        return None
    to = next((p for p in partners(view) if not _open_to(view, p)), None)
    if to is None:
        return None
    give_r, want_r = surplus[0], short[0]
    want = {view.qualify(want_r): int(round(OFFER_WANT * (1 + bot.margin)))}
    if bot.trade_policy == "diplomat" and view.turn_limit and view.turn + LATER_TURNS <= view.turn_limit:
        half = OFFER_GIVE // 2
        return to, {"give": {view.qualify(give_r): OFFER_GIVE - half}, "want": want,
                    "later": {"give": {view.qualify(give_r): half}, "by_turn": view.turn + LATER_TURNS}}
    return to, {"give": {view.qualify(give_r): OFFER_GIVE}, "want": want}


def province_offer(bot: Any, view: StateView) -> tuple[str, dict[str, Any]] | None:
    """The diplomat's purchase: a partner's province of its open hold_type objective next to its land."""
    types = [short_id(str((o.get("params") or {}).get("type", ""))) for o in view.open_objectives()
             if o.get("kind") == "hold_type"]
    if not types or view.resource(BUILD_RESOURCE) - PROVINCE_PRICE < ALLOY_FLOOR:
        return None
    friends = set(partners(view))
    for pid, prov in sorted(view.provinces.items()):
        if (prov.kind not in types or prov.owner not in friends or pid in bot.trade_mem.asked
                or prov.kind == "capital" or _open_to(view, prov.owner)):
            continue
        if not any(view.is_mine(n) for n in view.neighbors(prov)):
            continue
        bot.trade_mem.asked.add(pid)
        return prov.owner, {"give": {view.qualify(BUILD_RESOURCE): PROVINCE_PRICE}, "want": {"provinces": [pid]}}
    return None


def report_offer(bot: Any, view: StateView) -> tuple[str, dict[str, Any]] | None:
    """A fresh own report on a seat (of the last recon step), offered to a neighbour of that seat in negotiation_1,
    while the buyer's treasury is not yet held by orders (once a turn, each report once)."""
    mem: TradeMemory = bot.trade_mem
    if view.espionage is None or mem.report_turn == view.turn:
        return None
    for rid, (turn, _mission, target) in sorted(bot.memory.reports.items()):
        if turn != view.turn - 1 or rid in mem.sold:
            continue
        prov = view.provinces.get(target)
        seat = prov.owner if prov is not None else target
        if not seat or seat in view.me:
            continue
        buyers = [s for s in neighbours_of(view, seat) if s not in bot.memory.hostile and not _open_to(view, s)]
        if not buyers:
            continue
        friends = set(partners(view))
        buyer = next((s for s in buyers if s in friends), buyers[0])
        mem.sold.add(rid)
        mem.report_turn = view.turn
        price = bot.trade_rng.choice(REPORT_PRICES)
        return buyer, {"give": {"reports": [rid]}, "want": {view.qualify(BUILD_RESOURCE): price}}
    return None


def play(bot: Any, view: StateView, api: Any) -> None:
    """A negotiation phase: answer offers, pay promises, then make at most one offer of each kind."""
    answer(bot, view, api)
    if bot.trade_policy not in ("builder", "diplomat"):
        return
    keep_promises(bot, view, api)
    offers = []
    if view.phase == "negotiation_1" and bot.trade_mem.offered_turn != view.turn:
        bot.trade_mem.offered_turn = view.turn
        offers.append(resource_offer(bot, view))
        if bot.trade_policy == "diplomat":
            offers.append(province_offer(bot, view))
        offers.append(report_offer(bot, view))
    sent: set[str] = set()
    for offer in offers:
        if offer is None or offer[0] in sent:
            continue
        to, kwargs = offer
        try:
            api.propose_trade(to, **kwargs)
            sent.add(to)
        except ApiError as exc:     # OFFER_EXISTS, limits, a stock the reserve holds: no offer this time
            log.info("bot %s: offer to %s refused: %s", bot.seat, to, exc)
