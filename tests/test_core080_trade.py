"""Core 0.8.0, trade step A (docs/TRADE.md 1.1-1.3, 2.1, 3, 5, 9-11, 16а): the `trade` switch, offers and their
lifecycle, resource trades settled at the accept, OFFER_EXISTS / replaces / reply_to, ready clearing, the fog (an
executed trade is fully hidden), journal events, tools, routes and replay."""

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from dah.bridge.tools import TOOLS
from dah.engine import diplomacy as dip
from dah.engine import orders_hash, state_hash
from dah.engine import trade as tr
from dah.engine.model import GameState
from dah.engine.rules import TRADE_TOPIC_LIMIT, rules_text, rules_topic
from dah.game import Game, GameError, game_ruleset, make_config, replay
from dah.journal import read_events, view_events
from dah.server import create_app
from dah.store import init_game_dir
from dah.waker.text import wake_text
from slice_helpers import slice_config

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"},
         {"seat": "C", "nick": "Сид", "kind": "agent"}]
ALLOY, FOOD = "core:alloy", "core:provisions"


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def make_game(tmp_path, trade: bool = True, **systems) -> Game:
    config = slice_config(game_id="t1", scenario="core:slice3", seats=SEATS, seed=7,
                          systems={"espionage": False, "conquest": False, "trade": trade, **systems})
    return Game.create(init_game_dir(tmp_path, config)[0], Clock())


@pytest.fixture
def game(tmp_path) -> Game:
    return make_game(tmp_path)


def ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def journal(game: Game, kind: str) -> list[dict]:
    return [e for e in read_events(game.gdir.journal_path) if e["type"] == kind]


def res(game: Game, seat: str) -> dict:
    return dict(game.state.players[seat].resources)


def error(call) -> GameError:
    with pytest.raises(GameError) as err:
        call()
    return err.value


# ---------------------------------------------------------------- the switch (docs/TRADE.md 8, 15)

def test_trade_off_leaves_no_trace(tmp_path):
    off = make_game(tmp_path / "off", trade=False)
    assert "trade" not in json.dumps(off.ruleset.to_dict())
    assert not any(k.startswith("trade.") for k in off.ruleset.stats)
    assert not set(off.phase_info("A")["allowed"]) & {"propose_trade", "respond_trade", "list_trades"}
    assert "ОБМЕН" not in off.rules() and "trade" not in off.rules()
    assert "Продление: пока пакт со сроком действует" in rules_topic(off.ruleset, "pacts")
    assert "trades" not in off.state_json("A") and "ОБМЕН" not in off.state_text("A")
    assert "trades" not in off.state.to_dict() or not off.state.trades
    for call in (lambda: off.propose_trade("A", "Соло", {"alloy": 1}), lambda: off.trades("A"),
                 lambda: off.respond_trade("B", "trade-A-B-1", "accept")):
        assert error(call).code == "NOT_AVAILABLE"
    on = make_game(tmp_path / "on")
    assert on.ruleset.hash != off.ruleset.hash
    # an empty trade list is not hashed: the state of a trade game hashes as one without trade
    plain = GameState.from_dict({k: v for k, v in on.state.to_dict().items() if k not in ("trades", "trade_seq")})
    assert state_hash(on.state) == state_hash(plain)


def test_trade_is_on_in_the_core_scenarios_and_off_in_old_configs():
    specs = SEATS[:2]
    for scenario in ("slice3", "slice2", "quad4"):
        config = make_config(game_id="g", scenario=f"core:{scenario}", seats=specs, seed=1)
        assert config["systems"]["trade"] is True
        assert config["diplomacy_limits"]["trade_proposals_per_phase"] == 3
    blitz = make_config(game_id="g", scenario="core:blitz_spy", seats=specs, seed=1)
    assert "trade" not in blitz["systems"]          # off and not overridden: the config keeps its old keys
    old = dict(blitz, diplomacy_limits={"gifts_per_phase": 3, "pact_proposals_per_phase": 3})
    assert game_ruleset(old).scenario.diplomacy_limits["trade_proposals_per_phase"] == 3


def test_tools_and_phases(game):
    assert {"propose_trade", "respond_trade", "list_trades"} <= set(game.phase_info("A")["allowed"])
    ready_all(game)
    assert game.phase == "actions"
    allowed = game.phase_info("A")["allowed"]
    assert "list_trades" in allowed and "propose_trade" not in allowed
    err = error(lambda: game.propose_trade("A", "Соло", {"alloy": 5}))
    assert err.code == "PHASE_LOCKED" and "list_trades" in err.message
    assert game.trades("A")["text"].startswith("обменов нет")
    names = {t.name for t in TOOLS}
    assert {"propose_trade", "respond_trade", "list_trades"} <= names


# ---------------------------------------------------------------- offers (docs/TRADE.md 1.2, 1.3)

def test_a_resource_trade_settles_at_the_accept(game):
    out = game.propose_trade("A", "Соло", {"alloy": 10}, {"provisions": 5})
    tid = out["id"]
    assert tid == "trade-A-B-1" and out["status"] == "proposed" and len(out["text"]) <= 200
    assert "даёшь Сплав 10, просишь Провиант 5" in out["text"]
    assert res(game, "A")[ALLOY] == 30          # an offer reserves nothing
    done = game.respond_trade("B", tid, "accept")
    assert done["status"] == "done" and done["text"] == "исполнен: +Сплав 10, −Провиант 5"
    assert res(game, "A")[ALLOY] == 20 and res(game, "A")[FOOD] == 20
    assert res(game, "B")[ALLOY] == 40 and res(game, "B")[FOOD] == 10
    again = game.respond_trade("B", tid, "accept")
    assert again["already"] and res(game, "B")[ALLOY] == 40
    assert journal(game, "trade_settled")[0]["at"] == "accept"
    assert [e["type"] for e in read_events(game.gdir.journal_path) if e["type"].startswith("trade_")] == [
        "trade_proposed", "trade_accepted", "trade_settled"]
    inbox_a = game.inbox("A")["text"]
    assert "исполнен: +Провиант 5, −Сплав 10" in inbox_a
    assert "respond_trade(id=\"trade-A-B-1\"" in game.inbox("B")["text"]
    # the accept is one op in the diplomacy log
    assert [op["op"] for op in game.diplomacy_log] == ["trade_offer", "trade_answer"]


def test_proposer_side_checked_in_full_recipient_side_only_publicly(game):
    err = error(lambda: game.propose_trade("A", "Соло", {"alloy": 31}))
    assert err.code == "NOT_ENOUGH_RESOURCES" and "свободно 30" in err.message
    # asking more than the recipient has is fine: its treasury is not looked at (no leak)
    out = game.propose_trade("A", "Соло", {}, {"alloy": 500})
    err = error(lambda: game.respond_trade("B", out["id"], "accept"))
    assert err.code == "NOT_ENOUGH_RESOURCES" and "предложение ждёт" in err.message
    assert tr.trade_by_id(game.state, out["id"])["status"] == "proposed"
    assert error(lambda: game.propose_trade("A", "Соло")).code == "INVALID"     # an empty trade
    assert error(lambda: game.propose_trade("A", "Нико", {"alloy": 1})).code == "INVALID"   # oneself
    err = error(lambda: game.propose_trade("A", "Никто", {"alloy": 1}))
    assert err.code == "UNKNOWN_ID" and "Соло" in err.message


def test_proposer_lacks_at_the_accept(game):
    first = game.propose_trade("A", "Соло", {"alloy": 25}, {"provisions": 1})["id"]
    second = game.propose_trade("A", "Сид", {"alloy": 25}, {"provisions": 1})["id"]   # an auction is allowed
    game.respond_trade("C", second, "accept")
    out = game.respond_trade("B", first, "accept")
    assert out["status"] == "failed" and out["text"] == "не исполним: у предложившего больше нет обещанного"
    t = tr.trade_by_id(game.state, first)
    assert t["failed_reason"] == "proposer_lacks"
    assert res(game, "B")[ALLOY] == 30
    assert "свободно 5, нужно 25" in game.inbox("A")["text"]
    assert "свободно" not in game.inbox("B")["text"]          # the recipient learns no figure
    failed = journal(game, "trade_failed")[0]
    assert failed["short_side"] == "A" and failed["reason"] == "proposer_lacks"


def test_offer_exists_already_replaces_and_reply_to(game):
    first = game.propose_trade("A", "Соло", {"alloy": 10}, {"provisions": 5})["id"]
    same = game.propose_trade("A", "Соло", {"alloy": 10}, {"provisions": 5})    # a swarm teammate, the same offer
    assert same["already"] and same["id"] == first and len(game.state.trades) == 1
    err = error(lambda: game.propose_trade("A", "Соло", {"alloy": 12}, {"provisions": 5}))
    assert err.code == "OFFER_EXISTS" and first in err.message and f'replaces="{first}"' in err.message
    new = game.propose_trade("A", "Соло", {"alloy": 12}, {"provisions": 5}, replaces=first)["id"]
    assert tr.trade_by_id(game.state, first)["status"] == "replaced"
    err = error(lambda: game.respond_trade("B", first, "accept"))
    assert err.code == "UNKNOWN_ID" and new in err.message
    counter = game.propose_trade("B", "Нико", {"provisions": 5}, {"alloy": 15}, reply_to=new)["id"]
    assert tr.trade_by_id(game.state, new)["status"] == "countered"
    assert tr.trade_by_id(game.state, new)["countered_by"] == counter
    assert "встречное на твоё" in game.inbox("A")["text"]
    assert [e["trade_id"] for e in journal(game, "trade_replaced")] == [first]
    assert [e["trade_id"] for e in journal(game, "trade_countered")] == [new]
    # one open offer per ordered pair: the other direction is a pair of its own
    assert error(lambda: game.propose_trade("B", "Нико", {"provisions": 1})).code == "OFFER_EXISTS"
    assert error(lambda: game.propose_trade("A", "Соло", {"alloy": 1}, reply_to="trade-A-B-9")).code == "UNKNOWN_ID"


def test_reject_withdraw_and_the_limit_per_phase(game):
    t1 = game.propose_trade("A", "Соло", {"alloy": 1})["id"]
    assert game.respond_trade("B", t1, "reject")["status"] == "rejected"
    t2 = game.propose_trade("A", "Соло", {"alloy": 2})["id"]
    err = error(lambda: game.respond_trade("A", t2, "accept"))
    assert err.code == "INVALID" and "reject" in err.message
    assert game.respond_trade("A", t2, "reject")["status"] == "withdrawn"
    assert "отозвал" in game.inbox("B")["text"]
    game.propose_trade("A", "Соло", {"alloy": 3})
    err = error(lambda: game.propose_trade("A", "Сид", {"alloy": 3}))
    assert err.code == "LIMIT_EXCEEDED" and "обмена" in err.message
    assert error(lambda: game.respond_trade("B", t1, "accept")).code == "INVALID"


def test_an_offer_clears_the_recipients_ready_not_the_answer(game):
    game.set_ready("B", None)
    game.set_ready("A", None)
    tid = game.propose_trade("A", "Соло", {"alloy": 1})["id"]
    assert "B" not in game.ready_seats and "A" in game.ready_seats
    assert journal(game, "ready_cleared")[-1]["reason"] == "trade_proposed"
    game.respond_trade("B", tid, "accept")
    assert "A" in game.ready_seats
    assert game.phase_info("B").get("trades_waiting") is None
    game.propose_trade("C", "Соло", {"alloy": 1})
    info = game.phase_info("B")
    assert info["trades_waiting"] == 1 and "обменов ждут ответа: 1" in info["text"]
    assert "Обменов ждут ответа: 1" in wake_text(info)


def test_goods_forms(game):
    flat = game.propose_trade("A", "Соло", '{"alloy": 2}', {"Провиант": 1})
    t = tr.trade_by_id(game.state, flat["id"])
    assert t["give"] == {"resources": {ALLOY: 2}} and t["want"] == {"resources": {FOOD: 1}}
    nested = game.propose_trade("A", "Сид", {"resources": {"alloy": 2}}, None)
    assert tr.trade_by_id(game.state, nested["id"])["want"] == {}
    # this game has neither conquest nor espionage: provinces and reports are not traded (step B, test_core080_trade_b)
    for goods, why in (({"P01": 1}, "завоевании"), ({"provinces": ["P01"]}, "завоевании"),
                       ({"r4": 1}, "шпионаж выключен"), ({"reports": ["r4"]}, "шпионаж выключен")):
        err = error(lambda g=goods: game.propose_trade("B", "Нико", g))
        assert err.code == "NOT_AVAILABLE" and why in err.message
    err = error(lambda: game.propose_trade("B", "Нико", {"gold": 1}))
    assert err.code == "UNKNOWN_ID"
    err = error(lambda: game.propose_trade("B", "Нико", {"alloy": 1, "provisions": 1, "creed": 1},
                                           {"resources": {"alloy": 1}, "extra": 1}))
    assert err.code == "INVALID"


def test_later_is_recorded_not_carried_out(game):
    later = {"give": {"alloy": 5}, "want": {"provisions": 2}, "by_turn": 3}
    tid = game.propose_trade("A", "Соло", {"alloy": 1}, {}, later=later)["id"]
    game.respond_trade("B", tid, "accept")
    assert res(game, "A")[ALLOY] == 29            # only the immediate part moved
    text = game.trades("A")["text"]
    assert "обещано: ты → Соло: Сплав 5 к ходу 3" in text and "обещано: Соло → тебе: Провиант 2" in text
    assert "обещано" not in game.state_text("A")         # promises only in list_trades (docs/TRADE.md 16а)
    made = journal(game, "promise_made")
    assert [(e["seat"], e["to"], e["by_turn"]) for e in made] == [("A", "B", 3), ("B", "A", 3)]
    for bad in ({"give": {"alloy": 1}, "by_turn": 99}, {"give": {"alloy": 1}, "by_turn": 0}, {"by_turn": 2}):
        assert error(lambda b=bad: game.propose_trade("A", "Сид", {}, {}, later=b)).code == "INVALID"
    game.propose_trade("A", "Сид", {}, {}, later={"give": {"alloy": 1}, "by_turn": 2}, fulfils=tid)
    assert tr.trade_by_id(game.state, "trade-A-C-1")["fulfils"] == tid
    game.ruleset = dataclasses.replace(game.ruleset, stats={**game.ruleset.stats, "trade.later": 0})
    err = error(lambda: game.propose_trade("C", "Соло", {}, {}, later={"give": {"alloy": 1}, "by_turn": 2}))
    assert err.code == "NOT_AVAILABLE"


def test_p2_exit_price_of_a_reserve(game):
    ready_all(game)
    cap = next(p.id for p in game.state.owned("B") if p.type == "core:capital")
    game.submit("B", [{"kind": "recruit", "province": cap, "unit": "assault", "count": 2}])
    ready_all(game)
    assert game.phase == "negotiation_2"
    tid = game.propose_trade("A", "Соло", {"alloy": 1}, {"alloy": 20})["id"]
    err = error(lambda: game.respond_trade("B", tid, "accept"))
    assert err.code == "NOT_ENOUGH_RESOURCES" and "1 ОК" in err.message and "75%" in err.message
    err = error(lambda: game.propose_trade("B", "Сид", {"alloy": 20}))
    assert err.code == "NOT_ENOUGH_RESOURCES" and "свободно 6" in err.message


# ---------------------------------------------------------------- resolution: lapse, pruning (docs/TRADE.md 1.1)

def test_unanswered_offers_lapse_and_old_trades_leave_the_state(game):
    done = game.propose_trade("A", "Соло", {"alloy": 1})["id"]
    game.respond_trade("B", done, "accept")
    open_ = game.propose_trade("A", "Сид", {"alloy": 1})["id"]
    for _ in range(3):
        ready_all(game)
    assert game.state.turn == 2
    assert tr.trade_by_id(game.state, open_)["status"] == "lapsed"
    assert journal(game, "trade_lapsed")[0]["trade_id"] == open_
    assert "сгорело" in game.inbox("A")["text"]
    assert "trade_lapsed" in [e["kind"] for e in game.history("A")["events"]] if "events" in game.history("A") \
        else True
    assert {t["id"] for t in game.state.trades} == {done, open_}
    for _ in range(6):
        ready_all(game)
    assert game.state.turn == 4 and game.state.trades == []
    nxt = game.propose_trade("A", "Соло", {"alloy": 1})["id"]
    assert nxt == "trade-A-B-2"           # ids keep counting after old trades left the state


# ---------------------------------------------------------------- fog (docs/TRADE.md 5, 16а)

def test_an_executed_trade_is_fully_hidden(game):
    game.set_ready("C", None)
    tid = game.propose_trade("A", "Соло", {"alloy": 10}, {"provisions": 5})["id"]
    game.respond_trade("B", tid, "accept")
    assert "C" in game.ready_seats
    assert tid not in json.dumps(game.state_json("C"), ensure_ascii=False)
    assert game.state_json("A")["trades"][0]["id"] == tid and game.state_json("C")["trades"] == []
    assert "обмен" not in game.inbox("C")["text"].lower()
    assert game.trades("C")["text"].startswith("обменов нет")
    events = read_events(game.gdir.journal_path)
    seen_c = view_events(events, seat="C")
    assert not [e for e in seen_c if e["type"].startswith("trade_")]
    assert [e["type"] for e in view_events(events, seat="A") if e["type"].startswith("trade_")]
    assert not [e for e in view_events(events, spectator=True) if e["type"].startswith("trade_")]
    for _ in range(3):
        ready_all(game)
    report = json.loads(json.dumps(game.gdir.load_turn("reports", 1)))
    kinds = {e["kind"] for e in report["by_seat"]["A"]}
    assert "trade_proposed" in kinds and "trade_settled" in kinds
    assert not [e for e in report["by_seat"]["C"] if e["kind"].startswith("trade_")]
    assert not [e for e in report["public"]["diplomacy"] if e["kind"].startswith("trade_")]
    history = json.dumps(game.history("C"), ensure_ascii=False)
    assert tid not in history and tid in json.dumps(game.history("A"), ensure_ascii=False)


def test_ready_cleared_by_an_offer_is_private(game):
    game.set_ready("B", None)
    game.propose_trade("A", "Соло", {"alloy": 1})
    events = read_events(game.gdir.journal_path)
    cleared = lambda seat: [e for e in view_events(events, seat=seat) if e["type"] == "ready_cleared"]  # noqa: E731
    assert cleared("B") and not cleared("C") and not cleared("A")


# ---------------------------------------------------------------- get_state and list_trades (docs/TRADE.md 9)

def test_state_line_and_list(game):
    game.propose_trade("A", "Соло", {"alloy": 10}, {"provisions": 5})
    game.propose_trade("C", "Соло", {"alloy": 2})
    game.propose_trade("B", "Нико", {}, {"alloy": 3})
    line = next(x for x in game.state_text("B").splitlines() if x.startswith("ОБМЕН"))
    assert len(line) <= 160 and "+1 (list_trades)" in line and "respond_trade" in line
    js = game.state_json("B")["trades"]
    assert {t["id"] for t in js} == {"trade-A-B-1", "trade-B-C-1", "trade-A-B-2"}
    text = game.trades("B")["text"]
    assert len(text) <= 600 and "trade-A-B-1: от Нико — даёт Сплав 10, просит Провиант 5" in text
    assert "trade-A-B-2: ты → Нико — просишь Сплав 3; ждёт ответа" in text


def test_list_text_stays_within_its_limit(game):
    state = game.state
    for k in range(40):
        state.trades.append({"id": f"trade-A-B-{k + 10}", "a": "A", "b": "B", "give": {"resources": {ALLOY: 1}},
                             "want": {}, "later": None, "status": "rejected", "proposed_turn": 1, "ended_turn": 1})
    text = tr.list_text(state, game.ruleset, "A", lambda s: state.players[s].nick)
    assert len(text) <= 600 and "… ещё" in text


# ---------------------------------------------------------------- rules (docs/TRADE.md 10)

def test_rules_core_line_and_topic(game):
    core = game.rules()
    assert 'ОБМЕН: propose_trade(to, give, want) → respond_trade(id, accept|reject); подробно — rules(topic="trade")' \
        in core
    assert ", trade," in core
    topic = game.rules("trade")
    assert len(topic) <= 1600 and topic.startswith("ОБМЕН (только П1 и П2)") and "OFFER_EXISTS" in topic
    assert "≤3 предложений за фазу" in topic and "later=" in topic
    for alias in ("обмен", "торговля", "trades", "сделка", "ОБМЕН"):
        assert game.rules(alias) == topic
    assert "Продление: пока пакт со сроком действует" in game.rules("pacts")    # the pacts topic is unchanged


def test_pacts_short_core_with_trade(tmp_path):
    specs = SEATS
    config = make_config(game_id="g", scenario="core:quad4", seats=specs, seed=3, objectives_salt=9)
    rs = game_ruleset(config)
    text = rules_text(rs)
    assert 'нарушает пакт публично. Продление: rules(topic="pacts").' in text
    assert "ОБМЕН: propose_trade" in text and len(text) <= 5970
    off = game_ruleset(make_config(game_id="g", scenario="core:quad4", seats=specs, seed=3, objectives_salt=9,
                                   systems={"trade": False}))
    assert "Продление — propose_pact тому же партнёру, пока пакт действует" in rules_text(off)


# ---------------------------------------------------------------- engine ops and replay (docs/TRADE.md 11)

def test_ops_apply_exactly_and_refuse_a_foreign_state(game):
    state = GameState.from_dict(game.state.to_dict())
    tid = game.propose_trade("A", "Соло", {"alloy": 10}, {"provisions": 5})["id"]
    game.respond_trade("B", tid, "accept")
    dip.apply_ops(state, game.diplomacy_log)
    assert state_hash(state) == state_hash(game.state)
    with pytest.raises(dip.DiplomacyError):
        dip.apply_op(state, game.diplomacy_log[1])         # answered already


def test_a_game_with_trades_replays(tmp_path):
    game = make_game(tmp_path, espionage=True, conquest=True)
    t1 = game.propose_trade("A", "Соло", {"alloy": 10}, {"provisions": 5})["id"]
    game.respond_trade("B", t1, "accept")
    game.propose_trade("C", "Нико", {"alloy": 3})
    for _ in range(3):
        ready_all(game)
    t2 = game.propose_trade("B", "Сид", {"provisions": 2}, {"alloy": 1})["id"]
    ready_all(game)
    ready_all(game)
    game.respond_trade("C", t2, "accept")       # an accept in negotiation_2, after the recon step
    game.propose_trade("A", "Сид", {"alloy": 1}, later={"want": {"alloy": 2}, "by_turn": 4})
    ready_all(game)
    assert game.state.turn == 3
    rep = replay(game.gdir)
    assert rep.ok and len(rep.turns) == 2, rep


# ---------------------------------------------------------------- HTTP routes (docs/TRADE.md 9)

def test_http_routes(tmp_path):
    config = slice_config(game_id="h1", scenario="core:slice2", seats=SEATS[:2], seed=7,
                          systems={"espionage": False, "conquest": False, "trade": True})
    gdir, keys = init_game_dir(tmp_path, config)
    clock = Clock()
    Game.create(gdir, clock)
    client = TestClient(create_app(str(tmp_path), admin_key="adm", clock=clock, tick_interval=None))
    head = {s: {"Authorization": f"Bearer {k}"} for s, k in keys.items()}
    out = client.post("/v1/trades", headers=head["A"],
                      json={"to": "Соло", "give": {"alloy": 4}, "want": '{"provisions": 2}'}).json()
    assert out["ok"] and out["id"] == "trade-A-B-1"
    listed = client.get("/v1/trades", headers=head["B"]).json()
    assert listed["ok"] and "trade-A-B-1: от Нико" in listed["text"]
    done = client.post("/v1/trades/trade-A-B-1/respond", headers=head["B"], json={"decision": "accept"}).json()
    assert done["ok"] and done["status"] == "done"
    bad = client.post("/v1/trades/trade-A-B-1/respond", headers=head["A"], json={"decision": "accept"}).json()
    assert not bad["ok"] and bad["error"]["code"] == "INVALID"


ALL_ON = {"espionage": True, "conquest": True, "discoveries": True, "trade": True}


@pytest.mark.parametrize("scenario,seats,artifacts,extra", [
    ("quad4", 2, "draft", 2), ("quad4", 3, "draft", 2), ("quad4", 4, "draft", 2), ("quad4", 4, None, None),
    ("slice3", 3, "draft", 2), ("slice2", 2, None, 2), ("blitz_score", 2, None, None), ("blitz_finds", 2, None, None),
    ("blitz_spy", 2, None, None), ("blitz_test", 2, None, None)])
def test_the_rules_gate_with_trade(tmp_path, scenario, seats, artifacts, extra):
    """docs/TRADE.md 10, 14 (G7): every system on, Game.rules() <= 5970 and never cut, the setup phase included."""
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    config = make_config(game_id="g", scenario=f"core:{scenario}", seats=specs, seed=3, systems=ALL_ON,
                         objectives_salt=9, artifacts=artifacts,
                         discoveries={"extra": extra} if extra is not None else None)
    game = Game.create(init_game_dir(tmp_path, config)[0], Clock(), start=False)
    game.start()
    while True:
        text = game.rules()
        assert len(text) <= 5970 and "сокращено" not in text and "ОБМЕН: propose_trade" in text, (game.phase, len(text))
        if game.phase != "setup":
            break
        for seat in game.alive_seats():
            game.set_ready(seat, None)
    assert len(game.rules("trade")) <= TRADE_TOPIC_LIMIT
