"""Review of the m3 wake: a done, paused or blocked session is woken with `continue`; the ack counts from the moment
the wake was out, finds the first call after it in the host-only `player_calls`, and does not count phase-status
reads; the presence survives a server restart."""

from __future__ import annotations

from typing import Any

import pytest
from test_measurement1_fixes import Env, FakeClock, new_game, ready
from test_waker_loop import T0, Clock, FakeDreamTeam, FakeGame, make_waker, run
from test_waker_wake_all import _play_to_game_over, acks

from dah.game import PLAYER_CALLS_KEPT, Game


class CallsGame(FakeGame):
    """FakeGame whose host phase info lists the player's calls (`player_calls`): `calls` are (seconds after the phase
    opened, passive) for every phase."""

    def __init__(self, clock: Clock, *, calls: list[tuple[float, bool]], **kwargs: Any):
        super().__init__(clock, **kwargs)
        self.calls = calls

    def _info(self) -> dict[str, Any]:
        info = super()._info()
        now = self.clock.now
        listed = []
        for index in range(min(self._index(), len(self.phases) - 1) + 1):
            for offset, passive in self.calls:
                at = T0 + index * self.length + offset
                if at <= now:
                    listed.append({"phase_seq": index + 1, "s_ago": round(now - at, 1), "passive": passive})
        info.update(player_waiting=False, player_calls=listed)
        return info


# ---------------------------------------------------------------- a done, paused or blocked session gets `continue`


@pytest.mark.parametrize(("session", "fallback"), [
    ({"state": "done"}, "goal_done"),
    ({"state": "running", "goal": {"status": "done"}}, "goal_done"),
    ({"state": "paused"}, "paused"),
    ({"state": "waiting"}, "waiting"),
])
def test_session_a_message_would_not_wake_gets_continue(session: dict[str, Any], fallback: str):
    clock = Clock()
    game, dt = FakeGame(clock, length=100_000), FakeDreamTeam(clock)
    dt.session.update(final=False, **session)
    make_waker(game, dt, clock).step()
    assert dt.messages == [] and len(dt.continues) == 1
    sent = game.detail("wake_sent")[0]
    assert sent["via"] == "continue" and sent["fallback"] == fallback and sent["to"] == ["coordinator"]
    if fallback == "goal_done":
        assert "goal_reopened" in game.kinds()


def test_paused_session_with_wake_mode_both_gets_message_and_one_continue():
    clock = Clock()
    game, dt = FakeGame(clock, length=100_000), FakeDreamTeam(clock)
    dt.session.update(state="paused")
    make_waker(game, dt, clock, wake_mode="both").step()
    assert len(dt.messages) == 1 and len(dt.continues) == 1


def test_wrap_up_of_a_done_session_is_still_a_message_only():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    waker = _play_to_game_over(game, dt, clock)
    waker._session = {**dt.session, "state": "done", "final": False, "goal": {"status": "done"}}
    before_continues, before_messages = len(dt.continues), len(dt.messages)
    waker.after_game_over()
    assert len(dt.continues) == before_continues and len(dt.messages) == before_messages + 1


# ---------------------------------------------------------------- the ack counts from the moment the wake was out


def test_ack_starts_after_the_retried_send():
    clock = Clock()
    game = CallsGame(clock, calls=[(5, False)], length=100_000)
    dt = FakeDreamTeam(clock, message_replies=[(503, {"error": "x", "code": "internal"})])
    waker = make_waker(game, dt, clock, ack_seconds=30)
    for _ in range(4):
        waker.step()
    assert dt.messages[-1][0] == T0 + 10  # the retry went out 10 s later
    # the call at +5 came before any member had the wake: it is not an answer
    assert [(kind, d["seconds"]) for kind, d in acks(game)] == [("wake_unanswered", 30.0)]


# ---------------------------------------------------------------- the first call after the wake, not the last


def test_answer_is_the_first_call_after_a_reminder():
    clock = Clock()
    # a call long before the reminder (540), then two after it; the poll at the window's end sees both
    game = CallsGame(clock, calls=[(100, False), (545, False), (549, False)])
    dt = FakeDreamTeam(clock)
    run(game, dt, clock, ack_seconds=10)
    reminders = [(kind, d["seconds"]) for kind, d in acks(game) if d["reason"] == "reminder"]
    assert reminders == [("wake_answered", 5.0)] * 3


def test_call_in_the_window_seen_after_it_is_answered():
    clock = Clock()
    # 15-second polls: the call at 25 is in the 30-second window, the next poll (at 30) also lists a call at 29.5
    game = CallsGame(clock, calls=[(25, False), (29.5, False)], ready_after=300)
    dt = FakeDreamTeam(clock)
    run(game, dt, clock, ack_seconds=30)
    assert [(kind, d["seconds"]) for kind, d in acks(game)] == [("wake_answered", 25.0)] * 3


# ---------------------------------------------------------------- phase-status reads are not an answer


def test_waiting_again_is_not_an_answer():
    clock = Clock()
    game = CallsGame(clock, calls=[(20, True), (40, True)], ready_after=300)
    dt = FakeDreamTeam(clock)
    run(game, dt, clock)
    found = acks(game)
    assert [kind for kind, _ in found] == ["wake_unanswered"] * 3
    assert all(d["passive_only"] is True for _, d in found)


def test_a_move_after_waiting_is_the_answer():
    clock = Clock()
    game = CallsGame(clock, calls=[(20, True), (50, False)], ready_after=300)
    dt = FakeDreamTeam(clock)
    run(game, dt, clock)
    assert [(kind, d["seconds"]) for kind, d in acks(game)] == [("wake_answered", 50.0)] * 3


def test_phase_closed_before_the_poll_still_finds_the_call():
    clock = Clock()
    game = CallsGame(clock, calls=[(197, False)], length=200, ready_after=130)
    dt = FakeDreamTeam(clock)
    run(game, dt, clock, ack_seconds=300)
    assert [(kind, d["seconds"]) for kind, d in acks(game)] == [("wake_answered", 197.0)] * 3


# ---------------------------------------------------------------- server: player_calls


def test_server_lists_calls_and_marks_status_reads_passive(tmp_path):
    env = Env(tmp_path)
    env.ok("A", "GET", "/v1/phase")
    env.ok("A", "GET", "/v1/wait_phase", params={"max_seconds": 0})
    env.ok("A", "GET", "/v1/inbox")
    host = env.ok("A", "GET", "/v1/phase", host=True)
    assert [c["passive"] for c in host["player_calls"]] == [True, True, False]
    assert all(c["phase_seq"] == host["phase_seq"] for c in host["player_calls"])
    assert "player_calls" not in env.ok("A", "GET", "/v1/phase")


def test_server_keeps_only_the_last_calls(tmp_path):
    game = new_game(tmp_path, FakeClock())
    for _ in range(PLAYER_CALLS_KEPT + 5):
        game.note_player_activity("A", arrival=True)
    game.note_player_activity("A")  # the end of a call is not listed again
    assert len(game.phase_info("A", host=True)["player_calls"]) == PLAYER_CALLS_KEPT


def test_presence_survives_a_server_restart(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    seq = game.phase_seq
    game.note_player_activity("A", arrival=True)
    clock.now += 5
    ready(game, "A")
    ready(game, "B")  # the phase closes, the runtime is saved
    clock.now += 2
    loaded = Game.load(game.gdir, clock)
    info = loaded.phase_info("A", host=True)
    assert info["player_last_call"] == {"phase_seq": seq, "s_ago": 7.0}
    assert info["player_calls"] == [{"phase_seq": seq, "s_ago": 7.0, "passive": False}]
