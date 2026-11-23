"""The waker loop: follow the game's phases and wake a DreamTeam session when its seat has to act."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

from dah.bridge.client import GameApiError, GameClient
from dah.waker.dreamteam import DreamTeamClient, DtResponse
from dah.waker.text import wake_text

ACTIVE_PHASES = frozenset({"negotiation_1", "actions", "negotiation_2"})
POLL_SECONDS = 45
REMINDER_SECONDS = 60
RETRY_DELAYS = (10, 30, 60)
GAME_ERROR_DELAY = 5
# States of a session that a `continue` has to start again (DreamTeam http-api.md, "States").
STOPPED_STATES = frozenset({"stopped", "failed", "done", "budget", "created"})
SPEND_SOURCE = "participant_program"


def _stderr(message: str) -> None:
    print(f"dah-waker: {message}", file=sys.stderr, flush=True)


class Waker:
    def __init__(self, game: GameClient, dreamteam: DreamTeamClient, session_id: str, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = _stderr):
        self._game = game
        self._dt = dreamteam
        self._session_id = session_id
        self._clock = clock
        self._sleep = sleep
        self._log = log
        self.budget_exhausted = False
        self._seq: int | None = None
        self._phase_key: tuple[Any, Any] | None = None
        self._info: dict[str, Any] = {}
        self._deadline_at: float | None = None
        self._reminded = False

    # --- loop ---------------------------------------------------------------------------------------------

    def run(self) -> None:
        while not self.step():
            pass

    def step(self) -> bool:
        """One poll of the game; True once the game is over."""
        info = self._poll()
        if info is None:
            return False
        if info.get("game_over"):
            self._report_spend(self._phase_key)
            self._log("партия окончена")
            return True
        key = (info.get("turn"), info.get("phase"))
        self._observe(info)
        if key != self._phase_key:
            closed, self._phase_key = self._phase_key, key
            self._reminded = False
            self._report_spend(closed)
            if self._must_act():
                self._wake("phase_open")
                self._reminded = self._reminder_due()  # opened less than a minute before its deadline
        elif self._reminder_due():
            self._reminded = True
            self._wake("reminder")
        return False

    def _poll(self) -> dict[str, Any] | None:
        try:  # the first poll (no seq yet) returns at once
            return self._game.wait_phase(self._seq, self._wait_seconds())
        except GameApiError as exc:
            if exc.status in (401, 403):
                raise
            self._log(f"сервер игры: {exc.code} {exc.message}")
            self._sleep(GAME_ERROR_DELAY)
            return None

    def _observe(self, info: dict[str, Any]) -> None:
        self._info = info
        if isinstance(info.get("phase_seq"), int):
            self._seq = info["phase_seq"]
        seconds_left = info.get("seconds_left")
        self._deadline_at = self._clock() + seconds_left if isinstance(seconds_left, (int, float)) else None

    def _must_act(self) -> bool:
        info = self._info
        return bool(info.get("alive", True)) and info.get("phase") in ACTIVE_PHASES and not info.get("you_ready")

    def _until_reminder(self) -> float | None:
        if self._reminded or self._deadline_at is None or not self._must_act():
            return None
        return self._deadline_at - REMINDER_SECONDS - self._clock()

    def _reminder_due(self) -> bool:
        left = self._until_reminder()
        return left is not None and left <= 0 < (self._deadline_at or 0) - self._clock()

    def _wait_seconds(self) -> float:
        left = self._until_reminder()
        return POLL_SECONDS if left is None else max(1.0, min(POLL_SECONDS, left))

    # --- waking -------------------------------------------------------------------------------------------

    def _wake(self, reason: str) -> None:
        if self.budget_exhausted:
            return
        where = {"reason": reason, "turn": self._info.get("turn"), "phase": self._info.get("phase")}
        before = self._dt.get_session(self._session_id)
        resp, attempts = self._continue(wake_text(self._info, reminder=reason == "reminder"))
        if resp.ok:
            self._telemetry("continue_sent", {**where, "state": resp.session.get("state")})
            if before.ok:
                self._compare(before.session, resp.session, where)
        elif resp.code == "budget_exhausted":
            self._exhaust({**where, "via": "continue"})
        else:
            self._telemetry("wake_failed", {**where, "status": resp.status, "code": resp.code,
                                            "attempts": attempts})

    def _continue(self, text: str) -> tuple[DtResponse, int]:
        attempts = 1
        resp = self._dt.continue_session(self._session_id, text)
        for delay in RETRY_DELAYS:
            if not resp.retryable:
                break
            self._log(f"DreamTeam ответил {resp.status} {resp.code}, повтор через {delay} с")
            self._sleep(delay)
            attempts += 1
            resp = self._dt.continue_session(self._session_id, text)
        return resp, attempts

    def _compare(self, before: dict[str, Any], after: dict[str, Any], where: dict[str, Any]) -> None:
        detail = {**where, "state_before": before.get("state"), "state_after": after.get("state"),
                  "final_before": before.get("final")}
        goal = before.get("goal") if isinstance(before.get("goal"), dict) else {}
        if before.get("state") == "done" or goal.get("status") == "done":
            self._telemetry("goal_reopened", detail)
        if before.get("state") in STOPPED_STATES and after.get("state") in ("starting", "running"):
            self._telemetry("session_restarted", detail)

    def _exhaust(self, detail: dict[str, Any]) -> None:
        if not self.budget_exhausted:
            self.budget_exhausted = True
            self._telemetry("budget_exhausted", detail)
            self._log("бюджет сессии DreamTeam исчерпан, больше не бужу")

    # --- reporting ----------------------------------------------------------------------------------------

    def _report_spend(self, closed: tuple[Any, Any] | None) -> None:
        """Post the session's spend so far as the figure of the phase that just closed (`closed` key)."""
        resp = self._dt.get_session(self._session_id)
        if not resp.ok:
            self._log(f"сводка сессии DreamTeam: {resp.status} {resp.code}")
            return
        session = resp.session
        cost = session.get("cost_usd")
        if isinstance(cost, (int, float)):
            try:
                turn, phase = closed if closed else (None, None)
                self._game.spend(float(cost), SPEND_SOURCE, turn=turn, phase=phase)
            except GameApiError as exc:
                self._log(f"не удалось отправить расход: {exc.code} {exc.message}")
        error = session.get("error")
        if session.get("state") == "budget" or (isinstance(error, dict) and error.get("code") == "budget_exhausted"):
            self._exhaust({"via": "session", "state": session.get("state"), "cost_usd": cost,
                           "max_cost_usd": session.get("max_cost_usd")})

    def _telemetry(self, kind: str, detail: dict[str, Any]) -> None:
        try:
            self._game.telemetry(kind, detail)
        except GameApiError as exc:
            self._log(f"не удалось отправить телеметрию {kind}: {exc.code} {exc.message}")
