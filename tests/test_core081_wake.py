"""Core 0.8.1 step 2 (m10 findings 1 and 2, decision 32): an offer that clears a ready seat's ready returns that
seat's wait_phase at once with the reason, and dah-waker wakes a seat that is not polling (once); a seat in a
dispute over provinces is told it can trade (get_state СПОР line, the reply to a pact offer)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_waker_loop import KEY, SESSION, T0, TOKEN, Clock, FakeDreamTeam

from dah.bridge.client import GameClient
from dah.bridge.tools import TOOLS
from dah.engine import orders_hash
from dah.engine import trade as tr
from dah.game import Game
from dah.server import create_app
from dah.store import init_game_dir
from dah.waker.core import Waker
from dah.waker.dreamteam import DreamTeamClient
from dah.waker.text import ready_cleared_text
from slice_helpers import slice_config

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"},
         {"seat": "C", "nick": "Сид", "kind": "agent"}]


class GameClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def make_game(tmp_path, *, conquest: bool = False, trade: bool = True) -> Game:
    config = slice_config(game_id="w1", scenario="core:slice3", seats=SEATS, seed=7,
                          systems={"espionage": False, "conquest": conquest, "discoveries": False, "trade": trade})
    return Game.create(init_game_dir(tmp_path, config)[0], GameClock())


def ready(game: Game, seat: str) -> None:
    game.set_ready(seat, orders_hash(game._book(seat)))


# ---------------------------------------------------------------- the server: wait_phase returns

def test_a_trade_offer_returns_the_waiting_poll_of_a_ready_seat_at_once(tmp_path):
    game = make_game(tmp_path)
    ready(game, "B")
    seq = game.phase_seq

    async def scenario():
        poll = asyncio.create_task(game.wait_phase(seq, 30, player_seat="B"))
        other = asyncio.create_task(game.wait_phase(seq, 0.3, player_seat="C"))
        await asyncio.sleep(0.05)
        assert game.phase_info("B", host=True)["player_waiting"]
        game.propose_trade("A", "Соло", {"alloy": 2})
        changed = await asyncio.wait_for(poll, 2)
        # another seat's poll is not returned by B's news: it runs to its own timeout
        assert not other.done()
        return changed, await other

    changed, other_changed = asyncio.run(scenario())
    assert changed is False and other_changed is False
    host = game.phase_info("B", host=True)["ready_cleared"]
    assert host == {"reason": "trade_proposed", "by": "Нико", "id": "trade-A-B-1", "told": True, "n": 1,
                    "text": host["text"]}
    note = game.ready_cleared_note("B")
    assert note["reason"] == "trade_proposed" and note["id"] == "trade-A-B-1"
    assert note["text"].startswith("ГОТОВНОСТЬ СНЯТА: предложение обмена trade-A-B-1 от Нико")
    assert "respond_trade" in note["text"] and "ready()" in note["text"]
    # returned once: the next poll waits again
    assert game.ready_cleared_note("B") is None

    async def again():
        return await game.wait_phase(seq, 0.1, player_seat="B")

    assert asyncio.run(again()) is False and game.ready_cleared_note("B") is None


def test_a_pact_offer_does_the_same_and_a_clear_before_the_poll_returns_it_at_once(tmp_path):
    game = make_game(tmp_path)
    ready(game, "C")
    game.propose_pact("A", "Сид", 2)         # C is not polling now
    host = game.phase_info("C", host=True)["ready_cleared"]
    assert host["reason"] == "pact_proposed" and host["told"] is False
    seq = game.phase_seq

    async def poll():
        return await asyncio.wait_for(game.wait_phase(seq, 30, player_seat="C"), 2)

    assert asyncio.run(poll()) is False
    note = game.ready_cleared_note("C")
    assert note["reason"] == "pact_proposed" and "respond_pact" in note["text"]
    assert game.phase_info("C", host=True)["ready_cleared"]["told"] is True


def test_an_offer_to_a_seat_that_is_not_ready_returns_nothing(tmp_path):
    game = make_game(tmp_path)
    game.propose_trade("A", "Соло", {"alloy": 2})
    assert game.phase_info("B", host=True)["ready_cleared"] is None and game.ready_cleared_note("B") is None


def test_ready_again_ends_the_clear(tmp_path):
    game = make_game(tmp_path)
    ready(game, "B")
    game.propose_trade("A", "Соло", {"alloy": 2})
    ready(game, "B")
    game.unready("B")                       # its own unready is not an offer's clear
    assert game.phase_info("B", host=True)["ready_cleared"] is None and game.ready_cleared_note("B") is None


def test_the_host_poll_for_the_seat_returns_on_its_clear(tmp_path):
    game = make_game(tmp_path)
    ready(game, "B")
    seq = game.phase_seq

    async def scenario():
        host = asyncio.create_task(game.wait_phase(seq, 30, watch_seat="B"))
        await asyncio.sleep(0.05)
        assert not game.phase_info("B", host=True)["player_waiting"]   # the host's poll is not the player's
        game.propose_trade("A", "Соло", {"alloy": 2})
        return await asyncio.wait_for(host, 2)

    assert asyncio.run(scenario()) is False
    assert game.phase_info("B", host=True)["ready_cleared"]["told"] is False


def test_the_route_gives_the_reason_and_the_text_first(tmp_path):
    config = slice_config(game_id="h1", scenario="core:slice2", seats=SEATS[:2], seed=7,
                          systems={"espionage": False, "conquest": False, "trade": True})
    gdir, keys = init_game_dir(tmp_path, config)
    clock = GameClock()
    Game.create(gdir, clock)
    client = TestClient(create_app(str(tmp_path), admin_key="adm", clock=clock, tick_interval=None))
    head = {s: {"Authorization": f"Bearer {k}"} for s, k in keys.items()}
    seq = client.get("/v1/phase", headers=head["B"]).json()["phase_seq"]
    assert client.post("/v1/ready", headers=head["B"], json={}).json()["ok"]
    assert client.post("/v1/trades", headers=head["A"], json={"to": "Соло", "give": {"alloy": 2}}).json()["ok"]
    out = client.get(f"/v1/wait_phase?after_seq={seq}&max_seconds=30", headers=head["B"]).json()
    assert out["ok"] and out["changed"] is False and out["reason"] == "ready_cleared"
    assert out["ready_cleared"] == {"reason": "trade_proposed", "by": "Нико", "id": "trade-A-B-1"}
    assert out["you_ready"] is False and out["text"].startswith("ГОТОВНОСТЬ СНЯТА: предложение обмена trade-A-B-1")
    later = client.get(f"/v1/wait_phase?after_seq={seq}&max_seconds=1", headers=head["B"]).json()
    assert later["changed"] is False and "reason" not in later


def test_the_wait_phase_tool_names_the_early_return():
    spec = next(t for t in TOOLS if t.name == "wait_phase")
    assert "ready_cleared" in spec.description


# ---------------------------------------------------------------- dah-waker: one wake

class ScriptedGame:
    """Host phase infos in order (the last one repeats); every poll moves the clock by 5 s."""

    def __init__(self, clock: Clock, infos: list[dict[str, Any]]):
        self.clock = clock
        self.infos = infos
        self.telemetry: list[dict[str, Any]] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json
        path = request.url.path
        if path in ("/v1/wait_phase", "/v1/phase"):
            info = self.infos[min(self.polls, len(self.infos) - 1)]
            self.polls += 1
            self.clock.now += 5
            return httpx.Response(200, json={"ok": True, "changed": True, **info})
        if path == "/v1/telemetry":
            self.telemetry.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def detail(self, kind: str) -> list[dict[str, Any]]:
        return [t["detail"] for t in self.telemetry if t["kind"] == kind]


def _info(you_ready: bool, cleared: Any = None, **extra: Any) -> dict[str, Any]:
    return {"turn": 7, "turn_limit": 12, "phase": "negotiation_1", "deadline": T0 + 600, "seconds_left": 500,
            "ready": "2/3", "phase_seq": 19, "you_ready": you_ready, "allowed": [], "alive": True,
            "game_over": False, "player_waiting": False, "player_active_s_ago": None, "player_calls": [],
            "ready_cleared": cleared, **extra}


TRADE = {"reason": "trade_proposed", "by": "Бета", "id": "trade-B-C-1", "told": False, "text": "..."}


def _waker(infos: list[dict[str, Any]]) -> tuple[ScriptedGame, FakeDreamTeam]:
    clock = Clock()
    game, dt = ScriptedGame(clock, infos), FakeDreamTeam(clock)
    waker = Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game)),
                  DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
                  SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None)
    for _ in range(len(infos) + 2):
        waker.step()
    return game, dt


def test_the_waker_wakes_a_ready_seat_once_when_an_offer_clears_it():
    game, dt = _waker([_info(False), _info(True), _info(False, TRADE), _info(False, TRADE)])
    wakes = [d for d in game.detail("wake_sent")]
    assert [w["reason"] for w in wakes] == ["phase_open", "ready_cleared"]
    text = dt.messages[-1][2]
    assert text.startswith("[DAH] Ход 7/12, фаза ПЕРЕГОВОРЫ 1")
    assert "предложение обмена trade-B-C-1 от Бета" in text and "dah.respond_trade" in text
    assert "снова dah.ready()" in text


def test_the_waker_skips_when_the_player_poll_returned_it_or_the_player_is_active():
    told = {**TRADE, "told": True}
    game, dt = _waker([_info(False), _info(True), _info(False, told)])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open"]
    skipped = [d for d in game.detail("continue_skipped") if d.get("wake") == "ready_cleared"]
    assert skipped and skipped[0]["reason"] == "player_waiting" and skipped[0]["cleared_by"] == "trade_proposed"
    game, dt = _waker([_info(False), _info(True), _info(False, TRADE, player_active_s_ago=3.0)])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open"]
    assert [d["reason"] for d in game.detail("continue_skipped") if d.get("wake")] == ["player_active"]


def test_the_waker_wakes_on_a_clear_it_never_saw_the_seat_ready_for():
    # review W1: ready pressed and cleared by an offer within one waker poll: no poll saw you_ready true
    game, dt = _waker([_info(False), _info(False, {**TRADE, "n": 1}), _info(False, {**TRADE, "n": 1})])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open", "ready_cleared"]
    # a second offer after the seat pressed ready again is a new clear: woken again
    game, dt = _waker([_info(False), _info(False, {**TRADE, "n": 1}), _info(True),
                       _info(False, {**TRADE, "id": "trade-B-C-2", "n": 2})])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open", "ready_cleared", "ready_cleared"]
    # a clear already there at the phase's first poll (a restarted waker) is covered by phase_open
    game, dt = _waker([_info(False, {**TRADE, "n": 3}), _info(False, {**TRADE, "n": 3})])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open"]


def test_a_real_game_clear_within_one_poll_wakes_the_seat(tmp_path):
    # review W1 against the game itself: the host info names the clear with its counter `n`
    real = make_game(tmp_path)
    assert real.phase_info("B", host=True)["ready_cleared"] is None
    ready(real, "B")
    real.propose_trade("A", "Соло", {"alloy": 2})
    cleared = real.phase_info("B", host=True)["ready_cleared"]
    assert cleared["told"] is False and cleared["n"] == 1
    game, dt = _waker([_info(False), _info(False, cleared)])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open", "ready_cleared"]


def test_an_active_player_only_postpones_the_ready_cleared_wake():
    # review W2: the seat's own ready() call made it «active»; it pressed ready and stopped: woken once it is quiet
    game, dt = _waker([_info(False), _info(True), _info(False, TRADE, player_active_s_ago=10.0),
                       _info(False, TRADE, player_active_s_ago=20.0), _info(False, TRADE, player_active_s_ago=60.0),
                       _info(False, TRADE, player_active_s_ago=120.0)])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open", "ready_cleared"]
    skipped = [d for d in game.detail("continue_skipped") if d.get("wake") == "ready_cleared"]
    assert [(d["reason"], d["retry"]) for d in skipped] == [("player_active", True)]       # logged once
    # the player's own poll returned the clear meanwhile: no wake at all
    told = {**TRADE, "told": True}
    game, dt = _waker([_info(False), _info(True), _info(False, TRADE, player_active_s_ago=10.0),
                       _info(False, told, player_active_s_ago=60.0)])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open"]


def test_a_postponed_clear_does_not_hide_the_deadline_reminder():
    game, dt = _waker([_info(False), _info(True), _info(False, TRADE, player_active_s_ago=5.0, seconds_left=40)])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open", "reminder"]


def test_a_postponed_clear_shortens_the_next_poll():
    clock = Clock()
    game = ScriptedGame(clock, [_info(False), _info(True), _info(False, TRADE, player_active_s_ago=10.0)])
    waker = Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game)),
                  DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(FakeDreamTeam(clock))),
                  SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None)
    for _ in range(3):
        waker.step()
    assert waker._wait_seconds() <= 21.0


def test_the_waker_does_not_wake_for_the_seats_own_unready():
    game, dt = _waker([_info(False), _info(True), _info(False, None)])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open"]
    assert not [d for d in game.detail("continue_skipped") if d.get("wake")]


def test_a_new_phase_is_not_a_ready_clear():
    nxt = {**_info(False), "phase": "actions", "phase_seq": 20}
    game, dt = _waker([_info(False), _info(True), nxt])
    assert [w["reason"] for w in game.detail("wake_sent")] == ["phase_open", "phase_open"]


def test_the_ready_cleared_text_for_a_pact_and_in_negotiation_2():
    info = {**_info(False), "phase": "negotiation_2", "orders_hash": "h1",
            "ready_cleared": {"reason": "pact_proposed", "by": "Бета", "id": "pact-B-C-2"}}
    text = ready_cleared_text(info)
    assert "предложение пакта pact-B-C-2 от Бета" in text and "dah.respond_pact" in text
    assert 'dah.ready(confirm_hash="h1")' in text
    plain = ready_cleared_text({**_info(False), "ready_cleared": None})
    assert "чужое предложение" in plain and len(plain) <= 400


# ---------------------------------------------------------------- decision 32: the trade hint at a dispute

def test_a_held_province_of_a_rival_shows_the_dispute_line(tmp_path):
    game = make_game(tmp_path, conquest=True)
    # A holds P05..P08 (home P05), B P01..P04; give B's P02 to A as if conquered (origin stays B)
    game.state.provinces["P02"].owner = "A"
    text = game.state_text("A")
    line = next(x for x in text.splitlines() if x.startswith("СПОР:"))
    assert line == "СПОР: P02 (Соло) — можно предложить обмен: propose_trade (провинция, ресурсы, донесение)"
    assert len(line) <= tr.DISPUTE_LINE_LIMIT
    assert "СПОР: P02 (Нико)" in game.state_text("B")          # the side that lost it sees the same dispute
    assert "СПОР" not in game.state_text("C")                  # no dispute of C's
    assert tr.disputes(game.state, game.ruleset, "A") == [("P02", "B")]


def test_a_claim_is_a_dispute_and_a_dead_rival_is_not(tmp_path):
    game = make_game(tmp_path, conquest=True)
    game.state.claims = [{"seat": "C", "province": "P07", "until_turn": 9}]
    assert tr.disputes(game.state, game.ruleset, "A") == [("P07", "C")]
    game.state.players["C"].alive = False
    assert tr.disputes(game.state, game.ruleset, "A") == []


def test_no_dispute_line_without_trade_or_outside_negotiations(tmp_path):
    off = make_game(tmp_path / "off", conquest=True, trade=False)
    off.state.provinces["P02"].owner = "A"
    assert "СПОР" not in off.state_text("A") and tr.disputes(off.state, off.ruleset, "A") == []
    on = make_game(tmp_path / "on", conquest=True)
    on.state.provinces["P02"].owner = "A"
    for seat in ("A", "B", "C"):
        ready(on, seat)
    assert on.phase == "actions" and "СПОР" not in on.state_text("A")


def test_many_disputes_stay_within_the_line_limit(tmp_path):
    game = make_game(tmp_path, conquest=True)
    for pid in ("P01", "P02", "P03", "P04", "P09", "P10"):
        game.state.provinces[pid].owner = "A"
    line = tr.dispute_line(game.state, game.ruleset, "A", lambda s: game.state.players[s].nick * 6)
    assert line.startswith("СПОР: ") and len(line) <= tr.DISPUTE_LINE_LIMIT and "propose_trade" in line


def test_the_dispute_hint_names_only_goods_a_trade_can_carry(tmp_path):
    # review F1: «мир» is not a good of propose_trade (peace is propose_pact); a report is
    game = make_game(tmp_path, conquest=True)
    for text in (tr.DISPUTE_TAIL, tr.dispute_note(game.state, game.ruleset, "A", "B") or tr.DISPUTE_TAIL):
        assert "мир" not in text and "донесение" in text
    try:
        tr.parse_goods(game.ruleset, {"мир": 1}, "want")
    except Exception as exc:        # the hint used to invite exactly this
        assert "мир" in str(exc)
    else:
        raise AssertionError("«мир» must not parse as a good")


def test_trade_growth_of_get_state_with_obmen_and_spor_stays_within_its_budget(tmp_path):
    # review F3: the СПОР line is part of trade's get_state growth (TRADE.md 12 G7): ОБМЕН + СПОР <= 322
    game = make_game(tmp_path, conquest=True)
    for pid in ("P01", "P02", "P03", "P04", "P09", "P10"):
        game.state.provinces[pid].owner = "A"
    game.propose_trade("B", "Нико", {"alloy": 3}, {"alloy": 1})
    game.propose_trade("C", "Нико", {"alloy": 2}, {"alloy": 1})
    game.propose_trade("A", "Сид", {"alloy": 1}, {"alloy": 4})
    lines = game.state_text("A").splitlines()
    obmen = next(x for x in lines if x.startswith("ОБМЕН"))
    spor = next(x for x in lines if x.startswith("СПОР"))
    assert len(obmen) <= tr.STATE_LINE_LIMIT and len(spor) <= tr.DISPUTE_LINE_LIMIT
    assert tr.state_growth(obmen, spor) == len(obmen) + len(spor) + 2 <= tr.STATE_GROWTH_LIMIT == 322
    assert tr.state_growth("", "") == 0


def test_the_reply_to_a_pact_offer_in_a_dispute_names_the_trade(tmp_path):
    game = make_game(tmp_path, conquest=True)
    game.state.provinces["P02"].owner = "A"
    out = game.propose_pact("B", "Нико", 2)
    assert out["text"].endswith("; спор о P02: можно и обмен — propose_trade (провинция, ресурсы, донесение)")
    notice = game.messages[-1]
    assert notice["to"] == ["A"] and "(спор о P02: можно и обмен" in notice["text"]
    calm = game.propose_pact("C", "Нико", 2)
    assert "спор" not in calm["text"] and "спор" not in game.messages[-1]["text"]
