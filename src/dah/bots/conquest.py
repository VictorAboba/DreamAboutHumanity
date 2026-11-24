"""Conquest of the bots (docs/SPY_AND_CONQUEST.md section 7): policy, fates of occupations, directives, garrisons.

Pure functions of a StateView; prices come from `dah.bots.view` (DISPOSITION_FOOD x CONQUEST_VALUE x the faction's
multiplier), the server stays the authority. Every function returns orders and the provisions they hold, so a bot
spends its free provisions in one pass (fates first, then missions, then recruits).
"""

from __future__ import annotations

from typing import Any, Callable

from dah.bots.view import Province, StateView

Order = dict[str, Any]
POLICY_MIN_FOOD = 15        # the policy is subjugate while free provisions are at least this
RAID_HORIZON = 2            # rush: an occupation not affordable within this many turns of food income is raided
SUBJUGATE, EXTERMINATE, RAID, LIBERATE = "subjugate", "exterminate", "raid", "liberate"
Reserved = dict[str, dict[str, int]]


def policy_order(view: StateView, food: int) -> Order | None:
    """subjugate while `food` (free provisions) >= POLICY_MIN_FOOD, else none; only when it changes."""
    if not view.conquest:
        return None
    want = SUBJUGATE if food >= POLICY_MIN_FOOD else None
    if want == view.conquest_policy:
        return None
    return {"kind": "policy", "disposition": want}


def ruined(province: Province) -> bool:
    return bool(province.status.get("ruined_until")) if province.status else False


def dispose(province: Province, disposition: str, to: str | None = None) -> Order:
    order: Order = {"kind": "dispose", "province": province.id, "disposition": disposition}
    if to is not None:
        order["to"] = to
    return order


def occupation_orders(view: StateView, food: int, *, raid_short: bool = False,
                      liberate_for: Callable[[Province], str | None] | None = None) -> tuple[list[Order], int]:
    """Fates of the own occupations: `liberate_for(p)` names a seat to give it back to (diplomat: a partner's own
    land); else subjugate what `food` pays for, the dearest first; with `raid_short`, raid an occupation that `food`
    plus RAID_HORIZON turns of food income cannot pay for. The rest stays occupied (held by a garrison). Returns
    (orders, provisions held)."""
    orders: list[Order] = []
    spent = 0
    income = max(0, (view.net_income or {}).get("provisions", 0))
    for prov in sorted(view.own_occupations(), key=lambda p: (-view.disposition_food(SUBJUGATE, p), p.id)):
        to = liberate_for(prov) if liberate_for is not None else None
        if to is not None:
            orders.append(dispose(prov, LIBERATE, to))
            continue
        if ruined(prov):
            continue
        cost = view.disposition_food(SUBJUGATE, prov)
        if cost <= food - spent:
            orders.append(dispose(prov, SUBJUGATE))
            spent += cost
        elif raid_short and food - spent + RAID_HORIZON * income < cost:
            orders.append(dispose(prov, RAID))
    return orders, spent


def capital_directives(view: StateView, moves: list[Order], food: int) -> tuple[list[Order], int]:
    """rush: an enemy capital it attacks gets a directive: subjugate if `food` pays for it, else exterminate."""
    orders: list[Order] = []
    spent = 0
    for move in moves:
        if move.get("feint"):
            continue
        target = view.provinces.get(str(move.get("to")))
        if target is None or target.kind != "capital" or target.owner is None or view.is_mine(target):
            continue
        cost = view.disposition_food(SUBJUGATE, target)
        if not ruined(target) and cost <= food - spent:
            orders.append(dispose(target, SUBJUGATE))
            spent += cost
        else:
            orders.append(dispose(target, EXTERMINATE))
    return orders, spent


def occupation_garrison(view: StateView) -> Reserved:
    """One unit stays in every own occupation (an occupation without a garrison revolts)."""
    keep: Reserved = {}
    for prov in view.own_occupations():
        if prov.units:
            unit = sorted(prov.units)[0]
            keep[prov.id] = {unit: 1}
    return keep
