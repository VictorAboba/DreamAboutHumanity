"""The waker loop: follow the game's phases and wake a DreamTeam session when its seat has to act; after game_over,
ask the session to wrap up (or stop it) and keep reporting its spend until it is stopped or idle.

A wake goes to every member of a running session as a message to `["all"]` (`--wake-mode all`, the default): a
`continue` reaches only the coordinator, who may be off duty while another member holds the seat's turns. A stopped
session answers such a message 409 `not_running`; then the wake falls back to `continue`, which starts it again.
A message does not reopen a completed goal, resume a session the user paused, or answer a request that blocks the
work: a phase wake of a session in state `done` (or with a done goal), `paused` or `waiting` is a `continue` too.
`--wake-mode continue` is the old way, `both` sends the message and the `continue`. After a phase wake the waker
watches the player's own game calls (host-only presence fields) and records `wake_answered` or `wake_unanswered`.

An eliminated seat (`alive: false` before game_over) is done with the game: the waker sends it one notice (`ты выбыл,
партия для тебя окончена; заверши цель одним итоговым сообщением`), stops waking it and watches its spend as after
game_over (`after_game_over` figures, `post_game_finished` with `eliminated: true`).

Core 0.8.1 (m10 finding 1): an offer to a seat that was ready (`trade_proposed`, `pact_proposed`) clears its ready.
The host's phase info names such a clear in `ready_cleared` (with its counter `n`); for each new clear the waker sends
one `ready_cleared` wake (what cleared it, answer it, press ready again), even when no poll saw the seat ready in
between, unless the player's own wait_phase already returned the news (`told`). A player that called the game
moments ago (its ready() call counts) only postpones the wake: it is re-checked until that activity is older than
ACTIVE_SKIP_SECONDS. A game server without `ready_cleared`: the true -> false step of `you_ready`."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

from dah.bridge.client import GameApiError, GameClient
from dah.waker.dreamteam import DreamTeamClient, DtResponse
from dah.waker.text import eliminated_text, ready_cleared_text, wake_text, wrap_up_text

ACTIVE_PHASES = frozenset({"setup", "negotiation_1", "actions", "negotiation_2"})  # setup: stage 2
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
# A session whose goal is completed (`goal.status` done) and whose cost has not changed for this many seconds ends the
# post-game watch as `goal_done`, well before --post-game-minutes (which stays the upper bound).
GOAL_DONE_QUIET_SECONDS = 60.0
# ...but never while the session is at work: a running wrap-up turn may post its cost only when it ends, so a flat cost
# and a goal status left `done` from before the wrap-up do not mean the session has finished spending.
BUSY_STATES = frozenset({"starting", "running"})
WAKE_MODES = ("all", "continue", "both")
DEFAULT_WAKE_MODE = "all"
TO_ALL = ["all"]
# `continue` goes to the coordinator (the member who holds the planning task, else the first agent).
TO_COORDINATOR = ["coordinator"]
# A wake counts as answered when a player call of that seat comes in the same phase within ACK_SECONDS; while one
# is pending the game is polled at least every ACK_POLL_SECONDS.
ACK_SECONDS = 120.0
ACK_POLL_SECONDS = 15.0
# Summary states (or `final: true`) of a session that is not running: a message would get 409 `not_running`.
NOT_RUNNING_STATES = frozenset({"stopped", "failed", "created"})
# Summary states of a running session that a message to members does not get playing again, but `continue` does: it
# reopens a completed goal, resumes a session the user paused, answers a pending question (http-api.md, "The next
# instruction"). The value is the wake's `fallback`.
CONTINUE_STATES = {"done": "goal_done", "paused": "paused", "waiting": "waiting"}


def _stderr(message: str) -> None:
    print(f"dah-waker: {message}", file=sys.stderr, flush=True)


class Waker:
    def __init__(self, game: GameClient, dreamteam: DreamTeamClient, session_id: str, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = _stderr, stop_on_game_over: bool = False,
                 post_game_minutes: float = POST_GAME_MINUTES, wake_mode: str = DEFAULT_WAKE_MODE,
                 ack_seconds: float = ACK_SECONDS):
        if wake_mode not in WAKE_MODES:
            raise ValueError(f"wake_mode must be one of {', '.join(WAKE_MODES)}")
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
        # True once the seat is eliminated (the game goes on without it): its end comes before game_over.
        self.eliminated = False
        self.wake_mode = wake_mode
        self.ack_seconds = max(0.0, ack_seconds)
        # The last phase wake not yet answered: {at, turn, phase, seq, reason} (`at` on this process's clock).
        self._pending_ack: dict[str, Any] | None = None
        # `you_ready` of the last poll in the current phase (a true -> false step is a clear for a game server
        # without `ready_cleared`)
        self._you_ready = False
        # core 0.8.1 review W1/W2: the key of the last offer clear (host `ready_cleared`) that was woken or finally
        # skipped; a clear skipped only because the player was active is re-checked (`_clear_retry_at`)
        self._clear_done: Any = None
        self._clear_skip_logged: Any = None
        self._clear_retry_at: float | None = None

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
        self._check_ack(info)
        if info.get("game_over"):
            self._over_info = info
            if self._phase_key is None:  # no phase seen by this process: the figure is post-game spend
                self.started_after_game_over = True
                self._report_spend(None, after_game=True)
            else:
                self._report_spend(self._phase_key)
            self._pending_ack = None  # _check_ack above resolved it with the game_over info
            self._log("партия окончена")
            return True
        if info.get("alive") is False:  # eliminated: the game is over for this seat
            self._over_info = info
            self.eliminated = True
            if self._phase_key is None:  # eliminated before this process: the notice was due to an earlier one
                self.started_after_game_over = True
                self._report_spend(None, after_game=True)
            else:
                self._report_spend(self._phase_key)
            if self._pending_ack is not None:
                self._ack_resolved("wake_unanswered", self._clock() - self._pending_ack["at"], eliminated=True)
            self._log("место выбыло: партия для него окончена, больше не бужу")
            return True
        key = (info.get("turn"), info.get("phase"))
        was_ready = self._you_ready and key == self._phase_key
        self._observe(info)
        self._you_ready = bool(info.get("you_ready"))
        if key != self._phase_key:
            closed, self._phase_key = self._phase_key, key
            self._reminded = False
            self._clear_retry_at = self._clear_skip_logged = None
            # a clear already present at the first poll of a phase (a restarted waker) is covered by phase_open
            self._clear_done = self._clear_key(info.get("ready_cleared"))
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
        elif self._clear_due(info, was_ready):
            self._ready_cleared(info)
            if self._clear_retry_at is not None and self._reminder_due():   # a postponed clear never hides it
                self._reminded = True
                self._wake("reminder")
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

    @staticmethod
    def _clear_key(cleared: Any) -> Any:
        """Which offer clear the host's `ready_cleared` names: its counter `n`, or (a server without it) the offer."""
        if not isinstance(cleared, dict):
            return None
        if cleared.get("n") is not None:
            return ("n", cleared.get("n"))
        return ("offer", cleared.get("reason"), cleared.get("by"), cleared.get("id"))

    def _clear_due(self, info: dict[str, Any], was_ready: bool) -> bool:
        """Core 0.8.1 review W1: a server that names offer clears (`ready_cleared` in the host's info) wakes on a
        clear this waker has not handled yet, whether or not a poll saw the seat ready in between (a ready pressed
        and cleared within one poll is seen only as the clear). An earlier server (no such field): the true ->
        false step of `you_ready` within the phase, as before."""
        if not self._must_act():
            return False
        if "ready_cleared" not in info:
            return was_ready
        key = self._clear_key(info.get("ready_cleared"))
        return key is not None and key != self._clear_done

    def _ready_cleared(self, info: dict[str, Any]) -> None:
        """An offer cleared the seat's ready within the phase (or, for an earlier server, its ready went off): one
        wake. Skipped for good when the player's own poll returned the clear (`told`); skipped only for now when
        the player called the game moments ago (core 0.8.1 review W2: its ready() call itself counts, and a seat
        that pressed ready and stopped makes no next call): re-checked on the next polls until the activity is
        older than ACTIVE_SKIP_SECONDS."""
        cleared = info.get("ready_cleared") if isinstance(info.get("ready_cleared"), dict) else None
        key = self._clear_key(cleared)
        skip = self._ready_skip_reason(cleared)
        retry = skip == "player_active" and cleared is not None
        if skip and (key is None or self._clear_skip_logged != (key, skip)):
            self._clear_skip_logged = (key, skip)
            self._telemetry("continue_skipped", {"reason": skip, "wake": "ready_cleared", "turn": info.get("turn"),
                                                 "phase": info.get("phase"),
                                                 "cleared_by": (cleared or {}).get("reason"),
                                                 "player_active_s_ago": info.get("player_active_s_ago"),
                                                 "retry": retry, "session_state": self._session_state})
        if retry:
            ago = info.get("player_active_s_ago")
            self._clear_retry_at = self._clock() + max(1.0, ACTIVE_SKIP_SECONDS - float(ago) + 1.0)
            return
        self._clear_done, self._clear_retry_at = key, None
        if not skip:
            self._wake("ready_cleared")

    def _ready_skip_reason(self, cleared: dict[str, Any] | None) -> str | None:
        """_skip_reason for a ready_cleared wake: the player's own poll has returned the clear (or was pending at
        it: `told`), or it called the game moments ago (its next wait_phase returns the clear at once). Never
        skipped for a stopped session."""
        if cleared is None:
            return self._skip_reason()
        if self._session_state in STOPPED_STATES:
            return None
        if cleared.get("told"):
            return "player_waiting"
        ago = self._info.get("player_active_s_ago")
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
        wait = float(POLL_SECONDS)
        left = self._until_reminder()
        if left is not None:
            wait = min(wait, left)
        if self._clear_retry_at is not None:     # a ready_cleared wake skipped while the player was active
            wait = min(wait, self._clear_retry_at - self._clock())
        if self._pending_ack is not None:
            wait = min(wait, ACK_POLL_SECONDS, self._pending_ack["at"] + self.ack_seconds - self._clock())
        return max(1.0, wait)

    # --- did the wake reach someone who plays? ---------------------------------------------------------------

    def _check_ack(self, info: dict[str, Any]) -> None:
        """Resolve the pending phase wake from the host-only presence fields: `wake_answered` {seconds} once a player
        call of this seat came after the wake in the same phase within ack_seconds, `wake_unanswered` {seconds} once
        ack_seconds passed (or the phase closed) without one. With `player_calls` (the seat's recent calls) the
        answer is the first call after the wake that is not a phase-status read (`phase`, `wait_phase`): a member
        that only waits again has not played; such a wake is `wake_unanswered` with `passive_only`."""
        pending = self._pending_ack
        if pending is None:
            return
        calls = info.get("player_calls")
        if not isinstance(calls, list):  # a game server of an earlier version
            self._check_ack_presence(info)
            return
        now, at = self._clock(), pending["at"]
        answer, passive = None, False
        for call in calls:
            if not isinstance(call, dict) or call.get("phase_seq") != pending["seq"]:
                continue
            ago = _number(call.get("s_ago"))
            if ago is None or now - ago < at:
                continue
            if call.get("passive"):
                passive = passive or now - ago - at <= self.ack_seconds
            elif answer is None or now - ago < answer:
                answer = now - ago
        if answer is not None and answer - at <= self.ack_seconds:
            self._ack_resolved("wake_answered", answer - at)
            return
        closed = info.get("phase_seq") != pending["seq"] or bool(info.get("game_over"))
        if closed or now - at >= self.ack_seconds:
            extra: dict[str, Any] = {"phase_closed": True} if closed else {}
            if passive:
                extra["passive_only"] = True
            self._ack_resolved("wake_unanswered", now - at, **extra)

    def _check_ack_presence(self, info: dict[str, Any]) -> None:
        """_check_ack from the presence fields of an earlier game server (no `player_calls`): in the wake's phase the
        first call of the phase (`player_first_active_s_ago`) when it came after the wake, else the last one
        (`player_active_s_ago`); after the phase changed, `player_last_call` names the last call and its phase."""
        pending = self._pending_ack
        if pending is None:
            return
        now, at = self._clock(), pending["at"]
        if info.get("phase_seq") == pending["seq"] and not info.get("game_over"):
            call_at = None
            for field_name in ("player_first_active_s_ago", "player_active_s_ago"):
                ago = _number(info.get(field_name))
                if ago is not None and now - ago >= at:
                    call_at = now - ago
                    break
            if call_at is not None and call_at - at <= self.ack_seconds:
                self._ack_resolved("wake_answered", call_at - at)
            elif now - at >= self.ack_seconds:
                self._ack_resolved("wake_unanswered", now - at)
            return
        last = info.get("player_last_call")
        if isinstance(last, dict) and last.get("phase_seq") == pending["seq"]:
            ago = _number(last.get("s_ago"))
            if ago is not None and 0 <= now - ago - at <= self.ack_seconds:
                self._ack_resolved("wake_answered", now - ago - at)
                return
        self._ack_resolved("wake_unanswered", now - at, phase_closed=True)

    def _ack_resolved(self, kind: str, seconds: float, **extra: Any) -> None:
        pending, self._pending_ack = self._pending_ack, None
        if pending is None:
            return
        detail = {"turn": pending["turn"], "phase": pending["phase"], "reason": pending["reason"],
                  "seconds": round(max(0.0, seconds), 1), **extra}
        if kind == "wake_unanswered":
            detail["ack_seconds"] = self.ack_seconds
        self._telemetry(kind, detail)

    # --- waking -------------------------------------------------------------------------------------------

    def _wake(self, reason: str) -> None:
        if self.budget_exhausted:
            return
        where = {"reason": reason, "turn": self._info.get("turn"), "phase": self._info.get("phase")}
        before = self._dt.get_session(self._session_id)
        # `at`: when the wake was out (after any retries), not when the waker began to send it
        text = (ready_cleared_text(self._info) if reason == "ready_cleared"
                else wake_text(self._info, reminder=reason == "reminder"))
        at = self._deliver(text, where, before, restart=True)
        if at is not None:
            if self._pending_ack is not None:  # an earlier wake of this phase, still unanswered
                self._ack_resolved("wake_unanswered", at - self._pending_ack["at"], superseded=True)
            self._pending_ack = {"at": at, "turn": where["turn"], "phase": where["phase"], "seq": self._seq,
                                 "reason": reason}

    def _deliver(self, text: str, where: dict[str, Any], before: DtResponse | None, *,
                 restart: bool) -> float | None:
        """Send one wake as the wake mode says; the clock time when some member got it (the successful send
        returned), None when nobody did. `restart`: a stopped session may be started again, and a completed, paused
        or blocked one woken, with `continue` (phase wakes); without it (the wrap-up) a stopped session is left
        alone and any other gets the message only."""
        mode = self.wake_mode
        if mode == "continue":
            return self._send_continue(text, where, before)
        if before is not None and before.ok and _not_running(before.session):
            if restart:
                return self._send_continue(text, where, before, fallback="session_stopped")
            self._telemetry("continue_skipped", {**where, "reason": "not_running", "mode": mode,
                                                 "session_state": before.session.get("state")})
            return None
        blocked = _needs_continue(before.session) if before is not None and before.ok else None
        if restart and blocked and mode == "all":  # `both` sends the continue after the message anyway
            return self._send_continue(text, where, before, fallback=blocked)
        resp, attempts = self._retry(lambda: self._dt.send_message(self._session_id, text, TO_ALL), "messages")
        if resp.ok:
            sent_at = self._clock()
            self._telemetry("wake_sent", {**where, "mode": mode, "via": "messages", "to": TO_ALL,
                                          "status": resp.status})
            if mode == "both" and restart:
                self._send_continue(text, where, before, also=True)
            return sent_at
        if resp.code == "budget_exhausted":
            self._exhaust({**where, "via": "messages"})
            return None
        if resp.code != "not_running":
            self._telemetry("wake_failed", {**where, "mode": mode, "via": "messages", "to": TO_ALL,
                                            "status": resp.status, "code": resp.code, "attempts": attempts})
        if not restart:
            if resp.code == "not_running":
                self._telemetry("continue_skipped", {**where, "reason": "not_running", "mode": mode})
            return None
        return self._send_continue(text, where, before, fallback=resp.code)

    def _send_continue(self, text: str, where: dict[str, Any], before: DtResponse | None, *,
                       fallback: str | None = None, also: bool = False) -> float | None:
        """`continue` with the wake text; the clock time when it was taken, or None."""
        resp, attempts = self._retry(lambda: self._dt.continue_session(self._session_id, text), "continue")
        if resp.ok:
            sent_at = self._clock()
            detail = {**where, "mode": self.wake_mode, "via": "continue", "to": TO_COORDINATOR,
                      "status": resp.status, "state": resp.session.get("state")}
            if fallback:
                detail["fallback"] = fallback
            if also:
                detail["also"] = True
            self._telemetry("wake_sent", detail)
            if before is not None and before.ok:
                self._compare(before.session, resp.session, where)
            return sent_at
        if resp.code == "budget_exhausted":
            self._exhaust({**where, "via": "continue"})
        else:
            self._telemetry("wake_failed", {**where, "mode": self.wake_mode, "via": "continue",
                                            "to": TO_COORDINATOR, "status": resp.status, "code": resp.code,
                                            "attempts": attempts})
        return None

    def _retry(self, send: Callable[[], DtResponse], label: str) -> tuple[DtResponse, int]:
        attempts = 1
        resp = send()
        for delay in RETRY_DELAYS:
            if not resp.retryable:
                break
            self._log(f"DreamTeam ответил {resp.status} {resp.code} на {label}, повтор через {delay} с")
            self._sleep(delay)
            attempts += 1
            resp = send()
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
        """The one wrap-up (a message to all members; `continue` with --wake-mode continue; a stopped session is not
        started again for it) or `stop` with --stop-on-game-over, then a read of the session summary every
        POST_GAME_POLL_SECONDS, each new cost posted as `/v1/spend` with phase `after_game_over`, until the session is
        stopped (`final`), its goal is completed (`goal.status` done), it is not running (BUSY_STATES) and its cost
        has not changed for GOAL_DONE_QUIET_SECONDS (`goal_done`), it is idle (`done`/`budget` and no new spend over IDLE_POLLS reads in a
        row), or post_game_minutes have passed. A waker whose first poll already saw game_over sends neither the wrap-up nor
        the stop (an earlier process did) and only watches the spend. Ends with `post_game_finished` telemetry
        (reason, duration, spend delta), Ctrl-C included (reason `interrupted`)."""
        started = self._clock()
        cost_at_over = _cost(self._session)
        reason = _stopped(self._session)  # already stopped at game_over: neither wake it nor stop it again
        end = "eliminated" if self.eliminated else "game_over"
        where = {"phase": end, "turn": self._over_info.get("turn"), "session_state": self._session_state}
        if reason:
            self._telemetry("continue_skipped", {**where, "reason": "session_final"})
        elif self.started_after_game_over:
            self._telemetry("continue_skipped", {**where, "reason": f"started_after_{end}"})
        elif self.stop_on_game_over:
            self._stop()
        else:
            self._wrap_up()
        last_cost, posted, polls, idle_polls = cost_at_over, cost_at_over, 0, 0
        state = self._session_state
        deadline = started + self.post_game_minutes * 60
        cost_since = started  # when the cost was last seen to change (game_over counts as a change)
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
                if cost is not None and cost != last_cost:
                    cost_since = self._clock()
                reason = _stopped(session)
                if (reason is None and _goal_done(session) and state not in BUSY_STATES and cost is not None
                        and last_cost is not None and self._clock() - cost_since >= GOAL_DONE_QUIET_SECONDS):
                    reason = "goal_done"
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
        if self.eliminated:
            detail["eliminated"] = True
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
        """The one wrap-up message: the result at game_over, or the elimination notice."""
        end = "eliminated" if self.eliminated else "game_over"
        where = {"reason": end, "turn": self._over_info.get("turn"), "phase": end}
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
        restart = self.wake_mode == "continue"  # a message never starts a stopped session just to wrap up
        text = (eliminated_text if self.eliminated else wrap_up_text)(self._over_info,
                                                                      seat if isinstance(seat, str) else None)
        self._deliver(text, where,
                      DtResponse(200, None, {"session": self._session}) if self._session else None,
                      restart=restart)

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


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _not_running(session: dict[str, Any]) -> bool:
    """The summary says a message would get 409 `not_running`: stopped for good, or a stopped/failed/never
    started state."""
    return session.get("final") is True or session.get("state") in NOT_RUNNING_STATES


def _needs_continue(session: dict[str, Any]) -> str | None:
    """Why a message to members would not get this running session playing, but `continue` would: its goal is
    completed (`goal_done`), the user paused it (`paused`), or a request blocks the work (`waiting`). None
    otherwise."""
    goal = session.get("goal") if isinstance(session.get("goal"), dict) else {}
    if goal.get("status") == "done":
        return "goal_done"
    state = session.get("state")
    return CONTINUE_STATES.get(state) if isinstance(state, str) else None


def _goal_done(session: dict[str, Any]) -> bool:
    goal = session.get("goal")
    return isinstance(goal, dict) and goal.get("status") == "done"


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
