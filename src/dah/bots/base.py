"""Bot base class: the phase loop over the HTTP API and the hooks concrete bots override."""

from __future__ import annotations

import hashlib
import logging
import random
import threading
from typing import Any, Callable, Protocol

import httpx

from dah.bots.client import ApiClient, ApiError
from dah.bots.view import StateView, parse_state

log = logging.getLogger(__name__)

NEGOTIATION_PHASES = ("negotiation_1", "negotiation_2")
PLAYABLE_PHASES = ("negotiation_1", "actions", "negotiation_2")
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
    def ready(self, confirm_hash: str) -> dict[str, Any]: ...


def seat_seed(game_id: str, seat: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{game_id}|{seat}|bot".encode()).digest()[:8], "big")


class Bot:
    """Decision hooks are pure functions of a StateView; `run` drives them against the server."""

    name = "base"

    def __init__(self, seat: str, seed: int) -> None:
        self.seat = seat
        self.rng = random.Random(seed)

    # --- decision hooks (overridden by concrete bots) ---

    def message(self, view: StateView) -> str | None:
        """Public message to send in the current negotiation phase, if any."""
        return None

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        """Orders for the actions phase (submitted with mode replace_all)."""
        return []

    def p2_orders(self, view: StateView, current: list[Order], cp_left: int, estimate: Estimator) -> list[Order]:
        """Extra orders for negotiation_2 (submitted with mode append)."""
        return []

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
                if status.get("phase") in PLAYABLE_PHASES and seq != acted_seq and not status.get("you_ready"):
                    self.play_phase(api, str(status["phase"]), me)
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

    def play_phase(self, api: Api, phase: str, me: set[str]) -> None:
        view = parse_state(api.state(), me)
        estimate = _qualified_estimator(api, view)
        confirm_hash: str | None = None
        if phase in NEGOTIATION_PHASES:
            text = self.message(view)
            if text:
                try:
                    api.send_message("all", text)
                except ApiError as exc:  # message limits are not worth failing the phase over
                    log.info("bot %s: message rejected: %s", self.seat, exc)
        if phase == "actions":
            confirm_hash = api.submit_orders(self.action_orders(view, estimate), "replace_all").get("orders_hash")
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
