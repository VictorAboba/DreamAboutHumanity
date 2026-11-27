"""After game_over: dah-waker sends one wrap-up `continue` (or stops the session with --stop-on-game-over), keeps
posting the session's spend as `after_game_over` until it is stopped or idle (or --post-game-minutes pass), the server
journals those figures, and the report splits money and time into "during the game" and "after game_over"."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_measurement1_fixes import Env
from test_waker_loop import KEY, SESSION, T0, TOKEN, Clock, FakeGame

from dah.bridge.client import GameClient
from dah.journal import read_events
from dah.report import build_report, render_report
from dah.server.spectate import build_timings
from dah.waker.core import Waker
from dah.waker.dreamteam import DreamTeamClient
from dah.waker.main import parse_args
from dah.waker.text import wrap_up_text

RESULT = {"winners": ["B"], "reason": "turn_limit", "scores": {"A": 9, "B": 12, "C": 4}}
GAME_END = T0 + 3 * 600  # one turn of three 600-second phases


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


class OverGame(FakeGame):
    """FakeGame whose game_over carries the turn and the result, and that answers whoami as seat A."""

    def _info(self) -> dict[str, Any]:
        info = super()._info()
        if info.get("game_over"):
            info.update(turn=1, result=RESULT)
        return info

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/whoami":
            self.requests.append(request)
            return httpx.Response(200, json={"ok": True, "seat": "A", "nick": "Нико"})
        return super().__call__(request)

    def after_spend(self) -> list[dict[str, Any]]:
        return [s for s in self.spend if s.get("phase") == "after_game_over"]

    def detail(self, kind: str) -> list[dict[str, Any]]:
        return [t["detail"] for t in self.telemetry if t["kind"] == kind]


class ScriptedDreamTeam:
    """A session that costs 0.10 more per summary read. After game_over its summaries follow `after` (a list of
    {state, final, spend}; the last one repeats): `spend` is what that read adds."""

    def __init__(self, clock: Clock, after: list[dict[str, Any]] | None = None, *, final_at_over: bool = False,
                 over_state: str | None = None, over_continue: tuple[int, dict[str, Any]] | None = None,
                 stop_reply: tuple[int, dict[str, Any]] | None = None):
        """A step with `"down": True` makes that read fail as unreachable (no spend). `over_state`: the state of the
        read at game_over; `over_continue`/`stop_reply`: (status, body) answers to continue at/after game_over and
        to stop."""
        self.clock = clock
        self.after = list(after or [{"state": "running", "final": False, "spend": 0.05}])
        self.final_at_over = final_at_over
        self.over_state = over_state
        self.over_continue = over_continue
        self.stop_reply = stop_reply
        self.cost = 0.0
        self.continues: list[tuple[float, str]] = []
        self.messages: list[tuple[float, str]] = []  # wakes sent to all members (`--wake-mode all`, the default)
        self.stops: list[httpx.Request] = []
        self.reads_after = 0

    def _summary(self) -> dict[str, Any]:
        if self.clock.now < GAME_END:
            self.cost = round(self.cost + 0.10, 2)
            return {"id": SESSION, "state": "running", "final": False, "cost_usd": self.cost}
        if self.reads_after == 0:  # the read the waker makes at game_over
            self.reads_after += 1
            self.cost = round(self.cost + 0.10, 2)
            state = self.over_state or ("done" if self.final_at_over else "running")
            return {"id": SESSION, "state": state, "final": self.final_at_over, "cost_usd": self.cost}
        step = self.after[min(self.reads_after - 1, len(self.after) - 1)]
        self.reads_after += 1
        if step.get("down"):
            raise httpx.ConnectError("DreamTeam is down")
        self.cost = round(self.cost + step["spend"], 2)
        return {"id": SESSION, "state": step["state"], "final": step["final"], "cost_usd": self.cost}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/api/v1/sessions/{SESSION}":
            return httpx.Response(200, json={"session": self._summary()})
        if request.method == "POST" and path == f"/api/v1/sessions/{SESSION}/continue":
            self.continues.append((self.clock.now, json.loads(request.content)["text"]))
            if self.over_continue and self.clock.now >= GAME_END:
                return httpx.Response(self.over_continue[0], json=self.over_continue[1])
            return httpx.Response(200, json={"session": {"id": SESSION, "state": "running", "cost_usd": self.cost}})
        if request.method == "POST" and path == f"/api/v1/sessions/{SESSION}/messages":
            self.messages.append((self.clock.now, json.loads(request.content)["text"]))
            if self.over_continue and self.clock.now >= GAME_END:
                return httpx.Response(self.over_continue[0], json=self.over_continue[1])
            return httpx.Response(201, json={"message": {"id": len(self.messages), "to": ["all"]}})
        if request.method == "POST" and path == f"/api/v1/sessions/{SESSION}/stop":
            self.stops.append(request)
            if self.stop_reply:
                return httpx.Response(self.stop_reply[0], json=self.stop_reply[1])
            return httpx.Response(200, json={"session": {"id": SESSION, "state": "stopped", "final": False,
                                                         "cost_usd": self.cost}})
        return httpx.Response(404, json={"error": "no", "code": "not_found"})


def run_waker(game: OverGame, dt: ScriptedDreamTeam, clock: Clock, **kwargs: Any) -> Waker:
    waker = Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game)),
                  DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
                  SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None, **kwargs)
    waker.run()
    return waker


def wrap_ups(dt: ScriptedDreamTeam) -> list[str]:
    return [text for _, text in dt.continues + dt.messages if text.startswith("[DAH] Партия окончена")]


# ---------------------------------------------------------------- waker: the wrap-up continue


def test_wrap_up_continue_is_sent_once_with_the_result():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.05},
                                   {"state": "done", "final": True, "spend": 0.02}])
    run_waker(game, dt, clock)

    texts = wrap_ups(dt)
    # six phase wakes (open + reminder) and the wrap-up, all as messages to every member; no continue
    assert len(texts) == 1 and len(dt.messages) == 7 and dt.continues == []
    assert texts[0] == ("[DAH] Партия окончена: ход 1, лимит ходов, победитель B; ты A, твоё место 2 из 3; счёт: "
                        "B 12, A 9, C 4. Заверши цель одним коротким итоговым сообщением, больше ничего не делай.")
    assert dt.messages[-1][0] == GAME_END
    assert dt.stops == []
    assert game.detail("wake_sent")[-1]["reason"] == "game_over"
    assert [d["reason"] for d in game.detail("wake_sent")].count("game_over") == 1
    finished = game.detail("post_game_finished")
    assert len(finished) == 1 and finished[0]["reason"] == "stopped"


def test_wrap_up_text_without_a_seat_or_a_result():
    text = wrap_up_text({"game_over": True, "turn": 4, "result": {"winners": [], "reason": "last_standing",
                                                                  "scores": {"A": 0}}})
    assert text.startswith("[DAH] Партия окончена: ход 4, остался последний, победителей нет; счёт: A 0.")
    assert "твоё место" not in text
    assert wrap_up_text({}).startswith("[DAH] Партия окончена: итог в dah.phase(). Заверши цель")


def test_session_already_final_at_game_over_is_neither_woken_nor_stopped():
    clock = Clock()
    game, dt = OverGame(clock), ScriptedDreamTeam(clock, final_at_over=True)
    run_waker(game, dt, clock, stop_on_game_over=True)
    assert wrap_ups(dt) == [] and dt.stops == []
    assert game.detail("continue_skipped")[-1]["reason"] == "session_final"
    finished = game.detail("post_game_finished")[0]
    assert finished["reason"] == "stopped" and finished["duration_s"] == 0 and finished["polls"] == 0
    assert game.after_spend() == []


# ---------------------------------------------------------------- waker: --stop-on-game-over


def test_stop_flag_stops_the_session_instead_of_the_wrap_up():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "stopped", "final": True, "spend": 0.01}])
    run_waker(game, dt, clock, stop_on_game_over=True)

    assert wrap_ups(dt) == [] and len(dt.messages) == 6
    assert len(dt.stops) == 1
    stop = dt.stops[0]
    assert stop.headers["content-type"] == "application/json" and json.loads(stop.content) == {}
    assert stop.headers["authorization"] == f"Bearer {TOKEN}"
    stopped = game.detail("session_stopped_by_waker")
    assert len(stopped) == 1 and stopped[0]["state"] == "stopped" and stopped[0]["state_before"] == "running"
    finished = game.detail("post_game_finished")[0]
    assert finished["reason"] == "stopped" and finished["duration_s"] == 30 and finished["polls"] == 1
    assert [s["spent_usd"] for s in game.after_spend()] == [dt.cost]


def test_cli_flags():
    args = parse_args(["--server", "http://x", "--key", "k", "--dt-session", "s"])
    assert args.stop_on_game_over is False and args.post_game_minutes == 30
    args = parse_args(["--server", "http://x", "--key", "k", "--dt-session", "s", "--stop-on-game-over",
                       "--post-game-minutes", "5"])
    assert args.stop_on_game_over is True and args.post_game_minutes == 5


# ---------------------------------------------------------------- waker: post-game spend


def test_post_game_spend_is_polled_every_30_s_until_the_session_stops():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.05},
                                   {"state": "running", "final": False, "spend": 0.04},
                                   {"state": "done", "final": True, "spend": 0.01}])
    run_waker(game, dt, clock)

    finished = game.detail("post_game_finished")[0]
    cost_at_over = finished["cost_at_game_over_usd"]
    in_game = [s for s in game.spend if s.get("phase") != "after_game_over"]
    assert in_game[-1]["spent_usd"] == cost_at_over
    after = game.after_spend()
    assert [s["spent_usd"] for s in after] == [round(cost_at_over + d, 2) for d in (0.05, 0.09, 0.10)]
    assert all(s["source"] == "participant_program" and "turn" not in s for s in after)
    assert clock.sleeps[-3:] == [30, 30, 30]
    assert finished["reason"] == "stopped" and finished["duration_s"] == 90 and finished["polls"] == 3
    assert finished["spent_delta_usd"] == 0.1
    assert finished["cost_usd"] == round(cost_at_over + 0.1, 2) and finished["state"] == "done"
    assert clock.now == GAME_END + 90


def test_post_game_watch_ends_when_the_session_is_idle():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.03},
                                   {"state": "done", "final": False, "spend": 0.0}])
    run_waker(game, dt, clock)
    finished = game.detail("post_game_finished")[0]
    # idle takes two reads in a row with `done` and no new spend
    assert finished["reason"] == "idle" and finished["duration_s"] == 90 and finished["spent_delta_usd"] == 0.03
    assert [s["spent_usd"] for s in game.after_spend()] == [round(finished["cost_at_game_over_usd"] + 0.03, 2)]  # an unchanged cost is not posted again


def test_post_game_watch_stops_after_the_limit():
    clock = Clock()
    game, dt = OverGame(clock), ScriptedDreamTeam(clock)  # keeps running and spending
    run_waker(game, dt, clock, post_game_minutes=2)
    finished = game.detail("post_game_finished")[0]
    assert finished["reason"] == "timeout" and finished["duration_s"] == 120 and finished["polls"] == 4
    assert len(game.after_spend()) == 4 and finished["spent_delta_usd"] == 0.2
    assert clock.now == GAME_END + 120


def test_post_game_limit_zero_still_sends_the_wrap_up():
    clock = Clock()
    game, dt = OverGame(clock), ScriptedDreamTeam(clock)
    run_waker(game, dt, clock, post_game_minutes=0)
    assert len(wrap_ups(dt)) == 1 and game.after_spend() == []
    assert game.detail("post_game_finished")[0]["reason"] == "timeout"


# ---------------------------------------------------------------- server


def test_server_accepts_spend_after_game_over(env: Env):
    early = env.call("A", "POST", "/v1/spend", json={"spent_usd": 0.1, "source": "participant_program",
                                                     "phase": "after_game_over"})
    assert early["error"]["code"] == "INVALID" and "game_over" in early["error"]["message"]
    game = env.game()
    game.state.phase = "game_over"
    env.ok("A", "POST", "/v1/spend", json={"spent_usd": 0.42, "source": "participant_program",
                                           "phase": "after_game_over"})
    env.ok("A", "POST", "/v1/spend", json={"spent_usd": 0.5, "source": "participant_program", "turn": 1,
                                           "phase": "after_game_over"})
    bad_turn = env.call("A", "POST", "/v1/spend", json={"spent_usd": 0.5, "source": "participant_program",
                                                        "turn": 99, "phase": "after_game_over"})
    assert bad_turn["error"]["code"] == "INVALID"
    reports = [e for e in read_events(env.gdir.journal_path) if e["type"] == "spend_report"]
    assert [(r["spent_usd"], r["for_turn"], r["for_phase"], r["at_phase"]) for r in reports] == [
        (0.42, 1, "after_game_over", "game_over"), (0.5, 1, "after_game_over", "game_over")]
    calls = [e for e in read_events(env.gdir.journal_path) if e["type"] == "call" and e["route"] == "POST /v1/spend"]
    assert all(c["host"] for c in calls)


# ---------------------------------------------------------------- report


def _events() -> list[dict[str, Any]]:
    seats = [{"seat": "A", "nick": "Нико", "kind": "swarm"}, {"seat": "B", "nick": "Соло", "kind": "agent"}]
    evs: list[dict[str, Any]] = [
        {"type": "game_created", "ts": 0, "config": {"game_id": "g1", "scenario": "core:slice2", "seats": seats,
                                                     "turn_limit": 1}},
        {"type": "phase_open", "ts": 100, "turn": 1, "phase": "negotiation_1"},
        {"type": "spend_report", "ts": 150, "seat": "A", "spent_usd": 0.3, "for_turn": 1,
         "for_phase": "negotiation_1"},
        {"type": "phase_close", "ts": 400, "turn": 1, "phase": "negotiation_1", "reason": "all_ready",
         "not_ready": []},
        {"type": "game_over", "ts": 3700, "turn": 1, "winners": ["A"], "reason": "turn_limit", "scores": {}},
        {"type": "spend_report", "ts": 3701, "seat": "A", "spent_usd": 0.67, "for_turn": 1,
         "for_phase": "negotiation_2"},
        {"type": "spend_report", "ts": 3730, "seat": "A", "spent_usd": 0.8, "for_turn": 1,
         "for_phase": "after_game_over"},
        {"type": "spend_report", "ts": 4400, "seat": "A", "spent_usd": 0.95, "for_turn": 1,
         "for_phase": "after_game_over"},
        {"type": "telemetry", "ts": 4450, "seat": "A", "kind": "post_game_finished",
         "detail": {"reason": "stopped", "duration_s": 750.0, "spent_delta_usd": 0.28}},
        {"type": "telemetry", "ts": 4450, "seat": "B", "kind": "post_game_finished",
         "detail": {"reason": "idle", "duration_s": 30.0, "spent_delta_usd": 0.0}},
    ]
    return [{"seq": i + 1, **e} for i, e in enumerate(evs)]


def test_report_splits_money_and_time_at_game_over(tmp_path):
    report = build_report(_events())
    a, b = report["totals"]["A"], report["totals"]["B"]
    assert report["in_game_s"] == 3600
    assert a["spent_usd"] == 0.95 and a["spent_in_game_usd"] == 0.67
    assert a["spent_after_game_usd"] == 0.28 and a["after_game_s"] == 750.0 and a["in_game_s"] == 3600
    assert b["spent_usd"] is None and b["spent_in_game_usd"] is None
    # both times on the journal clock: game_over (3700) to post_game_finished (4450); the waker's own measure apart
    assert b["spent_after_game_usd"] == 0.0 and b["after_game_s"] == 750 and b["post_game_watch_s"] == 30.0
    assert report["turns"][0]["spent_usd"] == {"A": 0.67}  # after-game figures count in no turn or phase
    assert report["phases"][0]["spent_usd"] == {"A": 0.3}
    text = render_report(report)
    assert "деньги $0.95 (во время партии $0.67, 3600.0с; после game_over $0.28, 750.0с)" in text
    assert "деньги — (во время партии —, 3600.0с; после game_over $0.00, 750.0с)" in text

    path = tmp_path / "journal.jsonl"
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in _events()), encoding="utf-8")
    totals = build_timings(path)["totals"]["A"]
    assert (totals["spent_in_game_usd"], totals["spent_after_game_usd"], totals["in_game_s"],
            totals["after_game_s"]) == (0.67, 0.28, 3600, 750.0)


def test_report_after_game_falls_back_to_the_last_figure_time():
    evs = [e for e in _events() if e["type"] != "telemetry"]
    a = build_report(evs)["totals"]["A"]
    assert a["spent_after_game_usd"] == 0.28 and a["after_game_s"] == 700


def test_report_of_an_unfinished_game_has_no_split():
    evs = [e for e in _events() if e["ts"] < 3700]
    report = build_report(evs)
    a = report["totals"]["A"]
    assert a["spent_in_game_usd"] == 0.3 and a["spent_after_game_usd"] is None and a["in_game_s"] is None
    assert "во время партии" not in render_report(report)


# ---------------------------------------------------------------- waker: more post-game paths


def test_waker_started_after_game_over_sends_no_second_wrap_up():
    for stop in (False, True):
        clock = Clock()
        clock.now = GAME_END + 100  # a restart after a crash, or a waker started late
        game = OverGame(clock)
        dt = ScriptedDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.02},
                                       {"state": "done", "final": True, "spend": 0.01}])
        waker = run_waker(game, dt, clock, stop_on_game_over=stop)
        assert waker.started_after_game_over
        assert dt.continues == dt.messages == [] and dt.stops == []
        assert game.detail("continue_skipped")[-1]["reason"] == "started_after_game_over"
        assert "wake_sent" not in game.kinds()
        # every figure of this process is after-game money, the first one (the baseline) too
        assert [s.get("phase") for s in game.spend] == ["after_game_over"] * 3
        assert all("turn" not in s for s in game.spend)
        finished = game.detail("post_game_finished")[0]
        assert finished["reason"] == "stopped" and finished["started_after_game_over"] is True
        assert finished["spent_delta_usd"] == 0.03


def test_wrap_up_is_skipped_when_the_budget_is_already_exhausted():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "budget", "final": True, "spend": 0.0}], over_state="budget")
    waker = run_waker(game, dt, clock)
    assert waker.budget_exhausted and wrap_ups(dt) == []
    assert game.detail("continue_skipped")[-1]["reason"] == "budget_exhausted"
    assert game.detail("post_game_finished")[0]["reason"] == "stopped"


def test_wrap_up_refused_with_budget_exhausted():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "budget", "final": True, "spend": 0.0}],
                           over_continue=(409, {"error": "budget", "code": "budget_exhausted"}))
    waker = run_waker(game, dt, clock)
    assert len(wrap_ups(dt)) == 1 and waker.budget_exhausted
    exhausted = game.detail("budget_exhausted")
    assert len(exhausted) == 1 and exhausted[0]["reason"] == "game_over" and exhausted[0]["via"] == "messages"
    assert [d["reason"] for d in game.detail("wake_sent")].count("game_over") == 0
    assert "wake_failed" not in game.kinds()


def test_dreamteam_unreachable_after_game_over_runs_on_to_the_limit():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"down": True}])
    run_waker(game, dt, clock, post_game_minutes=2)
    finished = game.detail("post_game_finished")[0]
    assert finished["reason"] == "timeout" and finished["polls"] == 4 and finished["spent_delta_usd"] == 0.0
    assert game.after_spend() == [] and clock.now == GAME_END + 120


def test_stop_failure_is_retried_then_reported():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.0}],
                           stop_reply=(503, {"error": "busy", "code": "unavailable"}))
    run_waker(game, dt, clock, stop_on_game_over=True, post_game_minutes=1)
    assert len(dt.stops) == 4
    failed = game.detail("wake_failed")
    assert failed == [{"reason": "stop", "phase": "game_over", "status": 503, "code": "unavailable", "attempts": 4}]
    assert "session_stopped_by_waker" not in game.kinds()
    assert game.detail("post_game_finished")[0]["reason"] == "timeout"


def test_waiting_without_spend_is_not_idle():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "waiting", "final": False, "spend": 0.0},
                                   {"state": "waiting", "final": False, "spend": 0.0},
                                   {"state": "paused", "final": False, "spend": 0.0},
                                   {"state": "running", "final": False, "spend": 0.07},
                                   {"state": "done", "final": True, "spend": 0.0}])
    run_waker(game, dt, clock)
    finished = game.detail("post_game_finished")[0]
    assert finished["reason"] == "stopped" and finished["polls"] == 5 and finished["spent_delta_usd"] == 0.07
    assert [s["spent_usd"] for s in game.after_spend()] == [finished["cost_usd"]]


def test_done_once_then_more_spend_is_not_idle():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "done", "final": False, "spend": 0.0},
                                   {"state": "running", "final": False, "spend": 0.04},
                                   {"state": "done", "final": False, "spend": 0.0}])
    run_waker(game, dt, clock)
    finished = game.detail("post_game_finished")[0]
    assert finished["reason"] == "idle" and finished["polls"] == 4 and finished["spent_delta_usd"] == 0.04


class FlakySpendGame(OverGame):
    """Refuses the first after_game_over spend post with a 503."""

    failed = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (request.url.path == "/v1/spend" and not self.failed
                and json.loads(request.content).get("phase") == "after_game_over"):
            self.failed = True
            return httpx.Response(503, json={"ok": False, "error": {"code": "busy", "message": "busy"}})
        return super().__call__(request)


def test_failed_after_game_spend_is_posted_again_before_the_watch_ends():
    clock = Clock()
    game = FlakySpendGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "done", "final": True, "spend": 0.05}])
    run_waker(game, dt, clock)
    assert game.failed
    finished = game.detail("post_game_finished")[0]
    assert finished["reason"] == "stopped" and finished["polls"] == 1
    assert [s["spent_usd"] for s in game.after_spend()] == [finished["cost_usd"]]


def test_ctrl_c_during_the_watch_still_records_post_game_finished():
    clock = Clock()
    game = OverGame(clock)
    dt = ScriptedDreamTeam(clock)

    def sleep(seconds: float) -> None:
        if clock.now >= GAME_END:
            raise KeyboardInterrupt
        clock.sleep(seconds)

    waker = Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game)),
                  DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
                  SESSION, clock=clock, sleep=sleep, log=lambda message: None)
    with pytest.raises(KeyboardInterrupt):
        waker.run()
    assert len(wrap_ups(dt)) == 1
    finished = game.detail("post_game_finished")
    assert len(finished) == 1 and finished[0]["reason"] == "interrupted" and finished[0]["polls"] == 0


# ---------------------------------------------------------------- report: more money paths


def test_report_counts_a_baseline_taken_after_game_over_as_after_game_money():
    evs = [e for e in _events() if e["type"] != "telemetry" and e.get("for_phase") != "after_game_over"]
    evs.append({"seq": 99, "type": "spend_report", "ts": 3900, "seat": "A", "spent_usd": 0.9, "for_turn": None,
                "for_phase": None, "turn": 1, "phase": "game_over", "at_turn": 1, "at_phase": "game_over"})
    report = build_report(evs)
    a = report["totals"]["A"]
    assert a["spent_usd"] == 0.9 and a["spent_in_game_usd"] == 0.67
    assert a["spent_after_game_usd"] == 0.23 and a["after_game_s"] == 200
    assert report["turns"][0]["spent_usd"] == {"A": 0.67}


def test_report_falls_back_to_the_waker_delta_and_its_cost_at_game_over():
    seats = [{"seat": "A", "nick": "Нико", "kind": "swarm"}, {"seat": "H", "nick": "Человек", "kind": "human"}]
    evs: list[dict[str, Any]] = [
        {"type": "game_created", "ts": 0, "config": {"game_id": "g1", "scenario": "s", "seats": seats}},
        {"type": "phase_open", "ts": 100, "turn": 1, "phase": "negotiation_1"},
        {"type": "game_over", "ts": 700, "turn": 1, "winners": ["A"], "reason": "turn_limit", "scores": {}},
        # every in-game spend read failed: only after-game figures are journaled
        {"type": "spend_report", "ts": 760, "seat": "A", "spent_usd": 1.3, "for_turn": 1,
         "for_phase": "after_game_over"},
        {"type": "telemetry", "ts": 800, "seat": "A", "kind": "post_game_finished",
         "detail": {"reason": "stopped", "duration_s": 60.0, "spent_delta_usd": 0.1, "cost_at_game_over_usd": None}},
    ]
    report = build_report([{"seq": i + 1, **e} for i, e in enumerate(evs)])
    a, h = report["totals"]["A"], report["totals"]["H"]
    assert a["spent_in_game_usd"] is None and a["spent_after_game_usd"] == 0.1 and a["after_game_s"] == 100
    assert h["spent_after_game_usd"] is None and h["after_game_s"] is None
    text = render_report(report)
    assert "после game_over $0.10, 100.0с)" in text
    human = next(line for line in text.splitlines() if line.startswith("H "))
    assert "во время партии" not in human  # no after-game data: no split, as on /watch

    # the game_over post was lost: the in-game figure is stale, the waker's cost at game_over is the base
    evs[2:2] = [{"type": "spend_report", "ts": 400, "seat": "A", "spent_usd": 0.8, "for_turn": 1,
                 "for_phase": "negotiation_1"}]
    evs[-1]["detail"]["cost_at_game_over_usd"] = 1.2
    a = build_report([{"seq": i + 1, **e} for i, e in enumerate(evs)])["totals"]["A"]
    assert a["spent_in_game_usd"] == 1.2 and a["spent_after_game_usd"] == 0.1


def test_watch_shows_the_split_only_with_after_game_data(tmp_path):
    evs = _events()
    evs[0]["config"]["seats"].append({"seat": "H", "nick": "Человек", "kind": "human"})
    path = tmp_path / "journal.jsonl"
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs), encoding="utf-8")
    totals = build_timings(path)["totals"]
    assert totals["A"]["spent_after_game_usd"] == 0.28 and totals["A"]["after_game_s"] == 750
    assert totals["H"]["spent_after_game_usd"] is None and totals["H"]["after_game_s"] is None
    html = (Path(__file__).parent.parent / "src" / "dah" / "server" / "watch.html").read_text(encoding="utf-8")
    assert "if (v.spent_after_game_usd != null || v.after_game_s != null)" in html
