"""After game_over the waker stops watching a session whose goal is completed as soon as its cost has been still for
60 s (`post_game_finished` reason `goal_done`), instead of waiting out --post-game-minutes."""

from __future__ import annotations

from typing import Any

import httpx
from test_post_game import GAME_END, OverGame, ScriptedDreamTeam, run_waker, wrap_ups
from test_waker_loop import SESSION, Clock

from dah.waker.core import GOAL_DONE_QUIET_SECONDS, POST_GAME_POLL_SECONDS


class GoalDreamTeam(ScriptedDreamTeam):
    """ScriptedDreamTeam whose summaries after game_over carry `goal` ({"status": ...}) from the step (`goal` key)."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        resp = super().__call__(request)
        if (request.method == "GET" and request.url.path == f"/api/v1/sessions/{SESSION}"
                and self.clock.now >= GAME_END and self.reads_after > 1):
            step = self.after[min(self.reads_after - 2, len(self.after) - 1)]
            body = resp.json()
            if "goal" in step:
                body["session"]["goal"] = {"status": step["goal"]}
            return httpx.Response(200, json=body)
        return resp


def finished(game: OverGame) -> dict[str, Any]:
    details = game.detail("post_game_finished")
    assert len(details) == 1
    return details[0]


def test_goal_done_and_quiet_for_60_s_ends_the_watch_early():
    clock = Clock()
    game = OverGame(clock)
    dt = GoalDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.04, "goal": "in_progress"},
                               {"state": "running", "final": False, "spend": 0.01, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.0, "goal": "done"}])
    run_waker(game, dt, clock, post_game_minutes=30)
    done = finished(game)
    # the last change of cost was at +60; +90 is 30 s still, +120 is 60 s still: goal_done there, not at +30 min
    assert done["reason"] == "goal_done"
    assert done["duration_s"] == 60 + GOAL_DONE_QUIET_SECONDS and done["polls"] == 4
    assert clock.now == GAME_END + 120
    assert done["spent_delta_usd"] == 0.05 and done["state"] == "waiting"
    assert len(wrap_ups(dt)) == 1
    assert [s["spent_usd"] for s in game.after_spend()] == [round(done["cost_at_game_over_usd"] + d, 2)
                                                            for d in (0.04, 0.05)]


def test_goal_done_but_still_spending_is_watched_on():
    clock = Clock()
    game = OverGame(clock)
    dt = GoalDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.02, "goal": "done"}])
    run_waker(game, dt, clock, post_game_minutes=3)
    done = finished(game)
    assert done["reason"] == "timeout" and done["duration_s"] == 180  # the limit stays the upper bound


def test_quiet_since_game_over_counts():
    clock = Clock()
    game = OverGame(clock)
    dt = GoalDreamTeam(clock, [{"state": "waiting", "final": False, "spend": 0.0, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.0, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.03, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.0, "goal": "done"}])
    run_waker(game, dt, clock)
    done = finished(game)
    # no new spend since game_over: 60 s still at the second read
    assert done["reason"] == "goal_done" and done["duration_s"] == 60 and done["polls"] == 2


def test_goal_done_spend_after_the_first_reads_resets_the_window():
    clock = Clock()
    game = OverGame(clock)
    dt = GoalDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.03, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.02, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.0, "goal": "done"}])
    run_waker(game, dt, clock)
    done = finished(game)
    # cost changed at +30 and +60, still at +90 and +120
    assert done["reason"] == "goal_done" and done["duration_s"] == 120 and done["state"] == "waiting"


def test_stopped_session_still_ends_as_stopped_at_once():
    clock = Clock()
    game = OverGame(clock)
    dt = GoalDreamTeam(clock, [{"state": "stopped", "final": True, "spend": 0.01, "goal": "done"}])
    run_waker(game, dt, clock)
    done = finished(game)
    assert done["reason"] == "stopped" and done["duration_s"] == POST_GAME_POLL_SECONDS


def test_goal_in_progress_is_not_goal_done():
    clock = Clock()
    game = OverGame(clock)
    dt = GoalDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.0, "goal": "in_progress"}])
    run_waker(game, dt, clock, post_game_minutes=2)
    assert finished(game)["reason"] == "timeout"


def test_running_wrap_up_with_a_stale_goal_done_is_not_goal_done():
    """Review finding: the wrap-up turn runs with the goal still marked done from before and the cost only posted
    when the turn ends; a flat cost while `running` must not end the watch before that spend is seen."""
    clock = Clock()
    game = OverGame(clock)
    dt = GoalDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.0, "goal": "done"},
                               {"state": "running", "final": False, "spend": 0.0, "goal": "done"},
                               {"state": "running", "final": False, "spend": 0.0, "goal": "done"},
                               {"state": "running", "final": False, "spend": 0.0, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.07, "goal": "done"},
                               {"state": "waiting", "final": False, "spend": 0.0, "goal": "done"}])
    run_waker(game, dt, clock, post_game_minutes=30)
    done = finished(game)
    # running and flat for 120 s: watched on; the wrap-up's cost comes at +150, then 60 s still: goal_done at +210
    assert done["reason"] == "goal_done" and done["duration_s"] == 150 + GOAL_DONE_QUIET_SECONDS
    assert done["spent_delta_usd"] == 0.07
