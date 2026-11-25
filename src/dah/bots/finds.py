"""Stage 5 (docs/ABANDONED_LANDS.md 14.1, CONCEPT decision 25): the Abandoned lands for the bots.

Pure functions of a StateView (and of what a bot remembers), shared by the bot types; each type passes its style:

- exploring: an Abandoned province that may still hold a hidden find (not the ruins, never held by anyone the bot saw,
  no guard, no find it knows to be spent) is entered only by an army of at least `min_army` units (decision 25: any
  army sets a find off and a hindrance can wipe a small one, so the bots never scout with one or two units; the
  `scout` bot of the simulator does exactly that, to show it does not pay). On turns 1-5 such an empty province gets
  a small bonus on the attack estimate (free land while it is still free);
- guards: the ruins' guard and the neutrals of a triggered find are fought only with a surer estimate (a negative
  bonus: the threshold plus `guard_margin`, plus `lair_margin` for a lair that grows), never against units the fog
  hides;
- mixed finds (`choose_find`, by the rank on the visible score): last place, or more than 3 points behind the leader,
  takes the choice with the highest EV; a leader more than 3 points ahead of every other seat the least risky choice
  with EV >= 0; the others the highest EV with risk <= 4.5; else the default. The passive bot gives no order (the default applies).
"""

from __future__ import annotations

from typing import Any, Mapping

from dah.bots.tactics import ArmyOk
from dah.bots.view import Province, StateView

EARLY_TURNS = 5          # an empty Abandoned province gets EARLY_BONUS on turns 1..EARLY_TURNS
EARLY_BONUS = 0.05
BEHIND_POINTS = 3        # more than this behind the leader: the bot gambles; this far ahead of all: it plays safe
RISK_CAP = 4.5           # the middle of the table takes no choice riskier than this
Order = dict[str, Any]


class FindMemory:
    """Abandoned provinces some seat has held (owners are public): a find there, if any, has gone off."""

    def __init__(self) -> None:
        self.entered: set[str] = set()

    def absorb(self, view: StateView) -> None:
        for p in view.provinces.values():
            if p.abandoned and p.owner is not None:
                self.entered.add(p.id)


def may_hold_find(p: Province, memory: FindMemory) -> bool:
    """An Abandoned province where entering may still set off a hidden find (as far as the seat can tell)."""
    if not p.abandoned or p.ruins or p.owner is not None or p.id in memory.entered or p.guard is not None:
        return False
    return p.discovery is None or p.discovery.get("state") == "hidden"


def army_ok(memory: FindMemory, min_army: int) -> ArmyOk:
    """Armies below `min_army` units keep out of Abandoned provinces that may hold a find; a guard is fought only
    when the fog shows its units (and with `min_army` units too)."""
    def ok(source: Province, target: Province, free: dict[str, int]) -> bool:
        if may_hold_find(target, memory):
            return sum(free.values()) >= min_army
        if target.guard is not None and target.owner is None:
            return target.units is not None and sum(free.values()) >= min_army
        return True
    return ok


def attack_bonus(view: StateView, memory: FindMemory, guard_margin: float, lair_margin: float) -> dict[str, float]:
    """Province id -> bonus on the attack estimate for the Abandoned provinces next to the seat's land."""
    out: dict[str, float] = {}
    for own in view.own_provinces():
        for p in view.neighbors(own):
            if p.id in out or not p.abandoned or p.owner is not None:
                continue
            if p.guard is not None:
                out[p.id] = -(lair_margin if p.guard.get("lair") else guard_margin)
            elif view.turn <= EARLY_TURNS and may_hold_find(p, memory) and not p.unit_count:
                out[p.id] = EARLY_BONUS
    return out


def merge_bonus(*parts: Mapping[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in parts:
        for pid, b in part.items():
            out[pid] = out.get(pid, 0.0) + b
    return out


def rank(view: StateView) -> str:
    """`behind` (below every other seat, or more than BEHIND_POINTS behind one), `leader` (more than BEHIND_POINTS
    ahead of every other: a lead worth keeping) or `middle`. Each other seat is compared on what the fog shows of its score (`score_known`: the sources it gives, the
    own score on the same sources), else on the public totals."""
    me = view.seat or next(iter(sorted(view.me)), "")
    living = set(view.players) if view.players else set(view.scores)
    diffs = []
    for seat, theirs in view.scores.items():
        if seat == me or seat not in living:
            continue
        known = view.score_known.get(seat)
        if known and view.score_parts is not None:
            parts = {k: v for k, v in known.items() if v is not None}
            diffs.append(sum(view.score_parts.get(k, 0) for k in parts) - sum(parts.values()))
        else:
            diffs.append(view.scores.get(me, 0) - theirs)
    if not diffs:
        return "leader"
    if max(diffs) < 0 or min(diffs) < -BEHIND_POINTS:
        return "behind"
    return "leader" if min(diffs) > BEHIND_POINTS else "middle"


def choose_find(view: StateView, entry: Mapping[str, Any]) -> str | None:
    """The choice for one pending mixed find (`StateView.pending_finds` entry), by the seat's rank."""
    choices = [c for c in entry.get("choices") or [] if isinstance(c, Mapping) and c.get("id")]
    if not choices:
        return None
    default = entry.get("default") or choices[0]["id"]

    def ev(c: Mapping[str, Any]) -> float:
        return float(c.get("ev") or 0.0)

    def risk(c: Mapping[str, Any]) -> float:
        return float(c.get("risk") or 0.0)

    where = rank(view)
    if where == "behind":
        pick = max(choices, key=lambda c: (ev(c), c["id"] == default))
    elif where == "leader":
        safe = [c for c in choices if ev(c) >= 0]
        pick = min(safe, key=lambda c: (risk(c), -ev(c), c["id"] != default)) if safe else None
    else:
        tame = [c for c in choices if risk(c) <= RISK_CAP]
        pick = max(tame, key=lambda c: (ev(c), c["id"] == default)) if tame else None
    return str(pick["id"]) if pick is not None else str(default)


def discover_orders(view: StateView) -> list[Order]:
    """A `discover` order for every own pending mixed find (the choice by `choose_find`)."""
    out = []
    for entry in view.pending_finds:
        choice = choose_find(view, entry)
        if choice:
            out.append({"kind": "discover", "province": str(entry["province"]), "choice": choice})
    return out


def sweep_targets(view: StateView, memory: FindMemory) -> list[Province]:
    """Abandoned provinces next to the seat's land that may hold a hidden find (the scout's targets), by id."""
    seen: dict[str, Province] = {}
    for own in view.own_provinces():
        for p in view.neighbors(own):
            if may_hold_find(p, memory) and not p.unit_count:
                seen[p.id] = p
    return [seen[k] for k in sorted(seen)]
