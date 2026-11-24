"""Spectator API and the /watch page (CONCEPT.md section 10: the spectator sees the map one turn late, and
everything once the game is over).

Access is decided per request by the Bearer key; seat keys are never accepted here, and the spectator key never
works on the player routes (its prefix `dahw_` is not a seat key):

    admin key ($DAH_ADMIN_KEY)      level `live`      everything, live: units, resources, orders of resolved turns,
                                                      all messages, the turn in progress
    watch key ($DAH_WATCH_KEY)      level `live`      the same, but read-only: it opens nothing outside /v1/spectate,
                                                      so the master admin key need not be typed into a browser
    spectator key, game running     level `delayed`   political map, public events, scores and public messages up
                                                      to the last resolved turn minus one; live phase and ready N/M
    spectator key, game over        level `revealed`  everything (full reveal)

A "turn n" snapshot is the state after turn n was resolved (n = 0 is the starting map) with the events of turn n's
report and the messages written during turn n. For the admin, n = resolved + 1 is the turn in progress (live
state, its messages so far, no events yet). Snapshots of resolved turns never change and are cached; so is the
timing report, until the journal file changes (rebuilt in a worker thread, at most once per TIMINGS_MIN_AGE s).
Spectator calls are not journaled (a page polls every 2 s).

The handlers stay on the event loop on purpose: Game has no lock and every player route mutates it on the loop, so
running them in a threadpool would race `tick()` and the message list. Only the journal parse, which reads nothing
but the append-only journal file, runs in a thread.

Diplomacy (CONCEPT.md section 7): `pacts` of a snapshot are the pacts in force after that turn. The delayed spectator
sees them, and gifts and pact events in the feed, as public facts only: who gave to whom (no amounts), who signed,
left or broke a pact (no pact ids, which would count a pair's private proposals); proposals and refusals are hidden
until the game is over. The game's own inbox notices (`system` entries of the message log) are not player messages and
are not listed under messages: the events say the same.

The delayed spectator never sees the game-wide message id (its gaps would count the private messages in between):
public messages are numbered among public messages only. Seat kinds, bot types and declared setups are hidden until
the game is over, as they are from the players.

Stage 2 (TECH_AND_SETUP.md section 4): `live` has `setup` while a setup phase runs (mode, ready N/M; the admin also
sees each seat's choice so far). Snapshots taken during the setup phase are of the provisional turn 1 and are never
cached (turn 1 is rebuilt when the phase closes). Every level sees each seat's faction once the setup phase has closed
(as the players do); the full levels also see artifacts, techs, research progress and the active tech, the delayed
spectator only the number of techs each seat has researched. Setup choices are never shown to the delayed spectator.

Stage 3 (docs/SPY_AND_CONQUEST.md section 8): with conquest on, every level sees the public marks of provinces
(`status`: occupied {by, from, since}, ruined_until, ravaged_until), the claims and the public fates, revolts, claims
and ruins in the feed; the full levels also see each seat's conquest policy and the fates with prices, loot and source.
Espionage is private: the delayed spectator sees only «саботаж в P07» without nicks, never Shades, missions or captures;
the full levels see each seat's Shades (count, missions) and the turn's captures (`shade_captured` with the owner and
whether the victim learned it) next to the sabotage with its author.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from dah.engine import GameState, TurnReport, scores, stat_key
from dah.engine import scoring as sc_
from dah.engine import conquest as cq
from dah.engine import diplomacy as dip
from dah.engine.techs import public_techs
from dah.engine.view import PHASE_LABELS, event_text
from dah.game import GAME_OVER, SETUP, Game, iso_time
from dah.journal import read_events
from dah.report import build_report
from dah.store import hash_key

LIVE, DELAYED, REVEALED = "live", "delayed", "revealed"
TIMINGS_MIN_AGE = 5.0  # seconds: a live game's journal changes on nearly every call
LEVEL_LABELS = {LIVE: "Админ (вживую)", DELAYED: "Зритель (задержка 1 ход)", REVEALED: "Партия окончена — всё открыто"}
# Events that concern only their own seat: shown with the seat's nick in front.
_PERSONAL_KINDS = frozenset({"economy", "starvation", "recruited", "build_started", "build_done", "order_rejected",
                             "retreat", "retreat_lost", "fortify_broken", "research_started", "tech_completed",
                             "feint", "mission_started", "mission_end", "strike_cancelled", "policy_set",
                             "policy_unaffordable", "dispose_failed"})
WATCH_PAGE = Path(__file__).with_name("watch.html")
_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


@lru_cache(maxsize=1)
def watch_page_html() -> str:
    return WATCH_PAGE.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def page_headers() -> dict[str, str]:
    """The page's own inline script is allowed by its sha256 and there is no 'unsafe-inline': an injected <script>
    or on*= handler would not run even if some player text ever reached an HTML sink."""
    hashes = [base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
              for body in _SCRIPT_RE.findall(watch_page_html())]
    script_src = " ".join(f"'sha256-{h}'" for h in hashes) or "'none'"
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (f"default-src 'none'; script-src {script_src}; style-src 'unsafe-inline'; "
                                    "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                                    "frame-ancestors 'none'"),
    }


class SpectateError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def resolved_turns(game: Game) -> int:
    """Number of fully resolved turns."""
    return game.state.turn if game.game_over else game.state.turn - 1


def last_viewable(game: Game, level: str) -> int:
    done = resolved_turns(game)
    if level == LIVE:
        return done if game.game_over else done + 1
    if level == REVEALED:
        return done
    return max(0, done - 1)


class GameCache:
    """Immutable per-turn data of one game (snapshots, reports) and the journal-based timing report."""

    def __init__(self) -> None:
        self.snapshots: dict[tuple[int, str], dict[str, Any]] = {}
        self.reports: dict[int, dict[str, Any] | None] = {}
        self.start_scores: dict[str, int] | None = None
        self.start_parts: dict[str, dict[str, int]] | None = None     # turn-0 score by source (scored games)
        self.spectator_hash: tuple[tuple[int, int, int] | None, str | None] | None = None
        # (journal stamp, built at monotonic time, payload)
        self.timings: tuple[tuple[int, int], float, dict[str, Any]] | None = None
        self.timings_lock = asyncio.Lock()

    def scores_at_start(self, game: Game) -> dict[str, int]:
        if self.start_scores is None:
            raw = game.gdir.load_turn("states", 1)
            if raw is None:
                return {}
            start = scores(GameState.from_dict(raw), game.ruleset)
            if game.phase == SETUP:  # provisional: turn 1 is rebuilt when the setup phase closes
                return start
            self.start_scores = start
        return self.start_scores

    def parts_at_start(self, game: Game) -> dict[str, dict[str, int]]:
        """Turn-0 score by source (non-default scoring), cached like `scores_at_start`."""
        if self.start_parts is None:
            raw = game.gdir.load_turn("states", 1)
            if raw is None:
                return {}
            start = sc_.score_parts(GameState.from_dict(raw), game.ruleset)
            if game.phase == SETUP:  # provisional: turn 1 is rebuilt when the setup phase closes
                return start
            self.start_parts = start
        return self.start_parts

    def load_spectator_hash(self, game: Game) -> str | None:
        """The stored hash, re-read only when spectator.json changes (a reissued key is picked up at once)."""
        path = game.gdir.spectator_path
        try:
            st = path.stat()
            stamp: tuple[int, int, int] | None = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = None
        if self.spectator_hash is None or self.spectator_hash[0] != stamp:
            self.spectator_hash = (stamp, game.gdir.load_spectator_hash() if stamp else None)
        return self.spectator_hash[1]

    def report(self, game: Game, turn: int) -> dict[str, Any] | None:
        if turn not in self.reports:
            raw = game.gdir.load_turn("reports", turn)
            if raw is None:
                return None
            self.reports[turn] = raw
        return self.reports[turn]


def _caches(request: Request) -> dict[str, GameCache]:
    return request.app.state.spectate_cache


def cache_of(request: Request, game: Game) -> GameCache:
    return _caches(request).setdefault(game.config["game_id"], GameCache())


# ---------------------------------------------------------------- access

def _same(given: str, expected: str | None) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def access_level(game: Game, key: str, admin_key: str | None, watch_key: str | None = None,
                 cache: GameCache | None = None) -> tuple[str, str] | None:
    """(access, level) for a key, or None. Only the admin key, the read-only watch key and this game's spectator
    key are accepted."""
    if not key:
        return None
    if _same(key, admin_key):
        return "admin", LIVE
    if _same(key, watch_key):
        return "watcher", LIVE
    expected = cache.load_spectator_hash(game) if cache is not None else game.gdir.load_spectator_hash()
    if expected and hmac.compare_digest(hash_key(key).encode("ascii"), expected.encode("ascii")):
        return "spectator", REVEALED if game.game_over else DELAYED
    return None


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _authorize(request: Request, game_id: str) -> tuple[Game, str, str]:
    admin_key = request.app.state.admin_key
    watch_key = getattr(request.app.state, "watch_key", None)
    key = _bearer(request)
    game = request.app.state.registry.get(game_id)
    if game is None:
        if _same(key, admin_key) or _same(key, watch_key):
            raise SpectateError(404, "NOT_FOUND", f"нет партии {game_id!r}")
        raise SpectateError(401, "UNAUTHORIZED", "нужен ключ зрителя или админ-ключ")
    found = access_level(game, key, admin_key, watch_key, cache_of(request, game))
    if found is None:
        raise SpectateError(401, "UNAUTHORIZED", "нужен ключ зрителя или админ-ключ (ключ места здесь не подходит)")
    game.tick()
    access, _ = found
    level = LIVE if access in ("admin", "watcher") else (REVEALED if game.game_over else DELAYED)
    return game, access, level


# ---------------------------------------------------------------- data

def _short_name(name: str) -> str:
    word = name.split()[-1] if name.split() else name
    return word[:10]


def meta(game: Game, access: str, level: str) -> dict[str, Any]:
    rs = game.ruleset
    m = rs.map
    capitals = m.capitals
    provinces = [{"id": pid, "name": p.name, "short": _short_name(p.name), "type": p.type,
                  "type_name": rs.province_types[p.type].name if p.type in rs.province_types else p.type,
                  "capital": pid in capitals, "pos": list(m.layout[pid]), "neighbors": list(m.adjacency[pid])}
                 for pid, p in m.provinces.items()]
    edges = sorted({tuple(sorted((a, b))) for a, ns in m.adjacency.items() for b in ns})
    start = GameState.from_dict(game.gdir.load_turn("states", 1) or game.state.to_dict())
    home = {p.owner: pid for pid, p in start.provinces.items() if pid in capitals and p.owner}
    seats = []
    for s in game.config["seats"]:
        entry: dict[str, Any] = {"seat": s["seat"], "nick": s["nick"], "capital": home.get(s["seat"])}
        if level != DELAYED:  # players never see their opponents' kind or bot type either
            entry.update(kind=s["kind"], bot=s.get("bot"), declared=s.get("declared") or {})
        seats.append(entry)
    return {"game_id": game.config["game_id"], "scenario": {"id": rs.scenario.id, "name": rs.scenario.name},
            "turn_limit": game.config["turn_limit"], "phase_limits": game.config["phase_limits"],
            "map": {"id": m.id, "name": m.name, "layout_computed": m.layout_computed, "provinces": provinces,
                    "edges": [list(e) for e in edges]},
            "seats": seats, "access": access, "level": level, "level_label": LEVEL_LABELS[level],
            "works": sc_.on(rs, "works"), "objectives": sc_.on(rs, "objectives")}


def live(game: Game, cache: GameCache, access: str, level: str) -> dict[str, Any]:
    last = last_viewable(game, level)
    history = []
    for turn in range(0, min(last, resolved_turns(game)) + 1):
        if turn == 0:
            sc = cache.scores_at_start(game)
        else:
            rep = cache.report(game, turn)
            # the turn score is private to the players (SCORING.md 5); every spectator level sees it (delayed: late)
            sc = (rep or {}).get("scores") or (rep or {}).get("public", {}).get("scores", {})
        entry = {"turn": turn, "scores": sc}
        if not game.ruleset.default_scoring:
            entry["by_source"] = ((cache.report(game, turn) or {}).get("score_parts") if turn else
                                  cache.parts_at_start(game)) or {}
        history.append(entry)
    out: dict[str, Any] = {
        "game_id": game.config["game_id"], "turn": game.state.turn, "turn_limit": game.config["turn_limit"],
        "phase": game.phase, "phase_label": PHASE_LABELS.get(game.phase, game.phase),
        "deadline": iso_time(game.deadline) if game.deadline is not None else None, "seconds_left": game.seconds_left(), "ready": game.ready_text(),
        "phase_seq": game.phase_seq, "resolved_turns": resolved_turns(game), "last_viewable": last,
        "game_over": game.game_over, "access": access, "level": level, "level_label": LEVEL_LABELS[level],
        "timings_allowed": level != DELAYED, "scores_history": history,
    }
    if game.game_over:
        out["result"] = game.state.result
    if game.setup:
        out["setup"] = setup_info(game, level)
    return out


def setup_closed(game: Game) -> bool:
    """Factions are settled (and public): the game has no setup phase, or it has closed."""
    return not game.setup or game.phase != SETUP


def setup_info(game: Game, level: str) -> dict[str, Any]:
    """The setup phase as the header shows it; the admin (live level) also sees the choices so far."""
    setup = game.setup or {}
    out: dict[str, Any] = {"open": game.phase == SETUP, "artifacts": setup.get("artifacts"),
                           "unique_factions": bool(setup.get("unique_factions", True))}
    if level == LIVE and game.phase == SETUP:
        rs = game.ruleset
        out["choices"] = {
            seat: {"factions": [rs.factions[f].name for f in (c.get("factions") or []) if f in rs.factions],
                   "artifacts": [rs.artifacts[a].name for a in (c.get("artifacts") or []) if a in rs.artifacts]}
            for seat, c in sorted(game.setup_choices.items())}
    return out


def _end_state(game: Game, turn: int) -> GameState:
    """State after turn `turn` was resolved (0: the starting map); the live state past the last resolved turn."""
    if turn > resolved_turns(game) or (turn == resolved_turns(game) and game.game_over):
        return game.state
    raw = game.gdir.load_turn("states", turn + 1)
    if raw is None:
        raise SpectateError(404, "NOT_FOUND", f"нет снапшота хода {turn}")
    return GameState.from_dict(raw)


def _event_entry(e: dict[str, Any], game: Game, state: GameState) -> dict[str, Any]:
    try:
        text = event_text(e, game.ruleset, state, None)
    except (KeyError, TypeError, ValueError):
        text = e.get("kind", "?")
    seats = e.get("seats") or []
    if e.get("kind") in _PERSONAL_KINDS and seats and seats[0] in state.players:
        text = f"{state.players[seats[0]].nick}: {text}"
    return {**e, "text": text}


def _public_events(report: dict[str, Any], game: Game, state: GameState) -> list[dict[str, Any]]:
    public = report.get("public", {})
    events = [{"kind": "captured", "province": c["province"], "from": c["from"], "to": c["to"],
               **({"status": c["status"]} if c.get("status") else {}),
               "seats": sorted(s for s in (c["from"], c["to"]) if s)} for c in public.get("ownership_changes", [])]
    events += [{"kind": "eliminated", "province": None, "seat": s, "seats": [s]} for s in public.get("eliminated", [])]
    events += [dip.public_event(e) for e in public.get("diplomacy", [])]
    # conquest (SPY_AND_CONQUEST.md 2.7, 8): fates, revolts, claims, ruins in their public form (no prices, loot or
    # sources); captures are the ownership changes above
    events += [e for e in public.get("conquest", []) if e.get("kind") != "captured"]
    events += [dict(e) for e in public.get("works", [])]     # great works: public start and completion (SCORING.md 9)
    # espionage (SPY_AND_CONQUEST.md 8): only «саботаж в P07», without nicks (the strike shows in the broken fortify);
    # no Shades, missions or captures ever
    events += [{"kind": "sabotage", "province": e.get("province"), "seats": []}
               for e in report.get("events", []) if e.get("kind") == "sabotage"]
    return [_event_entry(e, game, state) for e in events]


def _capture_events(game: Game, turn: int, state: GameState) -> list[dict[str, Any]]:
    """Full levels only: the Shades caught at the recon step of `turn` (owner, victim, whether it was attributed)."""
    rep = game.gdir.load_turn("recon", turn) if game.ruleset.system("espionage") else None
    nick = {s: p.nick for s, p in state.players.items()}
    out = []
    for c in (rep or {}).get("caught", []):
        victim = nick.get(c.get("victim"), "нейтральная провинция") if c.get("victim") else "нейтральная провинция"
        known = "хозяин установлен" if c.get("attributed") else "хозяин не установлен"
        mission = c.get("mission", "?").rsplit(":", 1)[-1]
        out.append({"kind": "shade_captured", "province": c.get("province"), "seats": sorted(
                    x for x in (c.get("owner"), c.get("victim")) if x), "owner": c.get("owner"),
                    "victim": c.get("victim"), "attributed": bool(c.get("attributed")), "mission": c.get("mission"),
                    "target": c.get("target"),
                    "text": f"Тень {c.get('shade')} ({nick.get(c.get('owner'), c.get('owner'))}) поймана в "
                            f"{c.get('province')} у {victim}: {mission} → {c.get('target')}; {known}"})
    return out


def _shades(state: GameState, seat: str) -> list[dict[str, Any]]:
    out = []
    for sh in state.players[seat].shades:
        m = sh.get("mission") or {}
        out.append({"id": sh["id"], "status": sh.get("status"), "xp": sh.get("xp", 0),
                    **({"mission": m.get("type"), "target": m.get("target"), "played": m.get("played"),
                        "rounds": m.get("rounds"), "ended": m.get("ended")} if m else {})})
    return out


def _pacts(state: GameState, full: bool) -> list[dict[str, Any]]:
    """Pacts in force in `state` (public: sides and terms); the full view adds ids and every proposal and ended pact."""
    nick = {s: p.nick for s, p in state.players.items()}
    rows = state.pacts if full else dip.active_pacts(state)
    out = []
    for pact in rows:
        entry = {**dip.public_pact(pact), "status": pact["status"]}
        if full:
            entry.update(id=pact["id"], proposed_by=pact["a"], broken_by=list(pact.get("broken_by") or []),
                         ended_turn=pact.get("ended_turn"), ended_reason=pact.get("ended_reason"))
        entry["nicks"] = [nick.get(s, s) for s in entry["seats"]]
        out.append(entry)
    return out


def _message_entry(m: dict[str, Any], state: GameState, number: int) -> dict[str, Any]:
    nick = {s: p.nick for s, p in state.players.items()}
    public = m["to"] == "all"
    return {"id": number, "ts": m.get("ts"), "turn": m["turn"], "phase": m["phase"], "from": m["from"],
            "from_nick": nick.get(m["from"], m["from"]), "to": "all" if public else list(m["to"]),
            "to_nicks": "all" if public else [nick.get(s, s) for s in m["to"]], "public": public, "text": m["text"]}


def snapshot(game: Game, cache: GameCache, turn: int, level: str) -> dict[str, Any]:
    last = last_viewable(game, level)
    if turn < 0 or turn > (resolved_turns(game) + (0 if game.game_over else 1)):
        raise SpectateError(404, "NOT_FOUND", f"хода {turn} нет")
    if turn > last:
        raise SpectateError(403, "NOT_YET", f"ход {turn} ещё не открыт для зрителей (виден до хода {last})")
    in_progress = turn > resolved_turns(game)
    provisional = game.phase == SETUP  # turn 1 is rebuilt when the setup phase closes: nothing is cached
    key = (turn, level)
    if not in_progress and not provisional and key in cache.snapshots:
        return cache.snapshots[key]
    full = level != DELAYED
    state = _end_state(game, turn)
    rs = game.ruleset
    sc = scores(state, rs)
    parts = sc_.score_parts(state, rs) if not rs.default_scoring else None
    if parts is not None and state.phase == GAME_OVER and (state.result or {}).get("score_parts"):
        # the end: the final score by source, the revealed objectives included (SCORING.md 9)
        parts, sc = state.result["score_parts"], dict(state.result.get("scores") or sc)
    conquest = rs.system("conquest")
    statuses = cq.statuses_for(state) if conquest else {}
    provinces = []
    works_on = sc_.on(rs, "works")
    for pid, p in sorted(state.provinces.items()):
        entry: dict[str, Any] = {"id": pid, "owner": p.owner}
        if pid in statuses:
            entry["status"] = statuses[pid]      # public marks: occupied, ruined_until, ravaged_until
        if works_on and not full:
            # great works are public (SCORING.md 2.3, 9): the delayed level gets their map marks, ordinary
            # buildings stay hidden; the full levels see them in `buildings`/`building_now`
            marks = [{"building": b, "done": True} for b in p.buildings
                     if b in rs.buildings and rs.buildings[b].score]
            marks += [{"building": b.building, "turns_left": b.turns_left, "seat": b.seat}
                      for b in state.pending_builds if b.province == pid
                      and b.building in rs.buildings and rs.buildings[b.building].score]
            if marks:
                entry["great_works"] = marks
        if full:
            entry.update(units=dict(p.units), unit_total=p.unit_total(), buildings=list(p.buildings),
                         fortified=p.fortified,
                         building_now=[{"building": b.building, "turns_left": b.turns_left, "seat": b.seat}
                                       for b in state.pending_builds if b.province == pid])
        provinces.append(entry)
    players = []
    factions_public = setup_closed(game) and (len(rs.scenario.factions) > 1 or bool(game.setup))
    for seat, p in state.players.items():
        entry = {"seat": seat, "nick": p.nick, "alive": p.alive, "score": sc.get(seat, 0),
                 "provinces": len(state.owned(seat))}
        if parts is not None:
            entry["score_parts"] = parts.get(seat, {})
            if sc_.on(rs, "objectives"):
                # secret until the game is over («?» for the delayed spectator); the full levels see them and progress
                if full or state.phase == GAME_OVER:
                    entry["objectives"] = sc_.objectives_view(state, rs, seat)
                else:
                    entry["objectives"] = "?"
        if factions_public:
            entry.update(faction=p.faction, faction_name=rs.factions[p.faction].name if p.faction in rs.factions
                         else p.faction)
        if rs.techs:
            entry["techs_count"] = len(public_techs(rs, p))  # public (TECH_AND_SETUP.md 1.4), no artifact grants
        if full:
            entry.update(resources=dict(p.resources), command_points=p.command_points,
                         units=sum(pr.unit_total() for pr in state.owned(seat)))
            if rs.techs or rs.artifacts:
                entry.update(techs=list(p.techs), artifacts=list(p.artifacts), research=dict(p.research),
                             researching=({"tech": p.researching, "progress": p.research.get(p.researching, 0),
                                           "cost": rs.tech_cost(p.researching, stat_key(state, rs, seat))}
                                          if p.researching in rs.techs else None))
            if conquest:
                entry["conquest_policy"] = p.conquest_policy
            if rs.system("espionage"):
                entry["shades"] = _shades(state, seat)
        players.append(entry)
    events: list[dict[str, Any]] = []
    orders: dict[str, Any] | None = None
    if 1 <= turn <= resolved_turns(game):
        report = cache.report(game, turn) or {}
        if full:
            events = [_event_entry(e, game, state) for e in report.get("events", [])]
            events += _capture_events(game, turn, state)
            raw_orders = game.gdir.load_turn("orders", turn) or {}
            orders = {s: b.get("orders", []) for s, b in raw_orders.items()}
        else:
            events = _public_events(report, game, state)
    elif in_progress and full:
        # the turn in progress: its diplomacy so far (gifts with amounts, proposals) for the admin
        events = [_event_entry(e, game, state) for op in game.diplomacy_log
                  if (e := dip.op_event(op, state)) is not None]
    messages = []
    public_seen = 0
    for m in game.messages:
        if m.get("system"):  # the game's own notices: the events (and pacts) say the same
            continue
        if m["to"] == "all":
            public_seen += 1
        elif not full:
            continue
        if m["turn"] == turn:
            # delayed: numbered among public messages only; the global id would count the private ones between
            messages.append(_message_entry(m, state, m["id"] if full else public_seen))
    out: dict[str, Any] = {
        "turn": turn, "in_progress": in_progress, "level": level, "full": full,
        "provinces": provinces, "players": players, "scores": sc, "events": events, "messages": messages,
        "pacts": _pacts(state, full),
        "result": state.result if state.phase == GAME_OVER else None,
    }
    if conquest:
        out["claims"] = [{**c, "nick": state.players[c["seat"]].nick if c["seat"] in state.players else c["seat"]}
                         for c in state.claims]
    if orders is not None:
        out["orders"] = orders
    if full:
        out["unit_names"] = {u: d.name for u, d in rs.units.items()}
        out["building_names"] = {b: d.name for b, d in rs.buildings.items()}
        out["resource_names"] = {r: d.name for r, d in rs.resources.items()}
        if rs.techs or rs.artifacts:
            out["tech_names"] = {t: d.name for t, d in rs.techs.items()}
            out["tech_branches"] = {t: d.branch for t, d in rs.techs.items()}
            out["branch_names"] = {b: d.name for b, d in rs.branches.items()}
            out["artifact_names"] = {a: d.name for a, d in rs.artifacts.items()}
    if rs.techs:
        out["techs_total"] = len(rs.techs)
    if provisional:
        out["provisional"] = True
    if not in_progress and not provisional:
        cache.snapshots[key] = out
    return out


def _journal_stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_size, st.st_mtime_ns)


def _timings_fresh(cache: GameCache, stamp: tuple[int, int], game_over: bool) -> dict[str, Any] | None:
    if cache.timings is None:
        return None
    old_stamp, built_at, payload = cache.timings
    if old_stamp == stamp or (not game_over and time.monotonic() - built_at < TIMINGS_MIN_AGE):
        return payload
    return None


async def timings(game: Game, cache: GameCache, level: str) -> dict[str, Any]:
    """The timing report. The journal parse runs in a worker thread (it reads only the append-only journal file,
    never the Game) and at most once per TIMINGS_MIN_AGE seconds per game, however many pages poll."""
    if level == DELAYED:
        raise SpectateError(403, "FORBIDDEN", "тайминги открываются зрителю после конца партии")
    path = game.gdir.journal_path
    if (hit := _timings_fresh(cache, _journal_stamp(path), game.game_over)) is not None:
        return hit
    async with cache.timings_lock:
        stamp = _journal_stamp(path)
        if (hit := _timings_fresh(cache, stamp, game.game_over)) is not None:
            return hit
        out = await asyncio.to_thread(build_timings, path)
        cache.timings = (stamp, time.monotonic(), out)
        return out


def build_timings(path: Path) -> dict[str, Any]:
    rep = build_report(read_events(path))
    phase_fields = ("first_call_s", "first_submit_s", "ready_s", "calls", "req_chars", "resp_chars", "submits",
                    "edits", "p2_edits", "validate_rejected", "submit_rejected")
    total_fields = ("calls", "req_chars", "resp_chars", "host_calls", "host_req_chars", "host_resp_chars",
                    "submits", "edits", "p2_edits", "validate_rejected", "submit_rejected", "spent_usd",
                    "submits_by_phase", "no_orders_turns", "not_ready_phases", "idle_units", "telemetry",
                    "spent_in_game_usd", "spent_after_game_usd", "in_game_s", "after_game_s", "pacts_signed",
                    "pacts_kept", "pacts_broken", "pacts_renewed", "gifts_sent", "gifts_received", "gifted_resources",
                    "received_resources")
    out = {
        "seats": rep["seats"], "rules_chars": rep["rules_chars"],
        "phases": [{"turn": ph["turn"], "phase": ph["phase"], "closed": ph["closed"], "duration_s": ph["duration_s"],
                    "seats": {s: {k: v[k] for k in phase_fields} for s, v in ph["seats"].items()}}
                   for ph in rep["phases"]],
        "totals": {s: {k: t[k] for k in total_fields} for s, t in rep["totals"].items()},
        "spent_by_turn": [{"turn": t["turn"], "spent_usd": t["spent_usd"]} for t in rep["turns"]],
    }
    return out


# ---------------------------------------------------------------- router

def _json(status: int, content: dict[str, Any]) -> Response:
    return Response(json.dumps(content, ensure_ascii=False, separators=(",", ":")), status_code=status,
                    media_type="application/json", headers={"Cache-Control": "no-store"})


def _err(exc: SpectateError) -> Response:
    return _json(exc.status, {"ok": False, "error": {"code": exc.code, "message": exc.message}})


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/spectate/{game_id}/meta")
    async def spectate_meta(game_id: str, request: Request) -> Response:
        try:
            game, access, level = _authorize(request, game_id)
            return _json(200, {"ok": True, **meta(game, access, level)})
        except SpectateError as exc:
            return _err(exc)

    @router.get("/v1/spectate/{game_id}/live")
    async def spectate_live(game_id: str, request: Request) -> Response:
        try:
            game, access, level = _authorize(request, game_id)
            return _json(200, {"ok": True, **live(game, cache_of(request, game), access, level)})
        except SpectateError as exc:
            return _err(exc)

    @router.get("/v1/spectate/{game_id}/turn/{turn}")
    async def spectate_turn(game_id: str, turn: str, request: Request) -> Response:
        try:
            game, access, level = _authorize(request, game_id)
            if not turn.isdigit() or len(turn) > 6:
                raise SpectateError(400, "INVALID", "номер хода — целое число от 0")
            return _json(200, {"ok": True, **snapshot(game, cache_of(request, game), int(turn), level)})
        except SpectateError as exc:
            return _err(exc)

    @router.get("/v1/spectate/{game_id}/timings")
    async def spectate_timings(game_id: str, request: Request) -> Response:
        try:
            game, access, level = _authorize(request, game_id)
            return _json(200, {"ok": True, **(await timings(game, cache_of(request, game), level))})
        except SpectateError as exc:
            return _err(exc)

    @router.get("/watch/{game_id}")
    async def watch_page(game_id: str) -> Response:
        """The page shell needs no key: it asks for one and sends it only as a Bearer header."""
        return HTMLResponse(watch_page_html(), headers=page_headers())

    return router
