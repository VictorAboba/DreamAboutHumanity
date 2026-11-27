"""Core 0.8.2 step 2 (m11, decision 33): the hidden last turn over the real HTTP route (two games that differ only in
the drawn end answer every seat byte for byte the same until the earlier one ends), the waker and the report against
it, and m11 finding 4 (an eliminated seat's waker posts its spend as after the game, and the report counts it once)."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_waker_loop import SESSION, TOKEN, FakeDreamTeam
from test_waker_loop import Clock as WakerClock

from dah.bots import BOT_TYPES
from dah.bots.base import seat_seed
from dah.bots.client import ApiClient, ApiError
from dah.bridge.client import GameClient
from dah.bridge.tools import Bridge
from dah.engine.rules import topics as rule_topics
from dah.game import Game, make_config
from dah.journal import read_events
from dah.report import build_report, render_report
from dah.server import create_app
from dah.store import init_game_dir
from dah.waker.core import Waker
from dah.waker.dreamteam import DreamTeamClient
from dah.waker.text import eliminated_text, ready_cleared_text, wake_text, wrap_up_text

RUNS = Path(r"C:\Users\kuzmi\DreamAboutHumanity-runs-2026-11-23")
SEATS3 = [{"seat": "A", "nick": "Альфа", "kind": "agent"}, {"seat": "B", "nick": "Бета", "kind": "agent"},
          {"seat": "C", "nick": "Гамма", "kind": "agent"}]
BOTS = {"A": "diplomat", "B": "builder", "C": "rush"}


class Clock:
    def __call__(self) -> float:
        return 1_700_000_000.0


def forward(client: TestClient) -> httpx.MockTransport:
    """An httpx transport into the in-process app (as tests/test_stage2_bots.py)."""
    def send(request: httpx.Request) -> httpx.Response:
        resp = client.request(request.method, str(request.url.raw_path, "ascii"), headers=dict(request.headers),
                              content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers={"content-type": "application/json"})
    return httpx.MockTransport(send)


# ---------------------------------------------------------------- c) the fog test over HTTP

class Recorder(ApiClient):
    """The bots' HTTP client, every answer (errors too) kept in `log`."""

    def __init__(self, transport: httpx.BaseTransport, key: str, log: list) -> None:
        super().__init__("http://testserver", key, transport=transport)
        self.log = log

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._http.request(method, path, **kwargs)
        data = response.json()
        self.log.append(["bot", method, path, kwargs, response.status_code, data])
        if not data.get("ok", False):
            error = data.get("error") or {}
            raise ApiError(str(error.get("code", response.status_code)), str(error.get("message", "")))
        return data


class Side:
    """One of the two games: its app, a raw HTTP client per seat, the MCP bridge per seat, the host (waker) client,
    the bots, and the log of every seat-facing answer."""

    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        gdir, keys = init_game_dir(root, config)
        Game.create(gdir, Clock())
        self.client = TestClient(create_app(str(root), admin_key="adm", clock=Clock(), tick_interval=None))
        transport = forward(self.client)
        self.log: list = []
        self.http = {s: {"Authorization": f"Bearer {k}"} for s, k in keys.items()}
        self.bridge = {s: Bridge(GameClient("http://testserver", k, transport=transport)) for s, k in keys.items()}
        self.host = {s: GameClient("http://testserver", k, transport=transport, host="dah-waker")
                     for s, k in keys.items()}
        self.api = {s: Recorder(transport, k, self.log) for s, k in keys.items()}
        self.bots = {s: BOT_TYPES[kind](s, seat_seed(config["game_id"], s)) for s, kind in BOTS.items()}
        self.nicks = {s["seat"]: s["nick"] for s in config["seats"]}
        self.acted: dict[str, int] = {}

    @property
    def game(self) -> Game:
        return self.client.app.state.registry.games["fog"]

    def raw(self, seat: str, method: str, path: str, **kwargs: Any) -> None:
        resp = self.client.request(method, path, headers=self.http[seat], **kwargs)
        self.log.append(["http", seat, method, path, kwargs, resp.status_code, resp.text])

    def tool(self, seat: str, name: str, args: dict[str, Any]) -> None:
        self.log.append(["tool", seat, name, args, *self.bridge[seat].call(name, args)])

    def battery(self, seat: str, *, topics: bool) -> None:
        """Every read a seat (or its waker) has, and a few refusals, into the log."""
        g = self.game
        for path, params in (("/v1/whoami", {}), ("/v1/phase", {}), ("/v1/rules", {}),
                             ("/v1/wait_phase", {"after_seq": g.phase_seq, "max_seconds": 0}),
                             ("/v1/wait_phase", {"after_seq": 0, "max_seconds": 0}),
                             ("/v1/state", {}), ("/v1/state", {"format": "json"}),
                             ("/v1/state", {"since_turn": max(1, g.state.turn - 1)}),
                             ("/v1/orders", {}), ("/v1/spies", {}), ("/v1/intel", {}), ("/v1/trades", {}),
                             ("/v1/pacts", {}), ("/v1/inbox", {}), ("/v1/techs", {}), ("/v1/techs", {"full": "true"}),
                             ("/v1/history", {}), ("/v1/setup", {}), ("/v1/describe", {"id": "assault"}),
                             ("/v1/state", {"format": "xml"}), ("/v1/history", {"turn": "много"}),
                             ("/v1/describe", {"id": "нет-такого"})):
            self.raw(seat, "GET", path, params=params)
        self.raw(seat, "POST", "/v1/estimate", json={"attacker": {"assault": 3}, "defender": {"assault": 2},
                                                     "province_type": "plains"})
        self.raw(seat, "POST", "/v1/estimate/mission", json={"mission": "observe", "target": "P10"})
        self.raw(seat, "POST", "/v1/orders/validate", json={"orders": [{"kind": "fly", "province": "P01"}]})
        # the tool's wait first: the phase changed since its last call, so it returns at once (a wait in the same
        # phase would block for its 45 s)
        for name, args in (("wait_phase", {}), ("phase", {}), ("get_state", {}), ("get_state", {"format": "json"}), ("list_orders", {}),
                           ("list_trades", {}), ("list_pacts", {}), ("list_spies", {}), ("get_intel", {}),
                           ("list_techs", {}), ("history", {}), ("inbox", {}),
                           ("estimate", {"attacker": {"assault": 4}, "defender": {"assault": 1},
                                         "province_type": "forge", "fortified": True}),
                           ("estimate_mission", {"mission": "observe", "target": "P05", "rounds": 2}),
                           ("estimate_mission", {"mission": "sabotage", "target": "P99"}),
                           ("submit_orders", {"orders": "нет"}), ("respond_trade", {"id": "T99", "decision": "x"})):
            self.tool(seat, name, args)
        if topics:
            for topic in rule_topics(g.ruleset, setup=g.setup):
                self.tool(seat, "rules", {"topic": topic})
        info = self.host[seat].request("GET", "/v1/phase")
        self.log.append(["host", seat, info, wake_text(info), wake_text(info, reminder=True),
                         ready_cleared_text(info), eliminated_text(info, seat)])

    def play(self) -> None:
        """Each living seat's bot plays the phase once (in seat order), or presses ready again after a cleared
        ready, until the phase closes; the bots' calls and answers go into the log."""
        seq = self.game.phase_seq
        for _ in range(4):
            for seat in sorted(self.bots):
                g = self.game
                if g.game_over or g.phase_seq != seq or not g.state.players[seat].alive or seat in g.ready_seats:
                    continue
                bot, api, me = self.bots[seat], self.api[seat], {seat, self.nicks[seat]}
                try:
                    if self.acted.get(seat) != seq:
                        self.acted[seat] = seq
                        bot.play_phase(api, g.phase, me)
                    else:
                        bot.ready_again(api, g.phase, me)
                except ApiError as exc:
                    self.log.append(["bot_error", seat, exc.code, str(exc)])
            if self.game.game_over or self.game.phase_seq != seq:
                return
        raise AssertionError(f"phase {self.game.phase} of turn {self.game.state.turn} did not close")


def _first_difference(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"entry {i}:\n{json.dumps(x, ensure_ascii=False)[:1500]}\n!=\n{json.dumps(y, ensure_ascii=False)[:1500]}"
    return f"lengths {len(a)} != {len(b)}"


def test_two_games_differing_only_in_the_drawn_end_answer_every_seat_the_same_over_http(tmp_path):
    """quad4 with its own range 11-15, three bots (diplomat, builder, rush) playing over HTTP, drawn end 13 against
    15: every seat-facing answer (raw HTTP: whoami, phase, wait_phase, rules, state text/JSON, orders, spies, intel,
    trades, pacts, inbox, techs, history, setup, describe, estimate, estimate_mission, refusals; the MCP tools with
    every rules topic; the host's phase info and every waker text made from it; the bots' own calls) is identical
    through turn 12 and turn 13 up to its resolution; then the early game is over and the late one goes on."""
    config = make_config(game_id="fog", scenario="core:quad4", seats=SEATS3, seed=17, objectives_salt=5)
    assert config["turn_limit"] == 15 and config["end_turn_min"] == 11
    sides = []
    for name, end in (("early", 13), ("late", 15)):
        cfg = copy.deepcopy(config)
        cfg["end_turn"] = end
        sides.append(Side(tmp_path / name, cfg))
    early, late = sides
    assert early.game.ruleset.scenario.last_turn == 13 and late.game.ruleset.scenario.last_turn == 15
    turns_seen = set()
    while not early.game.game_over:
        turn, phase = early.game.state.turn, early.game.phase
        assert (late.game.state.turn, late.game.phase) == (turn, phase)
        turns_seen.add(turn)
        first_of_turn = phase in ("setup", "negotiation_1")
        for side in sides:
            for seat in "ABC":
                side.battery(seat, topics=first_of_turn and turn in (1, 11, 13))
            side.play()
        assert early.log == late.log, f"turn {turn} {phase}: {_first_difference(early.log, late.log)}"
    assert turns_seen == set(range(1, 14))
    assert early.game.state.turn == 13 and early.game.state.result["reason"] == "turn_limit"
    assert not late.game.game_over and late.game.state.turn == 14
    # the logs did see the range, the possible end and real play, and never the drawn turn
    text = json.dumps(early.log, ensure_ascii=False)
    assert "один из ходов 11–15, скрыт" in text and "может быть последним" in text and '"end_turns": [11, 15]' in text
    assert '\\"end_turn\\"' not in text and '"end_turn"' not in text
    kinds = {e[2] for e in early.log if e[0] == "bot"}
    assert {"/v1/orders", "/v1/ready", "/v1/state"} <= kinds
    events = {e["type"] for e in read_events(early.game.gdir.journal_path)}
    assert {"orders_submitted", "turn_resolved"} <= events
    # after the game the early side says which turn it was; the late one still does not
    over = early.host["A"].request("GET", "/v1/phase")
    assert over["end_turn"] == 13 and "end_turn" not in late.host["A"].request("GET", "/v1/phase")
    assert "Партия окончена: ход 13" in wrap_up_text(over, "A")


# ---------------------------------------------------------------- b) the waker texts and the report

def test_the_waker_texts_never_name_the_drawn_turn():
    base = {"turn": 12, "turn_limit": 15, "end_turns": [11, 15], "may_be_last": True, "phase": "actions",
            "deadline": None, "ready": "0/3", "orders_hash": "h"}
    texts = [wake_text(base), wake_text(base, reminder=True), ready_cleared_text(base), eliminated_text(base, "C")]
    for t in texts:
        assert "/15" not in t and "13" not in t and "14" not in t
    assert wake_text(base).startswith("[DAH] Ход 12 (может быть последним; конец — один из ходов 11–15, скрыт)")
    over = {**base, "game_over": True, "end_turn": 12, "result": {"reason": "turn_limit", "winners": ["A"],
                                                                  "scores": {"A": 3, "B": 2}}}
    assert "Ход 12, последний" in wake_text(over)
    assert wrap_up_text(over, "B").startswith("[DAH] Партия окончена: ход 12, лимит ходов, победитель A")


def test_the_report_names_the_drawn_turn_after_the_game(tmp_path):
    config = make_config(game_id="r", scenario="core:quad4", seats=SEATS3, seed=4, objectives_salt=2,
                         turn_limit="2-3")
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, Clock())
    while not game.game_over:
        for seat in game.alive_seats():
            game.set_ready(seat, None if game.phase in ("setup", "negotiation_1")
                           else game.orders(seat)["orders_hash"])
    report = build_report(read_events(gdir.journal_path))
    assert report["end_turn"] == config["end_turn"] == game.state.turn and report["end_turns"] == [2, 3]
    assert f"Скрытый последний ход: {game.state.turn} (из 2–3; местам не объявлен)" in render_report(report)


# ---------------------------------------------------------------- a) m11 finding 4: the eliminated seat's spend

def _eliminating_game(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    config = make_config(game_id="e", scenario="core:quad4", seats=SEATS3, seed=8, objectives_salt=3, turn_limit=3)
    gdir, keys = init_game_dir(tmp_path, config)
    Game.create(gdir, Clock())
    return TestClient(create_app(str(tmp_path), admin_key="adm", clock=Clock(), tick_interval=None)), keys


def _ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, None if game.phase in ("setup", "negotiation_1") else game.orders(seat)["orders_hash"])


def test_the_waker_of_an_eliminated_seat_posts_its_spend_and_the_report_counts_it_once(tmp_path):
    client, keys = _eliminating_game(tmp_path)
    game = client.app.state.registry.games["e"]
    while game.phase != "negotiation_1":
        _ready_all(game)
    clock, logs = WakerClock(), []
    dt = FakeDreamTeam(clock)
    waker = Waker(GameClient("http://testserver", keys["C"], transport=forward(client), host="dah-waker"),
                  DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)), SESSION,
                  clock=clock, sleep=clock.sleep, log=logs.append, post_game_minutes=2)
    assert waker.step() is False                     # turn 1 seen: the in-game spend is posted per phase
    for province in game.state.provinces.values():   # C loses every province: the turn eliminates it
        if province.owner == "C":
            province.owner, province.units = None, {}
    while game.state.turn == 1:
        _ready_all(game)
    assert not game.state.players["C"].alive and not game.game_over
    assert waker.step() is True and waker.eliminated
    waker.after_game_over()                          # the watch of its spend, before the game is over
    assert not [line for line in logs if "не удалось" in line], logs
    assert any("после партии" in line for line in logs)
    spends = [e for e in read_events(game.gdir.journal_path) if e["type"] == "spend_report" and e["seat"] == "C"]
    after = [e for e in spends if e["for_phase"] == "after_game_over"]
    assert after and all(e["at_phase"] != "game_over" for e in after)
    while not game.game_over:
        _ready_all(game)
    report = build_report(read_events(game.gdir.journal_path))
    c = report["totals"]["C"]
    in_game = [e for e in spends if e["for_phase"] != "after_game_over"][-1]["spent_usd"]
    assert c["spent_usd"] == after[-1]["spent_usd"] and c["spent_in_game_usd"] == in_game
    assert c["spent_after_game_usd"] == round(after[-1]["spent_usd"] - in_game, 6) > 0
    assert c["after_from"] == "eliminated" and c["eliminated_turn"] == 1
    # the after-game money is in no turn: the per-turn figures stop at the elimination
    assert all(t["spent_usd"].get("C", 0) <= in_game for t in report["turns"])
    assert "после выбытия (ход 1)" in render_report(report)
    # the living seats keep the old wording
    assert "after_from" not in report["totals"]["A"]


def _copy(src: Path, tmp_path: Path) -> Path:
    if not (src / "journal.jsonl").is_file():
        pytest.skip(f"measurement data {src} not present")
    dst = tmp_path / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("keys.json"))
    assert not (dst / "keys.json").exists()
    return dst


@pytest.mark.parametrize("name", ["m7", "m8", "m9", "m10", "m11"])
def test_old_journals_keep_their_spend_report(tmp_path, name):
    """Before core 0.8.2 the server refused an eliminated seat's after-game figures: no old journal has one, so no
    seat of a measured game gets the new elimination split (m11's Гамма keeps «после game_over $0.12, 0.0с»)."""
    gdir = _copy(RUNS / f"games-{name}" / name, tmp_path)
    report = build_report(read_events(gdir / "journal.jsonl"))
    assert not any("after_from" in t for t in report["totals"].values())
    if name == "m11":
        c = report["totals"]["C"]
        assert (round(c["spent_usd"], 2), round(c["spent_in_game_usd"], 2), round(c["spent_after_game_usd"], 2),
                c["after_game_s"]) == (2.68, 2.68, 0.12, 0.0)
        assert "после game_over $0.12, 0.0с" in render_report(report)
