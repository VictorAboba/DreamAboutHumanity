"""Victory points (docs/SCORING.md): score sources, secret objectives, the final total and the score under fog.

Sources (registry.SCORE_SOURCES, weights in `Ruleset.scenario.scoring`, 0 = off):
    territory   snapshot   full own provinces: score.territory.province, +score.territory.capital on map capitals
    science     snapshot   techs researched by order (score.science.tier<N>) + score.science.branch per complete branch
    works       snapshot   ready buildings with building.<id>.score in full own provinces
    war         tally      Player.tally.war_value {victim: value}, capped per victim and in total; core 0.7.0
                           (score.war.province_cap > 0): Player.tally.war_provinces {province: {points, since}};
                           core 0.7.1 (score.war.no_reclaim 1): Player.tally.owned [province], the land a seat has
                           owned in this game, pays no war points when taken back
    intel       tally      core 0.7.0: Player.tally.intel, successful spy missions counted (score.intel.*); core
                           0.8.0: Player.tally.intel_kinds [mission] (`once`), intel_targets [«mission:seat»]
                           (`once_per_target`), written only when used
    objectives  at the end the seat's secret objectives that are met (Player.objectives)

`score_parts` holds *weighted* points per source (they sum to the score); a source with weight 0 is not in it. Core
0.6.0 (decision 26): `betrayal`, a penalty line (<= 0) of `score.betrayal.per_break` per pact the seat broke, joins
the parts of a seat that broke one while that stat is > 0 (never under the default scoring). The turn
score leaves objectives out; the final total (`final_scores`, at game over only) adds them for living seats. With the
default scoring ({territory: 1}) `scores` is the slice's 1 per province + 3 per capital, bit for bit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from . import conquest as cq
from . import registry
from .content import ObjectiveDef, Ruleset, StatKey
from .model import GameState, Player
from .rng import stream
from .techs import stat_key

if TYPE_CHECKING:       # pragma: no cover
    from .combat import FightResult

SOURCES = registry.SCORE_SOURCES
TURN_SOURCES = registry.TURN_SOURCES
LABELS = {"territory": "зем", "science": "нау", "works": "стр", "war": "вой", "intel": "разв", "objectives": "цель",
          "betrayal": "разрыв"}
NAMES = {"territory": "земля", "science": "наука", "works": "стройки", "war": "война", "intel": "разведка",
         "objectives": "тайная цель", "betrayal": "разрыв пакта"}
BETRAYAL = "betrayal"           # the penalty line of the parts (not a weighted source)
PART_ORDER = (*registry.SCORE_SOURCES, BETRAYAL)      # the order breakdowns are shown in
SCORE_LINE_LIMIT = 300

# Report events: public (everyone sees them, SCORING.md 2.3) and private to their sides (2.4)
PUBLIC_KINDS = frozenset({"work_started", "work_completed"})
PRIVATE_KINDS = frozenset({"war_credit", "war_points", "build_stalled", "build_paid"})


# ---------------------------------------------------------------- weights

def on(rs: Ruleset, source: str) -> bool:
    return rs.weight(source) > 0


def turn_sources(rs: Ruleset) -> list[str]:
    """The turn score's sources that are on, in registry order."""
    return [src for src in TURN_SOURCES if on(rs, src)]


def _stat(rs: Ruleset, path: str, key: StatKey) -> int:
    return int(rs.stat(path, key))


# ---------------------------------------------------------------- sources

def gifted_techs(rs: Ruleset, player: Player) -> set[str]:
    """Techs the seat got without researching them: `grant_tech` of its artifacts and of its faction's start."""
    gifts = {e["tech"] for a in player.artifacts if a in rs.artifacts for e in rs.artifacts[a].effects
             if e["type"] == "grant_tech"}
    faction = rs.factions.get(player.faction)
    if faction is not None:
        gifts |= {e["tech"] for e in faction.start_effects if e["type"] == "grant_tech"}
    return gifts


def researched(rs: Ruleset, player: Player) -> list[str]:
    """Techs researched by order (SCORING.md 2.2): `player.techs` without artifact and faction-start grants."""
    gifts = gifted_techs(rs, player)
    return [t for t in player.techs if t in rs.techs and t not in gifts]


def branch_techs(rs: Ruleset, faction: str, branch: str) -> list[str]:
    """Techs of a branch available to a faction (the filter of list_techs and the catalog: `factions`; techs of a
    system that is off are not in the ruleset at all)."""
    return sorted(tid for tid, t in rs.techs.items()
                  if t.branch == branch and (not t.factions or faction in t.factions))


def complete_branches(rs: Ruleset, player: Player) -> list[str]:
    """Branches of which the seat has every tech available to it (gifts count); a branch with none does not."""
    out = []
    for bid in sorted(rs.branches):
        techs = branch_techs(rs, player.faction, bid)
        if techs and all(t in player.techs for t in techs):
            out.append(bid)
    return out


def _tier_stat(tier: int) -> str:
    return f"score.science.tier{min(max(int(tier), 1), 4)}"


def territory(state: GameState, rs: Ruleset, seat: str, only: Iterable[str] | None = None,
              key: StatKey = ...) -> int:
    """Full own provinces (cq.scores_province): province points, + capital points on a map capital. `only`: count
    just these provinces (the visible ones, for score_known)."""
    if key is ...:
        key = stat_key(state, rs, seat)
    one, capital = _stat(rs, "score.territory.province", key), _stat(rs, "score.territory.capital", key)
    allowed = set(only) if only is not None else None
    total = 0
    for prov in state.owned(seat):
        if (allowed is None or prov.id in allowed) and cq.scores_province(prov, state.turn):
            total += one + (capital if prov.id in rs.map.capitals else 0)
    return total


def science(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> int:
    player = state.players[seat]
    if key is ...:
        key = stat_key(state, rs, seat)
    total = sum(_stat(rs, _tier_stat(rs.techs[t].tier), key) for t in researched(rs, player))
    return total + _stat(rs, "score.science.branch", key) * len(complete_branches(rs, player))


def works_held(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> list[tuple[str, str, int]]:
    """(province, building, points) of the ready great works in the seat's full provinces."""
    if key is ...:
        key = stat_key(state, rs, seat)
    out = []
    for prov in state.owned(seat):
        if not cq.scores_province(prov, state.turn):
            continue
        for b in prov.buildings:
            if b in rs.buildings and rs.buildings[b].score:
                pts = _stat(rs, f"building.{b}.score", key)
                if pts:
                    out.append((prov.id, b, pts))
    return out


def works(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> int:
    return sum(p for _, _, p in works_held(state, rs, seat, key))


def war_by_province(rs: Ruleset) -> bool:
    """Core 0.7.0 (decision 27): the war score is the scale per province (score.war.province_cap > 0 for the scenario's
    base stats), not the price of killed units."""
    return _stat(rs, "score.war.province_cap", None) > 0


def no_reclaim(rs: Ruleset) -> bool:
    """Core 0.7.1 (decision 28): under the province scale, taking back a province the taker has owned earlier in this
    game (score.war.no_reclaim 1 for the scenario's base stats) pays no war points, neither the capture nor the
    holding."""
    return war_by_province(rs) and _stat(rs, "score.war.no_reclaim", None) > 0


def owned_before(state: GameState, seat: str) -> list[str]:
    """Player.tally.owned: provinces the seat owned at the start of the game or at the end of an earlier turn (kept
    while no_reclaim is on; see note_owners)."""
    return list(state.players[seat].tally.get("owned") or [])


def note_owners(state: GameState, rs: Ruleset) -> None:
    """At the start of a resolution (the owners are those at the end of the previous turn, or at the game start for
    the first one) add every seat's provinces to its Player.tally.owned, sorted. Only while no_reclaim is on: the
    tally of older rulesets stays empty, so their states hash as before."""
    if not no_reclaim(rs):
        return
    for seat in sorted(state.players):
        mine = {p.id for p in state.owned(seat)}
        if not mine:
            continue
        tally = state.players[seat].tally
        known = set(tally.get("owned") or [])
        if not mine <= known:
            tally["owned"] = sorted(known | mine)


def reclaim(state: GameState, rs: Ruleset, seat: str, pid: str) -> bool:
    """Taking `pid` would be a return of the seat's own land (no war points; core 0.7.1)."""
    return no_reclaim(rs) and pid in (state.players[seat].tally.get("owned") or ())


def war_provinces(state: GameState, seat: str) -> dict[str, dict[str, Any]]:
    """Player.tally.war_provinces {province: {points, since}} (`since`: the turn of the capture while the seat has held
    the province ever since, else None)."""
    return state.players[seat].tally.get("war_provinces") or {}


def war(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> int:
    """Kill credit: min(cap, Σ_victim min(cap_per_victim, war_value[victim] // per_point)). Per province (core 0.7.0):
    Σ of the points credited per province, at most score.war.province_total when that is > 0."""
    if key is ...:
        key = stat_key(state, rs, seat)
    if war_by_province(rs):
        total = sum(int(e.get("points", 0)) for e in war_provinces(state, seat).values())
        cap = _stat(rs, "score.war.province_total", key)
        return min(cap, total) if cap > 0 else total
    per = max(1, _stat(rs, "score.war.per_point", key))
    each = _stat(rs, "score.war.cap_per_victim", key)
    values = (state.players[seat].tally.get("war_value") or {}).values()
    return min(_stat(rs, "score.war.cap", key), sum(min(each, int(v) // per) for v in values))


def war_cap_left(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> int | None:
    """War points the seat may still get; None per province without a total cap (each province has its own)."""
    if key is ...:
        key = stat_key(state, rs, seat)
    if war_by_province(rs):
        cap = _stat(rs, "score.war.province_total", key)
        return max(0, cap - war(state, rs, seat, key)) if cap > 0 else None
    return max(0, _stat(rs, "score.war.cap", key) - war(state, rs, seat, key))


def war_view(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> dict[str, Any]:
    """you.war of get_state: points and cap_left; per province (core 0.7.0) also the cap per province, the turns per
    holding point and the credited provinces {province: points}."""
    if key is ...:
        key = stat_key(state, rs, seat)
    out: dict[str, Any] = {"points": war(state, rs, seat, key), "cap_left": war_cap_left(state, rs, seat, key)}
    if war_by_province(rs):
        out["per_province"] = _stat(rs, "score.war.province_cap", key)
        out["hold_turns"] = _stat(rs, "score.war.hold_turns", key)
        out["provinces"] = {pid: int(e.get("points", 0)) for pid, e in sorted(war_provinces(state, seat).items())}
        if no_reclaim(rs):
            # core 0.7.1: provinces of others the seat has owned earlier — taking them back pays no war points
            mine = {p.id for p in state.owned(seat)}
            out["reclaim"] = [pid for pid in owned_before(state, seat) if pid not in mine]
    return out


def _war_room(state: GameState, rs: Ruleset, seat: str, key: StatKey) -> bool:
    """Below the total war cap (score.war.province_total; 0 = none): another credited point still counts."""
    cap = _stat(rs, "score.war.province_total", key)
    return cap <= 0 or war(state, rs, seat, key) < cap


def credit_provinces(state: GameState, rs: Ruleset, turn: int, owners_before: Mapping[str, str | None],
                     alive: Sequence[str], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """War points of one resolution under the province scale (core 0.7.0, decision 27), after the battles and the fates
    of captures; adds to Player.tally.war_provinces and returns the `war_points` event data.

    Holding first: a province the seat has held since its capture gives +1 at every score.war.hold_turns full turns
    after it (the turn of the capture counts as 0); a province lost meanwhile stops counting (`since` None), its points
    stay. Then captures: a province a living rival seat (alive at the start of this resolution, not the taker) owned
    at the start of this resolution and the taker holds after the fates (occupied or ruined counts; a raid that gives
    it back or a release does not) gives +1. Every (seat, province) earns at most score.war.province_cap over the game,
    so a province taken back and forth stops paying, and nothing is credited (no point, no event) once the seat holds
    score.war.province_total in all (war() would clip it anyway; an event would promise a point that never counts).
    Neutral and Abandoned land gives no war points. Core 0.7.1 (score.war.no_reclaim 1): a province the taker has
    owned earlier in this game (Player.tally.owned, its own start provinces included) gives no point and no holding
    (`since` stays as it was: None once the province was lost); its event says so (points 0, reason `reclaim`)."""
    out: list[dict[str, Any]] = []
    for seat in sorted(state.players):
        entries = state.players[seat].tally.get("war_provinces") or {}
        for pid in sorted(entries):
            e = entries[pid]
            if e.get("since") is None:
                continue
            if state.provinces[pid].owner != seat:
                e["since"] = None
                continue
            key = stat_key(state, rs, seat)
            cap, every = _stat(rs, "score.war.province_cap", key), max(1, _stat(rs, "score.war.hold_turns", key))
            held = turn - int(e["since"])
            if held > 0 and held % every == 0 and int(e["points"]) < cap and _war_room(state, rs, seat, key):
                e["points"] = int(e["points"]) + 1
                out.append({"seat": seat, "province": pid, "points": 1, "reason": "hold", "total": e["points"],
                            "cap": cap, "held": held})
    for ev in events:
        if ev.get("kind") != "captured":
            continue
        pid, taker, loser = ev["province"], ev.get("to"), ev.get("from")
        if (taker not in state.players or loser not in state.players or taker == loser or loser not in alive
                or owners_before.get(pid) != loser or state.provinces[pid].owner != taker):
            continue
        key = stat_key(state, rs, taker)
        cap = _stat(rs, "score.war.province_cap", key)
        if reclaim(state, rs, taker, pid):
            # core 0.7.1: the land comes back, no war point and no holding; the event tells the taker why
            out.append({"seat": taker, "province": pid, "points": 0, "reason": "reclaim",
                        "total": int(war_provinces(state, taker).get(pid, {}).get("points", 0)), "cap": cap,
                        "victim": loser})
            continue
        entries = state.players[taker].tally.setdefault("war_provinces", {})
        e = entries.setdefault(pid, {"points": 0, "since": None})
        if e.get("since") == turn:
            continue                    # one capture per province a turn
        e["since"] = turn                 # held from now on even at a cap: the hold and recapture rules stay
        if int(e["points"]) < cap and _war_room(state, rs, taker, key):
            e["points"] = int(e["points"]) + 1
            out.append({"seat": taker, "province": pid, "points": 1, "reason": "capture", "total": e["points"],
                        "cap": cap, "victim": loser})
        state.players[taker].tally["war_provinces"] = dict(sorted(entries.items()))
    return out


def intel(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> int:
    """Core 0.7.0 (decision 27): score.intel.per_success per counted successful spy mission (Player.tally.intel), at
    most score.intel.cap."""
    if key is ...:
        key = stat_key(state, rs, seat)
    n = int(state.players[seat].tally.get("intel") or 0)
    return min(_stat(rs, "score.intel.cap", key), n * _stat(rs, "score.intel.per_success", key))


def intel_left(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> int:
    """Intel points the seat may still get (0 while the source is off)."""
    if not on(rs, "intel"):
        return 0
    if key is ...:
        key = stat_key(state, rs, seat)
    return max(0, _stat(rs, "score.intel.cap", key) - intel(state, rs, seat, key))


EACH_TURN, ONCE, ONCE_PER_TARGET, EFFECT, NO_INTEL = registry.INTEL_KINDS
# why a success scored nothing (core 0.8.0, docs/SPY_V2.md 5.2; `intel_denied` of the recon step, `sabotage_effect`)
DENY_PACT, DENY_ONCE, DENY_TARGET, DENY_CAP, DENY_TAKEN, DENY_NO_EFFECT = (
    "pact", "once", "once_per_target", "cap", "taken", "no_effect")
DENY_TEXT = {DENY_PACT: "партнёр по пакту", DENY_ONCE: "очко за это задание уже получено",
             DENY_TARGET: "очко за это задание против этого места уже получено",
             DENY_CAP: "предел очков разведки набран", DENY_TAKEN: "эффект уже засчитан другому удару",
             DENY_NO_EFFECT: "удар без последствий"}


def intel_kind(mission: Any) -> str:
    """A mission's `intel` kind (registry.INTEL_KINDS); each_turn for an unknown mission."""
    return getattr(mission, "intel", EACH_TURN) or EACH_TURN


def intel_pair(mission: str, target: str) -> str:
    """An entry of Player.tally.intel_targets (once_per_target): «mission:seat»."""
    return f"{mission}:{target}"


def pact_deep(rs: Ruleset, key: StatKey = None) -> bool:
    """Core 0.8.1 (decision 32): the deep missions (intel kinds once, once_per_target) score against a pact partner
    (score.intel.pact_deep 1); 0.8.0 and older: they do not (G1)."""
    return _stat(rs, "score.intel.pact_deep", key) > 0


def intel_block(state: GameState, rs: Ruleset, seat: str, target: str, mission: str, kind: str,
                key: StatKey = ..., pact: bool | None = None) -> str | None:
    """Why a success of `mission` against the rival `target` would not score now (None: it would). Every kind but
    each_turn (the rule of 0.7.0, which knows only the cap): a pact partner (G1; `pact` given: the recon snapshot's
    answer), the once / once_per_target rules; then the cap. Core 0.8.1 (decision 32, score.intel.pact_deep 1): the
    deep kinds (once, once_per_target) score against a pact partner too; `effect` keeps the pact block."""
    if key is ...:
        key = stat_key(state, rs, seat)
    tally = state.players[seat].tally
    if kind != EACH_TURN and not (kind in (ONCE, ONCE_PER_TARGET) and pact_deep(rs, key)):
        if pact is None:
            from .diplomacy import pact_between
            pact = pact_between(state, seat, target, state.turn) is not None
        if pact:
            return DENY_PACT
    if kind != EACH_TURN:
        if kind == ONCE and mission in (tally.get("intel_kinds") or ()):
            return DENY_ONCE
        if kind == ONCE_PER_TARGET and intel_pair(mission, target) in (tally.get("intel_targets") or ()):
            return DENY_TARGET
    if intel(state, rs, seat, key) >= _stat(rs, "score.intel.cap", key):
        return DENY_CAP
    return None


def _intel_credit(state: GameState, rs: Ruleset, seat: str, target: str, mission: str, kind: str,
                  key: StatKey) -> dict[str, Any]:
    tally = state.players[seat].tally
    tally["intel"] = int(tally.get("intel") or 0) + 1
    if kind == ONCE:
        tally["intel_kinds"] = sorted({*(tally.get("intel_kinds") or ()), mission})
    elif kind == ONCE_PER_TARGET:
        tally["intel_targets"] = sorted({*(tally.get("intel_targets") or ()), intel_pair(mission, target)})
    out = {"seat": seat, "target": target, "mission": mission, "points": _stat(rs, "score.intel.per_success", key),
           "total": intel(state, rs, seat, key)}
    if kind != EACH_TURN:
        out["kind"] = kind
    return out


def _rival(state: GameState, seat: str, target: Any) -> bool:
    player = state.players.get(target) if isinstance(target, str) else None
    return player is not None and target != seat and player.alive


def credit_intel(state: GameState, rs: Ruleset, rounds: Sequence[Mapping[str, Any]],
                 missions: Mapping[str, Any], denied: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The recon step's intel points (core 0.7.0, decision 27): a successful round (a report or a strike) against a
    living rival seat (the target seat, or the owner of the target province) counts once per (spy, target seat,
    mission kind) a turn, while the spy's points are below score.intel.cap. Core 0.8.0 (docs/SPY_V2.md 5.2): by the
    mission's `intel` kind: `no` never, `effect` not here (credit_intel_effect, at the end of the resolution),
    `once` once a game, `once_per_target` once per rival seat; none of these against a pact partner in force on this
    snapshot (G1; a refused pair is not spent). Adds to Player.tally.intel (and intel_kinds / intel_targets) and
    returns [{seat, target, mission, points, total, kind?}]; successes of the new kinds refused for a reason go to
    `denied` [{seat, target, mission, kind, reason}]."""
    if not on(rs, "intel"):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in rounds:
        if not r.get("success"):
            continue
        seat, mission = r["seat"], r["mission"]
        m = missions.get(mission)
        kind = intel_kind(m)
        if kind in (NO_INTEL, EFFECT):
            continue
        target = r["target"] if m is not None and m.target == "seat" else r.get("victim")
        if not _rival(state, seat, target) or (seat, target, mission) in seen:
            continue
        seen.add((seat, target, mission))
        key = stat_key(state, rs, seat)
        if _stat(rs, "score.intel.per_success", key) <= 0:
            continue
        why = intel_block(state, rs, seat, target, mission, kind, key)
        if why is not None:
            if denied is not None and kind != EACH_TURN:
                denied.append({"seat": seat, "target": target, "mission": mission, "kind": kind, "reason": why})
            continue
        out.append(_intel_credit(state, rs, seat, target, mission, kind, key))
    return out


def credit_intel_effect(state: GameState, rs: Ruleset, seat: str, target: Any, mission: str,
                        pact: bool) -> tuple[dict[str, Any] | None, str | None]:
    """Core 0.8.0 (docs/SPY_V2.md 5.2 p. 4, 4a): the point of a loud `effect` mission whose strike did something,
    at the end of the resolution, after the recon step's points (the cap is checked now); `pact`: a pact with the
    victim in force on the recon snapshot. (point entry, None), (None, why) or (None, None) when it cannot score
    at all (the source off, not a living rival)."""
    if not on(rs, "intel") or not _rival(state, seat, target):
        return None, None
    key = stat_key(state, rs, seat)
    if _stat(rs, "score.intel.per_success", key) <= 0:
        return None, None
    why = intel_block(state, rs, seat, target, mission, EFFECT, key, pact=pact)
    if why is not None:
        return None, why
    return _intel_credit(state, rs, seat, target, mission, EFFECT, key), None


_POINTS = {"territory": territory, "science": science, "works": works, "war": war, "intel": intel}


def raw_points(state: GameState, rs: Ruleset, seat: str) -> dict[str, int]:
    """Unweighted points of the turn sources that are on."""
    key = stat_key(state, rs, seat)
    return {src: _POINTS[src](state, rs, seat, key=key) for src in turn_sources(rs)}


def betrayal_on(rs: Ruleset) -> bool:
    """The pact-break penalty is in play (core 0.6.0): a non-default scoring and score.betrayal.per_break > 0 for some
    faction of the scenario."""
    return not rs.default_scoring and any(_stat(rs, "score.betrayal.per_break", f) > 0
                                          for f in (None, *rs.scenario.factions))


def pacts_broken(state: GameState, seat: str) -> int:
    """Pacts the seat broke (status broken, the seat among `broken_by`): public, every seat knows them."""
    return sum(1 for p in state.pacts if p.get("status") == "broken" and seat in (p.get("broken_by") or ()))


def betrayal(state: GameState, rs: Ruleset, seat: str, key: StatKey = ...) -> int:
    """The penalty line (<= 0): -score.betrayal.per_break per pact the seat broke; 0 when the penalty is off."""
    if not betrayal_on(rs):
        return 0
    if key is ...:
        key = stat_key(state, rs, seat)
    return -_stat(rs, "score.betrayal.per_break", key) * pacts_broken(state, seat)


def score_parts(state: GameState, rs: Ruleset) -> dict[str, dict[str, int]]:
    """{seat: {source: weighted points}} of the turn score (objectives left out), plus `betrayal` (negative) for a
    seat that broke a pact while the penalty is on."""
    out = {}
    for seat in state.players:
        parts = {src: rs.weight(src) * n for src, n in raw_points(state, rs, seat).items()}
        penalty = betrayal(state, rs, seat)
        if penalty:
            parts[BETRAYAL] = penalty
        out[seat] = parts
    return out


def scores(state: GameState, rs: Ruleset) -> dict[str, int]:
    """The turn score {seat: int}: Σ weight × points over territory, science, works, war."""
    if rs.default_scoring:
        return {seat: territory(state, rs, seat) for seat in state.players}
    return {seat: sum(parts.values()) for seat, parts in score_parts(state, rs).items()}


# ---------------------------------------------------------------- objectives

def _count_need(obj: ObjectiveDef) -> int:
    return int(obj.params.get("count", 1))


def homes(state: GameState) -> dict[str, str]:
    """{province: seat} of the seats' start capitals (Player.home)."""
    return {p.home: seat for seat, p in state.players.items() if p.home}


def objective_progress(state: GameState, rs: Ruleset, seat: str, entry: Mapping[str, Any]) -> tuple[bool, int, int]:
    """(met, have, need) of one dealt objective {id, target?} on the current state and the seat's tally."""
    obj = rs.objectives.get(entry.get("id", ""))
    if obj is None:
        return False, 0, 1
    p = obj.params
    held = [prov for prov in state.owned(seat) if cq.scores_province(prov, state.turn)]
    need = _count_need(obj)
    if obj.kind == "hold_type":
        have = sum(1 for prov in held if prov.type == p["type"])
    elif obj.kind == "hold_count":
        have = len(held)
    elif obj.kind == "hold_foreign":
        # core 0.8.0 (docs/TRADE.md 6.3): with `via: capture` land got without a battle (a trade, a release) does not
        # count until it is taken again
        taken_only = p.get("via") == "capture"
        have = sum(1 for prov in held if state.origins.get(prov.id) not in (None, seat)
                   and state.origins.get(prov.id) in state.players
                   and not (taken_only and (prov.status.get(cq.TRANSFER_MARK) or {}).get("to") == seat))
    elif obj.kind == "capital_other":
        others = {pid for pid, s in homes(state).items() if s != seat}
        have = sum(1 for prov in held if prov.id in others)
    elif obj.kind == "techs_branch":
        have = sum(1 for t in researched(rs, state.players[seat]) if rs.techs[t].branch == p["branch"])
    elif obj.kind == "great_work":
        have = len(works_held(state, rs, seat))
    elif obj.kind == "rival_capital_taken":
        target = entry.get("target")
        home = state.players[target].home if target in state.players else None
        taken = state.players[seat].tally.get("homes_taken") or []
        have = int(home is not None and (state.provinces[home].owner == seat or home in taken))
        need = 1
    else:       # pragma: no cover - kinds are checked when the pack is loaded
        have = 0
    return have >= need, have, need


def objective_status(state: GameState, rs: Ruleset, seat: str) -> list[dict[str, Any]]:
    """[{id, target, met, points, have, need}] of the seat's objectives now (points: the objective's, unweighted)."""
    out = []
    for entry in state.players[seat].objectives:
        met, have, need = objective_progress(state, rs, seat, entry)
        obj = rs.objectives.get(entry["id"])
        out.append({"id": entry["id"], "target": entry.get("target"), "met": met,
                    "points": obj.points if obj is not None else 0, "have": have, "need": need})
    return out


def objective_points(state: GameState, rs: Ruleset, seat: str) -> int:
    return sum(o["points"] for o in objective_status(state, rs, seat) if o["met"])


def final_parts(state: GameState, rs: Ruleset) -> dict[str, dict[str, int]]:
    """The final breakdown: the turn parts plus `objectives` (weighted; 0 for an eliminated seat: its objective is
    revealed but does not count, SCORING.md 1.3)."""
    out = score_parts(state, rs)
    if on(rs, "objectives"):
        for seat, parts in out.items():
            alive = state.players[seat].alive
            parts["objectives"] = rs.weight("objectives") * objective_points(state, rs, seat) if alive else 0
    return out


def final_scores(state: GameState, rs: Ruleset) -> dict[str, int]:
    if rs.default_scoring:
        return scores(state, rs)
    return {seat: sum(parts.values()) for seat, parts in final_parts(state, rs).items()}


def leaders(values: Mapping[str, int], seats: Sequence[str]) -> list[str]:
    best = max((values[s] for s in seats), default=0)
    return [s for s in seats if values[s] == best]


def result_extra(state: GameState, rs: Ruleset, alive: Sequence[str]) -> dict[str, Any]:
    """The fields a non-default scoring adds to `GameState.result` (SCORING.md 4): `score_parts` (final), the revealed
    `objectives` {seat: [{id, target, met, points}]} and `winners_open` (who would lead without objectives)."""
    turn = scores(state, rs)
    return {"score_parts": final_parts(state, rs),
            "objectives": {seat: [{k: o[k] for k in ("id", "target", "met", "points")}
                                  for o in objective_status(state, rs, seat)] for seat in state.players},
            "winners_open": leaders(turn, list(alive))}


# ---------------------------------------------------------------- war credit (resolution steps 2-4)

def unit_value(rs: Ruleset, units: Mapping[str, int], key: StatKey) -> int:
    """Σ n × the unit's price for its owner's stat key (cheap militia of a faction is worth what it cost it)."""
    return sum(int(n) * sum(rs.unit_cost(u, key).values()) for u, n in sorted(units.items()) if n > 0)


def credit_war(state: GameState, rs: Ruleset, keys: Mapping[str | None, StatKey], a: str | None, b: str | None,
               res: FightResult, defender: str | None, where: str) -> list[dict[str, Any]]:
    """One fight a (attacker) vs b: each seat side gets the price of the other seat side's losses; a defender (the
    owner of the province in a battle against it) gets score.war.defense_pct %. Neutrals and non-seats give and get
    nothing. Adds to Player.tally.war_value and returns the `war_credit` event data (one per paying side)."""
    out = []
    for killer, victim, losses in ((a, b, res.losses_b), (b, a, res.losses_a)):
        if killer not in state.players or victim not in state.players or killer == victim:
            continue
        lost = {u: n for u, n in losses.items() if n > 0}
        if not lost:
            continue
        value = unit_value(rs, lost, keys.get(victim))
        is_def = killer == defender
        if is_def:
            value = value * _stat(rs, "score.war.defense_pct", keys.get(killer)) // 100
        tally = state.players[killer].tally.setdefault("war_value", {})
        tally[victim] = tally.get(victim, 0) + value
        out.append({"killer": killer, "victim": victim, "where": where, "units": dict(sorted(lost.items())),
                    "value": value, "defender": is_def})
    return out


def note_capture(state: GameState, seat: str | None, pid: str) -> bool:
    """`captured` of another seat's home by `seat`: tally.homes_taken grows (rival_capital_taken). True if new."""
    if seat not in state.players or homes(state).get(pid) in (None, seat):
        return False
    taken = state.players[seat].tally.setdefault("homes_taken", [])
    if pid in taken:
        return False
    taken.append(pid)
    taken.sort()
    return True


# ---------------------------------------------------------------- dealing (setup)

def _start_grants(rs: Ruleset, faction: str) -> set[str]:
    f = rs.factions.get(faction)
    return {e["tech"] for e in f.start_effects if e["type"] == "grant_tech"} if f is not None else set()


def researchable(rs: Ruleset, faction: str, branch: str) -> list[str]:
    """Techs of a branch a faction can research by order (its start grants are not researched)."""
    if rs.research_resource is None:
        return []
    grants = _start_grants(rs, faction)
    return [t for t in branch_techs(rs, faction, branch) if t not in grants]


def _start_sizes(rs: Ruleset) -> list[int]:
    return [1 + len(s.provinces) for s in rs.map.starts]


def infeasible(rs: Ruleset, obj: ObjectiveDef, players: int) -> str | None:
    """Why the objective cannot be met in a game of `players` seats on this ruleset (SCORING.md 2.5), or None."""
    need = _count_need(obj)
    p = obj.params
    provinces = list(rs.map.provinces.values())
    if obj.kind == "hold_type":
        n = sum(1 for prov in provinces if prov.type == p["type"])
        if need > n:
            return f"needs {need} provinces of type {p['type']}, the map has {n}"
    elif obj.kind == "hold_count":
        if need > len(provinces):
            return f"needs {need} provinces, the map has {len(provinces)}"
    elif obj.kind == "hold_foreign":
        sizes = _start_sizes(rs)
        worst = min((sum(sorted(sizes[:i] + sizes[i + 1:])[:players - 1]) for i in range(len(sizes))), default=0)
        if need > worst:
            return f"needs {need} provinces of other seats' starts, a game of {players} has {worst}"
    elif obj.kind == "capital_other":
        if need > players - 1:
            return f"needs {need} capitals of other seats, a game of {players} has {players - 1}"
    elif obj.kind == "techs_branch":
        for faction in rs.scenario.factions:
            n = len(researchable(rs, faction, p["branch"]))
            if need > n:
                return f"needs {need} techs of branch {p['branch']} researched, faction {faction} can research {n}"
    elif obj.kind == "great_work":
        if not on(rs, "works"):
            return "needs great works, but works have weight 0"
        types = {prov.type for prov in provinces}
        fit = [bid for bid, b in rs.buildings.items() if b.score > 0
               and (not b.allowed_types or types & set(b.allowed_types))
               and all(not b.factions or f in b.factions for f in rs.scenario.factions)]
        if not fit:
            return "no building with score > 0 every faction of the scenario can build on this map"
        own = sum(b.max_per_owner or len(provinces) for b in (rs.buildings[x] for x in fit))
        if need > own:
            return f"needs {need} great works, a seat may build at most {own}"
    elif obj.kind == "rival_capital_taken":
        if players < 2:
            return "needs a rival seat"
    return None


def free_at_start(rs: Ruleset, obj: ObjectiveDef) -> str | None:
    """Why the objective is met at the start of some start with some faction (after its start effects), or None."""
    need = _count_need(obj)
    types = {pid: prov.type for pid, prov in rs.map.provinces.items()}
    for faction in rs.scenario.factions:
        f = rs.factions[faction]
        start_buildings = set(f.start_buildings) | {e["building"] for e in f.start_effects
                                                    if e["type"] == "grant_building"}
        for i, start in enumerate(rs.map.starts):
            own = [start.capital, *start.provinces]
            if obj.kind == "hold_type" and sum(1 for pid in own if types[pid] == obj.params["type"]) >= need:
                return f"met at start {i + 1} ({start.capital}) already"
            if obj.kind == "hold_count" and len(own) >= need:
                return f"met at start {i + 1} ({start.capital}) already ({len(own)} provinces)"
            if obj.kind == "great_work" and on(rs, "works"):
                n = sum(1 for b in start_buildings if b in rs.buildings and rs.buildings[b].score > 0)
                if n >= need:
                    return f"met at the start of faction {faction} already (its start buildings)"
    return None


def validate_objectives(rs: Ruleset) -> list[tuple[tuple[Any, ...], str]]:
    """Problems of the scenario's objectives (SCORING.md 2.5): for every number of seats 2..max_players, the pool
    after the filters has >= max(3, dealt) objectives and each is feasible; none is met at any start."""
    out: list[tuple[tuple[Any, ...], str]] = []
    dealt = int(rs.scenario.objectives.get("dealt", 1))
    lo = 2 if rs.map.max_players >= 2 else 1
    seen: set[str] = set()
    for n in range(lo, rs.map.max_players + 1):
        pool = [o for o in rs.objectives.values() if o.fits(rs.map.id, n)]
        if len(pool) < max(3, dealt):
            out.append((("pool",), f"{len(pool)} objectives for {n} seats on map {rs.map.id}: at least "
                                   f"{max(3, dealt)} are needed (a small pool shows the others' objectives)"))
        for o in pool:
            why = infeasible(rs, o, n)
            if why and o.id not in seen:
                seen.add(o.id)
                out.append((("pool",), f"objective {o.id} cannot be met ({n} seats): {why}"))
    for o in rs.objectives.values():
        why = free_at_start(rs, o)
        if why:
            out.append((("pool",), f"objective {o.id} is free: {why}"))
    return out


def deal_objectives(rs: Ruleset, seed: int | str, seats: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Each seat draws `dealt` different objectives on its own stream (seed, 0, "objectives", seat) from the pool
    filtered for this game (maps, players, feasible with this many seats); repeats between seats are allowed. A
    targeted objective names the next seat of a cycle shuffled on (seed, 0, "objective_targets"); a seat holds at
    most one targeted objective, so every seat is a target at most once."""
    if not on(rs, "objectives"):
        return {}
    order = sorted(seats)
    n = len(order)
    dealt = int(rs.scenario.objectives.get("dealt", 1))
    pool = [o for _, o in sorted(rs.objectives.items()) if o.fits(rs.map.id, n) and infeasible(rs, o, n) is None]
    cycle = list(order)
    stream(seed, 0, "objective_targets", "").shuffle(cycle)
    nxt = {cycle[i]: cycle[(i + 1) % n] for i in range(n)}
    out: dict[str, list[dict[str, Any]]] = {}
    for seat in order:
        cands = list(pool)
        stream(seed, 0, "objectives", seat).shuffle(cands)
        hand: list[dict[str, Any]] = []
        targeted = False
        for o in cands:
            if len(hand) >= dealt:
                break
            if o.targeted:
                if targeted or nxt[seat] == seat:
                    continue
                targeted = True
                hand.append({"id": o.id, "target": nxt[seat]})
            else:
                hand.append({"id": o.id})
        out[seat] = hand
    return out


def science_view(state: GameState, rs: Ruleset, seat: str) -> dict[str, Any]:
    """list_techs: the seat's science points, points per tier and progress of every branch (SCORING.md 8)."""
    player = state.players[seat]
    key = stat_key(state, rs, seat)
    tiers = {t: _stat(rs, _tier_stat(t), key) for t in sorted({d.tier for d in rs.techs.values()})}
    bonus = _stat(rs, "score.science.branch", key)
    done = set(complete_branches(rs, player))
    branches = []
    for bid in sorted(rs.branches, key=lambda b: (rs.branches[b].order, b)):
        techs = branch_techs(rs, player.faction, bid)
        if techs:
            branches.append({"id": bid, "name": rs.branches[bid].name, "have": sum(t in player.techs for t in techs),
                             "total": len(techs), "complete": bid in done})
    return {"points": science(state, rs, seat, key) * rs.weight("science"), "tier_points": tiers,
            "branch_bonus": bonus, "branches": branches, "weight": rs.weight("science")}


def science_text(view: Mapping[str, Any]) -> str:
    tiers = ", ".join(f"ярус {t} +{n}" for t, n in view["tier_points"].items())
    branches = ", ".join(f"{b['name']} {b['have']}/{b['total']}" + (" ✓" if b["complete"] else "")
                         for b in view["branches"])
    weight = f" ×{view['weight']}" if view["weight"] != 1 else ""
    return (f"Очки науки: {view['points']} (за изученную приказом: {tiers}; подарки — 0){weight}. Ветки: {branches}"
            f" — +{view['branch_bonus']} за ветку.")


# ---------------------------------------------------------------- fog (SCORING.md 5)

def score_known(state: GameState, rs: Ruleset, viewer: str, seat: str) -> dict[str, Any]:
    """What `viewer` knows of `seat`'s turn score: its own in full (`exact`); another seat's territory over the
    provinces the viewer sees now (a lower bound), its great works by the public start and completion events and the
    public map (`known`), science and war unknown (None). `score` is the sum of the known parts."""
    if viewer == seat:
        parts = score_parts(state, rs)[seat] if not rs.default_scoring else {"territory": territory(state, rs, seat)}
        return {"exact": True, "score": sum(parts.values()), "parts": parts}
    from .resolve import visible_provinces      # resolve imports this module
    key = stat_key(state, rs, seat)
    parts: dict[str, int | None] = {}
    for src in turn_sources(rs):
        if src == "territory":
            parts[src] = rs.weight(src) * territory(state, rs, seat, visible_provinces(state, rs, viewer), key)
        elif src == "works":
            parts[src] = rs.weight(src) * works(state, rs, seat, key)
        else:
            parts[src] = None
    penalty = betrayal(state, rs, seat, key)
    if penalty:         # pact breaks are public events: every seat knows the penalty exactly
        parts[BETRAYAL] = penalty
    out = {"exact": False, "score": sum(v for v in parts.values() if v is not None), "parts": parts}
    if not rs.default_scoring:
        out["hidden_max"] = sum(hidden_bound(state, rs, viewer, seat).values())
    return out


def _all_artifact_grants(rs: Ruleset) -> set[str]:
    """Every tech some artifact of the pack may grant (a rival's artifacts are hidden)."""
    return {e["tech"] for a in rs.artifacts.values() for e in a.effects if e["type"] == "grant_tech"}


def hidden_bound(state: GameState, rs: Ruleset, viewer: str, seat: str) -> dict[str, int]:
    """Core 0.8.2 (decision 33, m11: a seat read a rival's «≥9» as its score, it had 29): an upper bound, per source
    (weighted), of what `seat`'s score may hold beyond what `viewer` sees (score_known), the secret objective at the
    end included — computed from public facts and rule caps only, never from the hidden values themselves:
    - territory: the seat's provinces beyond the viewer's sight (owners are public), province + capital points;
    - science: its techs researched by order are at most its public tech count less its faction's start techs
      (factions are public; artifact grants are not counted) and at most one per resolution played: the best tier
      points of that many techs its faction may research, plus the branch bonus for as many branches as that many
      techs (and every tech an artifact may grant) could complete;
    - war, intel: their caps for the seat's faction, 0 before the first recon step and resolution;
    - objectives: the best points in the deal pool per objective the seat holds (0 for an eliminated seat).
    Score stats are read for the seat's faction (public); in the core only factions modify score.* stats (pinned by
    tests). Works and the pact-break penalty are known exactly, so they add nothing."""
    from .resolve import visible_provinces      # resolve imports this module
    from .techs import public_techs
    player = state.players[seat]
    key = player.faction or None
    played = max(0, state.turn - 1) if state.phase != "game_over" else state.turn
    # the recon step (intel points, SPY_V2.md 5) runs when actions close, before the turn's resolution
    scored = played > 0 or state.phase in ("negotiation_2", "game_over")
    out: dict[str, int] = {}
    if on(rs, "territory"):
        seen = visible_provinces(state, rs, viewer)
        one, capital = _stat(rs, "score.territory.province", key), _stat(rs, "score.territory.capital", key)
        out["territory"] = rs.weight("territory") * sum(one + (capital if p.id in rs.map.capitals else 0)
                                                        for p in state.owned(seat) if p.id not in seen)
    if on(rs, "science") and rs.techs:
        faction = rs.factions.get(player.faction)
        start = ({e["tech"] for e in faction.start_effects if e["type"] == "grant_tech"} if faction else set())
        n = max(0, min(played, len(public_techs(rs, player)) - len(start - _all_artifact_grants(rs))))
        if rs.research_resource is None:
            n = 0
        avail = [t for t, d in rs.techs.items() if (not d.factions or player.faction in d.factions) and t not in start]
        values = sorted((_stat(rs, _tier_stat(rs.techs[t].tier), key) for t in avail), reverse=True)
        points = sum(values[:n])
        bonus = _stat(rs, "score.science.branch", key)
        if bonus > 0:
            spare = n + len(_all_artifact_grants(rs))
            needs = sorted(len([t for t in branch_techs(rs, player.faction, b) if t not in start])
                           for b in rs.branches if branch_techs(rs, player.faction, b))
            done = 0
            for need in needs:
                if need > spare:
                    break
                spare -= need
                done += 1
            points += bonus * done
        out["science"] = rs.weight("science") * points
    if on(rs, "war"):
        if not scored:
            cap = 0
        elif war_by_province(rs):
            total = _stat(rs, "score.war.province_total", key)
            cap = total if total > 0 else _stat(rs, "score.war.province_cap", key) * len(rs.map.provinces)
        else:
            cap = _stat(rs, "score.war.cap", key)
        out["war"] = rs.weight("war") * cap
    if on(rs, "intel"):
        out["intel"] = rs.weight("intel") * (_stat(rs, "score.intel.cap", key) if scored else 0)
    if on(rs, "objectives") and player.alive:
        pool = rs.scenario.objectives.get("pool") or tuple(rs.objectives)
        best = max((rs.objectives[o].points for o in pool if o in rs.objectives), default=0)
        out["objectives"] = rs.weight("objectives") * best * len(player.objectives)
    return out


# ---------------------------------------------------------------- texts

def objective_text(rs: Ruleset, state: GameState | None, entry: Mapping[str, Any]) -> str:
    """The condition of an objective in Russian (`entry`: a dealt {id, target?} or {id})."""
    obj = rs.objectives.get(entry.get("id", ""))
    if obj is None:
        return "?"
    p = obj.params
    n = p.get("count", 1)
    if obj.kind == "hold_type":
        pt = rs.province_types.get(p["type"])
        return f"в конце держать ≥{n} провинц. типа «{pt.name if pt else p['type']}»"
    if obj.kind == "hold_count":
        return f"в конце держать ≥{n} провинций"
    if obj.kind == "hold_foreign":
        how = " (взятых боем, не обменом)" if p.get("via") == "capture" else ""
        return f"в конце держать ≥{n} провинц. из стартов других игроков{how}"
    if obj.kind == "capital_other":
        return f"в конце держать ≥{n} чужих стартовых столиц"
    if obj.kind == "techs_branch":
        br = rs.branches.get(p["branch"])
        return f"изучить приказом ≥{n} технологий ветки «{br.name if br else p['branch']}» (подарки не в счёт)"
    if obj.kind == "great_work":
        return f"в конце держать ≥{n} достроенных великих строек"
    if obj.kind == "rival_capital_taken":
        target = entry.get("target")
        who = (state.players[target].nick if state is not None and target in state.players
               else "соперника (назначается при раздаче)")
        return f"взять столицу {who}: владеть ею в конце (в любом статусе) или захватить её хоть раз"
    return obj.kind


def objectives_view(state: GameState, rs: Ruleset, seat: str) -> list[dict[str, Any]]:
    """`you.objectives` of get_state: [{id, name, kind, params, text, points, target?, target_nick?, met_now, have,
    need}] (`kind` and `params` as in the pack: the bots and scripts plan with them, the text says the same)."""
    out = []
    for st in objective_status(state, rs, seat):
        obj = rs.objectives.get(st["id"])
        entry: dict[str, Any] = {"id": st["id"], "name": obj.name if obj else st["id"],
                                 "kind": obj.kind if obj else None, "params": dict(obj.params) if obj else {},
                                 "text": objective_text(rs, state, st), "points": st["points"],
                                 "met_now": st["met"], "have": st["have"], "need": st["need"]}
        if obj is not None and obj.difficulty:
            entry["difficulty"] = obj.difficulty        # core 0.6.0: easy | medium | hard (points grow with it)
        if st.get("target"):
            entry["target"] = st["target"]
            entry["target_nick"] = state.players[st["target"]].nick if st["target"] in state.players else None
        out.append(entry)
    return out


def _parts_text(parts: Mapping[str, int | None], exact: bool) -> str:
    items = []
    for src in (*TURN_SOURCES, BETRAYAL):
        if src not in parts:
            continue
        v = parts[src]
        if v is None:
            items.append(f"{LABELS[src]} ?")
        else:
            items.append(f"{LABELS[src]} {'' if exact or src != 'territory' else '≥'}{v}")
    return "[" + " ".join(items) + "]" if items else ""


def score_line(state: GameState, rs: Ruleset, viewer: str, tags: Mapping[str, str] | None = None) -> str:
    """The one score breakdown for a model (<= SCORE_LINE_LIMIT chars, SCORING.md 8):
    «Счёт: ты 12 [зем 8 нау 3 стр 0 вой 1] | Борей ≥7 [зем ≥5 стр 2 нау ? вой ?] | … | твоя цель «…» +5 — сейчас: нет».
    Under the default scoring: «Счёт: ты 5 | Борей ≥3 …» (others: what the viewer sees). `tags` {seat: text} (the
    public faction names, view.py) follow the names: «ты (Литания) 12 …»; a rival's «≥» has its upper end next to it
    («Борей ≥7 ≤31», core 0.8.2). The shorter forms drop the others' breakdowns first, then the fog note's list of
    hidden sources, then the own breakdown, then the tags."""
    if state.phase == "game_over" and state.result and "scores" in state.result:
        return final_line(state, rs, viewer, state.result)
    known = {s: score_known(state, rs, viewer, s) for s in state.players}

    def head(s: str, with_tags: bool) -> str:
        p = state.players[s]
        name = "ты" if s == viewer else p.nick
        tag = (tags or {}).get(s) if with_tags else None
        k = known[s]
        num = f"{k['score']}" if k["exact"] else f"≥{k['score']}"
        # core 0.8.2 (decision 33): the scale of the fog — the rival's total now is at most «≥» + what the hidden
        # sources could add; written as the upper end «≥9 ≤47» (4 characters a rival: review F1, the 4-seat line)
        bound = (f" ≤{k['score'] + k['hidden_max']}" if not k["exact"] and p.alive and "hidden_max" in k else "")
        return f"{name}{f' ({tag})' if tag else ''} {num}{bound}" + ("" if p.alive else " выбыл")

    full_fog = fog_note(rs, known)
    # review F1: the list of hidden sources gives way before the own breakdown and the tags («≤» gives the scale)
    short_fog = full_fog.split(";")[0]

    def compose(detail_others: bool, detail_self: bool, with_tags: bool, with_fog: bool | str = True) -> str:
        fog = short_fog if with_fog == "short" else full_fog
        items = []
        for s in state.players:
            text = head(s, with_tags)
            show = detail_self if s == viewer else detail_others
            if show and not rs.default_scoring:
                parts = known[s]["parts"]
                text += " " + _parts_text(parts, known[s]["exact"])
            items.append(text)
        tail = _objective_tail(state, rs, viewer)
        return "Счёт: " + " | ".join(items + ([fog] if fog and with_fog else []) + ([tail] if tail else []))

    forms = ((True, True, True), (False, True, True), (False, False, True), (True, True, False),
             (False, True, False), (False, False, False))
    t = bool(tags)
    # review F1: before the own breakdown goes, the fog note keeps only «чужой «≥» — только видимое»
    lead = ((True, True, t, True), (False, True, t, True), (False, True, t, "short"))
    line = ""
    # core 0.8.1: the fog note goes last of all
    for others, me, with_tags, with_fog in lead + tuple((*f, fog) for fog in (True, "short", False) for f in forms):
        if with_tags and not tags:
            continue
        line = compose(others, me, with_tags, with_fog)
        if len(line) <= SCORE_LINE_LIMIT:
            return line
    return line[:SCORE_LINE_LIMIT - 1] + "…"


def fog_note(rs: Ruleset, known: Mapping[str, Mapping[str, Any]]) -> str:
    """Core 0.8.1 (decision 32, m10: a seat took a rival's «≥15» for its score, it had 35): what a rival's «≥» leaves
    out — «чужой «≥» — только видимое; скрыты: наука, война, разведка, тайная цель»; '' when nothing is hidden (the
    default scoring, after the game)."""
    if rs.default_scoring:
        return ""
    hidden = [NAMES[src] for src in TURN_SOURCES
              if any(not k["exact"] and k["parts"].get(src, 0) is None for k in known.values())]
    if not any(not k["exact"] for k in known.values()):
        return ""
    if on(rs, "objectives"):
        hidden.append(NAMES["objectives"])
    # review F5: one hidden source is «скрыта: наука», as cli._victory_goal_line says it
    return "чужой «≥» — только видимое" + (f"; {'скрыты' if len(hidden) > 1 else 'скрыта'}: {', '.join(hidden)}"
                                            if hidden else "")


def final_line(state: GameState, rs: Ruleset, viewer: str, result: Mapping[str, Any]) -> str:
    """After the game everything is open: «Итог: ты 21 [зем 10 нау 4 стр 2 вой 2 цель 3] | Борей 18 [...] | цели:
    ты «Хозяин кузниц» ✓, Борей «Житница» ✗» (<= SCORE_LINE_LIMIT chars; shorter forms drop the goals, then the
    breakdowns)."""
    final = result.get("scores") or {}
    parts_all = result.get("score_parts") or {}
    objectives = result.get("objectives") or {}

    def name(s: str) -> str:
        return "ты" if s == viewer else state.players[s].nick

    def item(s: str, detail: bool) -> str:
        text = f"{name(s)} {final.get(s, 0)}" + ("" if state.players[s].alive else " выбыл")
        parts = parts_all.get(s) or {}
        if detail and parts and not rs.default_scoring:
            text += " [" + " ".join(f"{LABELS[src]} {parts[src]}" for src in PART_ORDER if src in parts) + "]"
        return text

    goals = []
    for s in state.players:
        for o in objectives.get(s) or []:
            obj = rs.objectives.get(o["id"])
            goals.append(f"{name(s)} «{obj.name if obj else o['id']}» {'✓' if o['met'] else '✗'}")
    line = ""
    for detail, with_goals in ((True, True), (True, False), (False, True), (False, False)):
        tail = [f"цели: {', '.join(goals)}"] if goals and with_goals else []
        line = "Итог: " + " | ".join([item(s, detail) for s in state.players] + tail)
        if len(line) <= SCORE_LINE_LIMIT:
            return line
    return line[:SCORE_LINE_LIMIT - 1] + "…"


def _objective_tail(state: GameState, rs: Ruleset, seat: str) -> str:
    if not on(rs, "objectives") or seat not in state.players:
        return ""
    items = []
    for st in objective_status(state, rs, seat):
        obj = rs.objectives.get(st["id"])
        name = obj.name if obj else st["id"]
        pts = rs.weight("objectives") * st["points"]
        now = "да" if st["met"] else ("нет" if st["need"] <= 1 else f"нет ({st['have']}/{st['need']})")
        items.append(f"твоя цель «{name}» +{pts} — сейчас: {now}")
    return "; ".join(items)


def final_text(state: GameState, rs: Ruleset, result: Mapping[str, Any]) -> list[str]:
    """After the game: «Аргос 21 = зем 10 + нау 4 + стр 2 + вой 2 + цель 3 (Хозяин кузниц ✓)» per seat."""
    lines = []
    parts_all = result.get("score_parts") or {}
    objectives = result.get("objectives") or {}
    final = result.get("scores") or {}
    for seat, p in state.players.items():
        parts = parts_all.get(seat) or {}
        body = " + ".join(f"{LABELS[src]} {parts[src]}" for src in SOURCES if src in parts)
        if parts.get(BETRAYAL):
            body += f" − {LABELS[BETRAYAL]} {-parts[BETRAYAL]}"
        objs = []
        for o in objectives.get(seat) or []:
            obj = rs.objectives.get(o["id"])
            objs.append(f"{obj.name if obj else o['id']} {'✓' if o['met'] else '✗'}")
        tail = f" ({', '.join(objs)})" if objs else ""
        dead = "" if p.alive else " [выбыл]"
        lines.append(f"{p.nick} {final.get(seat, 0)} = {body or '0'}{tail}{dead}")
    return lines
