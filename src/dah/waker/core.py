"""The waker loop: follow the game's phases and wake a DreamTeam session when its seat has to act; after game_over,
ask the session to wrap up (or stop it) and keep reporting its spend until it is stopped or idle."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

from dah.bridge.client import GameApiError, GameClient
from dah.waker.dreamteam import DreamTeamClient, DtResponse
from dah.waker.text import wake_text, wrap_up_text

ACTIVE_PHASES = frozenset({"negotiation_1", "actions", "negotiation_2"})
POLL_SECONDS = 45
REMINDER_SECONDS = 60
RETRY_DELAYS = (10, 30, 60)
GAME_ERROR_DELAY = 5
# States of a session that a `continue` has to start again (DreamTeam http-api.md, "States").
STOPPED_STATES = frozenset({"stopped", "failed", "done", "budget", "created"})
SPEND_SOURCE = "participant_program"
# A player call in the new phase at most this many seconds ago means the session is already at work.
ACTIVE_SKIP_SECONDS = 30
# After game_over: how often the session summary is read, and the default limit of that watch.
POST_GAME_POLL_SECONDS = 30
POST_GAME_MINUTES = 30.0
AFTER_GAME_OVER = "after_game_over"
# States of a session that is not working (DreamTeam http-api.md, "States"): with no new spend over IDLE_POLLS reads
# in a row, the post-game watch ends as `idle`. `waiting` and `paused` are not here: the session still runs (blocked on
# a review or an approval, or paused by the user) and may spend again once resumed, so the watch keeps polling it.
IDLE_STATES = frozenset({"done", "budget"})
IDLE_POLLS = 2


def _stderr(message: str) -> None:
    print(f"dah-waker: {message}", file=sys.stderr, flush=True)


class Waker:
    def __init__(self, game: GameClient, dreamteam: DreamTeamClient, session_id: str, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = _stderr, stop_on_game_over: bool = False,
                 post_game_minutes: float = POST_GAME_MINUTES):
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
        self._link_pending = False  # True while run()'s link_session hit a transient error
        self._session_state: str | None = None  # from the last session summary that _report_spend read
        self._session: dict[str, Any] | None = None  # that summary itself (None when the read failed)
        self.stop_on_game_over = stop_on_game_over
        self.post_game_minutes = max(0.0, post_game_minutes)
        self._over_info: dict[str, Any] = {}  # the game's phase info at game_over
        # True when the first poll of this process already saw game_over (a waker started or restarted after the
        # match): the wrap-up was due to an earlier process, so it is not sent again.
        self.started_after_game_over = False

    # --- loop ---------------------------------------------------------------------------------------------

    def run(self) -> None:
        self.link_session()
        while not self.step():
            pass
        self.after_game_over()

    def link_session(self) -> bool:
        """Tell the game server which DreamTeam session plays this seat (journaled as a `seat_info` update).
        True once recorded. A network error or a 5xx (the server is not up yet) leaves it pending: `step()` tries
        again after its next successful poll. Any other refusal is final (logged, not retried)."""
        try:
            self._game.report_session(self._session_id)
        except GameApiError as exc:
            if exc.status in (401, 403):
                raise
            transient = exc.code == "network" or (exc.status is not None and exc.status >= 500)
            self._link_pending = transient
            self._log(f"не удалось записать сессию DreamTeam в партию: {exc.code} {exc.message}"
                      + (" (повторю после ответа сервера игры)" if transient else ""))
            return False
        self._link_pending = False
        return True

    def step(self) -> bool:
        """One poll of the game; True once the game is over."""
        info = self._poll()
        if info is None:
            return False
        if self._link_pending:  # the game server was not up when run() started
            self.link_session()
        if info.get("game_over"):
            self._over_info = info
            if self._phase_key is None:  # no phase seen by this process: the figure is post-game spend
                self.started_after_game_over = True
                self._report_spend(None, after_game=True)
            else:
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
                skip = self._skip_reason()
                if skip:
                    self._telemetry("continue_skipped", {"reason": skip, "turn": info.get("turn"),
                                                         "phase": info.get("phase"),
                                                         "player_active_s_ago": info.get("player_active_s_ago"),
                                                         "session_state": self._session_state})
                else:
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

    def _skip_reason(self) -> str | None:
        """Why a phase-open `continue` is not needed: the player is already in wait_phase (the poll wakes it with
        the new phase) or has called the game in this phase moments ago. Never skipped when the DreamTeam session
        is stopped (a poll can outlive the agent that made it: the server keeps it pending after the client is
        gone), and the deadline reminder is never skipped."""
        info = self._info
        if self._session_state in STOPPED_STATES:
            return None
        if info.get("player_waiting"):
            return "player_waiting"
        ago = info.get("player_active_s_ago")
        if isinstance(ago, (int, float)) and ago <= ACTIVE_SKIP_SECONDS:
            return "player_active"
        return None

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

    def _report_spend(self, closed: tuple[Any, Any] | None, *, after_game: bool = False) -> None:
        """Post the session's spend so far as the figure of the phase that just closed (`closed` key), or as an
        `after_game_over` figure (`after_game`)."""
        resp = self._dt.get_session(self._session_id)
        if not resp.ok:
            self._session_state = None
            self._session = None
            self._log(f"сводка сессии DreamTeam: {resp.status} {resp.code}")
            return
        session = resp.session
        self._session = session
        self._session_state = session.get("state") if isinstance(session.get("state"), str) else None
        cost = session.get("cost_usd")
        if isinstance(cost, (int, float)):
            try:
                turn, phase = closed if closed else (None, None)
                if after_game:
                    turn, phase = None, AFTER_GAME_OVER
                self._game.spend(float(cost), SPEND_SOURCE, turn=turn, phase=phase)
            except GameApiError as exc:
                self._log(f"не удалось отправить расход: {exc.code} {exc.message}")
        error = session.get("error")
        if session.get("state") == "budget" or (isinstance(error, dict) and error.get("code") == "budget_exhausted"):
            self._exhaust({"via": "session", "state": session.get("state"), "cost_usd": cost,
                           "max_cost_usd": session.get("max_cost_usd")})

    # --- after game_over ------------------------------------------------------------------------------------

    def after_game_over(self) -> None:
        """The one wrap-up `continue` (or `stop` with --stop-on-game-over), then a read of the session summary every
        POST_GAME_POLL_SECONDS, each new cost posted as `/v1/spend` with phase `after_game_over`, until the session is
        stopped (`final`) or idle (`done`/`budget` and no new spend over IDLE_POLLS reads in a row), or
        post_game_minutes have passed. A waker whose first poll already saw game_over sends neither the wrap-up nor
        the stop (an earlier process did) and only watches the spend. Ends with `post_game_finished` telemetry
        (reason, duration, spend delta), Ctrl-C included (reason `interrupted`)."""
        started = self._clock()
        cost_at_over = _cost(self._session)
        reason = _stopped(self._session)  # already stopped at game_over: neither wake it nor stop it again
        where = {"phase": "game_over", "turn": self._over_info.get("turn"), "session_state": self._session_state}
        if reason:
            self._telemetry("continue_skipped", {**where, "reason": "session_final"})
        elif self.started_after_game_over:
            self._telemetry("continue_skipped", {**where, "reason": "started_after_game_over"})
        elif self.stop_on_game_over:
            self._stop()
        else:
            self._wrap_up()
        last_cost, posted, polls, idle_polls = cost_at_over, cost_at_over, 0, 0
        state = self._session_state
        deadline = started + self.post_game_minutes * 60
        interrupted: KeyboardInterrupt | None = None
        try:
            while reason is None and self._clock() < deadline:
                self._sleep(max(0.0, min(POST_GAME_POLL_SECONDS, deadline - self._clock())))
                polls += 1
                resp = self._dt.get_session(self._session_id)
                if not resp.ok:
                    self._log(f"сводка сессии DreamTeam: {resp.status} {resp.code}")
                    idle_polls = 0
                    continue
                session = resp.session
                state = session.get("state") if isinstance(session.get("state"), str) else None
                cost = _cost(session)
                if cost is not None and cost != posted and self._post_after(cost):
                    posted = cost
                reason = _stopped(session)
                if state in IDLE_STATES and cost is not None and cost == last_cost:
                    idle_polls += 1
                else:
                    idle_polls = 0
                if reason is None and idle_polls >= IDLE_POLLS:
                    reason = "idle"
                if cost is not None:
                    last_cost = cost
        except KeyboardInterrupt as exc:
            interrupted, reason = exc, "interrupted"
        if last_cost is not None and last_cost != posted and self._post_after(last_cost):  # one last try
            posted = last_cost
        delta = (round(last_cost - cost_at_over, 6) if last_cost is not None and cost_at_over is not None
                 else None)
        detail = {"reason": reason or "timeout", "duration_s": round(self._clock() - started, 1),
                  "spent_delta_usd": delta, "cost_at_game_over_usd": cost_at_over, "cost_usd": last_cost,
                  "state": state, "polls": polls}
        if self.started_after_game_over:
            detail["started_after_game_over"] = True
        self._telemetry("post_game_finished", detail)
        self._log(f"после партии: {reason or 'timeout'}, ещё потрачено "
                  f"{'?' if delta is None else f'${delta:.2f}'}")
        if interrupted is not None:
            raise interrupted

    def _post_after(self, cost: float) -> bool:
        """Post `cost` as an `after_game_over` figure; True once the server took it."""
        try:
            self._game.spend(cost, SPEND_SOURCE, phase=AFTER_GAME_OVER)
        except GameApiError as exc:
            self._log(f"не удалось отправить расход: {exc.code} {exc.message}")
            return False
        return True

    def _wrap_up(self) -> None:
        where = {"reason": "game_over", "turn": self._over_info.get("turn"), "phase": "game_over"}
        if self.budget_exhausted:
            self._telemetry("continue_skipped", {**where, "reason": "budget_exhausted"})
            return
        seat = None
        try:
            seat = self._game.whoami().get("seat")
        except GameApiError as exc:
            if exc.status in (401, 403):
                raise
            self._log(f"whoami: {exc.code} {exc.message}")
        resp, attempts = self._continue(wrap_up_text(self._over_info, seat if isinstance(seat, str) else None))
        if resp.ok:
            self._telemetry("continue_sent", {**where, "state": resp.session.get("state")})
        elif resp.code == "budget_exhausted":
            self._exhaust({**where, "via": "continue"})
        else:
            self._telemetry("wake_failed", {**where, "status": resp.status, "code": resp.code, "attempts": attempts})

    def _stop(self) -> None:
        attempts = 1
        resp = self._dt.stop_session(self._session_id)
        for delay in RETRY_DELAYS:
            if not resp.retryable:
                break
            self._log(f"DreamTeam ответил {resp.status} {resp.code}, повтор stop через {delay} с")
            self._sleep(delay)
            attempts += 1
            resp = self._dt.stop_session(self._session_id)
        if resp.ok:
            self._telemetry("session_stopped_by_waker", {"turn": self._over_info.get("turn"),
                                                         "state_before": self._session_state,
                                                         "state": resp.session.get("state"),
                                                         "cost_usd": _cost(resp.session)})
        else:
            self._telemetry("wake_failed", {"reason": "stop", "phase": "game_over", "status": resp.status,
                                            "code": resp.code, "attempts": attempts})

    def _telemetry(self, kind: str, detail: dict[str, Any]) -> None:
        try:
            self._game.telemetry(kind, detail)
        except GameApiError as exc:
            self._log(f"не удалось отправить телеметрию {kind}: {exc.code} {exc.message}")


def _cost(session: dict[str, Any] | None) -> float | None:
    cost = (session or {}).get("cost_usd")
    return float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None


def _stopped(session: dict[str, Any] | None) -> str | None:
    """`stopped` once the session is stopped for good (`final`; a summary without `final`: a stopped or failed
    state), else None."""
    if not session:
        return None
    final = session.get("final")
    if final is True or (final is None and session.get("state") in ("stopped", "failed")):
        return "stopped"
    return None
