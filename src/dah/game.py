"""Game: phases with an injectable clock, ready/confirm_hash, orders, messages, journal (SLICE.md section 5).

A Game wraps the pure engine and owns everything time- and seat-related. Every mutation is journaled and the
live snapshot is written to `private/runtime.json`, so `Game.load` restores a game after a server restart.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from dah.engine import (DeadlineInfo, GameState, Ruleset, SeatOrders, SetupChoiceError, TurnReport, amend_order,
                        artifact_pool, build_ruleset, cancel_order, check_artifacts, check_faction_prefs, cp_left,
                        describe, draft_offers, estimate, find, income_of, issue_order_id, new_game_state,
                        orders_hash, render_report_text, render_state_text, resolve, resolve_setup, rules_text,
                        rules_topic, scores, seat_reserved, setup_config, stat_key, state_hash, submit_orders, techs_text,
                        techs_view, upkeep_of, view_state, visible_provinces)
from dah.engine import diplomacy as dip
from dah.engine import discoveries as disc
from dah.engine import espionage as esp
from dah.engine.view import army_presence
from dah.engine import scoring as sc_
from dah.engine import trade as tr
from dah.engine.content import ContentError, legacy_pack_sets
from dah.engine.catalog import KIND_LABELS
from dah.engine.content import DEFAULT_DIPLOMACY_LIMITS, DEFAULT_SETUP_LIMIT
from dah.engine.model import as_int, canonical_json, sha256_json
from dah.engine.registry import SYSTEMS
from dah.engine.orders import (FLAT_MOVE_EXAMPLE, P1_LOCKED_HINT, STRING_ORDERS_EXAMPLE, decode_json_arg,
                               resolve_unit_ref)
from dah.engine.rules import Section as RulesSection
from dah.engine.rules import discoveries_section, topic_key
from dah.engine.rules import topics as rule_topics
from dah.engine.techs import province_defense_mul
from dah.engine.setup import unfit_artifacts
from dah.engine.techs import short_techs_view, techs_text_short
from dah.engine.view import PHASE_LABELS, pending_research
from dah.journal import Journal, read_events
from dah.store import GameDir, write_json, read_json

Clock = Callable[[], float]

log = logging.getLogger("dah.game")

PLAYABLE_PHASES = ("negotiation_1", "actions", "negotiation_2")
SPEND_PHASES = ("setup", *PLAYABLE_PHASES)     # phases a spend figure may be for (setup: before turn 1, turn=1)
NEGOTIATION_PHASES = ("negotiation_1", "negotiation_2")
P2 = "negotiation_2"
GAME_OVER = "game_over"
SETUP = "setup"           # before turn 1 when the game config has `setup` (TECH_AND_SETUP.md section 2)
TIMED_PHASES = (SETUP, *PLAYABLE_PHASES)
PACT_STATUS = {"active": "действует", "proposed": "предложен", "rejected": "отклонён", "withdrawn": "отозван",
               "lapsed": "не принят вовремя", "left": "окончен (выход)", "broken": "нарушен",
               "expired": "истёк срок", "ended": "окончен (сторона выбыла)", "renewed": "продление принято",
               "cancelled": "продление отменено выходом"}
NEXT_PHASE = {"negotiation_1": "actions", "actions": "negotiation_2"}
SEAT_KINDS = ("swarm", "agent", "human", "bot")
# `continue_sent`: older wakers; newer ones write `wake_sent` (a message to all members or a `continue`).
TELEMETRY_KINDS = ("continue_sent", "continue_skipped", "session_restarted", "goal_reopened", "budget_exhausted",
                   "wake_failed", "session_stopped_by_waker", "post_game_finished", "wake_sent", "wake_answered",
                   "wake_unanswered")
# How many recent player calls per seat the host's phase info lists (`player_calls`): dah-waker finds there the first
# call after a wake. A phase-status read (`phase`, `wait_phase`) is `passive`: it is not a move.
PLAYER_CALLS_KEPT = 64
# `for_phase` of a spend figure taken after game_over (dah-waker keeps reporting while the swarm wraps up).
AFTER_GAME_OVER = "after_game_over"
SPEND_SOURCES = ("participant_program", "host")
MAX_TELEMETRY_DETAIL_CHARS = 2000
DT_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Rules, identity and phase reads work in every phase (and for seats that are out): listed in every `allowed`, so an
# agent told to call only allowed tools still reads the rules and waits for the next phase.
ALWAYS_TOOLS = ["rules", "whoami", "phase", "wait_phase"]
# Content and own-tree reads (list_techs, describe) work in every phase too.
CONTENT_TOOLS = ["list_techs", "describe"]
READ_TOOLS = ["get_state", "history", "inbox", "list_pacts", *CONTENT_TOOLS, *ALWAYS_TOOLS]
SETUP_TOOLS = ["get_setup", "choose_faction", "choose_artifacts", "describe", "list_techs", "ready", "unready"]
CHECK_TOOLS = ["list_orders", "validate_orders", "estimate"]
ORDER_TOOLS = [*CHECK_TOOLS, "submit_orders", "cancel_order", "amend_order"]
DIPLOMACY_TOOLS = ["send_gift", "propose_pact", "respond_pact", "leave_pact", "list_pacts"]
SPY_TOOLS = ["list_spies", "get_intel", "estimate_mission"]     # only in games with espionage on
# core 0.8.0 (docs/TRADE.md 9): only in games with trade on; offers and answers in P1/P2, the list in every phase
TRADE_TOOLS = ["propose_trade", "respond_trade"]
TRADE_READ_TOOLS = ["list_trades"]
# Orders are given only in actions; negotiation_1 can only check them (user decision 12).
ALLOWED = {
    "setup": [*SETUP_TOOLS, "get_state", "inbox", *ALWAYS_TOOLS],
    "negotiation_1": ["get_state", *CHECK_TOOLS, "send_message", *DIPLOMACY_TOOLS, "inbox", "ready", "unready",
                      "history", *CONTENT_TOOLS, *ALWAYS_TOOLS],
    "actions": ["get_state", *ORDER_TOOLS, "list_pacts", "inbox", "ready", "unready", "history", *CONTENT_TOOLS,
                *ALWAYS_TOOLS],
    "negotiation_2": ["get_state", *ORDER_TOOLS, "send_message", *DIPLOMACY_TOOLS, "inbox", "ready", "unready",
                      "history", *CONTENT_TOOLS, *ALWAYS_TOOLS],
}
def intel_tail(intel: dict[str, Any]) -> str:
    """The espionage tail of a negotiation_2 phase text (SPY_AND_CONQUEST.md 6.3), as in dah-waker's wake text."""
    text = f"донесений: {intel.get('reports', 0)}"
    if intel.get("caught"):
        text += f"; поймано у тебя: {intel['caught']}"
    return text + " (get_intel, list_spies)"


SETUP_HINT = ("фаза старта: get_setup() → choose_faction([\"id\", ...]) → choose_artifacts([\"id\"]) → ready(); "
              "приказы — с хода 1")
# How to press ready in each phase (phase(), wake texts and rules say the same): P1 needs no hash, D and P2 need the
# orders_hash of the current orders (given in phase() too, so ready is one call).
PHASE_READY_HINTS = {
    "negotiation_1": "П1: договорись (сообщения, пакты, подношения) и сразу нажми ready() — без confirm_hash, как "
                     "только сказать и сделать больше нечего; не жди таймера",
    # the hash of the moment of the call goes stale with the first submit_orders: name it only as "if already given"
    "actions": "Д: подай приказы (submit_orders) и нажми ready(confirm_hash=orders_hash из ответа submit_orders) — "
               "хэш меняется с каждой подачей и правкой; если приказы уже поданы и верны — "
               "ready(confirm_hash=\"{orders_hash}\")",
    "negotiation_2": "П2: ответь, поправь приказы при нужде и нажми ready(confirm_hash=\"{orders_hash}\") (orders_hash "
                     "меняется с каждой правкой), как только сказать и сделать больше нечего",
}
# An eliminated seat (no provinces left; the game goes on without it): phase(), get_state() and the waker's one notice
# tell it that the game is over for it, so its goal ends now (as at game_over).
ELIMINATED_NOTE = ("ты выбыл: провинций не осталось, партия для тебя окончена — приказов и переговоров больше нет; "
                   "заверши цель одним итоговым сообщением")
SETUP_STATE_NOTE = ("ФАЗА СТАРТА: состояние предварительное — фракции и артефакты ещё не выбраны (твой выбор и "
                    "предложения — get_setup()); ход 1 строится заново, когда фаза закроется. Фракции соперников "
                    "станут известны после фазы.")
# Conquest events of a resolution written to the journal (SPY_AND_CONQUEST.md 8; a capture as `province_captured`)
CONQUEST_JOURNAL = ("disposition", "policy_set", "policy_unaffordable", "dispose_failed", "occupation_revolt",
                    "claim_created", "claim_ended", "ruin_ended")
# Espionage events of a resolution written to the journal in full (SPY_AND_CONQUEST.md 8)
ESPIONAGE_JOURNAL = ("mission_started", "mission_end", "sabotage", "strike_cancelled", "sabotage_effect",
                     "build_sabotaged")
# stage 4 (SCORING.md 9); core 0.7.0: war points per province, builds paid in parts (shares, stalls)
SCORING_JOURNAL = ("war_credit", "work_started", "work_completed", "war_points", "build_stalled", "build_paid",
                   "intel_point")      # core 0.8.0: the point of a strike's effect, credited in the resolution
# stage 5 (ABANDONED_LANDS.md 10): finds, choices, rewards, lairs in their full form (admin and offline metrics)
DISCOVERY_JOURNAL = tuple(sorted(disc.JOURNAL_KINDS))
SYSTEM_SENDER = "ИГРА"   # inbox entries written by the game itself (gifts, pacts): not player text, not wrapped
OWN_SENDER = "ты"        # inbox entries the seat sent itself (`own: true`): a swarm's members see each other's words

# A `DAH` token (Latin or Cyrillic А/Н, spaces inside) in any brackets would pass for a host wake-up message.
# It is matched after NFKC normalization (fullwidth letters) with format characters (zero-width) removed.
_DAH_RE = re.compile(r"(?<!\w)D\s*[AА]\s*[HН](?!\w)", re.IGNORECASE)
_BRACKET_CATEGORIES = frozenset({"Ps", "Pe", "Pi", "Pf"})
# A bracketed game tag ([ИГРА], (GAME)...) in a player's text would pass for an inbox entry written by the game.
_GAME_TAG_RE = re.compile(r"[\[\(\{<«‹【〔]\s*(?:ИГРА|IGRA|GAME)\s*[\]\)\}>»›】〕]", re.IGNORECASE)


class GameError(Exception):
    """A refused request: `code` is a stable reason id, `message` is Russian text for the player."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- config and ruleset

def make_config(*, game_id: str, scenario: str, seats: list[dict[str, Any]], seed: int,
                phase_limits: dict[str, int] | None = None, turn_limit: int | None = None,
                pack_dirs: list[str] | None = None, artifacts: str | None = None, artifact_offer: int | None = None,
                artifact_points: int | None = None, systems: dict[str, bool | None] | None = None,
                scoring: dict[str, int] | None = None, objectives: int | None = None,
                objectives_salt: int | None = None, discoveries: dict[str, Any] | None = None) -> dict[str, Any]:
    """Game config (SLICE.md section 5). Limits default to the scenario's. Seats: {seat, nick, kind, bot?,
    declared?, dreamteam_session_id?, faction?}.

    Setup (TECH_AND_SETUP.md section 2): the scenario's `setup` with the host's overrides (`artifacts`
    draft|points|none, `artifact_offer`, `artifact_points`) goes into `config["setup"]`, and the phase limit
    `phase_limits["setup"]` (the scenario's, else 5 min). No `setup` key: no setup phase, as in the slice. A seat's
    `faction` is its default preference.

    Stage 3 (SPY_AND_CONQUEST.md 1.1): `systems` {espionage|conquest: bool | None} overrides the scenario's switches
    (None: the scenario's). The resulting switches go into `config["systems"]` when the host overrode one or some
    system is on; a config without the key plays the scenario's switches (all off before stage 3).

    Stage 4 (SCORING.md 1.2): `scoring` {source: weight} replaces the scenario's weights (a source not named: 0) and
    `objectives` N sets the objectives dealt (and offered) per seat. The resulting weights go into
    `config["scoring"]` when the host overrode them or they are not the default {territory: 1}; `--objectives` into
    `config["objectives"]`. A config without the keys plays the scenario's scoring. `objectives_salt` (a large
    random int from the host, secret like the seed) goes into `config["objectives_salt"]` only while objectives have
    weight > 0 or finds are on; the deal and the layout of finds then come from seed and salt (SCORING.md
    «Реализация: уточнения», FOG-1; ABANDONED_LANDS.md 5.1).

    Stage 5 (ABANDONED_LANDS.md 1, 11): `systems["discoveries"]` switches the Abandoned lands; the key goes into
    `config["systems"]` only when on or overridden (configs of older games keep their two keys). `discoveries`
    {knob: value} (dah game create --discovery-extra, the simulator's knobs) goes into `config["discoveries"]`."""
    overrides = {k: v for k, v in (systems or {}).items() if v is not None}
    rs = build_ruleset(pack_dirs, scenario, overrides or None, scoring, objectives, discoveries or None)
    setup = setup_config(rs, artifacts, artifact_offer, artifact_points)
    limits = {k: v for k, v in rs.scenario.phase_limits.items() if k != SETUP}
    if setup is not None:
        limits[SETUP] = rs.scenario.phase_limits.get(SETUP, DEFAULT_SETUP_LIMIT)
    limits.update(phase_limits or {})
    normalized = []
    for s in seats:
        if s.get("kind") not in SEAT_KINDS:
            raise ValueError(f"seat {s.get('seat')!r}: kind must be one of {', '.join(SEAT_KINDS)}")
        if (s["kind"] == "bot") != bool(s.get("bot")):
            raise ValueError(f"seat {s['seat']!r}: a bot type is required exactly for kind 'bot'")
        entry = {"seat": s["seat"], "nick": s["nick"], "kind": s["kind"], "declared": dict(s.get("declared") or {}),
                 "dreamteam_session_id": s.get("dreamteam_session_id")}
        for optional in ("bot", "faction"):
            if s.get(optional):
                entry[optional] = s[optional]
        normalized.append(entry)
    config = {"game_id": game_id, "seed": seed, "scenario": rs.scenario.id, "pack_dirs": pack_dirs,
              "seats": normalized, "phase_limits": limits, "message_limits": rs.scenario.message_limits,
              "diplomacy_limits": dict(rs.scenario.diplomacy_limits),
              "turn_limit": turn_limit or rs.scenario.turn_limit}
    if setup is not None:
        config["setup"] = setup
    if overrides or rs.scenario.systems:
        # discoveries (stage 5) and trade (core 0.8.0) only when on or overridden: configs of older games keep their keys
        config["systems"] = {name: rs.system(name) for name in SYSTEMS
                             if name not in ("discoveries", "trade") or rs.system(name) or name in overrides}
    if scoring is not None or not rs.default_scoring:
        config["scoring"] = dict(rs.scenario.scoring)
    if objectives is not None:
        config["objectives"] = int(objectives)
    if objectives_salt is not None and (rs.weight("objectives") > 0 or rs.system("discoveries")):
        config["objectives_salt"] = int(objectives_salt)
    if discoveries:
        config["discoveries"] = dict(discoveries)
    return config


def _salt(config: dict[str, Any]) -> dict[str, Any]:
    """The objectives salt of a config as a `new_game_state` keyword (none in games before it: the seed alone)."""
    return {"objectives_salt": config["objectives_salt"]} if "objectives_salt" in config else {}


def secret_salt(config: dict[str, Any]) -> int | None:
    """The config's secret salt for the resolution's rolls of finds (ABANDONED_LANDS.md 5.1), or None."""
    return config.get("objectives_salt")


def diplomacy_limits(config: dict[str, Any]) -> dict[str, int]:
    """Per-phase limits of gifts and pact proposals (a config of an earlier version has none: the defaults)."""
    return {**DEFAULT_DIPLOMACY_LIMITS, **(config.get("diplomacy_limits") or {})}


def game_ruleset(config: dict[str, Any], pack_dirs: list[str] | None = None) -> Ruleset:
    """The scenario's ruleset with this game's turn and phase limits (so rules text matches the game). `pack_dirs`
    overrides the config's packs."""
    rs = build_ruleset(pack_dirs if pack_dirs is not None else config.get("pack_dirs"), config["scenario"],
                       config.get("systems"), config.get("scoring"), config.get("objectives"),
                       config.get("discoveries"))
    scenario = replace(rs.scenario, turn_limit=config["turn_limit"], phase_limits=dict(config["phase_limits"]),
                       message_limits=config["message_limits"], diplomacy_limits=diplomacy_limits(config))
    return replace(rs, scenario=scenario)


@dataclass(frozen=True)
class RulesetPick:
    """The ruleset a game runs or replays with (`pick_ruleset`)."""
    ruleset: Ruleset
    pack_dirs: list[str] | None     # packs used instead of the config's (a frozen earlier version), or None
    matches: bool                   # its hash is the journaled one (True too when the journal has none)


def journaled_ruleset_hash(gdir: GameDir) -> str | None:
    """`ruleset_hash` of the journal's game_created event (the first event), or None."""
    for event in read_events(gdir.journal_path):
        if event.get("type") == "game_created":
            return event.get("ruleset_hash")
    return None


def ruleset_hashes(rs: Ruleset, config: dict[str, Any]) -> set[str]:
    """Every hash these rules may have been journaled under (now, and as before phase limits left the hash)."""
    return {rs.hash, legacy_ruleset_hash(rs, config)}


def pick_ruleset(config: dict[str, Any], journaled: str | None, pack_dirs: list[str] | None = None) -> RulesetPick:
    """The ruleset of a game: `pack_dirs` if given; else the config's packs when their hash is the journaled one;
    else, for a game on the default packs, a frozen earlier version (content/legacy) whose hash matches — so a game
    created before a content update replays and continues with the rules it was played under. No match: the
    config's ruleset with `matches=False` (the caller warns)."""
    if pack_dirs is not None:
        rs = game_ruleset(config, pack_dirs)
        return RulesetPick(rs, pack_dirs, journaled is None or journaled in ruleset_hashes(rs, config))
    rs = game_ruleset(config)
    if journaled is None or journaled in ruleset_hashes(rs, config):
        return RulesetPick(rs, None, True)
    if config.get("pack_dirs") is None:
        for dirs in legacy_pack_sets():
            try:
                cand = game_ruleset(config, [str(d) for d in dirs])
            except (ContentError, KeyError, ValueError):
                continue
            if journaled in ruleset_hashes(cand, config):
                return RulesetPick(cand, [str(d) for d in dirs], True)
    return RulesetPick(rs, None, False)


def _seat_specs(config: dict[str, Any], setup_result: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Seats for new_game_state: the config's, or the setup phase's result (faction and artifacts per seat)."""
    out = []
    for s in config["seats"]:
        spec: dict[str, Any] = {"seat": s["seat"], "nick": s["nick"], "faction": s.get("faction")}
        if setup_result and s["seat"] in setup_result:
            spec["faction"] = setup_result[s["seat"]]["faction"]
            spec["artifacts"] = list(setup_result[s["seat"]].get("artifacts") or [])
        out.append(spec)
    return out


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def escape_untrusted(text: str) -> str:
    """Neutralize wrapper markers and `[DAH]`-like tags inside a player's text."""
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")
    text = text.replace("<<", "‹‹").replace(">>", "››")
    out, pos = [], 0
    for match in _DAH_RE.finditer(text):
        start, end = max(pos, _bracket_edge(text, match.start(), -1)), _bracket_edge(text, match.end(), 1)
        out += [text[pos:start], "(DAH?)"]
        pos = end
    return _GAME_TAG_RE.sub("(ИГРА?)", "".join(out) + text[pos:])


def _bracket_edge(text: str, index: int, step: int) -> int:
    """Extend a tag boundary over spaces and one bracket-like character next to it (if there is one)."""
    i = index if step > 0 else index - 1
    while 0 <= i < len(text) and text[i].isspace():
        i += step
    if 0 <= i < len(text) and (unicodedata.category(text[i]) in _BRACKET_CATEGORIES or text[i] in "<>"):
        return i + 1 if step > 0 else i
    return index


def wrap_untrusted(nick: str, text: str) -> str:
    return f"<<untrusted from={escape_untrusted(nick)}>>{escape_untrusted(text)}<</untrusted>>"


def system_text(text: str) -> str:
    """An inbox entry from the game itself (it may quote nicks, which are escaped by the caller)."""
    return f"[{SYSTEM_SENDER}] {text}"


def _id_list(value: Any) -> Any:
    """A setup choice as sent: a list, a JSON string of one, one id, or ids separated by commas."""
    value = decode_json_arg(value)
    if isinstance(value, str) and "," in value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


RESEARCH_ALIASES = ("technology", "tech_id")


def _research_alias(item: Any) -> Any:
    """`{"kind": "research", "technology": ...}` is read as `tech` (the documented field)."""
    if isinstance(item, dict) and item.get("kind") == "research" and item.get("tech") is None:
        alias = next((item[k] for k in RESEARCH_ALIASES if item.get(k) is not None), None)
        if alias is not None:
            item = {**{k: v for k, v in item.items() if k not in RESEARCH_ALIASES}, "tech": alias}
    return item


def _resolve_future(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)


# ---------------------------------------------------------------- game

class Game:
    def __init__(self, gdir: GameDir, config: dict[str, Any], ruleset: Ruleset, journal: Journal, clock: Clock,
                 state: GameState) -> None:
        self.gdir = gdir
        self.config = config
        self.ruleset = ruleset
        self.ruleset_pick: RulesetPick | None = None      # set by load (which packs, does the hash match)
        self.journal = journal
        self.clock = clock
        self.state = state
        self.books: dict[str, SeatOrders] = {}
        self.ready_seats: set[str] = set()
        self.phase_seq = 0
        self.phase_opened_at = clock()
        self.deadline: float | None = None
        self.first_called: set[str] = set()
        self.msg_counts: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.diplomacy_log: list[dict[str, Any]] = []        # this turn's diplomacy ops (replayed before resolve)
        self.diplomacy_counts: dict[str, dict[str, int]] = {}  # seat -> {gifts, proposals} in the current phase
        self.seats = {s["seat"]: s for s in config["seats"]}
        self.nick_to_seat = {s["nick"]: s["seat"] for s in config["seats"]}
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        self._waiters_lock = threading.Lock()
        # Player (non-host) presence, for the host's phase info (dah-waker skips a `continue` the player does
        # not need): long-polls of wait_phase pending now, seats whose poll was pending when the current phase
        # opened (that poll returns with the new phase), and the clock time of each seat's last player call.
        self.player_polls: dict[str, int] = {}
        self.polled_at_open: set[str] = set()
        self.player_seen_at: dict[str, tuple[int, float]] = {}  # seat -> (phase_seq, clock time)
        self.player_first_seen_at: dict[str, tuple[int, float]] = {}  # seat -> its first call of that phase
        # seat -> its last PLAYER_CALLS_KEPT calls, oldest first: (phase_seq, clock time at arrival, passive)
        self.player_calls: dict[str, list[tuple[int, float, bool]]] = {}
        # setup phase (config["setup"]): offers by seat, choices {seat: {factions?, artifacts?}}, final result
        self.setup_offers: dict[str, list[str]] = {}
        self.setup_choices: dict[str, dict[str, Any]] = {}
        self.setup_result: dict[str, dict[str, Any]] | None = None

    # --- lifecycle ------------------------------------------------------------------------------------

    @classmethod
    def create(cls, gdir: GameDir, clock: Clock = time.time, *, start: bool = True) -> Game:
        """Set up a game in an initialized dir (see store.init_game_dir): journal, turn-1 snapshot.

        With `start`, П1 of turn 1 opens at once; otherwise it opens on `start()` (the server calls it when it
        first loads the game, so operator setup time does not count against the phase).
        """
        config = gdir.load_config()
        rs = game_ruleset(config)
        state = new_game_state(rs, _seat_specs(config), config["seed"], **_salt(config))
        if config.get("setup"):
            # provisional (default factions, no artifacts) until the setup phase closes; starts do not depend on it
            state.phase = SETUP
        game = cls(gdir, config, rs, Journal(gdir.journal_path, clock), clock, state)
        if config.get("setup"):
            game.setup_offers = draft_offers(rs, config["setup"], list(state.players), config["seed"])
        systems = {"systems": list(rs.scenario.systems)} if "systems" in config else {}
        if not rs.default_scoring:
            systems["scoring"] = dict(rs.scenario.scoring)
        # the seed is a secret until game_over (it deals the objectives): only the admin's journal has it
        # (journal.view_events strips it from every other view)
        game.journal.append("game_created", config=config, ruleset_hash=rs.hash, seed=config["seed"],
                            rules_chars=len(game.rules()), **systems)
        for s in config["seats"]:
            game.journal.append("seat_info", seat=s["seat"], kind=s["kind"], declared=s["declared"],
                                dreamteam_session_id=s.get("dreamteam_session_id"))
        for seat, p in state.players.items():
            if p.objectives:        # private to the seat until game_over (SCORING.md 9)
                game.journal.append("objectives_dealt", seat=seat, objectives=[dict(o) for o in p.objectives],
                                    turn=0)
        if not config.get("setup"):
            game._journal_free_shades(state)
        game._journal_layout(state)
        gdir.save_turn("states", 1, state.to_dict())
        if start:
            game.start()
        else:
            game._save()
        return game

    @property
    def started(self) -> bool:
        return self.phase_seq > 0

    def start(self) -> None:
        """Open the first phase: the setup phase if the game has one, else П1 of turn 1 (no-op once started)."""
        if not self.started:
            self._open_phase()

    @classmethod
    def load(cls, gdir: GameDir, clock: Clock = time.time) -> Game:
        """Restore a running (or finished) game from `private/runtime.json` and the message log."""
        config = gdir.load_config()
        rt = read_json(gdir.runtime_path)
        pick = pick_ruleset(config, journaled_ruleset_hash(gdir))
        if not pick.matches:
            log.warning("game %s: the packs' ruleset hash %s differs from the journaled one; the game continues "
                        "under changed rules (dah replay will report РАСХОЖДЕНИЕ ПРАВИЛ)", config.get("game_id"),
                        pick.ruleset.hash[:12])
        game = cls(gdir, config, pick.ruleset, Journal(gdir.journal_path, clock), clock,
                   GameState.from_dict(rt["state"]))
        game.ruleset_pick = pick
        game.books = {s: SeatOrders.from_dict(b) for s, b in rt["books"].items()}
        game.ready_seats = set(rt["ready"])
        game.phase_seq = rt["phase_seq"]
        game.phase_opened_at = rt["phase_opened_at"]
        game.deadline = rt["deadline"]
        game.first_called = set(rt["first_called"])
        game.msg_counts = rt["msg_counts"]
        game.diplomacy_log = list(rt.get("diplomacy_log") or [])
        game.diplomacy_counts = dict(rt.get("diplomacy_counts") or {})
        game.messages = gdir.load_messages()
        presence = rt.get("presence") or {}  # absent in a runtime.json of an earlier version
        game.player_seen_at = {s: (int(v[0]), float(v[1])) for s, v in (presence.get("seen") or {}).items()}
        game.player_first_seen_at = {s: (int(v[0]), float(v[1])) for s, v in (presence.get("first") or {}).items()}
        game.player_calls = {s: [(int(c[0]), float(c[1]), bool(c[2])) for c in calls]
                             for s, calls in (presence.get("calls") or {}).items()}
        setup = rt.get("setup") or {}
        game.setup_offers = {s: list(v) for s, v in (setup.get("offers") or {}).items()}
        game.setup_choices = {s: dict(v) for s, v in (setup.get("choices") or {}).items()}
        game.setup_result = setup.get("result")
        return game

    def _save(self) -> None:
        write_json(self.gdir.runtime_path, {
            "state": self.state.to_dict(), "books": {s: b.to_dict() for s, b in self.books.items()},
            "ready": sorted(self.ready_seats), "phase_seq": self.phase_seq, "phase_opened_at": self.phase_opened_at,
            "deadline": self.deadline, "first_called": sorted(self.first_called), "msg_counts": self.msg_counts,
            "diplomacy_log": self.diplomacy_log, "diplomacy_counts": self.diplomacy_counts,
            # the player's own calls (host-only presence): kept over a restart, so a wake answered just before the
            # phase closed is still seen as answered
            "presence": {"seen": {s: list(v) for s, v in self.player_seen_at.items()},
                         "first": {s: list(v) for s, v in self.player_first_seen_at.items()},
                         "calls": {s: [list(c) for c in calls] for s, calls in self.player_calls.items()}},
            "setup": {"offers": self.setup_offers, "choices": self.setup_choices, "result": self.setup_result},
        })

    # --- phases ---------------------------------------------------------------------------------------

    @property
    def phase(self) -> str:
        return self.state.phase

    @property
    def game_over(self) -> bool:
        return self.state.phase == GAME_OVER

    def alive_seats(self) -> list[str]:
        return [s for s, p in self.state.players.items() if p.alive]

    def seconds_left(self) -> int | None:
        if self.deadline is None:
            return None
        return max(0, int(round(self.deadline - self.clock())))

    def tick(self) -> bool:
        """Close the current phase if its deadline has passed. True if a phase changed."""
        if self.phase in TIMED_PHASES and self.deadline is not None and self.clock() >= self.deadline:
            self._close_phase("deadline")
            return True
        return False

    def _open_phase(self) -> None:
        now = self.clock()
        self.polled_at_open = {s for s, n in self.player_polls.items() if n > 0}
        self.phase_seq += 1
        self.ready_seats.clear()
        self.first_called.clear()
        self.msg_counts = {}
        self.diplomacy_counts = {}
        self.phase_opened_at = now
        self.deadline = now + self.config["phase_limits"][self.phase]
        self.journal.append("phase_open", turn=self.state.turn, phase=self.phase, deadline=iso_time(self.deadline))
        self._save()
        self._notify()

    def _close_phase(self, reason: str) -> None:
        phase = self.phase
        not_ready = sorted(set(self.alive_seats()) - self.ready_seats)
        self.journal.append("phase_close", turn=self.state.turn, phase=phase, reason=reason, not_ready=not_ready)
        if phase == SETUP:
            self._close_setup(reason)
            return
        if phase == "actions":
            self._recon()      # the recon step (espionage); an empty event without espionage, as in the slice
        if phase == P2:
            self._resolve_turn()
        else:
            self.state.phase = NEXT_PHASE[phase]
            self._open_phase()

    def _recon(self) -> None:
        """The recon step at the close of actions (SPY_AND_CONQUEST.md 1.5): the actions books are saved, recon runs
        on the state of the close, its report (rolls, `facts` next to `truth`) is saved privately and journaled, and
        each seat it touched gets one inbox entry when negotiation_2 opens."""
        turn = self.state.turn
        if not self.ruleset.system("espionage"):
            self.journal.append("recon", turn=turn)
            return
        books = {seat: self._book(seat) for seat in self.alive_seats()}
        self.gdir.save_turn("books_d", turn, {s: b.to_dict() for s, b in books.items()})
        new_state, rep = esp.recon(self.state, books, self.ruleset, self.config["seed"])
        self.gdir.save_turn("recon", turn, rep)
        self.journal.append("recon", turn=turn, state_hash=state_hash(new_state), recon_hash=rep["hash"],
                            **esp.recon_summary(rep))
        self._journal_recon(rep, turn)
        new_state.phase = self.state.phase
        self.state = new_state
        for seat in sorted(self.state.players):
            text = esp.notice_text(rep, seat)
            if text:
                self._notice([seat], text, "recon", turn=turn, phase=P2)

    def _journal_layout(self, state: GameState) -> None:
        """The layout of finds (ABANDONED_LANDS.md 5, 10): admin-only until game_over (journal.view_events); the
        same at the close of a setup phase (it reads no factions)."""
        info = disc.journal_layout(self.ruleset, state, self.config["seed"], secret_salt(self.config))
        if info is None:
            return
        self.journal.append("discoveries_placed", turn=0, **info)
        if not info["accepted"]:
            self.journal.append("discoveries_unfair", turn=0, spread=info["spread"], budgets=info["budgets"],
                                counts=info["counts"])
        if info.get("mirror") == "skipped":
            self.journal.append("fair_mirror_skipped", turn=0)

    def _journal_free_shades(self, state: GameState) -> None:
        """The Shades every seat holds from the start (core 0.6.0, `spy.shades_free`): `shade_hired` with `free`,
        turn 0, once the start is final (game creation, or the close of the setup phase)."""
        for seat, p in state.players.items():
            for sh in p.shades:
                if sh.get("hired_turn") == 0:
                    self.journal.append("shade_hired", seat=seat, shade=sh["id"], turn=0, free=True)

    def _journal_recon(self, rep: dict[str, Any], turn: int) -> None:
        """The recon step in full, for offline metrics (SPY_AND_CONQUEST.md 8): who saw what, next to the truth."""
        j = self.journal.append
        for seat, items in sorted(rep["rejected"].items()):
            for item in items:
                j("mission_rejected", seat=seat, order=item["order"], reason=item["reason"], phase="recon", turn=turn)
        for f in rep["feints"]:
            j("feint", turn=turn, **f)
        for h in rep["hired"]:
            j("shade_hired", seat=h["seat"], shade=h["shade"], turn=turn)
        for w in rep.get("wards") or []:             # core 0.8.0 (docs/SPY_V2.md 4.6)
            j("ward_set", turn=turn, **w)
        for m in rep["started"]:
            extra = {}
            md = self.ruleset.missions.get(m["mission"])
            if md is not None and md.behavior == esp.OBSERVE and md.intel != "each_turn":
                # core 0.8.0 (docs/SPY_V2.md 11): an Observation of a province the seat sees at the close of actions
                # (the report's «лишние наблюдения»); self.state is still that close
                extra["in_sight"] = m["target"] in visible_provinces(self.state, self.ruleset, m["seat"])
            j("mission_started", seat=m["seat"], shade=m["shade"], mission=m["mission"], target=m["target"],
              rounds=m["rounds"], phase=m["phase"], first_turn=m["first_turn"], turn=turn, **extra)
        for r in rep["rounds"]:
            j("spy_round", turn=turn, **{k: v for k, v in r.items() if k != "seat"}, seat=r["seat"])
        for seat, reports in sorted(rep["reports"].items()):
            for r in reports:
                exposed = [f for f in r.get("feints", []) if f["exposed"]]
                j("intel_report", seat=seat, report_id=r["id"], shade=r["shade"], mission=r["mission"],
                  target=r["target"], turn=turn, facts_hash=sha256_json(r["facts"]), truth_hash=sha256_json(r["truth"]),
                  informativeness=esp.informativeness(r), feints_shown=len(r.get("feints", [])),
                  feints_exposed=len(exposed))
                for f in exposed:
                    j("feint_exposed", spy_seat=seat, victim_seat=r["target"], order_id=f.get("order_id"),
                      index=f["index"], report_id=r["id"],
                      turn=turn)
        for c in rep.get("caught", []):
            j("shade_captured", victim=c["victim"], owner=c["owner"], shade=c["shade"], mission=c["mission"],
              target=c["target"], province=c["province"], attributed=c["attributed"], turn=turn)
        for e in rep["ended"]:
            j("mission_end", turn=turn, **e)
        for p in rep.get("intel_points") or []:      # core 0.7.0: the scoring source `intel`
            j("intel_point", turn=turn, **p)
        for d in rep.get("intel_denied") or []:      # core 0.8.0: successes that scored nothing, and why
            j("intel_denied", turn=turn, **d)

    def recon_report(self, turn: int | None = None) -> dict[str, Any] | None:
        """The saved recon step of a turn (default: the current one, once actions closed)."""
        turn = self.state.turn if turn is None else turn
        if not self.ruleset.system("espionage"):
            return None
        return self.gdir.load_turn("recon", turn)

    def _require_espionage(self) -> None:
        if not self.ruleset.system("espionage"):
            raise GameError("NOT_AVAILABLE", "шпионаж выключен в этой партии")

    def list_spies(self, seat: str) -> dict[str, Any]:
        self._require_espionage()
        rep = self.recon_report() if self.phase == P2 else None
        return {"text": esp.spies_text(self.state, self.ruleset, seat, rep)}

    def get_intel(self, seat: str, turn: Any = None, shade: Any = None, report_id: Any = None,
                  fmt: str = "text") -> dict[str, Any]:
        """The seat's reports of one recon step: text (default), or with `fmt="json"` the same as data (all its
        reports' facts, round outcomes, captures at the seat, its lost Shades) for programs and bots."""
        self._require_espionage()
        if fmt not in ("text", "json"):
            raise GameError("INVALID", f"format {str(fmt)[:20]!r} не бывает: text (по умолчанию) или json")
        t = as_int(turn) if turn not in (None, "") else None
        if t is not None and (isinstance(t, bool) or not isinstance(t, int)):
            raise GameError("INVALID", f"turn — номер хода, получено {turn!r}")
        last = self.state.turn if self.phase in (P2, GAME_OVER) else self.state.turn - 1
        t = last if t is None else t
        sid = esp.shade_id(shade) if shade not in (None, "") else None
        rid = str(report_id).strip().lstrip("#") if report_id not in (None, "") else None
        copy_entry = tr.copy_of(self.state, seat, rid.lower()) if rid and rid.lower().startswith("c") else None
        if rid and rid.lower().startswith("c"):
            # core 0.8.0 (docs/TRADE.md 2.3): a bought copy, from the state (its turn is the copy's own)
            if copy_entry is None:
                raise GameError("UNKNOWN_ID", f"у тебя нет копии донесения {rid[:20]}; копии появляются после "
                                              "покупки донесения обменом (list_trades)")
            self.journal.append("intel_read", seat=seat, report_ids=[copy_entry["id"]], via="get_intel",
                                turn=self.state.turn, phase=self.phase)
            if fmt == "json":
                return {"turn": copy_entry.get("turn"), "intel": {"copies": [copy_entry]}}
            return {"turn": copy_entry.get("turn"), "text": tr.copy_text(copy_entry, self.ruleset, self.state)}
        rep = self.recon_report(t) if 1 <= t <= max(last, 0) else None
        if fmt == "json":
            data = esp.intel_json(rep, seat)
            if self.state.players[seat].intel_copies:
                data["copies"] = list(self.state.players[seat].intel_copies)
            self.journal.append("intel_read", seat=seat, report_ids=[r["id"] for r in data["reports"]],
                                via="get_intel", format="json", turn=self.state.turn, phase=self.phase)
            return {"turn": t, "intel": data}
        text = esp.intel_text(rep, self.ruleset, self.state, seat, sid, rid)
        copies = tr.copies_line(self.state, self.ruleset, seat) if rid is None else ""
        if copies:
            text = f"{text}\n{copies}"
        self.journal.append("intel_read", seat=seat, report_ids=[rid] if rid else
                            [r["id"] for r in ((rep or {}).get("reports") or {}).get(seat, [])],
                            via="get_intel", turn=self.state.turn, phase=self.phase)
        return {"turn": t, "text": text}

    def estimate_mission(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_espionage()
        try:
            return esp.estimate_mission(self.state, self.ruleset, seat, body.get("mission"), body.get("target"),
                                        body.get("rounds"), body.get("shade"), body.get("assume_mirrors"),
                                        self._book(seat))
        except ValueError as exc:
            raise GameError("INVALID", str(exc)) from None

    def _resolve_turn(self) -> None:
        turn = self.state.turn
        self.state.phase = "resolution"
        books = {seat: self.books.get(seat, SeatOrders()) for seat in self.alive_seats()}
        self.gdir.save_turn("orders", turn, {s: b.to_dict() for s, b in books.items()})
        if self.diplomacy_log:
            self.gdir.save_turn("diplomacy", turn, self.diplomacy_log)
        new_state, report = resolve(self.state, books, self.ruleset, self.config["seed"], self.diplomacy_log,
                                    salt=secret_salt(self.config))
        self.gdir.save_turn("reports", turn, report.to_dict())
        self.diplomacy_log = []
        self._pact_results(turn, report)
        self._trade_results(turn, report, new_state)
        for e in report.events:
            if e["kind"] in ("research_started", "tech_completed"):
                extra = {}
                if e["kind"] == "tech_completed" and not self.ruleset.default_scoring and e["tech"] in self.ruleset.techs:
                    # what the seat paid (report: gift_share_of_score_spend, SCORING.md 9); journal only
                    extra["cost"] = self.ruleset.tech_cost(e["tech"], stat_key(self.state, self.ruleset, e["seats"][0]))
                self.journal.append(e["kind"], seat=e["seats"][0], tech=e["tech"], turn=turn, **extra)
            elif e["kind"] in CONQUEST_JOURNAL or (e["kind"] == "captured" and "mode" in e):
                self._journal_conquest(e, turn)
            elif e["kind"] in ESPIONAGE_JOURNAL:
                data = {k: v for k, v in e.items() if k not in ("kind", "seats")}
                self.journal.append(e["kind"], turn=turn, **data)
            elif e["kind"] in DISCOVERY_JOURNAL:
                data = {k: v for k, v in e.items() if k != "kind"}
                self.journal.append(e["kind"], turn=turn, **data)
            elif e["kind"] == "order_rejected" and e.get("reason") == "PROVINCE_PROMISED":
                # core 0.8.0 (docs/TRADE.md 13.2, promised_move_rejected): journal only, private to its seat
                self.journal.append("order_rejected", seat=e["seats"][0], order=e.get("order"), reason=e["reason"],
                                    turn=turn)
            elif e["kind"] in SCORING_JOURNAL:
                data = {k: v for k, v in e.items() if k not in ("kind", "seats")}
                if e["kind"] == "intel_point" and "intel_kind" in data:
                    data["kind"] = data.pop("intel_kind")      # as the recon step journals it
                if e["kind"] == "work_started" and e.get("building") in self.ruleset.buildings:
                    # the price the seat paid, journal only (the public event carries none); paid in parts (core
                    # 0.7.0) also the shares and the first one, paid now
                    key = stat_key(self.state, self.ruleset, e["seat"])
                    data["cost"] = dict(self.ruleset.building_cost(e["building"], key))
                    if self.ruleset.building_shares(e["building"]) > 1:
                        data["shares"] = self.ruleset.building_shares(e["building"])
                        data["paid"] = dict(self.ruleset.building_order_cost(e["building"], key))
                self.journal.append(e["kind"], turn=turn, **data)
        # `scores` (and `score_parts` with a non-default scoring) are private per seat (SCORING.md 9): the journal is the
        # admin's; journal.view_events gives each seat its own entry
        parts = {"score_parts": report.score_parts} if report.score_parts else {}
        self.journal.append(
            "turn_resolved", turn=turn, state_hash=state_hash(new_state), scores=report.scores_of(), **parts,
            idle_resources={s: v["resources"] for s, v in report.idle.items()},
            idle_units={s: v["units"] for s, v in report.idle.items()},
            resources={s: dict(p.resources) for s, p in new_state.players.items()},   # stocks at the turn's end
            autopilot=sorted(s for s, b in books.items() if not b.orders))
        if self.ruleset.system("espionage"):
            self._journal_orders_summary(turn, books)
        self.state = new_state
        self.books = {}
        if new_state.phase == GAME_OVER:
            result = new_state.result or {}
            if "objectives" in result:
                # every secret objective is revealed at the end (SCORING.md 9), met or not, with its evidence
                for seat in sorted(result["objectives"]):
                    for o in result["objectives"][seat]:
                        met, have, need = sc_.objective_progress(new_state, self.ruleset, seat, o)
                        self.journal.append("objectives_revealed", seat=seat, id=o["id"], target=o.get("target"),
                                            met=o["met"], points=o["points"],
                                            counted=bool(o["met"] and new_state.players[seat].alive),
                                            evidence={"have": have, "need": need}, turn=turn)
            extra = {k: result[k] for k in ("score_parts", "objectives", "winners_open") if k in result}
            self.journal.append("game_over", winners=result.get("winners", []), reason=result.get("reason"),
                                scores=result.get("scores", {}), turn=turn, **extra)
            self.polled_at_open = {s for s, n in self.player_polls.items() if n > 0}
            self.phase_seq += 1
            self.ready_seats.clear()
            self.deadline = None
            self.phase_opened_at = self.clock()
            self._save()
            self._notify()
        else:
            self.gdir.save_turn("states", new_state.turn, new_state.to_dict())
            self._open_phase()

    def _journal_conquest(self, e: dict[str, Any], turn: int) -> None:
        """Conquest events of a resolution in their full form (SPY_AND_CONQUEST.md 8), for offline metrics."""
        data = {k: v for k, v in e.items() if k not in ("kind", "seats")}
        if e["kind"] == "captured":
            data = {"province": e["province"], "by": e["to"], "from": e["from"], "mode": e["mode"],
                    "status": e["status"]}
            self.journal.append("province_captured", turn=turn, **data)
            return
        if e["kind"] == "occupation_revolt" and esp.unrest_in(self.ruleset):
            data["cause_private"] = True        # core 0.8.0 (docs/SPY_V2.md 7): others read it without its cause
        self.journal.append(e["kind"], turn=turn, **data)

    def _notify(self) -> None:
        with self._waiters_lock:
            waiters, self._waiters = self._waiters, []
        for loop, fut in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_resolve_future, fut)

    async def wait_phase(self, after_seq: int | None, max_seconds: float, *, player_seat: str | None = None) -> bool:
        """Wait until `phase_seq` differs from `after_seq` (or the timeout). True if it changed.

        `player_seat`: a player's (not the host's) long-poll; while it waits, the host's phase info says
        `player_waiting` for that seat."""
        if after_seq is None or after_seq != self.phase_seq:
            return True
        loop = asyncio.get_running_loop()
        entry = (loop, loop.create_future())
        with self._waiters_lock:
            self._waiters.append(entry)
            if player_seat is not None:
                self.player_polls[player_seat] = self.player_polls.get(player_seat, 0) + 1
        try:
            await asyncio.wait_for(entry[1], timeout=max_seconds)
        except TimeoutError:
            pass
        finally:
            with self._waiters_lock:
                if entry in self._waiters:
                    self._waiters.remove(entry)
                if player_seat is not None:
                    self.player_polls[player_seat] = max(0, self.player_polls.get(player_seat, 0) - 1)
        return self.phase_seq != after_seq

    def note_player_activity(self, seat: str, phase_seq: int | None = None, *, arrival: bool = False,
                             passive: bool = False) -> None:
        """A player's own (non-host) call arrived or finished now. `phase_seq`: the phase the call started in
        (the server passes the seq it saw before `tick()`); a call that started in an earlier phase (its own
        `ready` or a deadline closed that phase while it ran) is not activity in the current one, since the player
        has not seen the new phase yet. A wait_phase poll that returns with the new phase is `polled_at_open`.
        `arrival`: the call has just come in; it is listed in `player_calls`, `passive` for a phase-status read."""
        if phase_seq is not None and phase_seq != self.phase_seq:
            return
        now = self.clock()
        self.player_seen_at[seat] = (self.phase_seq, now)
        first = self.player_first_seen_at.get(seat)
        if first is None or first[0] != self.phase_seq:
            self.player_first_seen_at[seat] = (self.phase_seq, now)
        if arrival:
            calls = self.player_calls.setdefault(seat, [])
            calls.append((self.phase_seq, now, passive))
            del calls[:-PLAYER_CALLS_KEPT]

    def player_presence(self, seat: str) -> dict[str, Any]:
        """For the host only: is the player waiting in wait_phase (now, or its poll was pending when this phase
        opened and returns with it), how many seconds ago its last and its first call of this phase were, and its last
        call of any phase (`player_last_call` {phase_seq, s_ago}: dah-waker tells whether a wake was answered before
        the phase closed), and its last PLAYER_CALLS_KEPT calls (`player_calls` [{phase_seq, s_ago, passive}], oldest
        first: dah-waker finds the first call after a wake, and whether it was more than a phase-status read)."""
        now = self.clock()
        seen = self.player_seen_at.get(seat)
        first = self.player_first_seen_at.get(seat)

        def ago(entry: tuple[int, float] | None) -> float | None:
            return (round(max(0.0, now - entry[1]), 1)
                    if entry is not None and entry[0] == self.phase_seq else None)

        return {"player_waiting": self.player_polls.get(seat, 0) > 0 or seat in self.polled_at_open,
                "player_active_s_ago": ago(seen), "player_first_active_s_ago": ago(first),
                "player_last_call": ({"phase_seq": seen[0], "s_ago": round(max(0.0, now - seen[1]), 1)}
                                     if seen is not None else None),
                "player_calls": [{"phase_seq": seq, "s_ago": round(max(0.0, now - at), 1), "passive": passive}
                                 for seq, at, passive in self.player_calls.get(seat, [])]}

    def phase_info(self, seat: str, *, host: bool = False) -> dict[str, Any]:
        alive = self.alive_seats()
        ready = f"{len(self.ready_seats & set(alive))}/{len(alive)}"
        seat_alive = self.state.players[seat].alive
        info: dict[str, Any] = {
            "game_id": self.config["game_id"], "turn": self.state.turn, "turn_limit": self.config["turn_limit"],
            "phase": self.phase, "deadline": iso_time(self.deadline) if self.deadline is not None else None,
            "seconds_left": self.seconds_left(), "ready": ready, "you_ready": seat in self.ready_seats,
            "phase_seq": self.phase_seq,
            "allowed": self._allowed(seat_alive),
            "alive": seat_alive, "game_over": self.game_over,
        }
        if self.game_over:
            info["result"] = self.state.result
        elif not seat_alive:
            info["eliminated"] = True
            info["hint"] = ELIMINATED_NOTE
        if self.phase == SETUP:
            info["hint"] = SETUP_HINT
        elif self.phase in PHASE_READY_HINTS and seat_alive and not self.game_over:
            if self.phase != "negotiation_1":
                info["orders_hash"] = orders_hash(self._book(seat))   # ready(confirm_hash=...) in one call
            info["hint"] = PHASE_READY_HINTS[self.phase].format(orders_hash=info.get("orders_hash"))
        intel = self._intel_counts(seat)
        if intel is not None:
            info["intel"] = intel       # the wake tail «донесений: N; поймано у тебя: M» (SPY_AND_CONQUEST.md 6.3)
        idle = self._idle_shades(seat)
        if idle:
            info["idle_shades"] = idle  # core 0.7.0 (decision 27): the turn-start reminder of an idle Shade
        waiting = self._trades_waiting(seat)
        if waiting:
            info["trades_waiting"] = waiting    # core 0.8.0 (docs/TRADE.md 9): «обменов ждут ответа: N»
        if host:
            info.update(self.player_presence(seat))
        info["text"] = self._phase_text(info)
        return info

    def _intel_counts(self, seat: str) -> dict[str, int] | None:
        """In negotiation_2 of an espionage game: the seat's reports of this turn's recon step and the Shades caught
        at it; None otherwise (and in games without espionage, whose phase info never changes)."""
        if self.phase != P2 or self.game_over or not self.ruleset.system("espionage"):
            return None
        rep = self.recon_report()
        if rep is None:
            return None
        return {"reports": len((rep.get("reports") or {}).get(seat, [])),
                "caught": len((rep.get("captures") or {}).get(seat, []))}

    def _trades_waiting(self, seat: str) -> int:
        """Open trade offers the seat has to answer, in negotiation phases of a trade game (0 otherwise)."""
        if self.phase not in NEGOTIATION_PHASES or self.game_over or not tr.on(self.ruleset):
            return 0
        return len(tr.waiting_for(self.state, seat))

    def _idle_shades(self, seat: str) -> int:
        """Core 0.7.0 (decision 27, m8: free Shades sat idle all game): the seat's own idle Shades in negotiation_1 and
        actions of an espionage game (0 otherwise); never another seat's."""
        if (self.phase not in ("negotiation_1", "actions") or self.game_over or not self.ruleset.system("espionage")
                or not self.state.players[seat].alive):
            return 0
        return len(esp.idle_shades(self.state, seat))

    def _allowed(self, seat_alive: bool) -> list[str]:
        tools = list(ALLOWED.get(self.phase, READ_TOOLS) if seat_alive else READ_TOOLS)
        if self.ruleset.system("espionage") and self.phase != SETUP:
            tools += [t for t in SPY_TOOLS if t not in tools]       # espionage reads: every phase (6.1)
        if tr.on(self.ruleset) and self.phase != SETUP:
            if seat_alive and self.phase in NEGOTIATION_PHASES and not self.game_over:
                tools += TRADE_TOOLS
            tools += TRADE_READ_TOOLS
        return tools

    def _phase_text(self, info: dict[str, Any]) -> str:
        parts = [f"Ход {info['turn']}/{info['turn_limit']}", PHASE_LABELS.get(self.phase, self.phase)]
        if self.game_over:
            winners = ", ".join(self.state.players[w].nick for w in (self.state.result or {}).get("winners", []))
            # the final scores are public after the game (SCORING.md 9): totals here, the breakdown in get_state
            final = (self.state.result or {}).get("scores") or {}
            totals = ", ".join(f"{p.nick} {final[s]}" for s, p in self.state.players.items() if s in final)
            return " | ".join(parts + [f"победители: {winners or 'никто'}"] + ([f"итог: {totals}"] if totals else [])
                              + ["game_over"])
        if self.deadline is not None:
            parts.append(f"до {datetime.fromtimestamp(self.deadline, timezone.utc):%H:%MZ} "
                         f"(ещё {info['seconds_left']} с)")
        if info.get("eliminated"):
            return " | ".join(parts + [ELIMINATED_NOTE])
        parts.append(f"готово {info['ready']}" + (", ты готов" if info["you_ready"] else ", ты не готов"))
        if info.get("intel"):
            parts.append(intel_tail(info["intel"]))
        if info.get("idle_shades"):
            parts.append(esp.idle_reminder(info["idle_shades"]))
        if info.get("trades_waiting"):
            parts.append(f"обменов ждут ответа: {info['trades_waiting']} (list_trades)")
        if info.get("hint"):
            parts.append(info["hint"])
        return " | ".join(parts)

    # --- reading --------------------------------------------------------------------------------------

    def whoami(self, seat: str) -> dict[str, Any]:
        player = self.state.players[seat]
        out = {"game_id": self.config["game_id"], "seat": seat, "nick": player.nick, "faction": player.faction,
               "kind": self.seats[seat]["kind"], "alive": player.alive, "turn": self.state.turn,
               "phase": self.phase}
        if self.config.get("setup"):
            out["artifacts"] = list(player.artifacts)      # own only
            out["setup_done"] = self.phase != SETUP
            if self.phase == SETUP:
                # not decided yet: the provisional turn 1 has a default faction, which is not the seat's pick
                out["faction"], out["artifacts"] = None, []
                out["faction_choice"] = self._choice_first(seat)
        return out

    def _choice_first(self, seat: str) -> str | None:
        prefs = (self.setup_choices.get(seat) or {}).get("factions")
        return prefs[0] if prefs else None

    def rules(self, topic: str | None = None) -> str:
        """No topic: the short core (<= 6000 chars) with a table of contents. A topic (techs, factions, artifacts,
        pacts, combat, setup, units...): that section in full, built from this game's content; a content id or name
        gives its description (as describe)."""
        extra = [RulesSection("messages", self._messages_rules())]
        if disc.active(self.ruleset, self.state):
            extra.append(discoveries_section(self.ruleset))
        if not topic or not str(topic).strip():
            return rules_text(self.ruleset, setup=self.setup, extra=extra)
        text = rules_topic(self.ruleset, topic, setup=self.setup, extra=extra)
        if text is not None:
            return text
        if topic_key(topic) == "discoveries":
            return ("В этой партии нет Заброшенных земель: все старты карты заняты, находок нет."
                    if disc.on(self.ruleset) else "Заброшенные земли и находки в этой партии выключены.")
        hits = find(self.ruleset, topic)
        if hits:
            return describe(self.ruleset, *hits[0])["text"]
        names = ", ".join(rule_topics(self.ruleset, setup=self.setup, extra=extra))
        # just the list (the core text is one rules() away): a guessed topic must not cost the whole rules again
        return (f"Тема {str(topic)[:40]!r} не найдена. Темы: {names}. Ещё можно ID или название (можно начало) "
                "юнита, здания, технологии, артефакта, фракции. Правила кратко — rules() без темы.")

    def _messages_rules(self) -> str:
        lim = self.config["message_limits"]
        parts = []
        for phase, label in (("negotiation_1", "П1"), ("negotiation_2", "П2")):
            p = lim.get(phase, {})
            per = f", ≤{p['per_recipient']} одному адресату" if p.get("per_recipient") else ""
            parts.append(f"{label}: ≤{p.get('private', 0)} личных{per}, ≤{p.get('public', 0)} публичных")
        return (f"СООБЩЕНИЯ: только в фазах переговоров; to — список ников или \"all\"; "
                f"{'; '.join(parts)}; ≤{lim.get('max_chars', 800)} символов. Личное нескольким адресатам "
                "считается каждому.")

    def state_text(self, seat: str, since_turn: int | None = None) -> str:
        info = None
        if self.deadline is not None:
            hhmm = f"{datetime.fromtimestamp(self.deadline, timezone.utc):%H:%MZ}"
            info = DeadlineInfo(hhmm, self.seconds_left() or 0)
        since_state, reports = None, []
        if since_turn is not None and since_turn <= 0:
            since_turn = None  # "changes since turn 0" is the whole state
        if since_turn is not None and since_turn > self.state.turn:
            example = f"get_state(since_turn={self.state.turn - 1})" if self.state.turn > 1 else "get_state()"
            raise GameError("INVALID", f"since_turn={since_turn} больше текущего хода {self.state.turn}: можно "
                                       f"1..{self.state.turn} или без since_turn (всё состояние). Пример: {example}")
        if since_turn is not None:
            snapshot = self.gdir.load_turn("states", since_turn)
            since_state = GameState.from_dict(snapshot) if snapshot else None
            reports = [TurnReport.from_dict(r) for t in range(since_turn, self.state.turn + 1)
                       if (r := self.gdir.load_turn("reports", t)) is not None]
        text = render_state_text(self.state, self.ruleset, seat, info, since_turn, since_state, reports,
                                 self._book(seat))
        if self.phase in ("negotiation_1", "actions") and self.ruleset.system("espionage") and not self.game_over:
            idle = esp.idle_line(self.state, seat)
            if idle:
                text += "\n" + idle
        if self.phase == P2 and self.ruleset.system("espionage"):
            rep = self.recon_report()
            line = esp.state_line(rep, self.ruleset, self.state, seat)
            if line:
                text += "\n" + line
                self.journal.append("intel_read", seat=seat, report_ids=[r["id"] for r in
                                                                         (rep["reports"] or {}).get(seat, [])],
                                    via="get_state", turn=self.state.turn, phase=self.phase)
        if not self.state.players[seat].alive and not self.game_over:
            text = f"ВЫБЫЛ: {ELIMINATED_NOTE}.\n" + text
        return SETUP_STATE_NOTE + "\n" + text if self.phase == SETUP else text

    def state_json(self, seat: str) -> dict[str, Any]:
        view = view_state(self.state, self.ruleset, seat, self._book(seat))
        if self.phase == SETUP:
            # The provisional turn 1 has default factions: never present another seat's as its pick.
            view["phase"] = SETUP
            view["setup"] = {"open": True, "provisional": True, "note": SETUP_STATE_NOTE}
            for entry in view["players"]:
                if entry["seat"] != seat:
                    entry["faction"] = None
            # nor the seat's own default as its faction: it is decided when the phase closes
            view["you"]["faction"] = None
            view["you"]["faction_choice"] = self._choice_first(seat)
            for entry in view["players"]:
                if entry["seat"] == seat:
                    entry["faction"] = None
        if not self.state.players[seat].alive and not self.game_over:
            view["eliminated"] = {"note": ELIMINATED_NOTE}
        return view

    def history(self, seat: str, turn: int | None = None) -> dict[str, Any]:
        last = self.state.turn if self.game_over else self.state.turn - 1
        if turn is None and last < 1:
            return {"turn": None, "text": "Ещё ни один ход не рассчитан: отчёт хода 1 появится после его П2. "
                                          "Сейчас смотри get_state()."}
        turn = last if turn is None else turn
        if turn == self.state.turn and not self.game_over:
            # the turn in progress (asked for in its own P2, as in measurement m5): not resolved yet, not an error
            past = f"; прошлый ход — history(turn={last})" if last >= 1 else ""
            return {"turn": None, "text": f"Ход {turn} ещё идёт ({PHASE_LABELS.get(self.phase, self.phase)}): его "
                                          f"отчёт появится после расчёта. Сейчас смотри get_state(){past}."}
        raw = self.gdir.load_turn("reports", turn) if 1 <= turn <= last else None
        if raw is None:
            if last < 1:
                raise GameError("NOT_FOUND", f"отчёта хода {turn} нет: ещё ни один ход не рассчитан. Пример: "
                                             "history() после расчёта хода 1, а пока get_state()")
            raise GameError("NOT_FOUND", f"отчёта хода {turn} нет: рассчитаны ходы 1..{last}. "
                                         f"Пример: history(turn={last}) или history() (последний)")
        report = TurnReport.from_dict(raw)
        return {"turn": turn, "text": render_report_text(report, self.ruleset, self.state, seat)}

    # --- orders ---------------------------------------------------------------------------------------

    def _book(self, seat: str) -> SeatOrders:
        return self.books.get(seat) or SeatOrders()

    def orders(self, seat: str) -> dict[str, Any]:
        book = self._book(seat)
        lines = [self._order_line(o.to_dict()) for o in book.orders] or ["приказов нет (армии стоят)"]
        return {"orders": [o.to_dict() for o in book.orders], "orders_hash": orders_hash(book),
                "cp_left": cp_left(self.state, seat, book),
                "reserved": seat_reserved(self.state, self.ruleset, seat, book), "text": "\n".join(lines)}

    def _order_line(self, o: dict[str, Any]) -> str:
        short = self.ruleset.short
        hasty = " [П2, наспех]" if o["phase_added"] == P2 and o["kind"] == "move" else ""
        if o["kind"] == "move":
            units = " ".join(f"{short(u)}×{n}" for u, n in o["units"].items())
            breach = " [pact=break]" if o.get("pact") == "break" else ""
            feint = " [ЛОЖНАЯ ЗАПИСЬ: не исполняется]" if o.get("feint") else ""
            return f"{o['id']} move {o['from']}→{o['to']} {units}{breach}{hasty}{feint}"
        if o["kind"] == "spy":
            m = self.ruleset.missions.get(o.get("mission") or "")
            target = self._nick(o["target"]) if o.get("target") in self.state.players else o.get("target")
            return (f"{o['id']} spy {o['shade']} «{m.name if m else o.get('mission')}» → {target} "
                    f"{o.get('rounds')} р.")
        if o["kind"] == "recall":
            return f"{o['id']} recall {o['shade']}"
        if o["kind"] == "recruit":
            return f"{o['id']} recruit {o['province']} {short(o['unit'])}×{o['count']}"
        if o["kind"] == "build":
            return f"{o['id']} build {o['province']} {short(o['building'])}"
        if o["kind"] == "research":
            tech = self.ruleset.techs.get(o["tech"])
            return f"{o['id']} research {o['tech']}" + (f" «{tech.name}»" if tech else "")
        if o["kind"] in ("dispose", "policy"):
            d = self.ruleset.dispositions.get(o.get("disposition") or "")
            name = f" «{d.name}»" if d else (" (без политики)" if o["kind"] == "policy" else "")
            where = f" {o['province']}" if o["kind"] == "dispose" else ""
            to = f" → {self._nick(o['to'])}" if o.get("to") else ""
            return f"{o['id']} {o['kind']}{where} {o.get('disposition') or 'null'}{name}{to}"
        if o["kind"] == "discover":
            entry = self.state.discoveries.get(o.get("province") or "") or {}
            d = self.ruleset.discoveries.get(entry.get("id") or "")
            c = d.choice(o.get("choice")) if d is not None else None
            label = f" «{c['label']}»" if c and c.get("label") else ""
            return f"{o['id']} discover {o['province']} {o.get('choice')}{label}"
        return f"{o['id']} {o['kind']} {o.get('province', '')}".rstrip()

    def _orders_changed(self, seat: str) -> None:
        if seat in self.ready_seats:
            self.ready_seats.discard(seat)
            self.journal.append("ready_cleared", seat=seat, turn=self.state.turn, phase=self.phase)

    @staticmethod
    def _items(items: Any) -> list[Any]:
        """orders: a list of order objects, or the same list as one JSON string (guaranteed: some MCP clients
        lose nested objects but pass strings intact); one order object alone counts as a list of one. Orders
        that are JSON strings themselves are decoded too; the engine normalizes the rest (flat unit/count)."""
        items = decode_json_arg(items)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            got = "ничего" if items is None else repr(items)[:80]
            raise GameError("INVALID", f"orders должен быть списком приказов (или этим списком одной JSON-строкой), "
                                       f"получено {got}. Пример: orders=[{FLAT_MOVE_EXAMPLE}] или "
                                       f"{STRING_ORDERS_EXAMPLE}")
        return [_research_alias(decode_json_arg(item)) for item in items]

    def _require_orders_phase(self) -> None:
        if self.phase == "negotiation_1":
            raise GameError("PHASE_LOCKED", P1_LOCKED_HINT)
        if self.phase == SETUP:
            raise GameError("PHASE_LOCKED", "идёт фаза старта (выбор фракции и артефактов): приказы — с хода 1")

    def validate(self, seat: str, items: Any) -> dict[str, Any]:
        """Works in every playable phase; in negotiation_1 orders are checked as they would be in actions."""
        items = self._items(items)
        phase = "actions" if self.phase == "negotiation_1" else self.phase
        res = submit_orders(self.state, self.ruleset, seat, self._book(seat), items, phase)
        results = []
        for check in res.results:
            entry = check.to_dict()
            entry.pop("order", None)  # dry-run ids are meaningless to the player
            results.append(entry)
        rejected = [{"order": items[i], "reason": c.reason} for i, c in enumerate(res.results) if not c.ok]
        self.journal.append("orders_validated", seat=seat, ok_count=len(items) - len(rejected), rejected=rejected)
        return {"results": results, "ok_count": len(items) - len(rejected)}

    def submit(self, seat: str, items: Any, mode: str = "append") -> dict[str, Any]:
        self._require_orders_phase()
        items = self._items(items)
        book = self._book(seat)
        phase = self.phase
        res = submit_orders(self.state, self.ruleset, seat, book, items, phase, mode,
                            id_factory=lambda: issue_order_id(self.state, seat))
        accepted = [c for c in res.results if c.ok and c.order is not None]
        rejected = [{"index": i, "order": items[i], "reason": c.reason, "message": c.message}
                    for i, c in enumerate(res.results) if not c.ok]
        if orders_hash(res.book) != orders_hash(book) or res.book.cp_spent != book.cp_spent:
            self.books[seat] = res.book
            self._orders_changed(seat)
        if phase == P2:
            for c in accepted:
                self.journal.append("order_edit_p2", seat=seat, op="add", order_id=c.order.id, cp_cost=c.cp_cost)
        new_hash = orders_hash(self._book(seat))
        self.journal.append("orders_submitted", seat=seat, accepted=[c.order.id for c in accepted],
                            rejected=[{"order": r["order"], "reason": r["reason"]} for r in rejected],
                            orders_hash=new_hash, phase=phase, mode=mode)
        self._save()
        out = {"accepted": [c.order.to_dict() for c in accepted], "rejected": rejected, "orders_hash": new_hash,
               "cp_left": cp_left(self.state, seat, self._book(seat))}
        warnings = [{"order_id": c.order.id, "warning": c.warning} for c in accepted if c.warning]
        if warnings:
            out["warnings"] = warnings
        return out

    def cancel(self, seat: str, order_id: str) -> dict[str, Any]:
        self._require_orders_phase()
        res = cancel_order(self.state, self.ruleset, seat, self._book(seat), order_id, self.phase)
        return self._apply_edit(seat, "cancel", order_id, res)

    def amend(self, seat: str, order_id: str, patch: Any) -> dict[str, Any]:
        self._require_orders_phase()
        patch = decode_json_arg(patch)
        if isinstance(patch, dict) and set(patch) == {"patch"}:   # {"patch": {...}} or {"patch": "<JSON>"}
            patch = decode_json_arg(patch["patch"])
        if not isinstance(patch, dict):
            raise GameError("INVALID", "patch должен быть объектом {units?, unit?+count?, to?, count?} или им же "
                                       "одной JSON-строкой")
        old = self._book(seat).find(order_id)
        res = amend_order(self.state, self.ruleset, seat, self._book(seat), order_id, patch, self.phase)
        out = self._apply_edit(seat, "amend", order_id, res)
        if old is not None and old.feint and res.order is not None and not res.order.feint:
            self.journal.append("feint", seat=seat, order_id=order_id, turn=self.state.turn, phase=self.phase,
                                converted_in_p2=self.phase == P2, **{"from": old.src, "to": old.dst})
        return out

    def _apply_edit(self, seat: str, op: str, order_id: str, res: Any) -> dict[str, Any]:
        if not res.ok:
            raise GameError(res.reason or "INVALID", res.message)
        phase = self.phase
        self.books[seat] = res.book
        self._orders_changed(seat)
        edit_type = "order_edit_p2" if phase == P2 else "order_edit"
        self.journal.append(edit_type, seat=seat, op=op, order_id=order_id, cp_cost=res.cp_cost,
                            turn=self.state.turn, phase=phase)
        self._save()
        out = {"order_id": order_id, "cp_cost": res.cp_cost, "orders_hash": orders_hash(res.book),
               "cp_left": cp_left(self.state, seat, res.book)}
        if res.order is not None:
            out["order"] = res.order.to_dict()
        if getattr(res, "warning", None):
            out["warning"] = res.warning
        return out

    # --- estimate -------------------------------------------------------------------------------------

    def estimate_example(self) -> str:
        units = self.ruleset.units  # the cheapest unit makes the most typical example
        unit = min(units, key=lambda u: (sum(units[u].cost.values()), u)) if units else "core:assault"
        ptype = next(iter(self.ruleset.province_types), "core:forge")
        return f'estimate(attacker={{"{unit}": 4}}, defender={{"{unit}": 2}}, province_type="{ptype}")'

    def _estimate_error(self, code: str, what: str) -> GameError:
        return GameError(code, f"{what}. Пример: {self.estimate_example()}")

    @staticmethod
    def _label(text: str) -> str:
        return text.strip().lower().rstrip(".")

    def resolve_unit(self, ref: Any) -> str | None:
        """Full unit id for a full or short id, a state label (штурм) or a name (Штурмовики), any case."""
        return resolve_unit_ref(self.ruleset, ref)

    def resolve_province_type(self, ref: Any) -> str | None:
        """Full province type id for a full or short type id, a state label (агро, кузн.), a type name (Агромир),
        or a province id (P07) or name: a province stands for its type (the political map is public)."""
        if not isinstance(ref, str) or not ref.strip():
            return None
        ref = ref.strip()
        full = self.ruleset.resolve("province_type", ref) or self.ruleset.resolve("province_type", ref.lower())
        if full:
            return full
        province = self.state.provinces.get(ref.upper()) or next(
            (p for p in self.state.provinces.values() if self._label(p.name) == self._label(ref)), None)
        if province is not None:
            return province.type
        label = self._label(ref.split(":", 1)[-1])
        hits = [pid for pid, t in self.ruleset.province_types.items()
                if label in (self._label(t.short), self._label(t.name))]
        return hits[0] if len(hits) == 1 else None

    def _province_ref(self, raw: Any) -> str | None:
        """A province id (P07, any case) given where units are expected, else None."""
        if isinstance(raw, str) and raw.strip().upper() in self.state.provinces:
            return raw.strip().upper()
        return None

    def _province_units(self, side: str, pid: str, seat: str | None) -> dict[str, int]:
        """The army in a province the seat can see (its own and their neighbours), for `attacker="P05"` (the
        common m5 form). Hidden armies are refused with what to pass instead."""
        if seat is None or pid not in visible_provinces(self.state, self.ruleset, seat):
            # core 0.7.0 army fog: beyond sight only the presence of another seat's army is known, never its size
            prov = self.state.provinces[pid]
            seen = (army_presence(self.ruleset) and seat is not None and prov.owner not in (None, seat))
            what = (f"армия в {pid} {'есть' if prov.unit_total() else 'нет'}, её размер скрыт туманом войны"
                    if seen else f"войска в {pid} скрыты туманом войны")
            spy = " (размер узнаёт Тень: наблюдение)" if self.ruleset.system("espionage") else ""
            if spy and side == "defender":
                spy += ' или defender="r3" — по донесению Тени'      # core 0.8.0 (docs/SPY_V2.md 6.1)
            raise self._estimate_error("INVALID", f"{side}={pid!r}: {what} — укажи предполагаемый состав объектом "
                                                  f"{{юнит: число}}{spy}")
        units = {u: n for u, n in self.state.provinces[pid].units.items() if n > 0}
        if not units and side == "attacker":
            raise self._estimate_error("INVALID", f"{side}={pid!r}: в {pid} нет войск — укажи состав объектом "
                                                  "{юнит: число}")
        return units

    def _unknown_unit(self, side: str, ref: Any) -> GameError:
        """Unknown unit: the closest ids if any, the short ids of all units, and what to do about hidden armies."""
        text = str(ref)[:40]
        shorts = {u.short: uid for uid, u in self.ruleset.units.items()}
        names = [uid.split(":")[-1] for uid in self.ruleset.units] + list(shorts)
        close = difflib.get_close_matches(text.lower().split(":")[-1], names, n=3, cutoff=0.6)
        hint = f"; похоже на: {', '.join(close)}" if close else ""
        if text.strip() in ("?", "") or text.lower() in ("unknown", "hidden", "units", "army", "total", "?×?"):
            hint += "; войска за туманом (?) неизвестны — подставь предполагаемый состав"
        known = ", ".join(self.ruleset.units)
        return self._estimate_error("UNKNOWN_ID", f"{side}: нет юнита {text!r}{hint}. Юниты: {known} (можно ID, "
                                                  "метку из состояния или название; или ID своей/соседней "
                                                  "провинции — тогда берутся войска в ней)")

    def _estimate_side(self, side: str, raw: Any, seat: str | None = None) -> dict[str, int]:
        if raw is None:
            raise self._estimate_error("INVALID", f"не указан {side}: объект {{юнит: число}} или ID провинции, чьи "
                                                  "войска видны")
        pid = self._province_ref(raw)
        if pid is not None:
            return self._province_units(side, pid, seat)
        if isinstance(raw, list):  # [{"unit": "assault", "count": 3}] or [["assault", 3]]
            converted: dict[Any, Any] | None = {}
            for item in raw:
                if isinstance(item, dict) and "unit" in item and isinstance(item["unit"], str):
                    converted[item["unit"]] = item.get("count", item.get("n"))
                elif isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
                    converted[item[0]] = item[1]
                else:
                    converted = None
                    break
            raw = converted
        if not isinstance(raw, dict):
            raise self._estimate_error("INVALID", f"{side} должен быть объектом {{юнит: число}} или ID провинции с "
                                                  f"видимыми войсками (P07), а не {str(raw)[:30]!r}")
        units: dict[str, int] = {}
        for ref, n in raw.items():
            full = self.resolve_unit(ref)
            if full is None:
                raise self._unknown_unit(side, ref)
            count = n if isinstance(n, bool) else as_int(n)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise self._estimate_error("INVALID", f"{side}: число юнитов {full} — целое ≥ 0, а не "
                                                      f"{str(n)[:20]!r}")
            units[full] = units.get(full, 0) + count
        return units

    _REPORT_RE = re.compile(r"^#?([rRcC])(\d{1,6})$")

    def _report_ref(self, raw: Any) -> str | None:
        """A report id given as the defender ("r3", "#r3", a bought copy "c1"), else None."""
        m = self._REPORT_RE.match(raw.strip()) if isinstance(raw, str) else None
        if m and m.group(1).lower() == "c" and self._province_ref(raw):
            return None         # a province of the map named like a copy ("C2") stays a province
        return f"{m.group(1).lower()}{int(m.group(2))}" if m else None

    def find_report(self, seat: str, rid: str) -> dict[str, Any] | None:
        """One of the seat's own reports by id, from the recon steps played so far (ids are the seat's, never
        reused), or a copy it bought (`c<k>`, docs/TRADE.md 2.3: the seller's report from the state, no recon file
        needed)."""
        if rid.startswith("c"):
            entry = tr.copy_of(self.state, seat, rid)
            return tr.copy_report(entry) if entry is not None else None
        last = self.state.turn if self.phase in (P2, GAME_OVER) else self.state.turn - 1
        for t in range(max(last, 0), 0, -1):
            for r in ((self.recon_report(t) or {}).get("reports") or {}).get(seat, []):
                if r.get("id") == rid:
                    return r
        return None

    def _estimate_report(self, seat: str | None, rid: str) -> dict[str, Any]:
        """estimate(defender="r3") (core 0.8.0, docs/SPY_V2.md 6.1): the units of an Observation report and the
        recruits and fortify of its block «в расчёте этого хода»."""
        report = self.find_report(seat, rid) if seat is not None and self.ruleset.system("espionage") else None
        if report is None:
            raise self._estimate_error("UNKNOWN_ID", f"defender={rid!r}: у тебя нет донесения {rid} (get_intel)")
        if report.get("behavior") != esp.OBSERVE:
            raise self._estimate_error("INVALID", f"defender по донесению — только Наблюдение (observe); {rid} — "
                                                  f"{report.get('mission')}: у ledger армия всего, а не провинция")
        return esp.report_defender(report, self.state.turn, rid,
                                   persists=bool(self.ruleset.stat("combat.fortify_persists")))

    def estimate(self, body: dict[str, Any], seat: str | None = None) -> dict[str, Any]:
        """Battle forecast. Lenient on ids (resolve_unit, resolve_province_type); every refusal says what was
        wrong and shows a correct call. Core 0.8.0: defender="r3" takes an Observation report's garrison."""
        attacker = self._estimate_side("attacker", body.get("attacker"), seat)
        rid = self._report_ref(body.get("defender"))
        by_report = self._estimate_report(seat, rid) if rid is not None else None
        if by_report is not None:
            defender = by_report["units"]
            body = {**body, "defender": by_report["province"]}       # the battle is in the report's province
            if by_report["fortified"]:      # the report's fortify always counts (docs/SPY_V2.md 6.1)
                body["fortified"] = True
        else:
            defender = self._estimate_side("defender", body.get("defender"), seat)
        raw_type = body.get("province_type")
        if raw_type in (None, "") and self._province_ref(body.get("defender")):
            raw_type = self._province_ref(body.get("defender"))      # defender="P07": the battle is in P07
        ptype = self.resolve_province_type(raw_type)
        if ptype is None:
            known = ", ".join(f"{pid} ({t.short})" for pid, t in self.ruleset.province_types.items())
            what = "не указан province_type" if raw_type is None else f"province_type {str(raw_type)[:40]!r} не найден"
            raise self._estimate_error("INVALID" if raw_type is None else "UNKNOWN_ID",
                                       f"{what}; можно тип ({known}) или ID провинции (P07)")
        flags = {}
        for name in ("fortified", "hasty"):
            value = body.get(name)
            if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", ""):
                value = value.strip().lower() in ("true", "1")
            if value is not None and not isinstance(value, (bool, int)):
                raise self._estimate_error("INVALID", f"{name} — true или false, а не {str(value)[:20]!r}")
            flags[name] = bool(value)
        keys: dict[str, Any] = {"attacker": None, "defender": None}
        defense_mul = None
        side = body.get("side")
        side_note = None
        if seat is not None:
            # The caller's own techs, artifacts and buildings count on its side; the other side gets the public
            # faction of `opponent` (nick or seat), or base stats. Without `side`, province ids decide it: an own
            # province is the caller's side, an army in another player's province is the other side (so
            # attacker="P06" (enemy), defender="P05" (mine) forecasts the enemy attacking the caller); else attacker.
            owners = {k: (self.state.provinces[pid].owner if (pid := self._province_ref(body.get(k))) else None)
                      for k in keys}
            if side in (None, ""):
                if owners["defender"] == seat:
                    side, side_note = "defender", f"defender={self._province_ref(body.get('defender'))} — твоя"
                elif owners["attacker"] == seat:
                    side, side_note = "attacker", f"attacker={self._province_ref(body.get('attacker'))} — твоя"
                elif owners["attacker"] is not None and owners["defender"] is None:
                    side, side_note = "defender", (f"attacker={self._province_ref(body.get('attacker'))} — "
                                                   "чужая армия")
            side = str(side or "attacker").strip().lower()
            if side not in keys:
                raise self._estimate_error("INVALID", f"side — attacker или defender, а не {side[:20]!r}")
            mine = stat_key(self.state, self.ruleset, seat)
            keys[side] = mine
            other = "defender" if side == "attacker" else "attacker"
            opponent = body.get("opponent")
            if opponent not in (None, ""):
                ref = self._seat_ref(opponent)
                if not isinstance(ref, str) or ref not in self.state.players or ref == seat:
                    raise self._estimate_error("UNKNOWN_ID", f"opponent {str(opponent)[:40]!r}: нужен ник соперника")
                if self.phase != SETUP:
                    keys[other] = self.state.players[ref].faction
            elif owners[other] not in (None, seat) and self.phase != SETUP:
                keys[other] = self.state.players[owners[other]].faction   # the province's owner: a public faction
            pid = str(raw_type).strip().upper() if isinstance(raw_type, str) else None
            province = self.state.provinces.get(pid) if pid else None
            if side == "defender" and province is not None and province.owner == seat:
                defense_mul = province_defense_mul(self.state, self.ruleset, pid, mine)
            elif (side == "attacker" and province is not None and province.owner is None
                  and pid in visible_provinces(self.state, self.ruleset, seat)):
                defense_mul = province_defense_mul(self.state, self.ruleset, pid, None)   # neutrals: nothing hidden
        out = estimate(self.ruleset, attacker, defender, ptype, flags["fortified"], flags["hasty"],
                       attacker_key=keys["attacker"], defender_key=keys["defender"], defense_mul=defense_mul)
        out["losses"] = {"attacker": out["attacker_losses_avg"], "defender": out["defender_losses_avg"]}
        out["province_type"] = ptype
        if by_report is not None:
            out["defender"] = dict(defender)
            out["report"] = by_report["header"]
            if seat is not None:     # core 0.8.0 (docs/SPY_V2.md 11, «донесение → решение» (в)); journal only
                self.journal.append("estimate_report", seat=seat, report_id=rid, province=by_report["province"],
                                    turn=self.state.turn, phase=self.phase)
            if not flags["fortified"]:
                # the fortify of P2 is not in the snapshot (docs/SPY_V2.md 6.1): the second number, one line
                fort = estimate(self.ruleset, attacker, defender, ptype, True, flags["hasty"],
                                attacker_key=keys["attacker"], defender_key=keys["defender"], defense_mul=defense_mul)
                out["win_prob_if_fortified"] = fort["win_prob"]
                out["report"] += f"; если укрепится в П2: шанс {round(fort['win_prob'] * 100)}%"
        if seat is not None:
            out["side"] = side
            how = f"по владельцу провинции: {side_note}" if side_note else (
                "задано side" if body.get("side") not in (None, "") else "по умолчанию; своя оборона — side=\"defender\"")
            out["note"] = (f"твои технологии, артефакты и постройки учтены за сторону {side} ({how}); "
                           "win_prob — шанс атакующего")
            if province is not None and province.owner != seat and pid in self.state.abandoned:
                out["note"] += ("; Заброшенная провинция: находка при входе (помеха снимает юниты до боя) в "
                                "прогноз не входит")
        return out

    # --- ready ----------------------------------------------------------------------------------------

    def _require_active(self, seat: str) -> None:
        if self.phase not in PLAYABLE_PHASES:
            raise GameError("PHASE_LOCKED", "сейчас нет открытой фазы" if not self.game_over else "игра окончена")
        if not self.state.players[seat].alive:
            raise GameError("PHASE_LOCKED", "место выбыло из игры")

    def ready_text(self) -> str:
        alive = self.alive_seats()
        return f"{len(self.ready_seats & set(alive))}/{len(alive)}"

    def set_ready(self, seat: str, confirm_hash: Any) -> dict[str, Any]:
        if self.phase == SETUP:
            self._require_setup(seat)
            current = self.setup_hash(seat)
            if confirm_hash not in (None, current):    # optional in setup; if given, it must be the current choice
                raise GameError("STALE_HASH", f"выбор не совпадает с confirm_hash; актуальный confirm_hash: {current}")
        elif self.phase == "negotiation_1":
            # No orders can be given or changed in P1 (only validated): there is nothing for a hash to confirm, so
            # ready() needs none and any confirm_hash (a stale one from the last turn included) is accepted.
            self._require_active(seat)
        else:
            self._require_active(seat)
            book = self._book(seat)
            current = orders_hash(book)
            label = PHASE_LABELS.get(self.phase, self.phase)
            what = f"{len(book.orders)} приказ(ов)" if book.orders else "приказов нет — армии стоят"
            if confirm_hash in (None, ""):
                raise GameError("STALE_HASH", f"в фазе {label} нужен confirm_hash — orders_hash твоих приказов "
                                              f"(сейчас {what}); он подтверждает, что ты готов именно с ними. "
                                              f"Если они верны, вызови ready(confirm_hash=\"{current}\"). orders_hash "
                                              "есть в phase(), list_orders() и ответе submit_orders")
            if confirm_hash != current:
                raise GameError("STALE_HASH", f"confirm_hash устарел: приказы изменились после него (сейчас {what}; "
                                              "проверь list_orders()). Если они верны, вызови "
                                              f"ready(confirm_hash=\"{current}\")")
        closed = False
        if seat not in self.ready_seats:
            self.ready_seats.add(seat)
            self.journal.append("ready", seat=seat, turn=self.state.turn, phase=self.phase)
            ready = self.ready_text()
            if set(self.alive_seats()) <= self.ready_seats:
                self._close_phase("all_ready")
                closed = True
            else:
                self._save()
        else:
            ready = self.ready_text()
        return {"ready": ready, "phase_closed": closed}

    def unready(self, seat: str) -> dict[str, Any]:
        if self.phase == SETUP:
            self._require_setup(seat)
        else:
            self._require_active(seat)
        if seat in self.ready_seats:
            self.ready_seats.discard(seat)
            self.journal.append("unready", seat=seat, turn=self.state.turn, phase=self.phase)
            self._save()
        return {"ready": self.ready_text()}

    # --- messages -------------------------------------------------------------------------------------

    def _limits(self) -> dict[str, Any]:
        return self.config["message_limits"].get(self.phase, {})

    def _counts(self, seat: str) -> dict[str, Any]:
        return self.msg_counts.setdefault(seat, {"private": 0, "public": 0, "to": {}})

    def messages_left(self, seat: str) -> dict[str, Any]:
        lim, used = self._limits(), self._counts(seat)
        left: dict[str, Any] = {"private": max(0, lim.get("private", 0) - used["private"]),
                                "public": max(0, lim.get("public", 0) - used["public"])}
        if lim.get("per_recipient"):
            left["per_recipient"] = {self.state.players[s].nick: max(0, lim["per_recipient"] - used["to"].get(s, 0))
                                     for s in self.state.players if s != seat}
        return left

    def message_example(self, seat: str) -> str:
        other = next((p.nick for s, p in self.state.players.items() if s != seat), "Ник")
        return f'send_message(to=["{other}"], text="Предлагаю мир") или send_message(to="all", text="...")'

    def send_message(self, seat: str, to: Any, text: Any) -> dict[str, Any]:
        """Every refusal says what was wrong and, where the call itself was wrong, shows a correct one."""
        try:
            return self._send_message(seat, to, text)
        except GameError as exc:
            if self.game_over:
                raise GameError(exc.code, "игра окончена (game_over): сообщения больше не отправляются, "
                                          "заверши цель коротким итогом") from None
            if exc.code == "PHASE_LOCKED" and self.phase == "actions":
                raise GameError(exc.code, "в фазе Д переписка закрыта: сообщения — только в П1 и П2. Пример (в "
                                          f"П1/П2): {self.message_example(seat)}") from None
            if exc.code in ("INVALID", "UNKNOWN_ID", "TOO_LONG"):
                raise GameError(exc.code, f"{exc.message}. Пример: {self.message_example(seat)}") from None
            raise

    def _send_message(self, seat: str, to: Any, text: Any) -> dict[str, Any]:
        self._require_active(seat)
        if self.phase not in NEGOTIATION_PHASES:
            raise GameError("PHASE_LOCKED", "сообщения — только в фазах переговоров (П1, П2)")
        max_chars = self.config["message_limits"].get("max_chars", 800)
        if not isinstance(text, str) or not text.strip():
            raise GameError("INVALID", "text — непустая строка")
        if len(text) > max_chars:
            raise GameError("TOO_LONG", f"сообщение длиннее {max_chars} символов ({len(text)})")
        recipients = self._recipients(seat, to)
        lim, used = self._limits(), self._counts(seat)
        if recipients is None:
            if used["public"] + 1 > lim.get("public", 0):
                raise GameError("LIMIT_EXCEEDED", f"лимит публичных сообщений в фазе: {lim.get('public', 0)}")
        else:
            if used["private"] + len(recipients) > lim.get("private", 0):
                raise GameError("LIMIT_EXCEEDED", f"лимит личных сообщений в фазе: {lim.get('private', 0)}, "
                                                  f"осталось {lim.get('private', 0) - used['private']}")
            per = lim.get("per_recipient")
            full = [r for r in recipients if per and used["to"].get(r, 0) + 1 > per]
            if full:
                nicks = ", ".join(self.state.players[r].nick for r in full)
                raise GameError("LIMIT_EXCEEDED", f"лимит {per} сообщений одному адресату исчерпан: {nicks}")
        if recipients is None:
            used["public"] += 1
        else:
            used["private"] += len(recipients)
            for r in recipients:
                used["to"][r] = used["to"].get(r, 0) + 1
        record = {"id": len(self.messages) + 1, "ts": round(self.clock(), 3), "turn": self.state.turn,
                  "phase": self.phase, "from": seat, "to": "all" if recipients is None else recipients, "text": text}
        self.gdir.append_message(record)
        self.messages.append(record)
        self.journal.append("message", **{"from": seat, "to": record["to"], "chars": len(text),
                                          "phase": self.phase, "turn": self.state.turn})
        self._save()
        sent = sum(1 for m in self.messages if m["from"] == seat)  # own count: the global id would leak traffic
        # the message's number in the seat's own inbox (where it shows as from "ты"): the cursor inbox(since=) uses
        inbox_id = sum(1 for m in self.messages if (m["from"] == seat and not m.get("system"))
                       or (m["from"] != seat and (m["to"] == "all" or seat in m["to"])))
        return {"message_id": sent, "inbox_id": inbox_id, "left": self.messages_left(seat)}

    def _recipients(self, seat: str, to: Any) -> list[str] | None:
        if to == "all" or to == ["all"]:
            return None
        if isinstance(to, str):
            to = [to]
        if to is None:
            raise GameError("INVALID", "не указан to: список ников или \"all\"")
        if not isinstance(to, list) or not to or not all(isinstance(n, str) for n in to):
            raise GameError("INVALID", "to — список ников или \"all\"")
        out: list[str] = []
        for nick in to:
            target = self.nick_to_seat.get(nick) or (nick if nick in self.seats else None)
            if target is None:
                others = ", ".join(p.nick for s, p in self.state.players.items() if s != seat)
                raise GameError("UNKNOWN_ID", f"нет игрока с ником {nick[:40]!r} (игроки: {others})")
            if target == seat:
                raise GameError("INVALID", "нельзя писать самому себе")
            if target not in out:
                out.append(target)
        return out

    def inbox(self, seat: str, since: int | None = None) -> dict[str, Any]:
        """Messages addressed to the seat and the seat's own sent messages (`own: true`, from "ты": every member of a
        swarm sees what its teammates already said), numbered 1, 2, ... per seat (`since` is that number), so the
        ids say nothing about messages between other players."""
        players = self.state.players
        received = [m for m in self.messages
                    if (m["from"] == seat and not m.get("system"))
                    or (m["from"] != seat and (m["to"] == "all" or seat in m["to"]))]
        out = []
        for number, m in enumerate(received, start=1):
            if since is not None and number <= since:
                continue
            entry = {"id": number, "turn": m["turn"], "phase": m["phase"],
                     "to": "all" if m["to"] == "all" else [players[s].nick for s in m["to"]],
                     "public": m["to"] == "all"}
            if m.get("system"):  # written by the game, not by a player: not wrapped as untrusted
                entry.update({"from": SYSTEM_SENDER, "system": True, "event": m.get("event"),
                              "text": system_text(m["text"])})
            elif m["from"] == seat:   # sent by this seat (a teammate in a swarm): its own words, escaped not wrapped
                entry.update({"from": OWN_SENDER, "own": True, "text": escape_untrusted(m["text"])})
            else:
                entry.update({"from": players[m["from"]].nick,
                              "text": wrap_untrusted(players[m["from"]].nick, m["text"])})
            out.append(entry)
        # never beyond the newest entry: a `since` past it must not move a client's cursor over future messages
        last_id = out[-1]["id"] if out else min(since or 0, len(received))

        def head(m: dict[str, Any]) -> str:
            if m.get("own"):
                return f"ты → {'всем' if m['public'] else ', '.join(m['to'])}"
            return "всем" if m["public"] else "лично"

        lines = [f"#{m['id']} ход {m['turn']} {PHASE_LABELS.get(m['phase'], m['phase'])} {head(m)}: {m['text']}"
                 for m in out]
        return {"messages": out, "last_id": last_id, "text": "\n".join(lines) or "новых сообщений нет"}

    def _notice(self, to: list[str] | str, text: str, event: str, *, turn: int | None = None,
                phase: str | None = None) -> None:
        """An inbox entry from the game itself (`system`), to seats or "all"; not counted against message limits."""
        record = {"id": len(self.messages) + 1, "ts": round(self.clock(), 3),
                  "turn": self.state.turn if turn is None else turn, "phase": phase or self.phase, "from": None,
                  "to": to if to == "all" else sorted(to), "text": text, "system": True, "event": event}
        self.gdir.append_message(record)
        self.messages.append(record)

    # --- diplomacy: gifts and pacts --------------------------------------------------------------------

    def _nick(self, seat: str | None) -> str:
        return escape_untrusted(self.state.players[seat].nick) if seat in self.state.players else "—"

    def _seat_ref(self, ref: Any) -> Any:
        """A nick or a seat id -> the seat id (anything else is returned for the engine to refuse)."""
        if isinstance(ref, list) and len(ref) == 1:
            ref = ref[0]
        if isinstance(ref, str):
            return self.nick_to_seat.get(ref.strip()) or (ref.strip() if ref.strip() in self.seats else ref)
        return ref

    def _diplomacy(self, seat: str, make: Callable[[], dict[str, Any]], example: str) -> dict[str, Any]:
        self._require_active(seat)
        try:
            op = make()
        except dip.DiplomacyError as exc:
            if exc.code == "PHASE_LOCKED" and self.phase == "actions":
                raise GameError(exc.code, "в фазе Д дипломатия закрыта: подношения и пакты — в П1 и П2") from None
            others = ", ".join(p.nick for s, p in self.state.players.items() if s != seat and p.alive)
            hint = f" (игроки: {others})" if exc.code == "UNKNOWN_ID" and "игрок" in exc.message else ""
            # PACT_EXISTS names the exact next call itself; a generic propose example would contradict it
            tail = "" if exc.code == "PACT_EXISTS" else f". Пример: {example}"
            raise GameError(exc.code, f"{exc.message}{hint}{tail}") from None
        pact = dip.apply_op(self.state, op)
        self.diplomacy_log.append(op)
        return {"op": op, "pact": pact}

    def _count(self, seat: str, kind: str) -> None:
        limit = diplomacy_limits(self.config)[f"{kind}_per_phase"]
        used = self.diplomacy_counts.setdefault(seat, {})
        if used.get(kind, 0) >= limit:
            what = {"gifts": "подношений", "trade_proposals": "предложений обмена"}.get(kind, "предложений пакта")
            raise GameError("LIMIT_EXCEEDED", f"лимит {what} в фазе: {limit}")

    def _bump(self, seat: str, kind: str) -> int:
        used = self.diplomacy_counts.setdefault(seat, {})
        used[kind] = used.get(kind, 0) + 1
        return diplomacy_limits(self.config)[f"{kind}_per_phase"] - used[kind]

    def _resources_text(self, amounts: dict[str, int]) -> str:
        return ", ".join(f"{self.ruleset.resources[r].name} {n}" for r, n in amounts.items())

    def gift_example(self, seat: str) -> str:
        other = next((p.nick for s, p in self.state.players.items() if s != seat), "Ник")
        res = next(iter(self.ruleset.resources), "core:alloy").split(":")[-1]
        return f'send_gift(to="{other}", resources={{"{res}": 10}})'

    def send_gift(self, seat: str, to: Any, resources: Any) -> dict[str, Any]:
        """Give free (unreserved) resources to another living seat at once; the recipient gets a game notice."""
        self._require_active(seat)
        self._count(seat, "gifts")
        reserved = seat_reserved(self.state, self.ruleset, seat, self._book(seat))
        done = self._diplomacy(seat, lambda: dip.make_gift(self.state, self.ruleset, seat, self._seat_ref(to),
                                                           decode_json_arg(resources), self.phase, reserved),
                               self.gift_example(seat))
        op = done["op"]
        left = self._bump(seat, "gifts")
        self._save()     # the transfer is durable before the journal and the inbox say it happened
        what = self._resources_text(op["resources"])
        self.journal.append("gift", seat=seat, to=op["recipient"], resources=op["resources"], turn=self.state.turn,
                            phase=self.phase)
        self._notice([op["recipient"]], f"{self._nick(seat)} прислал тебе подношение: {what}. Ресурсы уже в твоей "
                                        "казне.", "gift")
        return {"gift": {"to": self.state.players[op["recipient"]].nick, "resources": op["resources"]},
                "gifts_left": left, "resources": dict(self.state.players[seat].resources),
                "text": f"подношение {self.state.players[op['recipient']].nick}: {what}; подношений в фазе "
                        f"осталось {left}"}

    def _pact_terms(self, pact: dict[str, Any]) -> str:
        if pact.get("until_turn") is not None:
            return f"до хода {pact['until_turn']} включительно"
        if pact.get("turns") is not None:
            return f"на {pact['turns']} ход(а)"
        return "бессрочно"

    def propose_pact(self, seat: str, to: Any, turns: Any = None) -> dict[str, Any]:
        self._require_active(seat)
        self._count(seat, "pact_proposals")
        example = 'propose_pact(to="Ник", turns=3) или propose_pact(to="Ник") — бессрочно'
        done = self._diplomacy(seat, lambda: dip.make_proposal(self.state, seat, self._seat_ref(to), turns,
                                                               self.phase), example)
        op, pact = done["op"], done["pact"]
        self._bump(seat, "pact_proposals")
        recipient = op["recipient"]
        renews = dip.pact_by_id(self.state, op.get("renews")) if op.get("renews") else None
        was_ready = recipient in self.ready_seats
        self.ready_seats.discard(recipient)      # an offer needs an answer: the phase waits for the recipient again
        self._save()
        if was_ready:
            self.journal.append("ready_cleared", seat=recipient, turn=self.state.turn, phase=self.phase,
                                reason="pact_proposed")
        extra = {"renews": renews["id"]} if renews else {}
        self.journal.append("pact_proposed", seat=seat, to=op["recipient"], pact_id=pact["id"], turns=op["turns"],
                            turn=self.state.turn, phase=self.phase, **extra)
        answer = (f"Ответь respond_pact(id=\"{pact['id']}\", decision=\"accept\" или \"reject\") до конца хода.")
        if renews is not None:
            terms = self._renewal_terms(renews, pact)
            self._notice([recipient], f"{self._nick(seat)} предлагает продлить пакт {renews['id']} ({terms}): "
                                      f"продление {pact['id']} начнётся сразу после нынешнего срока, без разрыва. "
                                      + answer, "pact_proposed")
            return {"pact": self._pact_view(pact, seat),
                    "text": f"предложение продлить пакт {renews['id']} отправлено ({pact['id']}, {terms}); ждёт "
                            "ответа до расчёта"}
        self._notice([recipient], f"{self._nick(seat)} предлагает пакт {pact['id']} ({self._pact_terms(pact)}"
                                  "): армии сторон пакта при встрече не сражаются. " + answer, "pact_proposed")
        return {"pact": self._pact_view(pact, seat),
                "text": f"предложение {pact['id']} отправлено ({self._pact_terms(pact)}); ждёт ответа до расчёта"}

    def _renewal_terms(self, current: dict[str, Any], renewal: dict[str, Any]) -> str:
        """`ещё на 3 ход(а): с хода 6 до хода 8 включительно` (or indefinitely after the current term)."""
        start = (current["until_turn"] or 0) + 1
        if renewal["turns"] is None:
            return f"бессрочно, начиная с хода {start}"
        return f"ещё на {renewal['turns']} ход(а): с хода {start} до хода {start + renewal['turns'] - 1} включительно"

    def respond_pact(self, seat: str, pact_id: Any, decision: Any) -> dict[str, Any]:
        self._require_active(seat)
        example = 'respond_pact(id="pact-A-B-1", decision="accept") или decision="reject"'
        known = dip.pact_by_id(self.state, str(pact_id).strip())
        if (known is not None and seat in (known["a"], known["b"]) and known["status"] in (dip.ACTIVE, dip.RENEWED)
                and str(decision).strip().lower() in ("accept", "true", "yes", "да", "принять")
                and self.phase in dip.NEGOTIATION_PHASES):
            # accepted already (often by a teammate of the same swarm): nothing to do, not an error -- as long as
            # the pact still holds and no renewal of it waits for this seat's answer
            current = dip.pact_by_id(self.state, known.get("renews")) if known["status"] == dip.RENEWED else known
            if current is None or not dip.in_force(current, self.state.turn):
                end = (f"выход объявлен, пакт закончился перед расчётом хода {current['ends_before_turn']}"
                       if current is not None and current["status"] == dip.ACTIVE
                       else f"статус {PACT_STATUS.get(current['status'], current['status'])}" if current else "")
                raise GameError("INVALID", f"{known['id']} уже принят, но пакт "
                                           f"{current['id'] if current else known.get('renews')} больше не действует "
                                           f"({end}); принимать нечего. list_pacts() покажет твои пакты")
            renewal = dip.pending_renewal(self.state, current["id"])
            if renewal is not None and renewal["b"] == seat:
                raise GameError("INVALID", f"{known['id']} — сам пакт, он уже принят; ответа ждёт продление "
                                           f"{renewal['id']}: respond_pact(id=\"{renewal['id']}\", "
                                           "decision=\"accept\") или decision=\"reject\"")
            leaving = (f", но выход объявлен: конец перед расчётом хода {current['ends_before_turn']}"
                       if current.get("leaving") else "")
            return {"pact": self._pact_view(known, seat), "already": True,
                    "text": f"{known['id']} уже принят: пакт {current['id']} действует "
                            f"{self._pact_terms(current)}{leaving}; отвечать больше не нужно"}
        done = self._diplomacy(seat, lambda: dip.make_response(self.state, seat, str(pact_id).strip(), decision,
                                                               self.phase), example)
        op, pact = done["op"], done["pact"]
        accepted = op["accept"]
        self._save()
        if accepted and pact.get("renews"):
            current = dip.pact_by_id(self.state, pact["renews"])
            self.journal.append("pact_renewed", seat=seat, pact_id=current["id"], renewal_id=pact["id"],
                                seats=sorted((pact["a"], pact["b"])), turns=pact["turns"],
                                from_turn=pact["since_turn"], until_turn=current["until_turn"],
                                turn=self.state.turn, phase=self.phase)
            self._notice("all", f"Пакт продлён: {self._nick(pact['a'])} и {self._nick(pact['b'])} — теперь "
                                f"{self._pact_terms(current)} (продление с хода {pact['since_turn']}, без разрыва).",
                         "pact_renewed")
            return {"pact": self._pact_view(current, seat),
                    "text": f"продление {pact['id']} принято: пакт {current['id']} действует "
                            f"{self._pact_terms(current)}"}
        self.journal.append("pact_accepted" if accepted else "pact_rejected", seat=seat, pact_id=pact["id"],
                            seats=sorted((pact["a"], pact["b"])), turns=pact["turns"],
                            until_turn=pact["until_turn"], turn=self.state.turn, phase=self.phase)
        if accepted:
            self._notice("all", f"Пакт заключён: {self._nick(pact['a'])} и {self._nick(pact['b'])} "
                                f"({self._pact_terms(pact)}). Их армии при встрече не сражаются, пока пакт "
                                "действует.", "pact_accepted")
        elif pact.get("renews"):
            current = dip.pact_by_id(self.state, pact["renews"])
            still = (f"пакт {current['id']} по-прежнему действует {self._pact_terms(current)}"
                     if current is not None and current["status"] == dip.ACTIVE
                     else f"пакт {pact['renews']} без изменений")
            self._notice([pact["a"]], f"{self._nick(seat)} отклонил продление пакта {pact['renews']} "
                                      f"({pact['id']}); {still}.", "pact_rejected")
            return {"pact": self._pact_view(pact, seat),
                    "text": f"продление {pact['id']} отклонено; {still}"}
        else:
            self._notice([pact["a"]], f"{self._nick(seat)} отклонил пакт {pact['id']}.", "pact_rejected")
        verdict = "заключён" if accepted else "отклонён"
        return {"pact": self._pact_view(pact, seat), "text": f"пакт {pact['id']} {verdict}"}

    def leave_pact(self, seat: str, pact_id: Any) -> dict[str, Any]:
        self._require_active(seat)
        example = 'leave_pact(id="pact-A-B-1")'
        pending = dip.pending_renewal(self.state, str(pact_id).strip())
        done = self._diplomacy(seat, lambda: dip.make_leave(self.state, seat, str(pact_id).strip(), self.phase),
                               example)
        pact = done["pact"]
        partner = dip.partner_of(pact, seat)
        self._save()
        cancelled = pending if pending is not None and pending["status"] == dip.CANCELLED else None
        if cancelled is not None:
            self.journal.append("pact_renewal_cancelled", seat=seat, pact_id=pact["id"], renewal_id=cancelled["id"],
                                turn=self.state.turn, phase=self.phase)
            self._notice([pact["a"], pact["b"]], f"Предложение продлить пакт {pact['id']} ({cancelled['id']}) отменено: "
                                                 f"{self._nick(seat)} выходит из пакта.", "pact_renewal_cancelled")
        if pact["status"] == dip.WITHDRAWN:
            self.journal.append("pact_withdrawn", seat=seat, pact_id=pact["id"], turn=self.state.turn,
                                phase=self.phase)
            self._notice([partner], f"{self._nick(seat)} отозвал предложение пакта {pact['id']}.", "pact_withdrawn")
            text = f"предложение {pact['id']} отозвано"
        else:
            self.journal.append("pact_left", seat=seat, pact_id=pact["id"], seats=sorted((pact["a"], pact["b"])),
                                ends_before_turn=pact["ends_before_turn"], turn=self.state.turn, phase=self.phase)
            self._notice("all", f"{self._nick(seat)} выходит из пакта с {self._nick(partner)}: пакт перестаёт "
                                f"действовать перед расчётом хода {pact['ends_before_turn']}.", "pact_left")
            text = (f"выход из пакта {pact['id']} объявлен всем; пакт действует до расчёта хода "
                    f"{pact['ends_before_turn']} (не включая его)"
                    + (f"; предложение продления {cancelled['id']} отменено" if cancelled else ""))
        return {"pact": self._pact_view(pact, seat), "text": text}

    def _pact_view(self, pact: dict[str, Any], seat: str) -> dict[str, Any]:
        nick = self.state.players
        out = {"id": pact["id"], "with": nick[dip.partner_of(pact, seat)].nick, "proposed_by": nick[pact["a"]].nick,
               "status": pact["status"], "turns": pact["turns"], "since_turn": pact["since_turn"],
               "until_turn": pact["until_turn"], "leaving": nick[pact["leaving"]].nick if pact.get("leaving") else None,
               "ends_before_turn": pact.get("ends_before_turn"), "broken_by": [nick[s].nick for s in
                                                                               pact.get("broken_by") or []]}
        if pact.get("renews"):
            out["renews"] = pact["renews"]
        if pact.get("renewals"):
            out["renewals"] = [dict(r) for r in pact["renewals"]]
        if pact["status"] == dip.ACTIVE:
            renewal = dip.pending_renewal(self.state, pact["id"])
            if renewal is not None:
                out["renewal_proposed"] = {"id": renewal["id"], "by": nick[renewal["a"]].nick,
                                           "turns": renewal["turns"]}
            out["renewable"] = dip.renewable(pact, self.state.turn) and renewal is None
        return out

    def pacts(self, seat: str) -> dict[str, Any]:
        """The seat's own pacts and proposals (with ids) and every active pact between others (public, no ids)."""
        mine = [self._pact_view(p, seat) for p in self.state.pacts if seat in (p["a"], p["b"])]
        public = []
        for p in (dip.public_pact(x) for x in dip.active_pacts(self.state)):
            if seat in p["seats"]:
                continue
            entry = {"seats": [self.state.players[s].nick for s in p["seats"]], "turns": p["turns"],
                     "until_turn": p["until_turn"],
                     "leaving": self.state.players[p["leaving"]].nick if p.get("leaving") else None,
                     "ends_before_turn": p["ends_before_turn"]}
            if p.get("renewed"):
                entry["renewed"] = p["renewed"]
            public.append(entry)
        lines = []
        for p in mine:
            if p["status"] == dip.ACTIVE:
                extra = (f"; {p['leaving']} выходит, конец перед расчётом хода {p['ends_before_turn']}"
                         if p["leaving"] else "")
                if p.get("renewals"):
                    extra += f"; продлён {len(p['renewals'])} раз(а)"
                if p.get("renewal_proposed"):
                    r = p["renewal_proposed"]
                    extra += (f"; {'ты предложил' if r['by'] != p['with'] else p['with'] + ' предлагает'} продление "
                              f"{r['id']}")
                elif p.get("renewable") and p["until_turn"] == self.state.turn:
                    extra += (f"; последний ход срока — продлить: propose_pact(to=\"{p['with']}\", turns=N)")
                lines.append(f"{p['id']}: пакт с {p['with']}, действует "
                             f"{'до хода ' + str(p['until_turn']) + ' включительно' if p['until_turn'] else 'бессрочно'}"
                             f"{extra}")
            elif p["status"] == dip.PROPOSED:
                terms = f"на {p['turns']} ход(а)" if p["turns"] else "бессрочно"
                what = f"продлить пакт {p['renews']} {terms}" if p.get("renews") else f"пакт {terms}"
                if p["proposed_by"] == p["with"]:
                    lines.append(f"{p['id']}: тебе предлагает {p['with']} — {what}; ответь "
                                 f"respond_pact(id=\"{p['id']}\", decision=\"accept\"|\"reject\") до конца хода")
                else:
                    lines.append(f"{p['id']}: ты предложил {p['with']} — {what}; ждёт ответа")
            else:
                lines.append(f"{p['id']}: с {p['with']} — {PACT_STATUS.get(p['status'], p['status'])}"
                             + (f" (нарушил {', '.join(p['broken_by'])})" if p["broken_by"] else ""))
        for p in public:
            lines.append(f"публично: {' — '.join(p['seats'])}, "
                         f"{'до хода ' + str(p['until_turn']) if p['until_turn'] else 'бессрочно'}"
                         + (f" (продлён {p['renewed']} раз(а))" if p.get("renewed") else "")
                         + (f"; {p['leaving']} выходит" if p["leaving"] else ""))
        return {"mine": mine, "public": public, "text": "\n".join(lines) or "пактов нет"}

    # --- trade (docs/TRADE.md, core 0.8.0) ------------------------------------------------------------------

    def _require_trade(self) -> None:
        if not tr.on(self.ruleset):
            raise GameError("NOT_AVAILABLE", "обмена в этой партии нет (торговля выключена); ресурсы можно подарить "
                                             "— send_gift(to, resources)")

    def trade_example(self, seat: str) -> str:
        other = next((p.nick for s, p in self.state.players.items() if s != seat and p.alive), "Ник")
        res = [r.split(":")[-1] for r in self.ruleset.resources] or ["alloy"]
        return f'propose_trade(to="{other}", give={{"{res[0]}": 10}}, want={{"{res[1 % len(res)]}": 10}})'

    def _trade_error(self, seat: str, exc: dip.DiplomacyError, example: str) -> GameError:
        if exc.code == "PHASE_LOCKED" and self.phase == "actions":
            return GameError(exc.code, "в фазе Д обмен закрыт: propose_trade и respond_trade — в П1 и П2; "
                                       "list_trades() работает и сейчас")
        others = ", ".join(p.nick for s, p in self.state.players.items() if s != seat and p.alive)
        hint = f" (игроки: {others})" if exc.code == "UNKNOWN_ID" and "игрок" in exc.message else ""
        # an answer that already names the next call needs no generic example
        named = any(x in exc.message for x in ("respond_trade(", "propose_trade(", "list_trades"))
        tail = "" if named or exc.code in ("PHASE_LOCKED", "NOT_AVAILABLE") else f". Пример: {example}"
        return GameError(exc.code, f"{exc.message}{hint}{tail}")

    def _trade_ctx(self) -> tr.Ctx:
        """What a trade check needs beyond the state: books (reserves and orders) and the seats' own reports."""
        return tr.Ctx(reserved=lambda s: seat_reserved(self.state, self.ruleset, s, self._book(s)),
                      orders=lambda s: list(self._book(s).orders),
                      report=lambda s, rid: self._own_report(s, rid),
                      turn_limit=self.config["turn_limit"])

    def _own_report(self, seat: str, rid: str) -> dict[str, Any] | None:
        """One of the seat's own reports (never a bought copy) of the recon steps played so far."""
        if not self.ruleset.system("espionage") or not rid.startswith("r"):
            return None
        return self.find_report(seat, rid)

    def propose_trade(self, seat: str, to: Any, give: Any = None, want: Any = None, later: Any = None,
                      reply_to: Any = None, replaces: Any = None, fulfils: Any = None) -> dict[str, Any]:
        """An offer to another living seat (docs/TRADE.md 1.2): it clears the recipient's ready and waits for its
        answer until the end of the turn. The same offer again is `already`; another one to the same seat while one
        is open is OFFER_EXISTS (`replaces=id` replaces it explicitly)."""
        self._require_trade()
        self._require_active(seat)
        example = self.trade_example(seat)
        try:
            made = tr.make_offer(self.state, self.ruleset, seat, self._seat_ref(to), decode_json_arg(give),
                                 decode_json_arg(want), decode_json_arg(later), reply_to, replaces, fulfils, self.phase,
                                 self._trade_ctx())
        except dip.DiplomacyError as exc:
            raise self._trade_error(seat, exc, example) from None
        if "already" in made:
            t = made["already"]
            return {"id": t["id"], "status": t["status"], "already": True,
                    "text": f"предложение {t['id']} для {self._nick(t['b'])} на тех же условиях уже ждёт ответа; "
                            "ничего не изменено"}
        self._count(seat, "trade_proposals")
        op = made
        trade = dip.apply_op(self.state, op)
        self.diplomacy_log.append(op)
        self._bump(seat, "trade_proposals")
        recipient = op["recipient"]
        was_ready = recipient in self.ready_seats
        self.ready_seats.discard(recipient)      # an offer needs an answer: the phase waits for the recipient again
        self._save()
        turn, phase, seats = self.state.turn, self.phase, sorted((seat, recipient))
        if was_ready:
            self.journal.append("ready_cleared", seat=recipient, turn=turn, phase=phase, reason="trade_proposed")
        for ref, kind in (("replaces", "trade_replaced"), ("reply_to", "trade_countered")):
            if op.get(ref):
                self.journal.append(kind, seat=seat, seats=seats, trade_id=op[ref], by=op["id"], turn=turn,
                                    phase=phase)
        extra = {k: op[k] for k in ("later", "replaces", "reply_to", "fulfils", "heads") if op.get(k)}
        self.journal.append("trade_proposed", seat=seat, to=recipient, seats=seats, trade_id=op["id"],
                            give=op["give"], want=op["want"], settle=trade["settle"], turn=turn, phase=phase, **extra)
        note = (f" (встречное на твоё {op['reply_to']})" if op.get("reply_to") else
                f" (заменяет {op['replaces']})" if op.get("replaces") else "")
        call = f'respond_trade(id="{op["id"]}", decision="accept") или decision="reject"'
        where = tr.province_notes(self.ruleset, self.state, trade, recipient)
        self._notice([recipient], f"{self._nick(seat)} предлагает обмен {op['id']}{note}: "
                                  f"{tr.terms_text(self.ruleset, trade, recipient, self.state)}"
                                  f"{'; ' + where if where else ''}. Ответь {call} до конца хода.",
                     "trade_proposed")
        # the reply ≤ 200 chars (docs/TRADE.md 10): the promise is in list_trades, the notes short; then bare ids
        where = tr.province_notes(self.ruleset, self.state, trade, seat, short=True)
        head = f"предложение {op['id']} → {self._nick(recipient)}: "
        promise = "; обещание — в list_trades" if op.get("later") else ""
        tail = "; ответ до конца хода"
        variants = [head + tr.terms_text(self.ruleset, trade, seat, self.state, promise=False, names=names)
                    + promise + (f"; {where}" if where else "") + tail for names in (True, False)]
        variants.append(head + tr.terms_text(self.ruleset, trade, seat, promise=False, names=False) + promise + tail
                        + (" (провинции — list_trades)" if where else ""))
        return {"id": op["id"], "status": trade["status"], "text": tr.fit_reply(variants)}

    def respond_trade(self, seat: str, trade_id: Any, decision: Any) -> dict[str, Any]:
        """The recipient accepts (both sides checked at once; a trade without a province is carried out at once, one
        with a province goes into escrow until this turn's resolution) or rejects; the proposer's reject withdraws
        its offer."""
        self._require_trade()
        self._require_active(seat)
        example = 'respond_trade(id="trade-A-B-1", decision="accept") или decision="reject"'
        known = tr.trade_by_id(self.state, str(trade_id).strip())
        if (known is not None and known["b"] == seat and known["status"] in (tr.DONE, tr.PENDING)
                and self.phase in NEGOTIATION_PHASES
                and str(decision).strip().lower() in ("accept", "true", "yes", "да", "принять")):
            # accepted already (often by a teammate of the same swarm): nothing to do, not an error
            what = (tr.settled_text(self.ruleset, known, seat) if known["status"] == tr.DONE
                    else "переход в расчёте этого хода")
            return {"id": known["id"], "status": known["status"], "already": True,
                    "text": f"{known['id']} уже принят ({what}); отвечать больше не нужно"}
        try:
            op = tr.make_answer(self.state, self.ruleset, seat, trade_id, decision, self.phase, self._trade_ctx())
        except dip.DiplomacyError as exc:
            raise self._trade_error(seat, exc, example) from None
        trade = dip.apply_op(self.state, op)
        self.diplomacy_log.append(op)
        self._save()
        a, b, tid = trade["a"], trade["b"], trade["id"]
        base = {"trade_id": tid, "seats": sorted((a, b)), "turn": self.state.turn, "phase": self.phase}
        if not op["accept"]:
            if trade["status"] == tr.WITHDRAWN:
                self.journal.append("trade_withdrawn", seat=seat, **base)
                self._notice([b], f"{self._nick(a)} отозвал предложение обмена {tid}.", "trade_withdrawn")
                return {"id": tid, "status": trade["status"], "text": f"предложение {tid} отозвано"}
            self.journal.append("trade_rejected", seat=seat, **base)
            self._notice([a], f"{self._nick(b)} отклонил обмен {tid}.", "trade_rejected")
            return {"id": tid, "status": trade["status"], "text": f"обмен {tid} отклонён"}
        if trade["status"] == tr.PENDING:
            self.journal.append("trade_accepted", seat=seat, settle=trade["settle"], escrow=trade.get("escrow"),
                                **base)
            for x, y in ((a, b), (b, a)):
                held = tr.escrow_text(self.ruleset, (trade.get("escrow") or {}).get(x) or {})
                self._notice([x], f"Обмен {tid} с {self._nick(y)} принят: переход в расчёте этого хода — "
                                  f"{tr.province_notes(self.ruleset, self.state, trade, x)}"
                                  + (f"; в залоге: {held}" if held else "") + ".", "trade_accepted")
            held = tr.escrow_text(self.ruleset, (trade.get("escrow") or {}).get(seat) or {})
            head = f"в залоге до расчёта: {held}" if held else "принят: переход в расчёте этого хода"
            where = tr.province_notes(self.ruleset, self.state, trade, seat, short=True)
            return {"id": tid, "status": trade["status"],
                    "text": tr.fit_reply([head + (f"; {where}" if where else ""), head + "; провинции — list_trades"])}
        self.journal.append("trade_accepted", seat=seat, settle=trade["settle"], **base)
        if trade["status"] == tr.DONE:
            self._journal_settled(trade, "accept", base, [c["id"] for c in self._copies_of(trade)])
            for x, y in ((a, b), (b, a)):
                self._notice([x], f"Обмен {tid} с {self._nick(y)} исполнен: {tr.settled_text(self.ruleset, trade, x)}"
                                  f"{self._copies_note(trade, x)}.", "trade_settled")
            return {"id": tid, "status": trade["status"],
                    "text": f"исполнен: {tr.settled_text(self.ruleset, trade, seat)}{self._copies_note(trade, seat)}"}
        lack = op.get("lack") or []
        self.journal.append("trade_failed", reason=trade["failed_reason"], short_side=a, lack=lack, **base)
        self._notice([a], f"Обмен {tid} с {self._nick(b)} сорван: у тебя не хватило обещанного — "
                          f"{tr.lack_text(self.ruleset, lack)}.", "trade_failed")
        self._notice([b], f"Обмен {tid} с {self._nick(a)} не исполним: у предложившего больше нет обещанного.",
                     "trade_failed")
        return {"id": tid, "status": trade["status"], "text": "не исполним: у предложившего больше нет обещанного"}

    def _copies_of(self, trade: dict[str, Any]) -> list[dict[str, Any]]:
        """The copies a trade gave its recipient (docs/TRADE.md 2.3)."""
        holder = self.state.players[trade["b"]]
        return [c for c in holder.intel_copies if c.get("trade") == trade["id"]]

    def _copies_note(self, trade: dict[str, Any], seat: str) -> str:
        copies = self._copies_of(trade)
        if not copies:
            return ""
        if seat == trade["b"]:
            ids = ", ".join(c["id"] for c in copies)
            return f"; копии донесений: {ids} — get_intel(report_id=\"{copies[0]['id']}\")"
        return f"; покупатель получил копии {', '.join(c['source'] for c in copies)} (оригиналы у тебя)"

    def _journal_settled(self, trade: dict[str, Any], at: str, base: dict[str, Any], copies: list[str]) -> None:
        """trade_settled, then promise_made for each promised part and intel_copied for each copy (journal only,
        private to the two seats until game_over)."""
        a, b = trade["a"], trade["b"]
        self.journal.append("trade_settled", give=trade["give"], want=trade["want"], at=at, **base)
        later = trade.get("later") or {}
        for side, giver, taker in (("give", a, b), ("want", b, a)):
            if later.get(side):
                self.journal.append("promise_made", seat=giver, to=taker, goods=later[side],
                                    by_turn=later["by_turn"], **base)
        for c in self.state.players[b].intel_copies:
            if c["id"] in copies:
                report = c.get("report") or {}
                self.journal.append("intel_copied", copy_id=c["id"], source=c["source"], mission=report.get("mission"),
                                    target=report.get("target"), report_turn=report.get("turn"), to=b,
                                    **{"from": a}, **base)

    def trades(self, seat: str) -> dict[str, Any]:
        """The seat's own offers, trades of the last turns and promises still due; nobody else's (docs/TRADE.md
        16а). In every phase."""
        self._require_trade()
        return {"trades": [tr.trade_json(t, seat, lambda x: self.state.players[x].nick)
                           for t in tr.mine(self.state, seat)],
                "text": tr.list_text(self.state, self.ruleset, seat, self._nick)}

    def _trade_results(self, turn: int, report: TurnReport, state: GameState) -> None:
        """Journal and announce what this resolution did to trades (`state`: the state after it): offers that lapsed
        (to their proposers), pending trades settled or failed at step 4b and the provinces they passed (private to
        the two seats)."""
        for e in report.events:
            kind = e["kind"]
            if kind == "trade_lapsed":
                self.journal.append("trade_lapsed", trade_id=e["trade"], seats=e["seats"], turn=turn)
                other = next((x for x in e["seats"] if x != e.get("sender")), None)
                self._notice([e["sender"]], f"Предложение обмена {e['trade']} для {self._nick(other)} не принято до "
                                            f"конца хода {turn} и сгорело.", "trade_lapsed", turn=turn,
                             phase="resolution")
                continue
            if kind not in (tr.TRANSFERRED, "trade_failed", "trade_settled") or (
                    kind != tr.TRANSFERRED and e.get("at") != "resolution"):
                continue
            base = {"trade_id": e["trade"], "seats": e["seats"], "turn": turn, "phase": "resolution"}
            if kind == tr.TRANSFERRED:
                self.journal.append(tr.TRANSFERRED, province=e["province"], status=e["status"],
                                    units_withdrawn=e.get("units_withdrawn") or {}, to=e["to"],
                                    **{"from": e["from"]}, **base)
            elif kind == "trade_failed":
                self.journal.append("trade_failed", reason=e["reason"], short_side=e["short_side"],
                                    province=e.get("lost_province"), at="resolution", **base)
                self._notice(list(e["seats"]), f"Обмен {e['trade']} сорван в расчёте хода {turn}: "
                                               f"{e.get('lost_province')} — {tr.FAILED_TEXT[e['reason']]}; залог "
                                               "возвращён.", "trade_failed", turn=turn, phase="resolution")
            else:
                b = next(x for x in e["seats"] if x != e["a"])
                trade = {**(tr.trade_by_id(state, e["trade"]) or {}), "a": e["a"], "b": b, "give": e["give"],
                         "want": e["want"], "id": e["trade"]}
                self._journal_settled_in(state, trade, base, e.get("copies") or [])
                for x, y in ((e["a"], b), (b, e["a"])):
                    self._notice([x], f"Обмен {e['trade']} с {self._nick(y)} исполнен в расчёте хода {turn}: "
                                      f"{tr.settled_text(self.ruleset, trade, x)}.", "trade_settled", turn=turn,
                                 phase="resolution")

    def _journal_settled_in(self, state: GameState, trade: dict[str, Any], base: dict[str, Any],
                            copies: list[str]) -> None:
        saved, self.state = self.state, state
        try:
            self._journal_settled(trade, "resolution", base, copies)
        finally:
            self.state = saved

    def _journal_orders_summary(self, turn: int, books: dict[str, SeatOrders]) -> None:
        """Core 0.8.0 (docs/SPY_V2.md 11, «донесение → решение»): each seat's resolved moves (not false entries) and
        fortifies with the phase they were given in, the owner of a move's target at the start of the resolution
        and its pact mark; journal only, private to the seat until game_over (journal.view_events)."""
        for seat, book in sorted(books.items()):
            moves = [{"to": o.dst, "owner": self.state.provinces[o.dst].owner, "p2": o.phase_added == P2,
                      **({"pact": "break"} if o.pact == "break" else {})}
                     for o in book.orders if o.kind == "move" and not o.feint and o.dst in self.state.provinces]
            fortify = [{"province": o.province, "p2": o.phase_added == P2} for o in book.orders if o.kind == "fortify"]
            if moves or fortify:
                self.journal.append("orders_summary", seat=seat, turn=turn, moves=moves, fortify=fortify)

    def _pact_results(self, turn: int, report: TurnReport) -> None:
        """Journal and announce the pact changes of a resolution (breaks, expiries, endings); public."""
        for e in report.events:
            kind = e["kind"]
            if kind not in ("pact_broken", "pact_expired", "pact_ended", "pact_lapsed"):
                continue
            seats = e["seats"]
            fields = {"pact_id": e["pact"], "seats": seats, "turn": turn}
            if kind == "pact_broken":
                fields["broken_by"] = e["broken_by"]
            if kind == "pact_ended":
                fields.update(reason=e.get("reason"), seat=e.get("seat"))
            self.journal.append(kind, **fields)
            pair = " и ".join(self._nick(s) for s in seats)
            if kind == "pact_broken":
                who = ", ".join(self._nick(s) for s in e["broken_by"])
                self._notice("all", f"Пакт нарушен: {pair}. Нарушил: {who} (ход {turn}).", kind, turn=turn,
                             phase="resolution")
            elif kind == "pact_expired":
                self._notice("all", f"Срок пакта истёк: {pair} (ход {turn}).", kind, turn=turn, phase="resolution")
            elif kind == "pact_ended":
                why = "выход из пакта вступил в силу" if e.get("reason") == "left" else "сторона выбыла"
                self._notice("all", f"Пакт окончен: {pair} — {why} (ход {turn}).", kind, turn=turn,
                             phase="resolution")
            else:
                lapsed = dip.pact_by_id(self.state, e["pact"])
                what = (f"Предложение продлить пакт {lapsed['renews']} ({e['pact']})" if lapsed and lapsed.get("renews")
                        else f"Предложение пакта {e['pact']}")
                self._notice(seats, f"{what} не принято до конца хода {turn} и сгорело.", kind, turn=turn,
                             phase="resolution")

    # --- setup phase (TECH_AND_SETUP.md section 2) ------------------------------------------------------

    @property
    def setup(self) -> dict[str, Any] | None:
        return self.config.get("setup")

    def _require_setup(self, seat: str) -> None:
        if seat not in self.state.players:
            raise GameError("UNKNOWN_ID", f"нет места {seat!r}")
        if not self.setup:
            raise GameError("PHASE_LOCKED", "в этой партии нет фазы старта: фракции назначены сценарием")
        if self.phase != SETUP:
            raise GameError("PHASE_LOCKED", "фаза старта закрыта: выбор сделан, игра идёт" if not self.game_over
                            else "игра окончена")

    def setup_hash(self, seat: str) -> str:
        """confirm_hash of the setup phase: the seat's current choice (ready may also be pressed without it)."""
        return sha256_json(self.setup_choices.get(seat) or {})

    def _setup_choice_changed(self, seat: str, kind: str, value: list[str]) -> None:
        self.setup_choices.setdefault(seat, {})[kind] = value
        self.journal.append("setup_choice", seat=seat, kind=kind, **{kind: value}, turn=self.state.turn,
                            phase=SETUP)
        if seat in self.ready_seats:
            self.ready_seats.discard(seat)
            self.journal.append("ready_cleared", seat=seat, turn=self.state.turn, phase=SETUP)
        self._save()

    def _setup_offer(self, seat: str) -> list[str]:
        setup = self.setup or {}
        if setup.get("artifacts") == "draft":
            return list(self.setup_offers.get(seat) or [])
        if setup.get("artifacts") == "points":
            return artifact_pool(self.ruleset, setup)
        return []

    def choose_faction(self, seat: str, factions: Any) -> dict[str, Any]:
        """Faction preferences, best first (one id or name counts as a list of one). With unique factions a clash
        is settled by the seed; the loser gets its next preference."""
        self._require_setup(seat)
        try:
            prefs = check_faction_prefs(self.ruleset, _id_list(factions))
        except SetupChoiceError as exc:
            example = f'choose_faction(["{self.ruleset.scenario.factions[0].split(":")[-1]}"])'
            raise GameError(exc.code, f"{exc.message}. Пример: {example}") from None
        self._setup_choice_changed(seat, "factions", prefs)
        names = ", ".join(self.ruleset.factions[f].name for f in prefs)
        out: dict[str, Any] = {"factions": prefs, "confirm_hash": self.setup_hash(seat),
                               "text": f"предпочтения фракций: {names}; при совпадении с соперником спор решает жребий"}
        unfit = unfit_artifacts(self.ruleset, (self.setup_choices.get(seat) or {}).get("artifacts") or [], prefs[0])
        if unfit:
            out["warning"] = (f"выбранный артефакт {', '.join(self.ruleset.artifacts[a].name for a in unfit)} — только "
                              f"для фракций: {self._artifact_factions(unfit[0])}; выбери другой (choose_artifacts)")
            out["text"] += f". ВНИМАНИЕ: {out['warning']}"
        return out

    def choose_artifacts(self, seat: str, artifacts: Any) -> dict[str, Any]:
        """Artifacts for the start: draft — one of the seat's offer; points — any within the budget."""
        self._require_setup(seat)
        setup = self.setup or {}
        offer = self._setup_offer(seat)
        try:
            ids = check_artifacts(self.ruleset, setup, offer, _id_list(artifacts))
        except SetupChoiceError as exc:
            example = f'choose_artifacts(["{offer[0].split(":")[-1]}"])' if offer else "choose_artifacts([])"
            raise GameError(exc.code, f"{exc.message}. Пример: {example}") from None
        self._setup_choice_changed(seat, "artifacts", ids)
        names = ", ".join(self.ruleset.artifacts[a].name for a in ids) or "без артефактов"
        out: dict[str, Any] = {"artifacts": ids, "confirm_hash": self.setup_hash(seat), "text": f"артефакты: {names}"}
        # An artifact the seat's faction may not hold is dropped at close: say so now, not silently then.
        prefs = (self.setup_choices.get(seat) or {}).get("factions")
        faction = prefs[0] if prefs else self._default_faction(seat)
        unfit = unfit_artifacts(self.ruleset, ids, faction)
        if unfit:
            later = ("заменён артефактом по умолчанию" if setup.get("artifacts") == "draft"
                     else "отброшен, освободившийся бюджет заполнится по умолчанию")
            out["warning"] = (f"{', '.join(self.ruleset.artifacts[a].name for a in unfit)} — только для фракций: "
                              f"{self._artifact_factions(unfit[0])}; при фракции «{self.ruleset.factions[faction].name}» "
                              f"он будет {later}")
            out["text"] += f". ВНИМАНИЕ: {out['warning']}"
        return out

    def _default_faction(self, seat: str) -> str:
        spec = next((s for s in self.config["seats"] if s["seat"] == seat), {})
        return ((self.ruleset.resolve("faction", spec["faction"]) if spec.get("faction") else None)
                or self.ruleset.scenario.factions[0])

    def _artifact_factions(self, artifact: str) -> str:
        return ", ".join(self.ruleset.factions[f].name if f in self.ruleset.factions else f.split(":")[-1]
                         for f in self.ruleset.artifacts[artifact].factions)

    def get_setup(self, seat: str) -> dict[str, Any]:
        """What the seat sees of the setup phase: all factions (full descriptions), its own artifact offer (draft)
        or the pool and budget (points), its current choice; after the phase, its result and every seat's faction
        (public). Never another seat's choice or artifacts."""
        setup = self.setup
        if not setup:
            return {"enabled": False, "phase": self.phase,
                    "text": "в этой партии нет фазы старта: фракции назначены сценарием"}
        from dah.engine.setup import artifact_choices, faction_choices

        open_ = self.phase == SETUP
        offer = self._setup_offer(seat)
        choice = self.setup_choices.get(seat) or {}
        out: dict[str, Any] = {
            "enabled": True, "phase": self.phase, "open": open_,
            "deadline": iso_time(self.deadline) if open_ and self.deadline is not None else None,
            "seconds_left": self.seconds_left() if open_ else None,
            "ready": self.ready_text() if open_ else None, "you_ready": open_ and seat in self.ready_seats,
            "unique_factions": bool(setup.get("unique_factions", True)), "artifacts_mode": setup.get("artifacts"),
            "artifact_offer": setup.get("artifact_offer"), "artifact_points": setup.get("artifact_points"),
            # full descriptions only while the choice is open (after it they would just repeat the rules)
            "factions": faction_choices(self.ruleset) if open_ else [],
            "offer": artifact_choices(self.ruleset, offer) if open_ else [],
            "your_choice": {"factions": choice.get("factions"), "artifacts": choice.get("artifacts")},
            "confirm_hash": self.setup_hash(seat),
        }
        if sc_.on(self.ruleset, "objectives") and seat in self.state.players:
            # the seat's secret objectives are dealt when the setup phase opens: a faction can be picked for them
            out["objectives"] = sc_.objectives_view(self.state, self.ruleset, seat)
        if not open_ and self.setup_result:
            mine = self.setup_result.get(seat) or {}
            out["result"] = {k: mine.get(k) for k in ("faction", "prefs", "artifacts", "auto_faction",
                                                      "auto_artifacts", "faction_conflict", "dropped")}
            out["players"] = [{"seat": s, "nick": p.nick, "faction": p.faction}
                              for s, p in self.state.players.items()]
        out["text"] = self._setup_text(out)
        return out

    def _setup_text(self, view: dict[str, Any]) -> str:
        rs = self.ruleset
        if not view["open"]:
            res = view.get("result") or {}
            lines = ["ФАЗА СТАРТА окончена."]
            if res:
                arts = ", ".join(rs.artifacts[a].name for a in res.get("artifacts") or []) or "нет"
                lines.append(f"Твоя фракция: {rs.factions[res['faction']].name}; артефакты: {arts}")
            lines += [f"{p['nick']}: {rs.factions[p['faction']].name}" for p in view.get("players") or []]
            return "\n".join(lines)
        mode = view["artifacts_mode"]
        objectives = [f"Твоя тайная цель «{o['name']}» +{o['points'] * rs.weight('objectives')}: {o['text']}"
                      for o in view.get("objectives") or []]
        lines = [f"ФАЗА СТАРТА: ещё {view['seconds_left']} с, готово {view['ready']}. Выбери фракцию "
                 "(choose_faction([...] — список по предпочтению)"
                 + (", артефакт (choose_artifacts([...]))" if mode != "none" else "") + " и нажми ready.",
                 "Не выбрал — первая свободная фракция списка" + (" и первый предложенный артефакт" if mode == "draft"
                                                                   else "") + "."]
        lines.append("Фракции" + (" (у каждого своя, спор — жребий):" if view["unique_factions"] else ":"))
        for f in view["factions"]:
            traits = "; ".join(f["bonuses"] + [f"− {x}" for x in f["penalties"]])
            lines.append(f"- {f['id'].split(':')[-1]} «{f['name']}»: "
                         + (traits or "нейтральная: без бонусов и минусов"))
        if mode == "draft":
            lines.append("Тебе предложены артефакты (возьми один; в скобках сила):")
        elif mode == "points":
            lines.append(f"Артефакты (бюджет {view['artifact_points']} очк., в скобках стоимость в очках):")
        for a in view["offer"]:
            num = a["power"] if mode == "draft" else a["cost"]
            only = f" [только {self._artifact_factions(a['id'])}]" if a.get("factions") else ""
            lines.append(f"- {a['id'].split(':')[-1]} «{a['name']}» ({num}){only}: {'; '.join(a['effects'])}")
        choice = view["your_choice"]

        def named(table: Any, ids: list[str] | None) -> str:
            return ", ".join(f"{i.split(':')[-1]} «{table[i].name if i in table else i}»" for i in ids or []) or "—"

        lines.append(f"Твой выбор: фракции {named(rs.factions, choice['factions'])}, "
                     f"артефакты {named(rs.artifacts, choice['artifacts'])}")
        return "\n".join(lines + objectives)

    def _close_setup(self, reason: str) -> None:
        """Settle factions and artifacts, rebuild turn 1 from them and open П1 (journal: setup_closed)."""
        setup = self.setup or {}
        result = resolve_setup(self.ruleset, setup, self.config["seed"], self.config["seats"], self.setup_choices,
                               self.setup_offers)
        self.setup_result = result
        state = new_game_state(self.ruleset, _seat_specs(self.config, result), self.config["seed"],
                               **_salt(self.config))
        write_json(self.gdir.private / "setup.json", {"offers": self.setup_offers, "choices": self.setup_choices,
                                                       "result": result})
        self.gdir.save_turn("states", 1, state.to_dict())
        self.journal.append("setup_closed", turn=1, reason=reason,
                            factions={s: r["faction"] for s, r in result.items()},
                            artifacts={s: r["artifacts"] for s, r in result.items()},  # hidden until game_over
                            results=result)
        self._journal_free_shades(state)
        self.state = state
        self.books = {}
        self._open_phase()

    # --- techs and content -----------------------------------------------------------------------------

    def list_techs(self, seat: str, full: bool = False) -> dict[str, Any]:
        """The seat's own techs with its research income per turn, plus Russian `text`. By default short (researched,
        active, available now; ~10x smaller); `full` gives the whole tree with requirements (techs.techs_view)."""
        res = self.ruleset.research_resource
        income = None
        if res is not None and seat in self.state.players:
            key = stat_key(self.state, self.ruleset, seat)
            income = (income_of(self.state, self.ruleset, seat, key)[res]
                      - upkeep_of(self.state, self.ruleset, seat, key)[res])
        view = techs_view(self.state, self.ruleset, seat, income)
        ordered = pending_research(self._book(seat)) if seat in self.state.players else None
        if ordered is not None and ordered["tech"] != (view["active"] or {}).get("tech"):
            view["ordered"] = ordered       # the research order in the book; active only after resolution (LIVE-5)
        science = (sc_.science_view(self.state, self.ruleset, seat)
                   if sc_.on(self.ruleset, "science") and seat in self.state.players and self.ruleset.techs else None)
        if not full:
            short = short_techs_view(view)
            short["text"] = techs_text_short(short, self.ruleset)
            if science is not None:
                short["science"] = science
                short["text"] += "\n" + sc_.science_text(science)
            return short
        view["full"] = True
        view["text"] = techs_text(view, self.ruleset)
        if science is not None:
            view["science"] = science
            view["text"] += "\n" + sc_.science_text(science)
        return view

    def describe(self, seat: str, ref: Any) -> dict[str, Any]:
        """Any content of this game by id (full or short) or name: unit, building, tech, artifact, faction,
        branch, resource, province type. Content is public; nothing about other seats."""
        hits = find(self.ruleset, ref)
        if not hits:
            raise GameError("NOT_FOUND", f"ничего не найдено по {str(ref)[:40]!r}: нужен ID (assault, core:assault) "
                                         "или название. Пример: describe(\"assault\")")
        kind, fid = hits[0]
        out = describe(self.ruleset, kind, fid)
        if len(hits) > 1:
            out["also"] = [{"kind": k, "id": i} for k, i in hits[1:]]
            out["text"] += "\nТакже: " + ", ".join(f"{KIND_LABELS[k]} {i}" for k, i in hits[1:])
        return out

    # --- host telemetry and per-call journaling --------------------------------------------------------

    def telemetry(self, seat: str, kind: Any, detail: Any) -> None:
        if kind not in TELEMETRY_KINDS:
            raise GameError("INVALID", f"kind должен быть одним из {', '.join(TELEMETRY_KINDS)}")
        if detail is not None and (not isinstance(detail, dict)
                                   or len(canonical_json(detail)) > MAX_TELEMETRY_DETAIL_CHARS):
            raise GameError("INVALID", f"detail — JSON-объект не длиннее {MAX_TELEMETRY_DETAIL_CHARS} символов")
        self.journal.append("telemetry", seat=seat, kind=kind, detail=detail, turn=self.state.turn,
                            phase=self.phase)

    def spend(self, seat: str, spent_usd: Any, source: Any, tokens: Any = None, *, turn: Any = None,
              phase: Any = None, host_allowed: bool = False) -> None:
        """Spend so far. `turn`/`phase` name the phase the figure is for (the one that just closed); without them
        the figure is a baseline taken before any phase closed. The journal keeps both sides: `for_turn`/
        `for_phase` (None for a baseline) and `at_turn`/`at_phase`, the phase current when it was received;
        `turn`/`phase` repeat the "for" pair, or the "at" pair for a baseline (older readers).
        After game_over `phase` may be `after_game_over` (`turn` optional, the last turn by default): the spend of
        the session while it wraps up, kept apart in the report.
        `source: host` is accepted only from the host (`host_allowed`)."""
        if isinstance(spent_usd, bool) or not isinstance(spent_usd, (int, float)) or spent_usd < 0:
            raise GameError("INVALID", "spent_usd — неотрицательное число")
        if source not in SPEND_SOURCES:
            raise GameError("INVALID", f"source должен быть одним из {', '.join(SPEND_SOURCES)}")
        if source == "host" and not host_allowed:
            raise GameError("FORBIDDEN", "source host — только с админ-ключом")
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
            raise GameError("INVALID", "tokens — неотрицательное целое")
        at_turn, at_phase = self.state.turn, self.phase
        if turn is None and phase is None:
            for_turn = for_phase = None
        elif phase == AFTER_GAME_OVER:
            if not self.game_over:
                raise GameError("INVALID", f"phase {AFTER_GAME_OVER} — только после конца партии (game_over)")
            if turn is not None and (isinstance(turn, bool) or not isinstance(turn, int)
                                     or not 1 <= turn <= self.state.turn):
                raise GameError("INVALID", f"turn — номер хода 1..{self.state.turn} или не задан")
            for_turn, for_phase = turn if turn is not None else self.state.turn, AFTER_GAME_OVER
        elif (isinstance(turn, bool) or not isinstance(turn, int) or not 1 <= turn <= self.state.turn
              or phase not in SPEND_PHASES or (phase == SETUP and turn != 1)):
            # the setup phase is before turn 1 and is reported as turn 1 (the waker's key of a closed setup phase)
            raise GameError("INVALID", f"turn — номер хода 1..{self.state.turn}, phase — одна из "
                                       f"{', '.join(SPEND_PHASES)} ({SETUP} — с turn=1)")
        else:
            for_turn, for_phase = turn, phase
        fields: dict[str, Any] = {"seat": seat, "spent_usd": float(spent_usd), "source": source,
                                  "turn": for_turn if for_turn is not None else at_turn,
                                  "phase": for_phase if for_phase is not None else at_phase,
                                  "for_turn": for_turn, "for_phase": for_phase,
                                  "at_turn": at_turn, "at_phase": at_phase}
        if tokens is not None:
            fields["tokens"] = tokens
        self.journal.append("spend_report", **fields)

    def set_dreamteam_session(self, seat: str, session_id: Any, source: str = "waker") -> dict[str, Any]:
        """Record the seat's DreamTeam session id (config.json + a `seat_info` journal update)."""
        config, changed = link_dreamteam_session(self.gdir, self.journal, seat, session_id, source)
        self.config = config
        self.seats = {s["seat"]: s for s in config["seats"]}
        return {"dreamteam_session_id": session_id, "changed": changed}

    def note_first_call(self, seat: str) -> None:
        if seat not in self.first_called and self.phase in PLAYABLE_PHASES:
            self.first_called.add(seat)
            self.journal.append("first_call_in_phase", seat=seat, turn=self.state.turn, phase=self.phase)
            self._save()

    def record_call(self, seat: str, route: str, req_chars: int, resp_chars: int, ms: float, status: int, *,
                    turn: int, phase: str, host: bool = False) -> None:
        """Journal one API call under the phase current when it arrived; `host` marks dah-waker traffic."""
        fields: dict[str, Any] = {"seat": seat, "route": route, "req_chars": req_chars, "resp_chars": resp_chars,
                                  "ms": round(ms, 1), "status": status, "turn": turn, "phase": phase}
        if host:
            fields["host"] = True
        self.journal.append("call", **fields)

    def admin_status(self) -> dict[str, Any]:
        seats = {}
        for seat, p in self.state.players.items():
            book = self._book(seat)
            seats[seat] = {**self.seats[seat], "alive": p.alive, "resources": p.resources,
                           "orders": [o.to_dict() for o in book.orders], "orders_hash": orders_hash(book),
                           "cp_left": cp_left(self.state, seat, book), "ready": seat in self.ready_seats,
                           "faction": p.faction, "artifacts": list(p.artifacts), "techs": list(p.techs),
                           "researching": p.researching, "research": dict(p.research)}
        pick = self.ruleset_pick
        return {"game_id": self.config["game_id"], "config": self.config, "ruleset_hash": self.ruleset.hash,
                "ruleset_packs": pick.pack_dirs if pick else None,
                "ruleset_warning": (None if pick is None or pick.matches else
                                    "хэш правил паков не совпадает с журналом: партия идёт по изменённым правилам"),
                "turn": self.state.turn, "phase": self.phase, "phase_seq": self.phase_seq,
                "deadline": iso_time(self.deadline) if self.deadline is not None else None,
                "seconds_left": self.seconds_left(), "ready": self.ready_text(), "seats": seats,
                "scores": scores(self.state, self.ruleset), "result": self.state.result,
                "messages": sum(1 for m in self.messages if not m.get("system")), "pacts": self.state.pacts,
                "diplomacy_log": self.diplomacy_log, "state_hash": state_hash(self.state),
                "setup": ({"offers": self.setup_offers, "choices": self.setup_choices, "result": self.setup_result}
                          if self.config.get("setup") else None)}


# ---------------------------------------------------------------- DreamTeam session link

def link_dreamteam_session(gdir: GameDir, journal: Journal, seat: str, session_id: Any,
                           source: str) -> tuple[dict[str, Any], bool]:
    """Write the seat's `dreamteam_session_id` into config.json (re-read from disk, so a concurrent change of
    another seat survives) and journal a `seat_info` update (`update: true`, `source`: cli | waker). Unchanged id:
    nothing is written. Returns (config, changed)."""
    if not isinstance(session_id, str) or not DT_SESSION_RE.match(session_id):
        raise GameError("INVALID", "dreamteam_session_id — ID сессии DreamTeam (буквы, цифры, . _ : -, до 128 "
                                   "символов). Пример: {\"dreamteam_session_id\": \"5f0c2a\"}")
    config = gdir.load_config()
    entry = next((s for s in config["seats"] if s["seat"] == seat), None)
    if entry is None:
        raise GameError("NOT_FOUND", f"нет места {seat!r}")
    if entry.get("dreamteam_session_id") == session_id:
        return config, False
    previous = entry.get("dreamteam_session_id")
    entry["dreamteam_session_id"] = session_id
    write_json(gdir.config_path, config)
    journal.append("seat_info", seat=seat, kind=entry["kind"], declared=entry.get("declared") or {},
                   dreamteam_session_id=session_id, update=True, previous=previous, source=source)
    return config, True


def set_dreamteam_session(gdir: GameDir, seat: str, session_id: str) -> bool:
    """`dah game set-session`: record the link from the command line. True if it changed."""
    return link_dreamteam_session(gdir, Journal(gdir.journal_path), seat, session_id, "cli")[1]


# ---------------------------------------------------------------- replay

@dataclass(frozen=True)
class ReplayTurn:
    turn: int
    expected: str
    actual: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


@dataclass(frozen=True)
class Replay:
    """Outcome of `replay()`: ruleset hash, per-turn state hashes and the final result, journal vs re-run."""
    ruleset_expected: str | None
    ruleset_actual: str
    turns: list[ReplayTurn]
    result_expected: dict[str, Any] | None   # {winners, scores} of the journal's game_over, if any
    result_actual: dict[str, Any] | None
    ruleset_legacy: str | None = None        # the same rules hashed as before phase limits left the hash
    pack_dirs: list[str] | None = None       # packs replayed with instead of the config's (override or legacy)
    recons: list[ReplayTurn] = field(default_factory=list)   # espionage: the recon step of every resolved turn

    @property
    def ruleset_ok(self) -> bool:
        return self.ruleset_expected in (self.ruleset_actual, self.ruleset_legacy)

    @property
    def result_ok(self) -> bool:
        return self.result_expected == self.result_actual

    @property
    def ok(self) -> bool:
        return self.ruleset_ok and self.result_ok and all(t.ok for t in self.turns) and all(t.ok for t in self.recons)


def replay(gdir: GameDir, pack_dirs: list[str] | None = None) -> Replay:
    """Rebuild the ruleset, re-run every resolved turn from the initial state and saved orders, compare the
    ruleset hash, every turn's state hash and the final result with the journal. The ruleset: `pack_dirs` if given,
    else `pick_ruleset` (the config's packs, or a frozen earlier version whose hash is the journaled one)."""
    config = gdir.load_config()
    events = read_events(gdir.journal_path)
    created = next((e for e in events if e["type"] == "game_created"), {})
    pick = pick_ruleset(config, created.get("ruleset_hash"), pack_dirs)
    rs = pick.ruleset
    over = next((e for e in events if e["type"] == "game_over"), None)
    setup = next((e for e in events if e["type"] == "setup_closed"), None)
    state = new_game_state(rs, _seat_specs(config, setup["results"] if setup else None), config["seed"],
                           **_salt(config))
    expected = {e["turn"]: e["state_hash"] for e in events if e["type"] == "turn_resolved"}
    recon_hashes = {e["turn"]: e["state_hash"] for e in events if e["type"] == "recon" and "state_hash" in e}
    turns: list[ReplayTurn] = []
    recons: list[ReplayTurn] = []
    for turn in sorted(expected):
        raw = gdir.load_turn("orders", turn)
        if raw is None:
            raise FileNotFoundError(f"no saved orders for turn {turn}")
        ops = gdir.load_turn("diplomacy", turn) or []
        if rs.system("espionage"):
            dip.apply_ops(state, [op for op in ops if op.get("phase") != P2])
            books_d = gdir.load_turn("books_d", turn)
            if books_d is None and turn in recon_hashes:
                raise FileNotFoundError(f"no saved actions books for turn {turn}")
            # a `recon` event without state_hash (a game played without espionage, replayed with packs that have it
            # on): an empty recon step (SPY_AND_CONQUEST.md 9); the ruleset hash tells the mismatch
            state, _ = esp.recon(state, {s: SeatOrders.from_dict(b) for s, b in (books_d or {}).items()}, rs,
                                 config["seed"])
            if turn in recon_hashes:
                recons.append(ReplayTurn(turn, recon_hashes[turn], state_hash(state)))
            dip.apply_ops(state, [op for op in ops if op.get("phase") == P2])
        else:
            dip.apply_ops(state, ops)
        state, _ = resolve(state, {s: SeatOrders.from_dict(b) for s, b in raw.items()}, rs, config["seed"], ops,
                           salt=secret_salt(config))
        turns.append(ReplayTurn(turn, expected[turn], state_hash(state)))
    result_expected = {"winners": over["winners"], "scores": over["scores"]} if over else None
    result_actual = ({"winners": state.result["winners"], "scores": state.result["scores"]}
                     if state.result else None)
    return Replay(created.get("ruleset_hash"), rs.hash, turns, result_expected, result_actual,
                  legacy_ruleset_hash(rs, config), pick.pack_dirs, recons)


def legacy_ruleset_hash(rs: Ruleset, config: dict[str, Any]) -> str:
    """The ruleset hash as games before diplomacy journaled it: the scenario's phase limits were hashed too (a game
    with the defaults of its time has them in its config). Kept so `dah replay` of such a game still matches."""
    data = rs.to_dict()
    data["scenario"]["phase_limits"] = dict(config["phase_limits"])
    return sha256_json(data)
