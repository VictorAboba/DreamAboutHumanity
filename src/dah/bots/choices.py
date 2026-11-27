"""Stage-2 choices of the bots (TECH_AND_SETUP.md section 4): faction preferences, artifacts and research.

Pure functions of what the server sends (`GET /v1/setup`, `GET /v1/techs`), so the same code serves the HTTP bots and
the in-process simulator. Each bot type passes its style: a faction order, liked artifacts, a branch priority.
"""

from __future__ import annotations

import itertools
from typing import Any, Sequence

from dah.bots.view import short_id

MAX_POINTS_PICK = 4  # points mode: at most this many artifacts are considered together


def faction_prefs(setup: dict[str, Any], style: Sequence[str]) -> list[str]:
    """Every offered faction, best first: the style's order (short ids), then the rest in the scenario's order."""
    offered = [str(f["id"]) for f in setup.get("factions") or [] if isinstance(f, dict) and f.get("id")]
    by_short = {short_id(f): f for f in offered}
    first = [by_short[s] for s in style if s in by_short]
    return first + [f for f in offered if f not in first]


def artifact_value(artifact: dict[str, Any], likes: Sequence[str]) -> float:
    """Simple valuation: the pack's own `power`, +1 for an artifact the bot's style likes (earlier likes a little
    more)."""
    value = float(artifact.get("power") or 0)
    sid = short_id(str(artifact.get("id", "")))
    if sid in likes:
        value += 1.0 + (len(likes) - list(likes).index(sid)) * 0.01
    return value


def _fits(artifact: dict[str, Any], faction: str | None) -> bool:
    allowed = artifact.get("factions") or []
    return not allowed or faction is None or faction in allowed or short_id(faction) in map(short_id, allowed)


def pick_artifacts(setup: dict[str, Any], faction: str | None, likes: Sequence[str]) -> list[str] | None:
    """Draft: the best-valued offered artifact; points: the best-valued set within the budget (no exclusive pairs).
    Artifacts of another faction than `faction` (the bot's first preference) are skipped. None: nothing to choose."""
    mode = setup.get("artifacts_mode")
    offer = [a for a in setup.get("offer") or [] if isinstance(a, dict) and a.get("id") and _fits(a, faction)]
    if mode == "draft":
        if not offer:
            return None
        best = max(offer, key=lambda a: (artifact_value(a, likes), -offer.index(a)))
        return [str(best["id"])]
    if mode != "points":
        return None
    budget = int(setup.get("artifact_points") or 0)
    ranked = sorted(offer, key=lambda a: (-artifact_value(a, likes), str(a["id"])))
    best_set: tuple[float, int, list[str]] = (0.0, 0, [])
    for size in range(1, MAX_POINTS_PICK + 1):
        for combo in itertools.combinations(ranked, size):
            cost = sum(int(a.get("cost") or 0) for a in combo)
            if cost > budget or _exclusive(combo):
                continue
            value = sum(artifact_value(a, likes) for a in combo)
            if (value, -cost) > (best_set[0], -best_set[1]):
                best_set = (value, cost, [str(a["id"]) for a in combo])
    return sorted(best_set[2])


def _exclusive(combo: Sequence[dict[str, Any]]) -> bool:
    ids = {short_id(str(a["id"])) for a in combo}
    return any(short_id(str(x)) in ids for a in combo for x in a.get("exclusive_with") or [])


def pick_research(techs: dict[str, Any], branches: Sequence[str], first: Sequence[str] = ()) -> str | None:
    """The next tech to research: one of `first` (short ids, in order) that is available, else the available tech of
    the highest-priority branch, the higher tier first, then the cheaper. Branches not in `branches` come last."""
    if not techs.get("enabled"):
        return None
    available = [t for t in techs.get("techs") or [] if isinstance(t, dict) and t.get("status") == "available"]
    if not available:
        return None
    by_short = {short_id(str(t["id"])): t for t in available}
    for sid in first:
        if sid in by_short:
            return str(by_short[sid]["id"])
    rank = {b: i for i, b in enumerate(branches)}

    def key(t: dict[str, Any]) -> tuple[int, int, int, str]:
        return (rank.get(short_id(str(t.get("branch", ""))), len(rank)), -int(t.get("tier") or 1),
                int(t.get("cost") or 0), str(t["id"]))

    return str(min(available, key=key)["id"])
