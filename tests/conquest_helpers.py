"""Shared helpers for the stage-3 conquest tests (fixture pack tests/packs/t3, docs/SPY_AND_CONQUEST.md 3).

Map `tri`: capitals A1, B1, C1 (a triangle), forges A2, B2, C2 (a triangle), the neutral agri M next to every forge;
A1-A2, B1-B2, C1-C2. `board` puts seat X on X1 and X2 whatever the seeded start deal was, so tests read like the map.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dah.engine import GameState, Ruleset, SeatOrders, build_ruleset, issue_order_id, new_game_state, submit_orders

PACK = Path(__file__).parent / "packs" / "t3"
PACKS = [str(PACK)]
MIL = "t3:militia"
FACTIONS = {"A": "iron", "B": "iron", "C": "ash"}


def ruleset(scenario: str = "tri_conquest", systems: dict[str, Any] | None = None) -> Ruleset:
    return build_ruleset(PACKS, scenario, systems)


def board(rs: Ruleset, units: dict[str, dict[str, int]] | None = None, seed: int = 1) -> GameState:
    """Seats A, B, C on X1 (capital) and X2 (forge); M neutral. Units: `units` per province (default: 3 militia in
    each capital, nothing elsewhere). Resources: alloy 30, provisions 30 each."""
    st = new_game_state(rs, [{"seat": s, "nick": f"Ник{s}", "faction": FACTIONS[s]} for s in "ABC"], seed)
    for pid, prov in st.provinces.items():
        prov.owner = pid[0] if pid[0] in "ABC" else None
        prov.units = {}
        prov.buildings = []
    for seat, player in st.players.items():
        player.resources = {"t3:alloy": 30, "t3:provisions": 30, "t3:creed": 0}
        if player.home is not None:
            player.home = f"{seat}1"
    if st.origins:
        st.origins = {pid: pid[0] for pid in st.provinces if pid[0] in "ABC"}
    for pid, u in (units if units is not None else {f"{s}1": {MIL: 3} for s in "ABC"}).items():
        st.provinces[pid].units = {k if ":" in k else f"t3:{k}": n for k, n in u.items()}
    return st


def book(st: GameState, rs: Ruleset, seat: str, items: list[dict[str, Any]], phase: str = "actions",
         base: SeatOrders | None = None) -> SeatOrders:
    res = submit_orders(st, rs, seat, base or SeatOrders(), items, phase,
                        id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def mv(src: str, dst: str, n: int = 3) -> dict[str, Any]:
    return {"kind": "move", "from": src, "to": dst, "units": {"militia": n}}


def dispose(province: str, disposition: str, to: str | None = None) -> dict[str, Any]:
    out = {"kind": "dispose", "province": province, "disposition": disposition}
    if to is not None:
        out["to"] = to
    return out


def kinds(report: Any, kind: str) -> list[dict[str, Any]]:
    return [e for e in report.events if e["kind"] == kind]


def occupy(st: GameState, pid: str, by: str, frm: str, since: int = 0, units: int = 1) -> None:
    """Make `pid` an occupation of `by` (taken from `frm`) held since turn `since`, with `units` militia."""
    prov = st.provinces[pid]
    prov.owner = by
    prov.units = {MIL: units} if units else {}
    prov.status["occupied"] = {"by": by, "from": frm, "since": since}
