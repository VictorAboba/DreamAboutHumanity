"""Core 0.8.0, fixes after the review of spy v2 and trade: two waiting finds in one resolution, the report's
fortification in estimate(defender="r3") (the bridge and a kept fortification), the goal's intel source, the
no-effect sabotage text, «ты» forms in a report copy, the trade replies' 200-char budget and kingmaking of provinces
and copies (docs/SPY_V2.md, docs/TRADE.md «Реализация: уточнения», «Исправления по ревью»)."""

from __future__ import annotations

import json
import re

import httpx

import pytest

from dah.bridge.client import GameClient
from dah.bridge.tools import TOOLS, Bridge
from dah.cli import goal_text
from dah.engine import espionage as esp
from dah.engine import orders_hash
from dah.engine import trade as tr
from dah.engine.content import LEGACY_PACKS_DIR
from dah.engine.view import _sabotage_effect_text
from dah.game import Game, make_config
from dah.report_trade import KINGMAKING_COPY_VALUE, KINGMAKING_PROVINCE_VALUE, trade_report
from dah.store import init_game_dir
from test_core080_trade_b import SEATS, make_game, report, to_next_turn


@pytest.fixture
def sure(monkeypatch):
    """Every round succeeds and nobody is caught."""
    monkeypatch.setattr(esp, "chances", lambda rs, key, **kw: (1.0, 0.0))


def _pending(game: Game, pid: str, seat: str) -> None:
    game.state.discoveries[pid] = {"id": "core:sealed_vault", "state": "pending", "known": [seat],
                                   "pending": {"seat": seat, "turn": 0}}


# ---------------------------------------------------------------- two waiting finds (DiscoveryStep.choose)

def test_two_waiting_finds_resolve_in_one_turn_without_trade(tmp_path):
    game = make_game(tmp_path, discoveries=True, trade=False)
    _pending(game, "P06", "A")
    _pending(game, "P07", "A")
    to_next_turn(game)
    chosen = sorted(e["province"] for e in report(game, 1)["events"] if e["kind"] == "discovery_chosen")
    assert chosen == ["P06", "P07"]


def test_a_promised_find_and_a_free_one_in_the_same_resolution(tmp_path):
    game = make_game(tmp_path, discoveries=True)
    _pending(game, "P06", "A")             # A's own, resolved by default
    _pending(game, "P08", "A")             # a second free one after it: the loop goes on
    _pending(game, "P07", "A")             # promised to B: back to site, then B's
    tid = game.propose_trade("A", "Соло", {"P07": 1})["id"]
    game.respond_trade("B", tid, "accept")
    to_next_turn(game)
    chosen = sorted(e["province"] for e in report(game, 1)["events"] if e["kind"] == "discovery_chosen")
    assert chosen == ["P06", "P08"]
    entry = game.state.discoveries["P07"]
    assert entry["state"] == "pending" and entry["pending"]["seat"] == "B"


# ---------------------------------------------------------------- estimate(defender="r3") and the fortification

def test_the_bridge_sends_the_flags_only_when_given():
    spec = next(t for t in TOOLS if t.name == "estimate")
    assert "default" not in spec.schema["properties"]["fortified"]
    assert "по донесению" in spec.schema["properties"]["fortified"]["description"]


def test_estimate_by_report_through_the_bridge_keeps_the_reports_fortify(tmp_path, sure):
    game, rid, _ = _observed_game(tmp_path, fortify=True)

    def server(request: httpx.Request) -> httpx.Response:     # what app.py does: the body to Game.estimate
        return httpx.Response(200, json=game.estimate(json.loads(request.content), "A"))
    bridge = Bridge(GameClient("http://game.test", "k", transport=httpx.MockTransport(server)))
    bridge.call("estimate", {"attacker": {"assault": 9}, "defender": rid})
    via_bridge = game.estimate({"attacker": {"assault": 9}, "defender": rid}, "A")
    direct_fortified = game.estimate({"attacker": {"assault": 9}, "defender": rid, "fortified": True}, "A")
    assert via_bridge["win_prob"] == direct_fortified["win_prob"]
    sent = []
    bridge2 = Bridge(GameClient("http://game.test", "k", transport=httpx.MockTransport(
        lambda r: sent.append(json.loads(r.content)) or httpx.Response(200, json={"ok": True}))))
    bridge2.call("estimate", {"attacker": {"assault": 9}, "defender": rid})
    assert "fortified" not in sent[-1] and "hasty" not in sent[-1]


def _observed_game(tmp_path, fortify: bool) -> tuple[Game, str, str]:
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "agent"} for s in "ABC"]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="e", scenario="core:blitz_spy", seats=seats, seed=4))
    game = Game.create(gdir, _Clock())
    _ready(game)                                                               # П1 -> Д
    b_cap = game.state.players["B"].home
    game.submit("A", [{"kind": "spy", "shade": "s1", "mission": "observe", "target": b_cap, "rounds": 1}])
    if fortify:
        game.submit("B", [{"kind": "fortify", "province": b_cap}])
    _ready(game)                                                               # разведсрез -> П2
    rid = game.get_intel("A", fmt="json")["intel"]["reports"][0]["id"]
    return game, rid, b_cap


class _Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def _ready(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def test_the_reports_fortify_wins_over_an_explicit_false(tmp_path, sure):
    game, rid, _ = _observed_game(tmp_path, fortify=True)
    plain = game.estimate({"attacker": {"assault": 9}, "defender": rid}, "A")
    explicit = game.estimate({"attacker": {"assault": 9}, "defender": rid, "fortified": False, "hasty": False}, "A")
    assert explicit["win_prob"] == plain["win_prob"]
    assert "win_prob_if_fortified" not in explicit and "если укрепится" not in explicit["report"]


def test_a_kept_fortification_counts_in_the_report_forecast():
    report = {"id": "r3", "turn": 4, "facts": {"province": "P16", "units": {"core:assault": 3}, "fortified": True,
                                               "planned": {}}}
    kept = esp.report_defender(report, 4, persists=True)
    assert kept["fortified"] and "укрепление держится (укр. в донесении)" in kept["header"]
    assert "в П2 возможны укрепление" not in kept["header"]
    assert "если войска не уходили" in esp.report_defender(report, 5, persists=True)["header"]
    off = esp.report_defender(report, 4)                                        # no fortify_persists
    assert not off["fortified"] and "в П2 возможны укрепление (1 ОК)" in off["header"]
    empty = {**report, "facts": {**report["facts"], "units": {}}}
    assert not esp.report_defender(empty, 4, persists=True)["fortified"]


def test_the_server_passes_fortify_persists(tmp_path, sure, monkeypatch):
    game, rid, b_cap = _observed_game(tmp_path, fortify=False)
    seen = {}
    real = esp.report_defender

    def spy_(report, turn, rid=None, persists=False):
        seen["persists"] = persists
        return real(report, turn, rid, persists)
    monkeypatch.setattr(esp, "report_defender", spy_)
    game.estimate({"attacker": {"assault": 9}, "defender": rid}, "A")
    assert seen["persists"] == bool(game.ruleset.stat("combat.fortify_persists"))


# ---------------------------------------------------------------- the goal's intel source

def test_the_goal_names_the_scoring_missions():
    config = make_config(game_id="g", scenario="core:quad4", seats=SEATS, seed=1)
    line = goal_text(config, "A")
    assert "разведка (secret, read_orders, sabotage; ≤3)" in line and "успешные задания Тени" not in line
    old = make_config(game_id="g", scenario="core:quad4", seats=SEATS, seed=1,
                      pack_dirs=[str(LEGACY_PACKS_DIR / "core-0.7.1")])
    assert "разведка (успешные задания Тени)" in goal_text(old, "A")


# ---------------------------------------------------------------- the no-effect sabotage text

def test_a_strike_without_effect_says_it_once(tmp_path):
    game = make_game(tmp_path)
    none = {"shade": "s3", "province": "P07", "effects": [], "intel_point": False, "reason": "no_effect"}
    text = _sabotage_effect_text(none, game.ruleset, None)
    assert text == "удар Тени s3 по P07: без счётного эффекта (бой/стройка/бунт) — очка нет"
    taken = {"shade": "s3", "province": "P07", "effects": ["battle"], "intel_point": False, "reason": "taken"}
    assert _sabotage_effect_text(taken, game.ruleset, None).endswith(
        "бой с потерями — очка нет (эффект уже засчитан другому удару)")


# ---------------------------------------------------------------- a copy names the seller

def test_a_copy_never_calls_the_buyer_ty(tmp_path):
    game = make_game(tmp_path, espionage=True)
    entry = {"id": "c1", "from": "A", "turn": 2, "source": "r1", "trade": "t",
             "report": {"id": "r1", "mission": "core:observe", "behavior": esp.OBSERVE, "target": "P11",
                        "round": 1, "rounds": 1, "turn": 2,
                        "facts": {"province": "P11", "owner": "C", "units": {"core:assault": 2},
                                  "status": {"occupied": {"by": "C", "from": "A", "since": 1}}}}}
    text = tr.copy_text(entry, game.ruleset, game.state)
    assert "взята у Нико" in text and re.search(r"(?<![\w-])ты(?![\w-])", text) is None


# ---------------------------------------------------------------- the trade replies ≤ 200 chars

def test_the_worst_case_trade_replies_fit_200(tmp_path):
    game = make_game(tmp_path, espionage=True)
    out = game.propose_trade("A", "Соло", {"P07": 1, "alloy": 5, "provisions": 3}, {"alloy": 10, "P02": 1},
                             later={"give": {"alloy": 5}, "want": {"provisions": 5}, "by_turn": 3})
    assert len(out["text"]) <= tr.REPLY_LIMIT and "обещание — в list_trades" in out["text"]
    assert "обещание к ходу 3" in game.trades("A")["text"]          # the promise is in list_trades
    answer = game.respond_trade("B", out["id"], "accept")
    assert len(answer["text"]) <= tr.REPLY_LIMIT and answer["text"].startswith("в залоге до расчёта: Сплав 10")


def test_a_single_province_reply_keeps_its_warnings(tmp_path):
    game = make_game(tmp_path)
    out = game.propose_trade("A", "Соло", {"P07": 1}, {"alloy": 10})
    text = out["text"]
    assert len(text) <= tr.REPLY_LIMIT
    assert "P07 «" in text and "в расчёте: P07 → Соло: оккупация" in text and text.endswith("ответ до конца хода")
    assert tr.fit_reply(["x" * 201, "y" * 300]) == "y" * 199 + "…"


# ---------------------------------------------------------------- kingmaking of provinces and copies

def _scores(turns, board):
    return [{"type": "turn_resolved", "turn": t, "scores": board, "resources": {"A": {"core:alloy": 10}}}
            for t in turns]


def test_kingmaking_counts_a_province_and_a_copy_given_by_the_last_seat():
    ev = _scores(range(1, 6), {"A": 10, "B": 20, "C": 1})
    ev += [{"type": "trade_proposed", "seat": "C", "to": "B", "trade_id": "t1", "give": {"provinces": ["P09"]},
            "want": {}, "turn": 6},
           {"type": "trade_accepted", "seat": "B", "trade_id": "t1", "turn": 6},
           {"type": "trade_settled", "trade_id": "t1", "seats": ["B", "C"], "give": {"provinces": ["P09"]},
            "want": {}, "at": "resolution", "turn": 6},
           {"type": "province_transferred", "province": "P09", "status": "occupation", "from": "C", "to": "B",
            "trade_id": "t1", "seats": ["B", "C"], "turn": 6},
           {"type": "intel_copied", "copy_id": "c1", "source": "r2", "mission": "core:observe", "target": "P01",
            "from": "C", "to": "B", "trade_id": "t2", "seats": ["B", "C"], "turn": 6},
           {"type": "disposition", "seat": "C", "province": "P10", "behavior": "release", "owner_after": "B",
            "turn": 6},
           {"type": "disposition", "seat": "C", "province": "P11", "behavior": "release", "owner_after": None,
            "turn": 6},
           {"type": "province_transferred", "province": "P02", "status": "occupation", "from": "A", "to": "B",
            "trade_id": "t3", "seats": ["A", "B"], "turn": 6},
           *_scores([6], {"A": 10, "B": 25, "C": 0}), {"type": "game_over", "turn": 6}]
    seats = trade_report(ev, ["A", "B", "C"])["seats"]
    c = seats["C"]
    assert c["kingmaking_provinces"] == 2 and c["kingmaking_copies"] == 1
    assert c["kingmaking"] == 2 * KINGMAKING_PROVINCE_VALUE + KINGMAKING_COPY_VALUE
    assert seats["A"]["kingmaking"] == 0.0 and seats["A"]["kingmaking_provinces"] == 0      # A is not last
