"""Bot base class: the phase loop over the HTTP API and the hooks concrete bots override."""

from __future__ import annotations

import hashlib
import logging
import random
import threading
from typing import Any, Callable, Protocol

import httpx

from dah.bots.choices import faction_prefs, pick_artifacts, pick_research
from dah.bots.client import ApiClient, ApiError
from dah.bots.view import StateView, parse_state, short_id

log = logging.getLogger(__name__)

NEGOTIATION_PHASES = ("negotiation_1", "negotiation_2")
SETUP_PHASE = "setup"  # stage 2: before turn 1 (TECH_AND_SETUP.md section 2)
PLAYABLE_PHASES = (SETUP_PHASE, "negotiation_1", "actions", "negotiation_2")
WAIT_SECONDS = 10  # short long-poll so a stop request is noticed quickly
RETRY_SECONDS = 1.0

Order = dict[str, Any]
# (attacker units, defender units, province type, fortified, hasty) -> win probability; unit IDs are short.
Estimator = Callable[[dict[str, int], dict[str, int], str, bool, bool], float]


class Api(Protocol):
    def whoami(self) -> dict[str, Any]: ...
    def phase(self) -> dict[str, Any]: ...
    def wait_phase(self, after_seq: int, max_seconds: int) -> dict[str, Any]: ...
    def state(self) -> dict[str, Any]: ...
    def orders(self) -> dict[str, Any]: ...
    def submit_orders(self, orders: list[Order], mode: str) -> dict[str, Any]: ...
    def estimate(
        self, attacker: dict[str, int], defender: dict[str, int], province_type: str, fortified: bool, hasty: bool
    ) -> float: ...
    def send_message(self, to: list[str] | str, text: str) -> dict[str, Any]: ...
    def ready(self, confirm_hash: str | None) -> dict[str, Any]: ...


class StageTwoApi(Api, Protocol):
    """Setup phase and techs (used only when the game has them: a setup phase, `you.techs` in the state)."""

    def get_setup(self) -> dict[str, Any]: ...
    def choose_faction(self, prefs: list[str]) -> dict[str, Any]: ...
    def choose_artifacts(self, ids: list[str]) -> dict[str, Any]: ...
    def list_techs(self) -> dict[str, Any]: ...


class SpyApi(Api, Protocol):
    """Stage 3: the seat's own intel as data (`GET /v1/intel?format=json`; only games with espionage)."""

    def intel(self) -> dict[str, Any]: ...


class DiplomacyApi(Api, Protocol):
    """Gifts and pacts (only the bots that override `Bot.diplomacy` use these)."""

    def send_gift(self, to: str, resources: dict[str, int]) -> dict[str, Any]: ...
    def propose_pact(self, to: str, turns: int | None) -> dict[str, Any]: ...
    def respond_pact(self, pact_id: str, decision: str) -> dict[str, Any]: ...
    def leave_pact(self, pact_id: str) -> dict[str, Any]: ...


def seat_seed(game_id: str, seat: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{game_id}|{seat}|bot".encode()).digest()[:8], "big")


class Bot:
    """Decision hooks are pure functions of a StateView; `run` drives them against the server."""

    name = "base"
    # Stage-2 style (TECH_AND_SETUP.md section 4); empty: the bot makes no such choice (defaults, no research).
    faction_style: tuple[str, ...] = ()  # faction short ids, best first
    artifact_likes: tuple[str, ...] = ()  # artifact short ids valued above their power
    branch_priority: tuple[str, ...] = ()  # tech branch short ids, first researched first
    tech_first: tuple[str, ...] = ()  # techs taken before the branch order whenever available

    def __init__(self, seat: str, seed: int) -> None:
        self.seat = seat
        self.rng = random.Random(seed)
        self.renewals_asked: set[tuple[str, int]] = set()   # (pact id, turn) renewals proposed
        from dah.bots.spy import SpyMemory
        self.memory = SpyMemory()       # stage 3: what the bot keeps of its reports and of Shades caught at it

    # --- decision hooks (overridden by concrete bots) ---

    def message(self, view: StateView) -> str | None:
        """Public message to send in the current negotiation phase, if any."""
        return None

    def diplomacy(self, view: StateView, api: DiplomacyApi) -> None:
        """Gifts and pact moves in a negotiation phase (called before `message`); none by default."""

    def answer_pacts(self, view: StateView, api: DiplomacyApi) -> None:
        """Answer pact proposals made to this seat; called again when a proposal arrives after the bot pressed
        ready (the proposal clears it). Ignores them by default (the proposal then lapses with the phase)."""

    def renew_expiring_pacts(self, view: StateView, api: DiplomacyApi) -> list[str]:
        """In the last turn of an own pact with a fixed end, propose to renew it on the same terms (once per pact
        and turn; not when someone is leaving or a renewal is already proposed). Returns the partners asked."""
        asked = self.renewals_asked
        pending = {str(p.get("renews")) for p in view.pact_proposals if p.get("renews")}
        partners = []
        for pact in view.pacts:
            pid, until = pact.get("id"), pact.get("until_turn")
            if not pid or until != view.turn or pact.get("leaving") or str(pid) in pending:
                continue
            if (str(pid), view.turn) in asked:
                continue
            other = next((s for s in pact.get("seats", ()) if s not in view.me), None)
            if other is None:
                continue
            asked.add((str(pid), view.turn))
            try:
                api.propose_pact(other, int(pact.get("turns") or 1))
            except ApiError as exc:     # e.g. the partner proposed the renewal first: answer_pacts takes it
                log.info("bot %s: renewal of %s refused: %s", self.seat, pid, exc)
                continue
            partners.append(other)
        return partners

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        """Orders for the actions phase (submitted with mode replace_all)."""
        return []

    def p2_orders(self, view: StateView, current: list[Order], cp_left: int, estimate: Estimator) -> list[Order]:
        """Extra orders for negotiation_2 (submitted with mode append)."""
        return []

    def setup_choices(self, setup: dict[str, Any]) -> tuple[list[str] | None, list[str] | None]:
        """(faction preferences, artifacts) for the setup phase; None leaves that choice to the defaults."""
        if not self.faction_style:
            return None, None
        prefs = faction_prefs(setup, self.faction_style)
        return prefs or None, pick_artifacts(setup, prefs[0] if prefs else None, self.artifact_likes)

    def research_order(self, view: StateView, techs: dict[str, Any]) -> Order | None:
        """A research order when nothing is being researched (the active tech stays active between turns)."""
        if not self.branch_priority or view.researching is not None:
            return None
        from dah.bots.score import objective_branch, research_branches
        first, branch = self.tech_first, objective_branch(view)
        if branch is not None:      # stage 4: an open techs_branch objective puts its branch first
            in_branch = {short_id(str(t.get("id"))) for t in techs.get("techs") or []
                         if isinstance(t, dict) and short_id(str(t.get("branch", ""))) == branch}
            first = tuple(t for t in first if t in in_branch)
        tech = pick_research(techs, research_branches(view, self.branch_priority), first)
        return {"kind": "research", "tech": tech} if tech else None

    # --- server loop ---

    def run(self, api: Api, stop_event: threading.Event) -> None:
        me: set[str] | None = None
        status: dict[str, Any] | None = None
        acted_seq: int | None = None
        while not stop_event.is_set():
            try:
                if me is None:
                    who = api.whoami()
                    me = {str(who.get("seat", self.seat)), str(who.get("nick", self.seat))}
                if status is None:
                    status = api.phase()
                if status.get("game_over") or status.get("phase") == "game_over":
                    return
                seq = int(status.get("phase_seq", 0))
                if status.get("phase") in PLAYABLE_PHASES and not status.get("you_ready"):
                    if seq != acted_seq:
                        self.play_phase(api, str(status["phase"]), me)
                    else:  # the server cleared our ready mid-phase (e.g. a pact proposed to us): answer, ready again
                        self.ready_again(api, str(status["phase"]), me)
                acted_seq = seq
                status = api.wait_phase(after_seq=seq, max_seconds=WAIT_SECONDS)
            except (httpx.HTTPError, ApiError) as exc:
                log.warning("bot %s (%s): %s", self.seat, self.name, exc)
                status = None
                stop_event.wait(RETRY_SECONDS)
            except (KeyError, TypeError, ValueError):  # unexpected payload shape: keep the seat alive
                log.exception("bot %s (%s): cannot handle server response", self.seat, self.name)
                status = None
                stop_event.wait(RETRY_SECONDS)

    def play_setup(self, api: StageTwoApi) -> None:
        """Setup phase: faction preferences and artifacts by the bot's style, then ready (no hash needed)."""
        setup = api.get_setup()
        if setup.get("enabled") and setup.get("open"):
            prefs, artifacts = self.setup_choices(setup)
            try:
                if prefs:
                    api.choose_faction(prefs)
                if artifacts is not None:
                    api.choose_artifacts(artifacts)
            except ApiError as exc:  # a refused choice leaves the default: not worth failing the phase over
                log.info("bot %s: setup choice rejected: %s", self.seat, exc)
        api.ready(None)

    def ready_again(self, api: Api, phase: str, me: set[str]) -> None:
        """Press ready again in a phase already played (without replaying it: no new messages or orders), after
        answering pact proposals made since."""
        if phase == SETUP_PHASE:
            api.ready(None)
            return
        if phase in NEGOTIATION_PHASES and type(self).answer_pacts is not Bot.answer_pacts:
            try:
                self.answer_pacts(parse_state(api.state(), me), api)  # type: ignore[arg-type]
            except ApiError as exc:
                log.info("bot %s: pact answer rejected: %s", self.seat, exc)
        api.ready(api.orders().get("orders_hash", ""))

    def read_intel(self, api: Api, view: StateView, phase: str) -> StateView:
        """Espionage games: in negotiation_2 (and in actions when that was missed) absorb the seat's own reports of
        the last recon step into `self.memory`; the view gets the remembered armies of hidden provinces."""
        if view.espionage is None:
            return view
        from dah.bots.spy import apply_memory
        last = view.turn if phase == "negotiation_2" else view.turn - 1
        if phase in ("negotiation_2", "actions") and last >= 1 and self.memory.seen_turn != last:
            try:
                self.memory.absorb((api.intel() or {}).get("intel"))  # type: ignore[attr-defined]
            except (ApiError, AttributeError) as exc:
                log.info("bot %s: intel not read: %s", self.seat, exc)
        return apply_memory(view, self.memory)

    def play_phase(self, api: Api, phase: str, me: set[str]) -> None:
        if phase == SETUP_PHASE:
            self.play_setup(api)  # type: ignore[arg-type]
            return
        view = self.read_intel(api, parse_state(api.state(), me), phase)
        estimate = _qualified_estimator(api, view)
        confirm_hash: str | None = None
        if phase in NEGOTIATION_PHASES:
            if type(self).diplomacy is not Bot.diplomacy:
                try:
                    self.diplomacy(view, api)  # type: ignore[arg-type]
                except ApiError as exc:  # a refused gift or pact is not worth failing the phase over
                    log.info("bot %s: diplomacy rejected: %s", self.seat, exc)
                view = parse_state(api.state(), me)
            text = self.message(view)
            if text:
                try:
                    api.send_message("all", text)
                except ApiError as exc:  # message limits are not worth failing the phase over
                    log.info("bot %s: message rejected: %s", self.seat, exc)
        if phase == "actions":
            orders = self.action_orders(view, estimate)
            if self.branch_priority and view.research_enabled and view.researching is None:
                try:
                    research = self.research_order(view, api.list_techs())  # type: ignore[attr-defined]
                except ApiError as exc:
                    log.info("bot %s: list_techs failed: %s", self.seat, exc)
                    research = None
                if research is not None:
                    orders.append(research)
            confirm_hash = api.submit_orders(orders, "replace_all").get("orders_hash")
        elif phase == "negotiation_2":
            current = api.orders()
            confirm_hash = current.get("orders_hash")
            extra = self.p2_orders(view, list(current.get("orders", [])), int(current.get("cp_left", 0)), estimate)
            if extra:
                confirm_hash = api.submit_orders(extra, "append").get("orders_hash")
        if confirm_hash is None:
            confirm_hash = api.orders().get("orders_hash", "")
        api.ready(confirm_hash)


def _qualified_estimator(api: Api, view: StateView) -> Estimator:
    def estimate(attacker: dict[str, int], defender: dict[str, int], ptype: str, fortified: bool, hasty: bool) -> float:
        return api.estimate(
            {view.qualify(u): n for u, n in attacker.items()},
            {view.qualify(u): n for u, n in defender.items()},
            ptype,
            fortified,
            hasty,
        )

    return estimate
