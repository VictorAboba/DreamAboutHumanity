import json
from typing import Any

import httpx
import pytest

from dah.bridge.client import GameClient
from dah.waker import main as waker_main
from dah.waker.core import Waker
from dah.waker.dreamteam import DreamTeamClient
from dah.waker.text import wake_text

KEY = "dah_g1_A_seatkey"
TOKEN = "dt-api-token-xyz"
SESSION = "sess1"
T0 = 1_800_000_000.0
PHASES = ("negotiation_1", "actions", "negotiation_2")


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeGame:
    """Phases of `length` seconds back to back for `turns` turns, then game_over. Long polls move the clock."""

    def __init__(self, clock: Clock, *, turns: int = 1, length: float = 600, ready_after: float | None = None,
                 alive: bool = True):
        self.clock = clock
        self.alive = alive
        self.length = length
        self.ready_after = ready_after
        self.phases = [(turn, phase) for turn in range(1, turns + 1) for phase in PHASES]
        self.requests: list[httpx.Request] = []
        self.telemetry: list[dict[str, Any]] = []
        self.spend: list[dict[str, Any]] = []

    def _index(self) -> int:
        return int((self.clock.now - T0) // self.length)

    def _info(self) -> dict[str, Any]:
        index = self._index()
        if index >= len(self.phases):
            return {"ok": True, "game_over": True, "phase": "game_over", "phase_seq": index + 1}
        turn, phase = self.phases[index]
        start = T0 + index * self.length
        deadline = start + self.length
        return {"ok": True, "turn": turn, "turn_limit": 10, "phase": phase, "deadline": deadline,
                "seconds_left": deadline - self.clock.now, "ready": "0/2", "phase_seq": index + 1,
                "you_ready": self.ready_after is not None and self.clock.now - start >= self.ready_after,
                "allowed": [], "alive": self.alive, "game_over": False}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/phase":
            return httpx.Response(200, json=self._info())
        if path == "/v1/wait_phase":
            if "after_seq" not in request.url.params:
                return httpx.Response(200, json={**self._info(), "changed": True})
            after = int(request.url.params["after_seq"])
            wait = float(request.url.params["max_seconds"])
            next_start = T0 + after * self.length
            if next_start <= self.clock.now + wait:
                self.clock.now = max(self.clock.now, next_start)
                return httpx.Response(200, json={**self._info(), "changed": True})
            self.clock.now += wait
            return httpx.Response(200, json={**self._info(), "changed": False})
        if path == "/v1/telemetry":
            self.telemetry.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if path == "/v1/spend":
            self.spend.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False, "error": {"code": "not_found", "message": path}})

    def kinds(self) -> list[str]:
        return [t["kind"] for t in self.telemetry]


class FakeDreamTeam:
    def __init__(self, clock: Clock, continue_replies: list[tuple[int, dict[str, Any]]] | None = None):
        self.clock = clock
        self.replies = list(continue_replies or [])
        self.session: dict[str, Any] = {"id": SESSION, "state": "running", "final": False, "cost_usd": 0.0,
                                        "max_cost_usd": 5.0, "goal": {"status": "in_progress"}, "error": None}
        self.requests: list[httpx.Request] = []
        self.continues: list[tuple[float, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == f"/api/v1/sessions/{SESSION}":
            self.session["cost_usd"] = round(self.session["cost_usd"] + 0.25, 2)
            return httpx.Response(200, json={"session": dict(self.session)})
        if request.method == "POST" and request.url.path == f"/api/v1/sessions/{SESSION}/continue":
            self.continues.append((self.clock.now, json.loads(request.content)["text"]))
            if self.replies:
                status, body = self.replies.pop(0)
                return httpx.Response(status, json=body)
            return httpx.Response(200, json={"session": {**self.session, "state": "running"}})
        return httpx.Response(404, json={"error": "no", "code": "not_found"})


def make_waker(game: FakeGame, dt: FakeDreamTeam, clock: Clock) -> Waker:
    return Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game)),
                 DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
                 SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None)


def run(game: FakeGame, dt: FakeDreamTeam, clock: Clock) -> Waker:
    waker = make_waker(game, dt, clock)
    for _ in range(10_000):
        if waker.step():
            return waker
    raise AssertionError("waker never saw game_over")


def test_wakes_on_phase_open_and_reminds_before_deadline():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    run(game, dt, clock)

    assert len(dt.continues) == 6
    opens, reminders = dt.continues[0::2], dt.continues[1::2]
    for index, (at, text) in enumerate(opens):
        assert at == T0 + index * 600
        assert text.startswith("[DAH] Ход 1/10, фаза ")
        assert "dah.ready" in text
    for index, (at, text) in enumerate(reminders):
        assert at == T0 + index * 600 + 540
        assert text.startswith("[DAH] Напоминание")
        assert "ещё 1 мин" in text
    assert "фаза ДЕЙСТВИЯ" in opens[1][1]
    assert game.kinds() == ["continue_sent"] * 6
    assert clock.sleeps == []


def test_ready_seat_gets_no_reminder():
    clock = Clock()
    game, dt = FakeGame(clock, ready_after=100), FakeDreamTeam(clock)
    run(game, dt, clock)
    assert [text for _, text in dt.continues if "Напоминание" in text] == []
    assert len(dt.continues) == 3


def test_spend_reported_after_each_phase_change():
    clock = Clock()
    game, dt = FakeGame(clock, turns=2), FakeDreamTeam(clock)
    run(game, dt, clock)
    assert len(game.spend) == 6 + 1  # each phase opening and game over
    assert all(s["source"] == "participant_program" for s in game.spend)
    spent = [s["spent_usd"] for s in game.spend]
    assert spent == sorted(spent) and spent[0] > 0
    # each figure is filed under the phase that just closed (the first one has none before it)
    closed = [(s.get("turn"), s.get("phase")) for s in game.spend]
    assert closed == [(None, None), *[(t, p) for t in (1, 2) for p in PHASES]]


def test_first_poll_is_a_host_wait_and_carries_host_header():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    waker = Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game), host="waker"),
                  DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
                  SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None)
    waker.step()
    assert game.requests[0].url.path == "/v1/wait_phase" and "after_seq" not in game.requests[0].url.params
    assert all(r.headers["X-DAH-Host"] == "waker" for r in game.requests)
    assert not any(r.url.path == "/v1/phase" for r in game.requests)


def test_eliminated_seat_is_not_woken():
    clock = Clock()
    game, dt = FakeGame(clock, alive=False), FakeDreamTeam(clock)
    run(game, dt, clock)
    assert dt.continues == [] and game.kinds() == []


def test_budget_exhausted_stops_waking():
    clock = Clock()
    game = FakeGame(clock)
    dt = FakeDreamTeam(clock, [(409, {"error": "budget is spent", "code": "budget_exhausted"})])
    waker = run(game, dt, clock)
    assert len(dt.continues) == 1
    assert game.kinds() == ["budget_exhausted"]
    assert waker.budget_exhausted


def test_budget_state_in_summary_stops_waking():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    dt.session["state"] = "budget"
    run(game, dt, clock)
    assert dt.continues == []
    assert game.kinds() == ["budget_exhausted"]
    assert game.telemetry[0]["detail"]["via"] == "session"


def test_retries_then_wake_failed():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    errors = [(503, {"error": "x", "code": "internal"}), (429, {"error": "x", "code": "too_many_sessions"}),
              (500, {"error": "x", "code": "internal"}), (503, {"error": "x", "code": "internal"})]
    dt = FakeDreamTeam(clock, errors)
    waker = make_waker(game, dt, clock)
    waker.step()
    assert clock.sleeps == [10, 30, 60]
    assert len(dt.continues) == 4
    assert game.kinds() == ["wake_failed"]
    assert game.telemetry[0]["detail"]["attempts"] == 4
    assert game.telemetry[0]["detail"]["status"] == 503


def test_retry_then_success():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, [(502, {})])
    make_waker(game, dt, clock).step()
    assert clock.sleeps == [10]
    assert game.kinds() == ["continue_sent"]


def test_refusal_is_not_retried():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, [(409, {"error": "refused", "code": "conflict"})])
    make_waker(game, dt, clock).step()
    assert clock.sleeps == []
    assert game.kinds() == ["wake_failed"]
    assert game.telemetry[0]["detail"]["code"] == "conflict"


def test_session_restarted_detected():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, [(200, {"session": {"id": SESSION, "state": "starting"}})])
    dt.session.update(state="stopped", final=True)
    make_waker(game, dt, clock).step()
    assert game.kinds() == ["continue_sent", "session_restarted"]
    detail = game.telemetry[1]["detail"]
    assert detail["state_before"] == "stopped" and detail["state_after"] == "starting"


def test_goal_reopened_detected():
    clock = Clock()
    game = FakeGame(clock, length=100_000)
    dt = FakeDreamTeam(clock, [(200, {"session": {"id": SESSION, "state": "starting"}})])
    dt.session.update(state="done", final=True, goal={"status": "done"})
    make_waker(game, dt, clock).step()
    assert game.kinds() == ["continue_sent", "goal_reopened", "session_restarted"]


def test_running_session_is_not_restarted():
    clock = Clock()
    game, dt = FakeGame(clock, length=100_000), FakeDreamTeam(clock)
    make_waker(game, dt, clock).step()
    assert game.kinds() == ["continue_sent"]


def test_secrets_stay_with_their_hosts():
    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    run(game, dt, clock)
    for request in game.requests:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert TOKEN not in request.content.decode() and TOKEN not in str(request.url)
    for request in dt.requests:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert KEY not in request.content.decode() and KEY not in str(request.url)


def test_game_errors_back_off_without_crashing():
    clock = Clock()
    calls = {"n": 0}
    game = FakeGame(clock, length=100_000)

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        return game(request)

    dt = FakeDreamTeam(clock)
    waker = Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(flaky)),
                  DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
                  SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None)
    assert waker.step() is False
    assert clock.sleeps == [5]
    waker.step()
    assert len(dt.continues) == 1


def test_wake_text_format():
    text = wake_text({"turn": 3, "turn_limit": 10, "phase": "actions", "deadline": "2026-11-23T14:30:00Z",
                      "seconds_left": 1200, "ready": "1/3"})
    assert text == ("[DAH] Ход 3/10, фаза ДЕЙСТВИЯ, до 14:30Z (ещё 20 мин). Готово 1/3. "
                    "Посмотри dah.get_state(since_turn=2), подай приказы, нажми dah.ready.")


def test_cli_requires_token_from_env(monkeypatch, capsys):
    monkeypatch.delenv("DREAMTEAM_API_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        waker_main.main(["--server", "http://game.test", "--key", KEY, "--dt-session", SESSION])
    assert exc.value.code == 2
    assert KEY not in capsys.readouterr().err
