"""Setup phase before turn 1 (TECH_AND_SETUP.md section 2): faction preferences, artifact draft/points, defaults.

Pure and deterministic: offers and conflicts are decided by `rng.stream(seed, 0, "setup", ...)`, so the same
config, seed and choices always give the same result (the game journals the result; replay rebuilds turn 1 from it).

Setup config (game config `setup`, from the scenario's `setup` with the host's overrides):
    {unique_factions: bool, artifacts: draft|points|none, artifact_offer: N, artifact_points: N,
     artifact_pool: [full ids] (empty: every artifact of the ruleset)}
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .content import ARTIFACT_MODES, DEFAULT_SETUP, Ruleset
from .rng import stream

DRAFT_TRIES = 64


class SetupChoiceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def setup_config(ruleset: Ruleset, artifacts: str | None = None, offer: int | None = None,
                 points: int | None = None) -> dict[str, Any] | None:
    """The game's setup config: the scenario's `setup` with the host's overrides, or None (no setup phase).

    A host override of the artifact mode (other than "none") turns a setup on for a scenario without one."""
    base = ruleset.scenario.setup
    if base is None and (artifacts in (None, "none")):
        return None
    cfg = {**DEFAULT_SETUP, **(base or {})}
    if base is None:
        cfg["unique_factions"] = True
    if artifacts is not None:
        if artifacts not in ARTIFACT_MODES:
            raise ValueError(f"artifacts must be one of {', '.join(ARTIFACT_MODES)}")
        cfg["artifacts"] = artifacts
    if offer is not None:
        if not 1 <= int(offer) <= 10:
            raise ValueError("artifact offer must be 1..10")
        cfg["artifact_offer"] = int(offer)
    if points is not None:
        if not 0 <= int(points) <= 50:
            raise ValueError("artifact points must be 0..50")
        cfg["artifact_points"] = int(points)
    cfg["artifact_pool"] = list(cfg.get("artifact_pool") or ())
    return cfg


def artifact_pool(ruleset: Ruleset, setup: Mapping[str, Any]) -> list[str]:
    """Artifacts that may be offered or bought: the scenario's pool (or all), only those some allowed faction may
    hold, sorted by id."""
    pool = [a for a in (setup.get("artifact_pool") or ruleset.artifacts) if a in ruleset.artifacts]
    allowed = set(ruleset.scenario.factions)
    return sorted(a for a in pool if not ruleset.artifacts[a].factions or allowed & set(ruleset.artifacts[a].factions))


def draft_offers(ruleset: Ruleset, setup: Mapping[str, Any], seats: Sequence[str],
                 seed: int | str) -> dict[str, list[str]]:
    """Draft: each seat is offered `artifact_offer` different artifacts. Offers do not overlap while the pool is
    big enough; among DRAFT_TRIES seeded deals the one with the smallest spread of total `power` between seats is
    taken (the first such deal on a tie)."""
    seats = sorted(seats)
    if setup.get("artifacts") != "draft":
        return {s: [] for s in seats}
    pool = artifact_pool(ruleset, setup)
    # Offers are dealt before factions are known: an artifact only some factions may hold would be a dead slot for
    # the others (and count at full power in the balance). Deal from the artifacts every faction of the scenario may
    # hold, while they are enough for one offer.
    neutral = [a for a in pool if _fits_all(ruleset, a)]
    if len(neutral) >= int(setup.get("artifact_offer", 3)):
        pool = neutral
    k = min(int(setup.get("artifact_offer", 3)), len(pool))
    if not seats or k == 0:
        return {s: [] for s in seats}
    rng = stream(seed, 0, "setup", "draft")
    best: tuple[float, dict[str, list[str]]] | None = None
    for _ in range(DRAFT_TRIES):
        order = list(pool)
        rng.shuffle(order)
        deal = {}
        for i, seat in enumerate(seats):
            deal[seat] = sorted(order[(i * k + j) % len(order)] for j in range(k))
        totals = [sum(ruleset.artifacts[a].power for a in offer) for offer in deal.values()]
        spread = max(totals) - min(totals)
        if best is None or spread < best[0]:
            best = (spread, deal)
        if spread == 0:
            break
    assert best is not None
    return best[1]


def check_faction_prefs(ruleset: Ruleset, prefs: Any) -> list[str]:
    """Faction preference list (ids, short ids or names; one string counts as a list of one) -> full ids."""
    if isinstance(prefs, str):
        prefs = [prefs]
    if not isinstance(prefs, list) or not prefs:
        raise SetupChoiceError("INVALID", "factions — список фракций по предпочтению (первая — желанная)")
    allowed = list(ruleset.scenario.factions)
    out: list[str] = []
    for ref in prefs:
        full = _faction_ref(ruleset, ref)
        if full is None:
            names = ", ".join(f"{f.split(':')[-1]} «{ruleset.factions[f].name}»" for f in allowed)
            raise SetupChoiceError("UNKNOWN_ID", f"фракция {str(ref)[:40]!r} недоступна в этой партии; есть: {names}")
        if full not in out:
            out.append(full)
    return out


def _faction_ref(ruleset: Ruleset, ref: Any) -> str | None:
    if not isinstance(ref, str) or not ref.strip():
        return None
    full = ruleset.resolve("faction", ref.strip()) or ruleset.resolve("faction", ref.strip().lower())
    if full:
        return full
    hits = [f for f, d in ruleset.factions.items() if d.name.lower() == ref.strip().lower()]
    return hits[0] if len(hits) == 1 else None


def _artifact_ref(ruleset: Ruleset, ref: Any) -> str | None:
    if not isinstance(ref, str) or not ref.strip():
        return None
    full = ruleset.resolve("artifact", ref.strip()) or ruleset.resolve("artifact", ref.strip().lower())
    if full:
        return full
    label = ref.strip().lower()
    hits = [a for a, d in ruleset.artifacts.items() if label in (d.name.lower(), d.name_en.lower())]
    return hits[0] if len(hits) == 1 else None


def check_artifacts(ruleset: Ruleset, setup: Mapping[str, Any], offer: Sequence[str], ids: Any) -> list[str]:
    """Validate an artifact choice for the mode -> full ids. Draft: exactly one of the seat's offer. Points: any of
    the pool, each once, total cost within the budget, no two exclusive ones. None: must be empty."""
    if isinstance(ids, str):
        ids = [ids]
    if ids is None:
        ids = []
    if not isinstance(ids, list):
        raise SetupChoiceError("INVALID", "artifacts — список ID артефактов")
    mode = setup.get("artifacts", "none")
    if mode == "none":
        if ids:
            raise SetupChoiceError("PHASE_LOCKED", "в этой партии артефактов нет (режим none)")
        return []
    out: list[str] = []
    for ref in ids:
        full = _artifact_ref(ruleset, ref)
        if full is None:
            raise SetupChoiceError("UNKNOWN_ID", f"нет артефакта {str(ref)[:40]!r}")
        if full in out:
            raise SetupChoiceError("INVALID", f"артефакт {full} указан дважды")
        out.append(full)
    if mode == "draft":
        if len(out) != 1:
            raise SetupChoiceError("INVALID", f"в драфте берут ровно один артефакт из предложенных: "
                                              f"{', '.join(offer) or 'нет предложений'}")
        if out[0] not in offer:
            raise SetupChoiceError("NOT_OFFERED", f"{out[0]} тебе не предложен; предложены: {', '.join(offer)}")
        return out
    pool = artifact_pool(ruleset, setup)
    budget = int(setup.get("artifact_points", 3))
    for a in out:
        if a not in pool:
            raise SetupChoiceError("NOT_OFFERED", f"{a} нет в пуле этой партии")
    total = sum(ruleset.artifacts[a].cost for a in out)
    if total > budget:
        raise SetupChoiceError("NOT_ENOUGH_POINTS", f"стоимость {total} больше бюджета {budget}")
    for a in out:
        clash = [b for b in out if b != a and (b in ruleset.artifacts[a].exclusive_with
                                               or a in ruleset.artifacts[b].exclusive_with)]
        if clash:
            raise SetupChoiceError("EXCLUSIVE", f"{a} нельзя брать вместе с {', '.join(clash)}")
    return out


def _fits_all(ruleset: Ruleset, artifact: str) -> bool:
    factions = ruleset.artifacts[artifact].factions
    return not factions or set(ruleset.scenario.factions) <= set(factions)


def unfit_artifacts(ruleset: Ruleset, artifacts: Iterable[str], faction: str) -> list[str]:
    """The artifacts `faction` may not hold (they are dropped when the setup phase closes)."""
    return [a for a in artifacts if a in ruleset.artifacts and not _fits(ruleset, a, faction)]


def _fits(ruleset: Ruleset, artifact: str, faction: str) -> bool:
    factions = ruleset.artifacts[artifact].factions
    return not factions or faction in factions


def default_artifacts(ruleset: Ruleset, setup: Mapping[str, Any], offer: Sequence[str], faction: str) -> list[str]:
    """What a seat gets without a choice: draft — the first offered artifact its faction may hold; points — pool
    artifacts in id order while they fit the budget (skipping exclusive and other factions' ones); none — nothing."""
    mode = setup.get("artifacts", "none")
    if mode == "draft":
        return next(([a] for a in offer if _fits(ruleset, a, faction)), [])
    if mode != "points":
        return []
    return _fill_points(ruleset, setup, faction, [])


def _fill_points(ruleset: Ruleset, setup: Mapping[str, Any], faction: str, start: Sequence[str],
                 cap: int | None = None) -> list[str]:
    """`start` plus pool artifacts in id order while they fit the rest of the budget (skipping exclusive ones and
    those the faction may not hold). `cap` limits how many points are added (the freed budget of dropped ones)."""
    out = list(start)
    budget = int(setup.get("artifact_points", 3)) - sum(ruleset.artifacts[a].cost for a in out)
    if cap is not None:
        budget = min(budget, cap)
    for a in artifact_pool(ruleset, setup):
        if a in out:
            continue
        d = ruleset.artifacts[a]
        if (d.cost <= budget and _fits(ruleset, a, faction)
                and not any(b in d.exclusive_with or a in ruleset.artifacts[b].exclusive_with for b in out)):
            out.append(a)
            budget -= d.cost
    return out


def resolve_setup(ruleset: Ruleset, setup: Mapping[str, Any], seed: int | str, seats: Iterable[Mapping[str, Any]],
                  choices: Mapping[str, Mapping[str, Any]],
                  offers: Mapping[str, Sequence[str]]) -> dict[str, dict[str, Any]]:
    """Final factions and artifacts of every seat.

    seats: [{seat, faction?}] (`faction`: the config's default for the seat); choices: {seat: {factions?: [...],
    artifacts?: [...]}} (validated). Factions: seats that chose come first, in a seeded order, then the rest in a
    seeded order; each takes the first of its preferences (then of the scenario's list) not taken yet when
    `unique_factions` is on (duplicates only once every faction is taken). Artifacts: the choice, minus any the final
    faction may not hold (draft: replaced by the default; points: only the freed budget, the dropped ones' cost, is filled as by
    default; points the seat left unspent stay unspent). Returns {seat: {faction, prefs, artifacts,
    auto_faction, auto_artifacts, faction_conflict, dropped}}.
    """
    seats = list(seats)
    allowed = list(ruleset.scenario.factions)
    order = sorted(s["seat"] for s in seats)
    stream(seed, 0, "setup", "factions").shuffle(order)
    rank = {seat: i for i, seat in enumerate(order)}
    defaults = {s["seat"]: (ruleset.resolve("faction", s["faction"]) if s.get("faction") else None) or allowed[0]
                for s in seats}
    chose = {s["seat"]: bool((choices.get(s["seat"]) or {}).get("factions")) for s in seats}
    unique = bool(setup.get("unique_factions", True))
    taken: set[str] = set()
    out: dict[str, dict[str, Any]] = {}
    for seat in sorted(defaults, key=lambda s: (not chose[s], rank[s])):
        prefs = list((choices.get(seat) or {}).get("factions") or [defaults[seat]])
        candidates = prefs + [f for f in allowed if f not in prefs]
        faction = next((f for f in candidates if f not in taken), prefs[0]) if unique else prefs[0]
        taken.add(faction)
        out[seat] = {"faction": faction, "prefs": prefs, "auto_faction": not chose[seat],
                     "faction_conflict": faction != prefs[0]}
    for seat, entry in out.items():
        chosen = (choices.get(seat) or {}).get("artifacts")
        offer = list(offers.get(seat) or [])
        if chosen is None:
            arts, auto = default_artifacts(ruleset, setup, offer, entry["faction"]), True
        else:
            arts, auto = [a for a in chosen if _fits(ruleset, a, entry["faction"])], False
        dropped = [a for a in (chosen or []) if a not in arts]
        if dropped and setup.get("artifacts") == "draft":
            arts = default_artifacts(ruleset, setup, offer, entry["faction"])
        elif dropped and setup.get("artifacts") == "points":
            freed = sum(ruleset.artifacts[a].cost for a in dropped if a in ruleset.artifacts)
            arts = _fill_points(ruleset, setup, entry["faction"], arts, cap=freed)  # only the freed budget
        entry.update(artifacts=sorted(arts), auto_artifacts=auto, dropped=dropped)
    return dict(sorted(out.items()))


def faction_choices(ruleset: Ruleset) -> list[dict[str, Any]]:
    """The scenario's factions with full descriptions (catalog.describe), in the scenario's order."""
    from .catalog import describe
    return [describe(ruleset, "faction", f) for f in ruleset.scenario.factions]


def artifact_choices(ruleset: Ruleset, ids: Iterable[str]) -> list[dict[str, Any]]:
    from .catalog import describe
    return [describe(ruleset, "artifact", a) for a in ids if a in ruleset.artifacts]
