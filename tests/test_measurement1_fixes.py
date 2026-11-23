"""Regressions for the issues found by measurement 1 (DreamTeam swarm vs two bots, core:slice3, 10 turns):
report labels and per-phase submissions, spend "for"/"at" fields, self-explanatory estimate/history/state/messages
errors with lenient ids, the waker skipping a `continue` the player does not need, and the DreamTeam session link.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from dah.bridge.client import GameClient
from dah.bridge.tools import Bridge
from dah.cli import goal_text, main
from dah.engine import orders_hash
from dah.game import Game, GameError, make_config
from dah.journal import Journal, read_events
from dah.report import build_report, render_report
from dah.server import create_app
from dah.store import GameDir, init_game_dir, read_json

SEATS = [{"seat": "A", "nick": "Нико", "kind": "swarm"}, {"seat": "B", "nick": "Соло", "kind": "agent"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def new_game(tmp_path, clock: FakeClock) -> Game:
    config = make_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7)
    gdir, _ = init_game_dir(tmp_path, config)
    return Game.create(gdir, clock)


def ready(game: Game, seat: str) -> None:
    game.set_ready(seat, orders_hash(game._book(seat)))


def events(game: Game, type_: str) -> list[dict]:
    return [e for e in read_events(game.gdir.journal_path) if e["type"] == type_]


class Env:
    def __init__(self, tmp_path) -> None:
        self.clock = FakeClock()
        config = make_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7)
        self.gdir, keys = init_game_dir(tmp_path, config)
        Game.create(self.gdir, self.clock)
        self.client = TestClient(create_app(str(tmp_path), admin_key="adm", clock=self.clock, tick_interval=None))
        self.headers = {seat: {"Authorization": f"Bearer {key}"} for seat, key in keys.items()}

    def call(self, seat: str, method: str, path: str, host: bool = False, **kwargs) -> dict:
        headers = {**self.headers[seat], **({"X-DAH-Host": "waker"} if host else {})}
        return self.client.request(method, path, headers=headers, **kwargs).json()

    def ok(self, seat: str, method: str, path: str, **kwargs) -> dict:
        data = self.call(seat, method, path, **kwargs)
        assert data["ok"], data
        return data

    def game(self) -> Game:
        return self.client.app.state.registry.games["g1"]


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


# ---------------------------------------------------------------- issues 1 and 2: report labels


def test_report_splits_no_orders_from_not_ready_and_counts_submissions_per_phase(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    cap = next(p.id for p in game.state.owned("B") if p.type == "core:capital")
    # П1: B submits (orders are accepted in П1), both press ready. Д: only A presses ready, the deadline closes
    # it with B not ready. П2: deadline with nobody ready. A never submitted anything all turn.
    game.submit("B", [{"kind": "fortify", "province": cap}], "append")
    ready(game, "A")
    ready(game, "B")
    assert game.phase == "actions"
    ready(game, "A")
    clock.now += 1200
    game.tick()
    assert game.phase == "negotiation_2"
    clock.now += 600
    game.tick()
    assert game.state.turn == 2

    closes = events(game, "phase_close")
    assert [c["not_ready"] for c in closes] == [[], ["B"], ["A", "B"]]

    report = build_report(read_events(game.gdir.journal_path))
    a, b = report["totals"]["A"], report["totals"]["B"]
    assert a["no_orders_turns"] == [1] and b["no_orders_turns"] == []
    assert a["not_ready_phases"] == [[1, "negotiation_2"]]
    assert b["not_ready_phases"] == [[1, "actions"], [1, "negotiation_2"]]
    assert b["submits_by_phase"] == {"negotiation_1": 1, "actions": 0, "negotiation_2": 0}
    text = render_report(report)
    assert "автопилот" not in text
    assert "не подал приказов в ходах [1]" in text
    assert "не нажал готово (закрыто по таймеру): 1 Д, 1 П2" in text
    assert "подач в П1 / Д / П2: 1 / 0 / 0" in text


def test_report_not_ready_falls_back_for_old_journals():
    seats = [{"seat": "A", "nick": "a", "kind": "agent"}, {"seat": "B", "nick": "b", "kind": "agent"}]
    evs = [
        {"seq": 1, "ts": 0, "type": "game_created", "config": {"game_id": "g", "scenario": "s", "seats": seats}},
        {"seq": 2, "ts": 1, "type": "phase_open", "turn": 1, "phase": "actions"},
        {"seq": 3, "ts": 2, "type": "ready", "seat": "A", "turn": 1, "phase": "actions"},
        {"seq": 4, "ts": 3, "type": "phase_close", "turn": 1, "phase": "actions", "reason": "deadline"},
    ]
    report = build_report(evs)
    assert report["totals"]["A"]["not_ready_phases"] == []
    assert report["totals"]["B"]["not_ready_phases"] == [[1, "actions"]]


# ---------------------------------------------------------------- issue 3: spend "for" and "at"


def test_spend_records_for_and_at_and_report_uses_for(env):
    env.ok("A", "POST", "/v1/spend", json={"spent_usd": 0.0, "source": "participant_program"})  # baseline
    env.game().set_ready("A", orders_hash(env.game()._book("A")))
    env.game().set_ready("B", orders_hash(env.game()._book("B")))
    assert env.game().phase == "actions"
    # The figure of П1, received when Д is already open (what dah-waker does after a phase change).
    env.ok("A", "POST", "/v1/spend", json={"spent_usd": 0.06, "source": "participant_program", "turn": 1,
                                           "phase": "negotiation_1"})
    base, closed = [e for e in read_events(env.gdir.journal_path) if e["type"] == "spend_report"]
    assert (base["for_turn"], base["for_phase"]) == (None, None)
    assert (base["at_turn"], base["at_phase"]) == (1, "negotiation_1")
    assert (closed["for_turn"], closed["for_phase"]) == (1, "negotiation_1")
    assert (closed["at_turn"], closed["at_phase"]) == (1, "actions")
    assert (closed["turn"], closed["phase"]) == (1, "negotiation_1")

    report = build_report(read_events(env.gdir.journal_path))
    by_phase = {(p["turn"], p["phase"]): p["spent_usd"] for p in report["phases"]}
    assert by_phase[(1, "negotiation_1")] == {"A": 0.06}
    assert by_phase[(1, "actions")] == {}
    assert report["turns"][0]["spent_usd"] == {"A": 0.06}
    assert report["totals"]["A"]["spent_usd"] == 0.06
    assert "деньги к концу фазы: Нико $0.06" in render_report(report)


# ---------------------------------------------------------------- issue 4: estimate, history, state, messages


def test_estimate_accepts_labels_names_and_province_ids(env):
    game = env.game()
    province = next(iter(game.state.provinces.values()))
    ptype = game.ruleset.province_types[province.type]
    base = {"attacker": {"core:assault": 5}, "defender": {"core:assault": 1}, "province_type": province.type}
    expected = env.ok("A", "POST", "/v1/estimate", json=base)["win_prob"]
    for alias in (province.id, province.id.lower(), ptype.short, ptype.name, ptype.id.split(":")[1]):
        got = env.ok("A", "POST", "/v1/estimate", json={**base, "province_type": alias})
        assert (got["win_prob"], got["province_type"]) == (expected, province.type), alias
    by_label = env.ok("A", "POST", "/v1/estimate", json={**base, "attacker": {"штурм": 5},
                                                          "defender": {"Штурмовики": "1"}})
    assert by_label["win_prob"] == expected
    as_list = env.ok("A", "POST", "/v1/estimate", json={**base, "attacker": [{"unit": "assault", "count": 5}],
                                                         "fortified": "false"})
    assert as_list["win_prob"] == expected


@pytest.mark.parametrize("body,code,must", [
    ({"attacker": {"assault": 4}, "defender": {"assault": 2}, "province_type": "core:agro"}, "UNKNOWN_ID",
     ["core:agro", "core:forge", "P07"]),
    ({"attacker": {"assault": 4}, "defender": {"assault": 2}, "province_type": "core:wasteland"}, "UNKNOWN_ID",
     ["core:waste"]),
    ({"task_id": 2}, "INVALID", ["не указан attacker"]),
    ({"attacker": {"pikemen": 1}, "defender": {}, "province_type": "core:forge"}, "UNKNOWN_ID",
     ["pikemen", "core:assault"]),
    ({"attacker": {"assault": -1}, "defender": {}, "province_type": "core:forge"}, "INVALID", ["≥ 0"]),
    ({"attacker": {"assault": 1}, "defender": {}}, "INVALID", ["не указан province_type"]),
])
def test_estimate_errors_explain_and_show_an_example(env, body, code, must):
    resp = env.client.post("/v1/estimate", headers=env.headers["A"], json=body)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == code
    assert "Пример: estimate(attacker=" in error["message"]
    for part in must:
        assert part in error["message"]


def test_state_and_history_accept_reasonable_inputs_and_explain_the_rest(env):
    full = env.ok("A", "GET", "/v1/state")["text"]
    assert env.ok("A", "GET", "/v1/state", params={"since_turn": 0})["text"] == full
    too_far = env.client.get("/v1/state", headers=env.headers["A"], params={"since_turn": 5})
    assert too_far.status_code == 400 and "Пример: get_state()" in too_far.json()["error"]["message"]
    not_int = env.call("A", "GET", "/v1/state", params={"since_turn": "прошлый"})
    assert "Пример: get_state(since_turn=1)" in not_int["error"]["message"]
    bad_format = env.call("A", "GET", "/v1/state", params={"format": "yaml"})
    assert "Пример:" in bad_format["error"]["message"]

    before = env.ok("A", "GET", "/v1/history")  # turn 1: nothing resolved yet, still an answer
    assert before["turn"] is None and "ни один ход не рассчитан" in before["text"]
    missing = env.client.get("/v1/history", headers=env.headers["A"], params={"turn": 3})
    assert missing.status_code == 404 and "Пример: history()" in missing.json()["error"]["message"]


def test_message_errors_show_an_example(env):
    no_to = env.call("A", "POST", "/v1/messages", json={"text": "мир?"})
    assert no_to["error"]["code"] == "INVALID"
    assert "не указан to" in no_to["error"]["message"] and 'send_message(to=["Соло"]' in no_to["error"]["message"]
    nobody = env.call("A", "POST", "/v1/messages", json={"to": ["Строитель"], "text": "мир?"})
    assert "Соло" in nobody["error"]["message"] and "Пример:" in nobody["error"]["message"]
    assert env.ok("A", "POST", "/v1/messages", json={"to": ["all"], "text": "всем"})
    game = env.game()
    for seat in ("A", "B"):
        game.set_ready(seat, orders_hash(game._book(seat)))
    in_actions = env.call("A", "POST", "/v1/messages", json={"to": "all", "text": "x"})
    assert in_actions["error"]["code"] == "PHASE_LOCKED" and "П1 и П2" in in_actions["error"]["message"]
    game.state.phase = "game_over"
    over = env.client.post("/v1/messages", headers=env.headers["A"], json={"to": ["Соло"], "text": "итог"})
    assert over.status_code == 409 and "game_over" in over.json()["error"]["message"]


def test_bridge_passes_estimate_to_server_and_adds_examples_to_argument_errors():
    seen = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content) if request.content else None)
        return httpx.Response(400, json={"ok": False, "error": {"code": "INVALID", "message": "нет attacker"}})

    bridge = Bridge(GameClient("http://game.test", "dah_g_A_k", transport=httpx.MockTransport(server)))
    text, is_error = bridge.call("estimate", {"task_id": 2})
    assert is_error and seen == [{"attacker": None, "defender": None, "province_type": None, "fortified": False,
                                  "hasty": False}]
    text, is_error = bridge.call("send_message", {"text": "мир"})
    assert is_error and "не указан аргумент to" in text and "Пример: send_message(to=" in text


def test_goal_text_says_to_finish_after_game_over(tmp_path):
    config = make_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7)
    assert "После game_over сразу заверши цель одним коротким итогом (счёт, место)" in goal_text(config, "A")


# ---------------------------------------------------------------- issue 6: player presence and waker skip


def test_player_presence_tracks_pending_wait_phase(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    assert game.phase_info("A", host=True)["player_waiting"] is False
    assert "player_waiting" not in game.phase_info("A")

    async def scenario() -> None:
        poll = asyncio.create_task(game.wait_phase(game.phase_seq, 5, player_seat="A"))
        host = asyncio.create_task(game.wait_phase(game.phase_seq, 5))  # the waker's own poll is not a player's
        await asyncio.sleep(0.01)
        assert game.phase_info("A", host=True)["player_waiting"] is True
        assert game.phase_info("B", host=True)["player_waiting"] is False
        ready(game, "A")
        ready(game, "B")  # П1 closes: both polls return with the new phase
        assert await poll is True and await host is True

    asyncio.run(scenario())
    info = game.phase_info("A", host=True)
    assert info["phase"] == "actions" and info["player_waiting"] is True  # its poll brought it the new phase
    assert game.phase_info("B", host=True)["player_waiting"] is False
    clock.now += 5
    game.note_player_activity("B")
    clock.now += 2
    assert game.phase_info("B", host=True)["player_active_s_ago"] == 2.0
    ready(game, "A")
    ready(game, "B")
    assert game.phase_info("A", host=True)["player_waiting"] is False
    assert game.phase_info("B", host=True)["player_active_s_ago"] is None  # the call was in the previous phase


def test_host_phase_info_over_http(env):
    assert "player_waiting" not in env.ok("A", "GET", "/v1/phase")
    host = env.ok("A", "GET", "/v1/phase", host=True)
    # The player's own /v1/phase call just above counts as activity in this phase.
    assert host["player_waiting"] is False and host["player_active_s_ago"] == 0.0


def _http_ready(env: Env, seat: str) -> dict:
    return env.ok(seat, "POST", "/v1/ready", json={"confirm_hash": orders_hash(env.game()._book(seat))})


def test_player_ready_that_closes_the_phase_is_not_activity_in_the_new_one(env):
    """The measured pattern: the bots are ready, the swarm's own /v1/ready closes the phase while the host's
    long-poll is pending; the host must not see that call as the player being at work in the new phase."""
    app = env.client.app

    async def scenario() -> tuple[dict, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            host_headers = {**env.headers["A"], "X-DAH-Host": "waker"}
            seq = (await client.get("/v1/phase", headers=host_headers)).json()["phase_seq"]
            host_poll = asyncio.create_task(client.get("/v1/wait_phase", headers=host_headers,
                                                       params={"after_seq": seq, "max_seconds": 5}))
            await asyncio.sleep(0.05)
            game = env.game()
            for seat in ("B", "A"):  # A's own ready closes П1
                resp = await client.post("/v1/ready", headers=env.headers[seat],
                                         json={"confirm_hash": orders_hash(game._book(seat))})
                assert resp.json()["ok"], resp.json()
            polled = (await host_poll).json()
            again = (await client.get("/v1/phase", headers=host_headers)).json()
            return polled, again

    polled, again = asyncio.run(scenario())
    for info in (polled, again):
        assert info["phase"] == "actions"
        assert info["player_waiting"] is False and info["player_active_s_ago"] is None
    other = env.ok("B", "GET", "/v1/phase", host=True)  # B's ready was a call of the previous phase too
    assert other["player_active_s_ago"] is None


def test_player_call_after_the_phase_opened_is_activity(env):
    _http_ready(env, "B")
    _http_ready(env, "A")  # closes П1
    assert env.ok("A", "GET", "/v1/phase", host=True)["player_active_s_ago"] is None
    env.clock.now += 4
    env.ok("A", "GET", "/v1/state")
    env.clock.now += 3
    host = env.ok("A", "GET", "/v1/phase", host=True)
    assert host["phase"] == "actions" and host["player_active_s_ago"] == 3.0


def test_player_call_that_arrives_after_the_deadline_is_not_activity_in_the_new_phase(env):
    env.clock.now += env.game().config["phase_limits"]["negotiation_1"] + 1
    env.ok("A", "GET", "/v1/state")  # tick() inside this call closes П1
    host = env.ok("A", "GET", "/v1/phase", host=True)
    assert host["phase"] == "actions" and host["player_active_s_ago"] is None


def test_waker_skips_continue_when_player_waits_and_keeps_the_reminder():
    from test_waker_loop import PHASES, T0, Clock, FakeDreamTeam, FakeGame, run

    clock = Clock()

    class WaitingGame(FakeGame):
        def _info(self):
            info = super()._info()
            if not info.get("game_over"):
                info.update(player_waiting=True, player_active_s_ago=None)
            return info

    game, dt = WaitingGame(clock), FakeDreamTeam(clock)
    run(game, dt, clock)
    assert [text for _, text in dt.continues if not text.startswith("[DAH] Напоминание")] == []
    assert len(dt.continues) == len(PHASES)  # the deadline reminders are still sent
    skipped = [t for t in game.telemetry if t["kind"] == "continue_skipped"]
    assert [(s["detail"]["reason"], s["detail"]["phase"]) for s in skipped] == [
        ("player_waiting", phase) for phase in PHASES]
    assert all(at == T0 + i * 600 + 540 for i, (at, _) in enumerate(dt.continues))


def test_waker_skips_continue_when_player_just_called():
    from test_waker_loop import Clock, FakeDreamTeam, FakeGame, run

    clock = Clock()

    class ActiveGame(FakeGame):
        def _info(self):
            info = super()._info()
            if not info.get("game_over"):
                info.update(player_waiting=False, player_active_s_ago=3.0 if info["phase"] == "actions" else None)
            return info

    game, dt = ActiveGame(clock), FakeDreamTeam(clock)
    run(game, dt, clock)
    reasons = [t["detail"]["reason"] for t in game.telemetry if t["kind"] == "continue_skipped"]
    assert reasons == ["player_active"]
    opens = [text for _, text in dt.continues if not text.startswith("[DAH] Напоминание")]
    assert len(opens) == 2  # П1 and П2 still woken


def test_waker_wakes_a_stopped_session_even_if_its_poll_is_pending():
    from test_waker_loop import PHASES, Clock, FakeDreamTeam, FakeGame, run

    clock = Clock()

    class WaitingGame(FakeGame):
        def _info(self):
            info = super()._info()
            if not info.get("game_over"):
                info.update(player_waiting=True, player_active_s_ago=1.0)
            return info

    game, dt = WaitingGame(clock), FakeDreamTeam(clock)
    dt.session["state"] = "stopped"  # the agent is gone, its wait_phase still pending on the server
    run(game, dt, clock)
    assert "continue_skipped" not in game.kinds()
    opens = [t["detail"] for t in game.telemetry if t["kind"] == "continue_sent"
             and t["detail"]["reason"] == "phase_open"]
    assert [o["phase"] for o in opens] == list(PHASES)


def test_waker_skip_detail_carries_the_session_state():
    from test_waker_loop import Clock, FakeDreamTeam, FakeGame, run

    clock = Clock()

    class WaitingGame(FakeGame):
        def _info(self):
            info = super()._info()
            if not info.get("game_over"):
                info.update(player_waiting=True, player_active_s_ago=None)
            return info

    game, dt = WaitingGame(clock), FakeDreamTeam(clock)
    run(game, dt, clock)
    skipped = [t["detail"] for t in game.telemetry if t["kind"] == "continue_skipped"]
    assert skipped and all(d["session_state"] == "running" for d in skipped)


# ---------------------------------------------------------------- issue 9: DreamTeam session link


def test_waker_reports_its_session_and_server_journals_it(env):
    def forward(request: httpx.Request) -> httpx.Response:
        resp = env.client.request(request.method, request.url.path, headers=dict(request.headers),
                                  content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers={"content-type": "application/json"})

    game_client = GameClient("http://testserver", env.headers["A"]["Authorization"].split()[1],
                             transport=httpx.MockTransport(forward), host="waker")
    assert game_client.report_session("dt-sess-42") == {"dreamteam_session_id": "dt-sess-42", "changed": True}
    assert game_client.report_session("dt-sess-42")["changed"] is False
    infos = [e for e in read_events(env.gdir.journal_path) if e["type"] == "seat_info"]
    assert infos[-1] == {**infos[-1], "seat": "A", "dreamteam_session_id": "dt-sess-42", "update": True,
                         "source": "waker", "previous": None}
    assert len(infos) == 3  # two at creation, one update
    assert read_json(env.gdir.config_path)["seats"][0]["dreamteam_session_id"] == "dt-sess-42"
    call = [e for e in read_events(env.gdir.journal_path) if e["type"] == "call"
            and e["route"] == "POST /v1/seat/dreamteam_session"][0]
    assert call["host"] is True
    bad = env.call("A", "POST", "/v1/seat/dreamteam_session", host=True, json={"dreamteam_session_id": "a b"})
    assert bad["error"]["code"] == "INVALID" and "Пример" in bad["error"]["message"]


def test_waker_run_links_session_first():
    from test_waker_loop import SESSION, Clock, FakeDreamTeam, FakeGame, make_waker

    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)
    make_waker(game, dt, clock).run()
    first = game.requests[0]
    assert first.url.path == "/v1/seat/dreamteam_session"
    assert json.loads(first.content) == {"dreamteam_session_id": SESSION}


def test_cli_set_session_writes_config_and_journal(tmp_path, capsys):
    code = main(["game", "create", "--scenario", "core:slice2", "--seat", "A:swarm:Нико", "--seat", "B:agent:Соло",
                 "--seed", "7", "--id", "s1", "--games-dir", str(tmp_path)])
    assert code == 0
    gdir = GameDir(tmp_path / "s1")
    server_journal = Journal(gdir.journal_path)  # a running server's writer, opened before the CLI writes
    assert main(["game", "set-session", "s1", "A", "5f0c2a", "--games-dir", str(tmp_path)]) == 0
    assert "записана" in capsys.readouterr().out
    assert read_json(gdir.config_path)["seats"][0]["dreamteam_session_id"] == "5f0c2a"
    server_journal.append("recon", turn=1)
    evs = read_events(gdir.journal_path)
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(set(seqs))  # the server's writer continues after the CLI's event, no duplicate seq
    update = [e for e in evs if e["type"] == "seat_info"][-1]
    assert (update["seat"], update["dreamteam_session_id"], update["source"]) == ("A", "5f0c2a", "cli")
    assert main(["game", "set-session", "s1", "Z", "5f0c2a", "--games-dir", str(tmp_path)]) == 2
    assert main(["game", "set-session", "s1", "A", "bad id", "--games-dir", str(tmp_path)]) == 2


def test_set_session_rejects_unknown_seat(tmp_path):
    game = new_game(tmp_path, FakeClock())
    with pytest.raises(GameError):
        game.set_dreamteam_session("Z", "abc")


def test_waker_retries_the_session_link_when_the_server_was_not_up():
    from test_waker_loop import SESSION, Clock, FakeDreamTeam, FakeGame, make_waker

    clock = Clock()

    class LateGame(FakeGame):
        def __init__(self, clock):
            super().__init__(clock)
            self.links = 0

        def __call__(self, request):
            if request.url.path == "/v1/seat/dreamteam_session":
                self.requests.append(request)
                self.links += 1
                if self.links == 1:
                    raise httpx.ConnectError("connection refused", request=request)
                return httpx.Response(200, json={"ok": True, "dreamteam_session_id": SESSION, "changed": True})
            return super().__call__(request)

    game, dt = LateGame(clock), FakeDreamTeam(clock)
    make_waker(game, dt, clock).run()
    assert game.links == 2  # failed at start, recorded after the first successful poll, then never again
    paths = [r.url.path for r in game.requests]
    assert paths[:3] == ["/v1/seat/dreamteam_session", "/v1/wait_phase", "/v1/seat/dreamteam_session"]


def test_waker_does_not_retry_a_refused_session_link():
    from test_waker_loop import Clock, FakeDreamTeam, FakeGame, make_waker

    clock = Clock()
    game, dt = FakeGame(clock), FakeDreamTeam(clock)  # FakeGame answers the link route with 404
    make_waker(game, dt, clock).run()
    assert [r.url.path for r in game.requests].count("/v1/seat/dreamteam_session") == 1


def test_bridge_passes_an_estimate_side_given_as_a_list(env):
    def forward(request: httpx.Request) -> httpx.Response:
        resp = env.client.request(request.method, request.url.path, headers=dict(request.headers),
                                  content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers={"content-type": "application/json"})

    bridge = Bridge(GameClient("http://testserver", env.headers["A"]["Authorization"].split()[1],
                               transport=httpx.MockTransport(forward)))
    province = next(iter(env.game().state.provinces.values()))
    base = {"defender": {"core:assault": 1}, "province_type": province.type}
    as_dict, err1 = bridge.call("estimate", {**base, "attacker": {"core:assault": 5}})
    as_list, err2 = bridge.call("estimate", {**base, "attacker": [{"unit": "assault", "count": 5}]})
    as_json, err3 = bridge.call("estimate", {**base, "attacker": '[["assault", 5]]'})
    assert not (err1 or err2 or err3), (as_dict, as_list, as_json)
    assert as_dict == as_list == as_json
