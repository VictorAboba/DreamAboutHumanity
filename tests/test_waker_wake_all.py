"""Measurement m3: a wake reaches every member of the swarm (messages to ["all"]), falls back to `continue` for a
stopped session, and is followed up: `wake_answered` / `wake_unanswered` from the player's own game calls."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from test_measurement1_fixes import SEATS, Env, FakeClock, new_game, ready
from test_waker_loop import KEY, SESSION, T0, TOKEN, Clock, FakeDreamTeam, FakeGame, make_waker, run

from dah.bridge.client import GameClient
from dah.cli import goal_text
from slice_helpers import slice_config
from dah.report import build_report, render_report
from dah.waker.core import Waker
from dah.waker.dreamteam import DreamTeamClient
from dah.waker.main import parse_args

NOT_RUNNING = (409, {"error": "the session is not running", "code": "not_running"})


class PlayerGame(FakeGame):
    """FakeGame whose host phase info carries the player's presence: the player calls the game `call_after` seconds
    after each phase opens (None: never)."""

    def __init__(self, clock: Clock, *, call_after: float | None, **kwargs: Any):
        super().__init__(clock, **kwargs)
        self.call_after = call_after

    def _call_at(self, index: int) -> float | None:
        return None if self.call_after is None else T0 + index * self.length + self.call_after

    def _info(self) -> dict[str, Any]:
        info = super()._info()
        index = min(self._index(), len(self.phases))
        now = self.clock.now
        here = self._call_at(index) if not info.get("game_over") else None
        seen = here is not None and here <= now
        info.update(player_waiting=False, player_active_s_ago=round(now - here, 1) if seen else None,
                    player_first_active_s_ago=round(now - here, 1) if seen else None)
        last = None  # the last call of any phase so far
        for i in range(index, -1, -1):
            at = self._call_at(i)
            if at is not None and at <= now:
                last = {"phase_seq": i + 1, "s_ago": round(now - at, 1)}
                break
        info["player_last_call"] = last
        return info


# ---------------------------------------------------------------- delivery


def test_running_session_is_woken_by_a_message_to_all():
    clock = Clock()
    game, dt = FakeGame(clock, length=100_000), FakeDreamTeam(clock)
    make_waker(game, dt, clock).step()
    assert dt.continues == []
    assert len(dt.messages) == 1
    at, to, text = dt.messages[0]
    assert at == T0 and to == ["all"] and text.startswith("[DAH] Ход 1/10, фаза ПЕРЕГОВОРЫ 1")
    request = next(r for r in dt.requests if r.method == "POST")
    assert request.url.path == f"/api/v1/sessions/{SESSION}/messages"
    assert game.kinds() == ["wake_sent"]
    sent = game.detail("wake_sent")[0]
    assert sent == {"reason": "phase_open", "turn": 1, "phase": "negotiation_1", "mode": "all", "via": "messages",
                    "to": ["all"], "status": 201}


def test_not_running_message_falls_back_to_continue():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, [(200, {"session": {"id": SESSION, "state": "starting"}})], [NOT_RUNNING])
    make_waker(game, dt, clock).step()
    assert len(dt.messages) == 1 and len(dt.continues) == 1
    assert dt.continues[0][1] == dt.messages[0][2]  # the same [DAH] text
    assert game.kinds() == ["wake_sent"]  # no wake_failed for the expected 409
    sent = game.detail("wake_sent")[0]
    assert sent["via"] == "continue" and sent["fallback"] == "not_running" and sent["to"] == ["coordinator"]
    assert sent["status"] == 200 and sent["state"] == "starting" and sent["mode"] == "all"
    assert clock.sleeps == []


def test_stopped_session_goes_straight_to_continue_and_is_restarted():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, [(200, {"session": {"id": SESSION, "state": "starting"}})])
    dt.session.update(state="stopped", final=True)
    make_waker(game, dt, clock).step()
    assert dt.messages == [] and len(dt.continues) == 1
    assert game.kinds() == ["wake_sent", "session_restarted"]
    assert game.detail("wake_sent")[0]["fallback"] == "session_stopped"


def test_wake_mode_continue_sends_no_message():
    clock = Clock()
    game, dt = FakeGame(clock, length=100_000), FakeDreamTeam(clock)
    make_waker(game, dt, clock, wake_mode="continue").step()
    assert dt.messages == [] and len(dt.continues) == 1
    sent = game.detail("wake_sent")[0]
    assert sent["mode"] == "continue" and sent["via"] == "continue" and "fallback" not in sent


def test_wake_mode_both_sends_message_and_continue():
    clock = Clock()
    game, dt = FakeGame(clock, length=100_000), FakeDreamTeam(clock)
    make_waker(game, dt, clock, wake_mode="both").step()
    assert len(dt.messages) == 1 and len(dt.continues) == 1
    sent = game.detail("wake_sent")
    assert [(d["via"], d.get("also")) for d in sent] == [("messages", None), ("continue", True)]
    assert all(d["mode"] == "both" for d in sent)


def test_wake_mode_both_on_not_running_continues_once():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, message_replies=[NOT_RUNNING])
    make_waker(game, dt, clock, wake_mode="both").step()
    assert len(dt.messages) == 1 and len(dt.continues) == 1
    assert [d["via"] for d in game.detail("wake_sent")] == ["continue"]


def test_bad_wake_mode_is_refused():
    clock = Clock()
    with pytest.raises(ValueError):
        make_waker(FakeGame(clock), FakeDreamTeam(clock), clock, wake_mode="some")


def test_message_retried_on_5xx_then_sent():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, message_replies=[(503, {"error": "x", "code": "internal"})])
    make_waker(game, dt, clock).step()
    assert clock.sleeps == [10] and len(dt.messages) == 2 and dt.continues == []
    assert game.kinds() == ["wake_sent"]


def test_message_budget_exhausted_stops_waking():
    clock = Clock()
    game = FakeGame(clock)
    dt = FakeDreamTeam(clock, message_replies=[(409, {"error": "spent", "code": "budget_exhausted"})])
    waker = run(game, dt, clock)
    assert waker.budget_exhausted and len(dt.messages) == 1 and dt.continues == []
    assert game.kinds() == ["budget_exhausted"]
    assert game.detail("budget_exhausted")[0]["via"] == "messages"


def test_message_refused_otherwise_is_reported_and_continue_tried():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, message_replies=[(400, {"error": "unknown member", "code": "bad_request"})])
    make_waker(game, dt, clock).step()
    assert game.kinds() == ["wake_failed", "wake_sent"]
    failed, sent = game.detail("wake_failed")[0], game.detail("wake_sent")[0]
    assert failed["via"] == "messages" and failed["status"] == 400 and failed["code"] == "bad_request"
    assert sent["via"] == "continue" and sent["fallback"] == "bad_request"


def test_reminder_goes_to_all_too():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    run(game, dt, clock)
    reminders = [(at, to) for at, to, text in dt.messages if text.startswith("[DAH] Напоминание")]
    assert reminders == [(T0 + i * 600 + 540, ["all"]) for i in range(3)]
    assert dt.continues == []


# ---------------------------------------------------------------- wrap-up after game_over


def _play_to_game_over(game: FakeGame, dt: FakeDreamTeam, clock: Clock, **options: Any) -> Waker:
    waker = make_waker(game, dt, clock, post_game_minutes=0, **options)
    while not waker.step():
        pass
    return waker


def test_wrap_up_goes_to_all_as_a_message():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    waker = _play_to_game_over(game, dt, clock)
    before = len(dt.messages)
    waker.after_game_over()
    assert len(dt.messages) == before + 1 and dt.continues == []
    _, to, text = dt.messages[-1]
    assert to == ["all"] and text.startswith("[DAH] Партия окончена")
    assert game.detail("wake_sent")[-1]["reason"] == "game_over"


def test_wrap_up_does_not_restart_a_stopped_session():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock, message_replies=[])
    waker = _play_to_game_over(game, dt, clock, wake_mode="both")
    dt.message_replies.append(NOT_RUNNING)  # stopped between the summary read and the wrap-up
    before_continues = len(dt.continues)
    waker.after_game_over()
    assert len(dt.continues) == before_continues  # `both`: the wrap-up is a message only, no continue
    skipped = game.detail("continue_skipped")[-1]
    assert skipped["reason"] == "not_running" and skipped["phase"] == "game_over"


def test_wrap_up_skips_a_session_the_summary_shows_stopped():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    waker = _play_to_game_over(game, dt, clock)
    waker._session = {**dt.session, "state": "stopped", "final": False}
    before = len(dt.messages)
    waker.after_game_over()
    assert len(dt.messages) == before and dt.continues == []
    assert game.detail("continue_skipped")[-1]["reason"] == "not_running"


def test_wrap_up_with_wake_mode_continue_is_a_continue():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    waker = _play_to_game_over(game, dt, clock, wake_mode="continue")
    waker.after_game_over()
    assert dt.messages == [] and dt.continues[-1][1].startswith("[DAH] Партия окончена")


# ---------------------------------------------------------------- answered / unanswered


def acks(game: FakeGame) -> list[tuple[str, dict[str, Any]]]:
    return [(t["kind"], t["detail"]) for t in game.telemetry if t["kind"] in ("wake_answered", "wake_unanswered")]


def test_wake_answered_when_the_player_calls_soon():
    clock = Clock()
    game, dt = PlayerGame(clock, call_after=40, ready_after=100), FakeDreamTeam(clock)
    run(game, dt, clock)
    found = acks(game)
    assert [kind for kind, _ in found] == ["wake_answered"] * 3
    assert [(d["turn"], d["phase"], d["seconds"], d["reason"]) for _, d in found] == [
        (1, phase, 40.0, "phase_open") for phase in ("negotiation_1", "actions", "negotiation_2")]


def test_wake_unanswered_after_ack_seconds():
    clock = Clock()
    game, dt = PlayerGame(clock, call_after=None, ready_after=300), FakeDreamTeam(clock)
    run(game, dt, clock)
    found = acks(game)
    assert [kind for kind, _ in found] == ["wake_unanswered"] * 3
    assert all(d["seconds"] == 120.0 and d["ack_seconds"] == 120.0 for _, d in found)
    assert [d["phase"] for _, d in found] == ["negotiation_1", "actions", "negotiation_2"]


def test_call_after_the_window_is_unanswered():
    clock = Clock()
    game, dt = PlayerGame(clock, call_after=200, ready_after=300), FakeDreamTeam(clock)
    run(game, dt, clock)
    assert [kind for kind, _ in acks(game)] == ["wake_unanswered"] * 3


def test_ack_seconds_is_configurable():
    clock = Clock()
    game, dt = PlayerGame(clock, call_after=40, ready_after=300), FakeDreamTeam(clock)
    run(game, dt, clock, ack_seconds=30)
    assert [(kind, d["seconds"]) for kind, d in acks(game)] == [("wake_unanswered", 30.0)] * 3


def test_call_just_before_the_phase_closes_is_answered():
    clock = Clock()
    # 200-second phases, ready at 130 (no reminder), the call at 197: the last in-phase poll (195) misses it
    game, dt = PlayerGame(clock, call_after=197, length=200, ready_after=130), FakeDreamTeam(clock)
    run(game, dt, clock, ack_seconds=300)
    found = acks(game)
    assert [(kind, d["seconds"]) for kind, d in found] == [("wake_answered", 197.0)] * 3


def test_phase_closed_without_a_call_is_unanswered():
    clock = Clock()
    game, dt = PlayerGame(clock, call_after=None, length=200, ready_after=130), FakeDreamTeam(clock)
    run(game, dt, clock, ack_seconds=300)
    found = acks(game)
    assert [kind for kind, _ in found] == ["wake_unanswered"] * 3
    assert all(d["phase_closed"] is True and d["seconds"] == 200.0 for _, d in found)


def test_reminder_supersedes_an_unanswered_open_wake():
    clock = Clock()
    game, dt = PlayerGame(clock, call_after=None), FakeDreamTeam(clock)
    run(game, dt, clock, ack_seconds=1000)  # 600-second phases: the reminder at 540 comes first
    found = acks(game)
    assert [(d["reason"], d.get("superseded"), d.get("phase_closed")) for _, d in found[:2]] == [
        ("phase_open", True, None), ("reminder", None, True)]


def test_ack_polls_are_short_while_a_wake_is_pending():
    clock = Clock()
    game, dt = PlayerGame(clock, call_after=None, ready_after=300), FakeDreamTeam(clock)
    waker = make_waker(game, dt, clock)
    for _ in range(4):
        waker.step()
    waits = [float(r.url.params["max_seconds"]) for r in game.requests
             if r.url.path == "/v1/wait_phase" and "after_seq" in r.url.params]
    assert waits[:3] == [15, 15, 15]


# ---------------------------------------------------------------- server: host-only presence fields


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)



def test_host_presence_has_first_and_last_call(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    info = game.phase_info("A", host=True)
    assert info["player_first_active_s_ago"] is None and info["player_last_call"] is None
    game.note_player_activity("A")
    clock.now += 10
    game.note_player_activity("A")
    clock.now += 5
    info = game.phase_info("A", host=True)
    assert info["player_first_active_s_ago"] == 15.0 and info["player_active_s_ago"] == 5.0
    seq = game.phase_seq
    assert info["player_last_call"] == {"phase_seq": seq, "s_ago": 5.0}
    ready(game, "A")
    ready(game, "B")
    info = game.phase_info("A", host=True)
    assert game.phase_seq == seq + 1
    assert info["player_first_active_s_ago"] is None and info["player_active_s_ago"] is None
    assert info["player_last_call"] == {"phase_seq": seq, "s_ago": 5.0}  # the wake's phase, after it closed
    for field in ("player_first_active_s_ago", "player_last_call"):
        assert field not in game.phase_info("A")


def test_presence_fields_stay_off_player_requests(env: Env):
    plain = env.ok("A", "GET", "/v1/phase")
    assert not any(key.startswith("player_") for key in plain)
    other = env.ok("B", "GET", "/v1/phase", host=True)  # B's host view: B made no player call, A's is not shown
    assert other["player_last_call"] is None and other["player_first_active_s_ago"] is None
    host = env.ok("A", "GET", "/v1/phase", host=True)
    assert host["player_first_active_s_ago"] is not None


def test_server_accepts_the_new_telemetry_kinds(env: Env):
    for kind in ("wake_sent", "wake_answered", "wake_unanswered"):
        env.ok("A", "POST", "/v1/telemetry", host=True, json={"kind": kind, "detail": {"seconds": 1}})


# ---------------------------------------------------------------- report


def _report_events() -> list[dict[str, Any]]:
    config = {"game_id": "g1", "scenario": "core:slice2", "seats": SEATS}
    events: list[dict[str, Any]] = [{"type": "game_created", "config": config, "ts": 0.0}]

    def tele(seat: str, kind: str, **detail: Any) -> None:
        events.append({"type": "telemetry", "seat": seat, "kind": kind, "detail": detail, "turn": 1,
                       "phase": "actions", "ts": float(len(events))})

    tele("A", "wake_sent", reason="phase_open", via="messages")
    tele("A", "wake_answered", seconds=40.0)
    tele("A", "wake_sent", reason="reminder", via="messages")
    tele("A", "wake_sent", reason="reminder", via="continue", also=True)  # `both`: the same wake
    tele("A", "wake_unanswered", seconds=120.0)
    tele("A", "wake_sent", reason="phase_open", via="messages")
    tele("A", "wake_answered", seconds=10.0)
    tele("A", "wake_sent", reason="phase_open", via="continue")
    tele("A", "wake_answered", seconds=100.0)
    tele("A", "wake_sent", reason="game_over", via="messages")  # the wrap-up is not a phase wake
    tele("B", "continue_sent", reason="phase_open")  # an older waker's phase wake
    return events


def test_report_counts_wakes_per_seat():
    report = build_report(_report_events())
    a, b = report["totals"]["A"], report["totals"]["B"]
    assert (a["wakes_sent"], a["wakes_answered"], a["wakes_unanswered"]) == (4, 3, 1)
    assert a["wake_answer_median_s"] == 40.0
    assert (b["wakes_sent"], b["wakes_answered"], b["wakes_unanswered"], b["wake_answer_median_s"]) == (1, 0, 0, None)
    text = render_report(report)
    assert "пробуждения: отправлено 4, ответ 3, без ответа 1, медиана до хода 40.0с" in text
    assert "пробуждения: отправлено 1, ответ 0, без ответа 0, медиана до хода —" in text


# ---------------------------------------------------------------- goal text and CLI


def test_goal_text_says_dah_reaches_every_member():
    config = slice_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7)
    text = goal_text(config, "A")
    assert ("Сообщения [DAH] о смене фазы приходят всем участникам роя. Кто бы ни был дежурным, при [DAH] кто-то "
            "из вас должен сразу сделать ход в игре; если вы делите ходы по сменам — передайте [DAH] дежурному "
            "немедленно.") in text


def test_cli_wake_mode_and_ack_seconds():
    base = ["--server", "http://game.test", "--key", KEY, "--dt-session", SESSION]
    args = parse_args(base)
    assert args.wake_mode == "all" and args.ack_seconds == 120
    args = parse_args([*base, "--wake-mode", "both", "--ack-seconds", "60"])
    assert args.wake_mode == "both" and args.ack_seconds == 60
    with pytest.raises(SystemExit):
        parse_args([*base, "--wake-mode", "nobody"])
    with pytest.raises(SystemExit):
        parse_args([*base, "--ack-seconds", "-1"])


def test_message_carries_only_the_dreamteam_token():
    clock = Clock()
    game, dt = FakeGame(clock, length=100_000), FakeDreamTeam(clock)
    Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game)),
          DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
          SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None).step()
    posts = [r for r in dt.requests if r.method == "POST"]
    assert posts and all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in posts)
    assert all(KEY not in r.content.decode() for r in posts)
