"""Core 0.8.0, trade step B (docs/TRADE.md 1.4-1.5, 2.2-2.3, 3, 6.3): provinces in trades (escrow at the accept,
settlement at step 4b, the giver's units leaving, PROVINCE_PROMISED / BUILD_IN_PROGRESS, one province a turn, the
capitals rule, a waiting find choice passing to the receiver, `hold_foreign.via: capture`), certified copies of
spy reports (c<k>, the head before the accept, no resale, no intel points) and promises with provinces."""

from __future__ import annotations

import dataclasses

import pytest

from dah.engine import orders_hash, state_hash
from dah.engine import scoring as sc_
from dah.engine import trade as tr
from dah.engine.model import GameState
from dah.engine.rules import TRADE_TOPIC_LIMIT, rules_topic
from dah.game import Game, GameError, game_ruleset, make_config, replay
from dah.journal import read_events, view_events
from dah.store import init_game_dir
from slice_helpers import slice_config

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"},
         {"seat": "C", "nick": "Сид", "kind": "agent"}]
ALLOY, FOOD, ASSAULT = "core:alloy", "core:provisions", "core:assault"


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def make_game(tmp_path, **systems) -> Game:
    """slice3 with conquest and trade on: A holds P05 (home) P06 P07 P08, B P01 (home) P02 P03 P04, C P09 (home) P10
    P11 P12; P07 borders B's P02, P06 borders C's P11."""
    config = slice_config(game_id="t1", scenario="core:slice3", seats=SEATS, seed=7,
                          systems={"espionage": False, "conquest": True, "discoveries": False, "trade": True,
                                   **systems})
    return Game.create(init_game_dir(tmp_path, config)[0], Clock())


@pytest.fixture
def game(tmp_path) -> Game:
    return make_game(tmp_path)


def ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def to_next_turn(game: Game) -> None:
    turn = game.state.turn
    while game.state.turn == turn and game.phase != "game_over":
        ready_all(game)


def journal(game: Game, kind: str) -> list[dict]:
    return [e for e in read_events(game.gdir.journal_path) if e["type"] == kind]


def error(call) -> GameError:
    with pytest.raises(GameError) as err:
        call()
    return err.value


def report(game: Game, turn: int) -> dict:
    return game.gdir.load_turn("reports", turn)


def kinds(rep: dict, seat: str) -> list[str]:
    return [e["kind"] for e in rep["by_seat"][seat]]


# ---------------------------------------------------------------- escrow and settlement (docs/TRADE.md 1.4, 1.5)

def test_a_province_passes_at_step_4b_with_its_escrow(game):
    tid = game.propose_trade("A", "Соло", {"P07": 1}, {"alloy": 10})["id"]
    t = tr.trade_by_id(game.state, tid)
    assert t["settle"] == "resolution" and t["give"] == {"provinces": ["P07"]}
    answer = game.respond_trade("B", tid, "accept")
    assert answer["status"] == "pending" and "в залоге до расчёта: Сплав 10" in answer["text"]
    assert t["escrow"] == {"B": {ALLOY: 10}} and game.state.players["B"].resources[ALLOY] == 20
    assert "в залоге: Сплав 10" in game.state_text("B").splitlines()[1]
    assert game.state_json("B")["you"]["escrow"] == {ALLOY: 10}
    assert "P07 уйдёт в расчёте" in game.state_text("A") and "P07 придёт в расчёте" in game.state_text("B")
    assert "переход в расчёте" in game.trades("A")["text"]
    again = game.respond_trade("B", tid, "accept")          # a swarm teammate accepts again: nothing changes
    assert again["already"] and game.state.players["B"].resources[ALLOY] == 20
    # an escrow is out of the treasury: no other trade, gift or order can spend it
    assert error(lambda: game.propose_trade("B", "Сид", {"alloy": 21})).code == "NOT_ENOUGH_RESOURCES"
    ready_all(game)
    assert game.phase == "actions"
    game.submit("A", [{"kind": "move", "from": "P05", "to": "P07", "units": {"assault": 2}}])
    to_next_turn(game)
    p07 = game.state.provinces["P07"]
    assert p07.owner == "B" and p07.status["occupied"] == {"by": "B", "from": "A", "since": 1} and not p07.units
    t = tr.trade_by_id(game.state, tid)
    assert t["status"] == "done" and t["settled_turn"] == 1 and t["escrow"] is None
    assert game.state.provinces["P05"].units[ASSAULT] == 4          # the giver's units came back
    rep = report(game, 1)
    moved = [e for e in rep["events"] if e["kind"] == "province_transferred"]
    assert moved == [{"kind": "province_transferred", "seats": ["A", "B"], "province": "P07", "from": "A",
                      "to": "B", "trade": tid, "status": "occupation", "units_withdrawn": {ASSAULT: 2}}]
    assert not [e for e in rep["events"] if e["kind"] in ("captured", "war_points", "claim_created")]
    assert {"kind": "retreat", "seats": ["A"], "province": "P05", "units": {ASSAULT: 2}, "from": "P07"} \
        in rep["events"]
    # the others see a plain change of owner, never a trade (docs/TRADE.md 16а)
    seen = [e for e in rep["by_seat"]["C"] if e.get("province") == "P07" and e["kind"] != "retreat"]
    assert seen == [{"kind": "captured", "seats": ["A", "B"], "province": "P07", "from": "A", "to": "B",
                     "status": "occupied"}]
    assert "trade_settled" not in kinds(rep, "C") and "province_transferred" in kinds(rep, "A")
    assert rep["public"]["ownership_changes"] == [{"province": "P07", "from": "A", "to": "B",
                                                   "status": "occupied"}]
    assert "transferred" not in str(rep["public"]) and "trade" not in str(rep["public"]["conquest"])
    events = read_events(game.gdir.journal_path)
    assert [e["type"] for e in view_events(events, seat="C") if e["type"].startswith(("trade", "province_t"))] == []
    assert [e["province"] for e in view_events(events, seat="A") if e["type"] == "province_transferred"] == ["P07"]
    settled = journal(game, "trade_settled")
    assert settled[-1]["at"] == "resolution" and settled[-1]["turn"] == 1
    assert "исполнен в расчёте хода 1" in game.inbox("A")["text"]
    assert replay(game.gdir).ok


def test_the_receiver_cannot_move_into_the_promised_province(game):
    game.state.provinces["P02"].units = {ASSAULT: 2}           # B stands next to P07
    tid = game.propose_trade("A", "Соло", {"P07": 1}, {})["id"]
    ready_all(game)
    game.submit("B", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 2}}])
    ready_all(game)
    assert game.phase == "negotiation_2"
    game.respond_trade("B", tid, "accept")                      # accepted after the move: step 1 drops it
    res = game.validate("B", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 1},
                               "pact": "break"}])
    assert "PROVINCE_PROMISED" in str(res)
    to_next_turn(game)
    rep = report(game, 1)
    rejected = [e for e in rep["events"] if e["kind"] == "order_rejected"]
    assert rejected and rejected[0]["reason"] == "PROVINCE_PROMISED"
    assert game.state.provinces["P07"].owner == "B" and game.state.provinces["P02"].units == {ASSAULT: 2}
    assert not [e for e in rep["events"] if e["kind"] in ("captured", "battle")]
    # next turn the receiver may go in (and must, or subjugate: no garrison, a revolt at T+1)
    assert game.validate("B", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 1}}])


def test_validate_refuses_the_receivers_move_in_actions(game):
    game.state.provinces["P02"].units = {ASSAULT: 2}
    tid = game.propose_trade("A", "Соло", {"P07": 1}, {})["id"]
    game.respond_trade("B", tid, "accept")
    ready_all(game)
    res = game.validate("B", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 2}}])
    bad = res["results"][0] if "results" in res else res
    assert "PROVINCE_PROMISED" in str(bad) and "со следующего хода" in str(bad)
    res = game.validate("A", [{"kind": "move", "from": "P05", "to": "P07", "units": {"assault": 1}}])
    assert "уходит обменом" in str(res)                         # the giver is warned, not refused
    for item in ({"kind": "recruit", "province": "P07", "unit": "assault", "count": 1},
                 {"kind": "build", "province": "P07", "building": "barracks"}):
        assert "PROVINCE_PROMISED" in str(game.validate("A", [item]))


def test_a_province_lost_in_battle_fails_the_trade_and_returns_the_escrow(game):
    game.state.provinces["P11"].units = {ASSAULT: 4}           # C next to A's empty P06
    tid = game.propose_trade("A", "Соло", {"P06": 1}, {"alloy": 10, "provisions": 2})["id"]
    game.respond_trade("B", tid, "accept")
    assert game.state.players["B"].resources[ALLOY] == 20
    ready_all(game)
    game.submit("C", [{"kind": "move", "from": "P11", "to": "P06", "units": {"assault": 4}}])
    to_next_turn(game)
    t = tr.trade_by_id(game.state, tid)
    assert t["status"] == "failed" and t["failed_reason"] == "lost"
    assert game.state.provinces["P06"].owner == "C"            # an ordinary capture by the third seat
    rep = report(game, 1)
    assert "captured" in [e["kind"] for e in rep["events"]]
    assert not [e for e in rep["events"] if e["kind"] == "province_transferred"]
    economy = next(e for e in rep["events"] if e["kind"] == "economy" and e["seats"] == ["B"])
    assert game.state.players["B"].resources[ALLOY] == 30 + economy["income"][ALLOY] - economy["upkeep"][ALLOY]
    assert journal(game, "trade_failed")[-1]["reason"] == "lost"
    assert "сорван в расчёте" in game.inbox("B")["text"]


def test_a_buyback_is_a_return_and_drops_the_claim(game):
    p07 = game.state.provinces["P07"]
    p07.owner = "B"
    p07.status["occupied"] = {"by": "B", "from": "A", "since": 0}
    p07.units = {ASSAULT: 1}
    game.state.claims.append({"seat": "A", "province": "P07", "until_turn": 3})
    tid = game.propose_trade("B", "Нико", {"P07": 1}, {"alloy": 5})["id"]
    assert "сразу полноценная" in game.inbox("A")["text"]
    game.respond_trade("A", tid, "accept")
    to_next_turn(game)
    assert p07 is not game.state.provinces["P07"]
    p07 = game.state.provinces["P07"]
    assert p07.owner == "A" and "occupied" not in p07.status
    assert not [c for c in game.state.claims if c["province"] == "P07" and c["seat"] == "A"]
    moved = next(e for e in report(game, 1)["events"] if e["kind"] == "province_transferred")
    assert moved["status"] == "return" and moved["units_withdrawn"] == {ASSAULT: 1}
    assert game.state.provinces["P02"].units.get(ASSAULT) == 1    # B's unit went to its own neighbour


# ---------------------------------------------------------------- what can be given (docs/TRADE.md 2.2)

def test_the_capitals_rule_and_public_checks(game):
    assert error(lambda: game.propose_trade("A", "Соло", {"P05": 1})).code == "INVALID"       # own home
    err = error(lambda: game.propose_trade("A", "Соло", {}, {"P01": 1}))                      # B's home
    assert err.code == "INVALID" and "столица" in err.message
    err = error(lambda: game.propose_trade("A", "Соло", {}, {"P11": 1}))                      # C's land
    assert err.code == "INVALID" and "не у Соло" in err.message
    assert error(lambda: game.propose_trade("A", "Соло", {"P02": 1})).code == "INVALID"       # not A's
    assert error(lambda: game.propose_trade("A", "Соло", {"P99": 1})).code == "UNKNOWN_ID"
    err = error(lambda: game.propose_trade("A", "Соло", {"provinces": ["P06", "P07"]}))
    assert err.code == "INVALID" and "не больше 1" in err.message
    # B holds A's home occupied: it may sell it back only to A
    p05 = game.state.provinces["P05"]
    p05.owner = "B"
    p05.status["occupied"] = {"by": "B", "from": "A", "since": 0}
    assert error(lambda: game.propose_trade("B", "Сид", {"P05": 1})).code == "INVALID"
    assert game.propose_trade("B", "Нико", {"P05": 1}, {"alloy": 20})["status"] == "proposed"
    # ruins and the value cap
    game.state.provinces["P08"].status["ruined_until"] = 5
    err = error(lambda: game.propose_trade("A", "Сид", {"P08": 1}))
    assert err.code == "INVALID" and "Пепелище" in err.message
    game.ruleset = dataclasses.replace(game.ruleset, stats={**game.ruleset.stats, "trade.province_max_value": 0})
    assert "предела" in error(lambda: game.propose_trade("A", "Сид", {"P06": 1})).message


def test_one_province_a_turn_and_one_trade_a_province(game):
    first = game.propose_trade("A", "Соло", {"P07": 1})["id"]
    game.propose_trade("A", "Сид", {"P07": 1}, {"alloy": 1})        # an auction: offers reserve nothing
    game.respond_trade("B", first, "accept")
    err = error(lambda: game.propose_trade("A", "Сид", {"P06": 1}, replaces="trade-A-C-1"))
    assert err.code == "LIMIT_EXCEEDED" and "не больше 1" in err.message
    second = tr.trade_by_id(game.state, "trade-A-C-1")
    answer = game.respond_trade("C", second["id"], "accept")       # P07 is promised already: A lacks it
    assert answer["status"] == "failed" and "больше нет обещанного" in answer["text"]
    assert "PROVINCE_PROMISED" not in game.inbox("C")["text"]       # no detail of A's other trade to C
    assert "уже обещана" in game.inbox("A")["text"]
    # B's own province in its pending trade: B learns it itself
    game.propose_trade("B", "Сид", {"P02": 1})
    tid = game.propose_trade("C", "Соло", {}, {"P03": 1})["id"]
    game.respond_trade("B", "trade-B-C-1", "reject")
    game.respond_trade("B", tid, "accept")
    third = game.propose_trade("A", "Соло", {}, {"P03": 1})["id"]
    err = error(lambda: game.respond_trade("B", third, "accept"))
    assert err.code == "PROVINCE_PROMISED" and "trade-B-C-2" in err.message and "ждёт ответа" in err.message


def test_build_in_progress_and_orders_block_a_province(game):
    ready_all(game)
    game.submit("A", [{"kind": "build", "province": "P06", "building": "barracks"}])
    game.submit("B", [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 1}])
    ready_all(game)
    assert game.phase == "negotiation_2"
    err = error(lambda: game.propose_trade("A", "Соло", {"P06": 1}))
    assert err.code == "BUILD_IN_PROGRESS" and "1 ОК" in err.message
    tid = game.propose_trade("A", "Соло", {"P07": 1}, {})["id"]
    assert game.respond_trade("B", tid, "accept")["status"] == "pending"
    to_next_turn(game)
    assert any(b.province == "P06" for b in game.state.pending_builds)
    err = error(lambda: game.propose_trade("A", "Сид", {"P06": 1}))
    assert err.code == "BUILD_IN_PROGRESS" and "идёт твоя стройка" in err.message


def test_the_proposers_lack_at_the_accept_is_a_failed_trade(game):
    tid = game.propose_trade("A", "Соло", {"P07": 1}, {"alloy": 3})["id"]
    ready_all(game)
    game.submit("A", [{"kind": "build", "province": "P07", "building": "barracks"}])
    ready_all(game)
    answer = game.respond_trade("B", tid, "accept")
    assert answer["status"] == "failed" and tr.trade_by_id(game.state, tid)["failed_reason"] == "proposer_lacks"
    assert "BUILD_IN_PROGRESS" in str(journal(game, "trade_failed")[-1]["lack"])
    assert game.state.players["B"].resources[ALLOY] == 30          # nothing moved
    # the recipient's own lack is its error; the offer waits
    tid = game.propose_trade("C", "Нико", {}, {"P07": 1})["id"]
    err = error(lambda: game.respond_trade("A", tid, "accept"))
    assert err.code == "BUILD_IN_PROGRESS" and "ждёт ответа" in err.message
    assert tr.trade_by_id(game.state, tid)["status"] == "proposed"


def test_provinces_need_conquest(tmp_path):
    game = make_game(tmp_path, conquest=False)
    err = error(lambda: game.propose_trade("A", "Соло", {"P07": 1}))
    assert err.code == "NOT_AVAILABLE" and "завоевании" in err.message
    assert "провинции" not in rules_topic(game.ruleset, "trade")
    assert "залог" not in rules_topic(game.ruleset, "trade")


def test_a_province_for_a_province_sends_no_unit_into_the_given_one(game):
    game.state.provinces["P07"].units = {ASSAULT: 1}
    game.state.provinces["P02"].units = {ASSAULT: 1}
    tid = game.propose_trade("A", "Соло", {"P07": 1}, {"P02": 1})["id"]
    game.respond_trade("B", tid, "accept")
    to_next_turn(game)
    s = game.state
    assert s.provinces["P07"].owner == "B" and s.provinces["P02"].owner == "A"
    # A's unit from P07 cannot go to P02 (just given to it? it is A's now: P05 first, a full province)
    assert s.provinces["P05"].units[ASSAULT] == 5 and s.provinces["P01"].units[ASSAULT] == 5
    assert not s.provinces["P07"].units and not s.provinces["P02"].units


# ---------------------------------------------------------------- finds pass to the receiver (docs/TRADE.md 1.5.4)

def test_a_waiting_find_choice_passes_to_the_receiver(tmp_path):
    game = make_game(tmp_path, discoveries=True)
    game.state.discoveries["P07"] = {"id": "core:sealed_vault", "state": "pending", "known": ["A"],
                                     "pending": {"seat": "A", "turn": 0}}
    tid = game.propose_trade("A", "Соло", {"P07": 1})["id"]
    assert "находка «Запечатанное хранилище» (ждёт выбора)" in game.inbox("B")["text"]
    game.respond_trade("B", tid, "accept")
    ready_all(game)
    assert "PROVINCE_PROMISED" in str(game.validate("A", [{"kind": "discover", "province": "P07",
                                                           "choice": "pry"}]))
    to_next_turn(game)
    entry = game.state.discoveries["P07"]
    assert entry["state"] == "pending" and entry["pending"]["seat"] == "B" and "B" in entry["known"]
    assert not [e for e in report(game, 1)["events"] if e["kind"] == "discovery_chosen"]


def test_no_find_head_when_the_seller_does_not_know_it(tmp_path):
    game = make_game(tmp_path, discoveries=True)
    game.state.discoveries["P07"] = {"id": "core:sealed_vault", "state": "site", "known": ["C"]}
    game.propose_trade("A", "Соло", {"P07": 1})
    assert "находка" not in game.inbox("B")["text"]


# ---------------------------------------------------------------- hold_foreign via: capture (docs/TRADE.md 6.3)

def test_conqueror_counts_only_land_taken_in_battle():
    rs = game_ruleset(make_config(game_id="g", scenario="core:slice3", seats=SEATS, seed=1))
    assert rs.objectives["core:conqueror"].params["via"] == "capture" and tr.via_capture(rs)
    assert "взятых боем" in sc_.objective_text(rs, None, {"id": "core:conqueror"})
    from dah.engine.resolve import new_game_state
    state = new_game_state(rs, SEATS, 1)
    p07 = state.provinces["P07"]
    p07.owner = "B"
    entry = {"id": "core:conqueror"}
    assert sc_.objective_progress(state, rs, "B", entry)[:2] == (True, 1)
    p07.status["transferred"] = {"from": "A", "to": "B", "turn": 1}
    assert sc_.objective_progress(state, rs, "B", entry)[:2] == (False, 0)
    # `via: any` (an older pack, no key): as before
    plain = dataclasses.replace(rs.objectives["core:conqueror"], params={"count": 1})
    rs2 = dataclasses.replace(rs, objectives={**rs.objectives, "core:conqueror": plain})
    assert sc_.objective_progress(state, rs2, "B", entry)[:2] == (True, 1)
    assert not tr.via_capture(rs2)


def test_the_mark_is_written_only_when_an_objective_reads_it(tmp_path):
    plain = make_game(tmp_path / "plain")                     # territory only: no objectives in this game
    tid = plain.propose_trade("A", "Соло", {"P07": 1})["id"]
    plain.respond_trade("B", tid, "accept")
    to_next_turn(plain)
    assert "transferred" not in plain.state.provinces["P07"].status
    config = slice_config(game_id="t2", scenario="core:slice3", seats=SEATS, seed=7, scoring=None,
                          systems={"espionage": False, "conquest": True, "discoveries": False, "trade": True})
    config["scoring"] = game_ruleset(make_config(game_id="g", scenario="core:slice3", seats=SEATS,
                                                 seed=7)).scenario.scoring
    game = Game.create(init_game_dir(tmp_path / "scored", config)[0], Clock())
    if not tr.via_capture(game.ruleset):
        pytest.skip("the scenario deals no conqueror")
    tid = game.propose_trade("A", "Соло", {"P07": 1})["id"]
    game.respond_trade("B", tid, "accept")
    to_next_turn(game)
    assert game.state.provinces["P07"].status["transferred"] == {"from": "A", "to": "B", "turn": 1}
    assert "transferred" not in str(game.state_json("C")) and "transferred" not in str(game.state_json("B"))
    assert replay(game.gdir).ok


def test_a_capture_removes_the_mark(game):
    p06 = game.state.provinces["P06"]
    p06.status["transferred"] = {"from": "B", "to": "A", "turn": 0}
    game.state.provinces["P11"].units = {ASSAULT: 4}
    ready_all(game)
    game.submit("C", [{"kind": "move", "from": "P11", "to": "P06", "units": {"assault": 4}}])
    to_next_turn(game)
    assert game.state.provinces["P06"].owner == "C" and "transferred" not in game.state.provinces["P06"].status


# ---------------------------------------------------------------- report copies (docs/TRADE.md 2.3)

def _recon(game: Game) -> dict:
    """A recon step of turn 1 with two found reports of A (an Observation of P11 and a Ledger of C)."""
    observe = {"id": "r1", "seat": "A", "shade": "s1", "mission": "core:observe", "behavior": "observe_province",
               "target": "P11", "round": 1, "rounds": 1, "turn": 1,
               "facts": {"province": "P11", "owner": "C", "units": {ASSAULT: [1, 3]}, "buildings": [],
                         "building_now": [], "fortified": False, "status": {}},
               "truth": {"province": "P11", "owner": "C", "units": {ASSAULT: 2}}}
    ledger = {"id": "r2", "seat": "A", "shade": "s1", "mission": "core:ledger", "behavior": "read_ledger",
              "target": "C", "round": 1, "rounds": 1, "turn": 1,
              "facts": {"stock": {ALLOY: [20, 40]}, "techs": [], "whisper": 1, "mirrors": 0, "shades": 1,
                        "score_parts": {"territory": 7}},
              "truth": {"stock": {ALLOY: 31}}}
    rep = game.gdir.load_turn("recon", 1) or {}
    rep = {**rep, "turn": 1, "reports": {"A": [observe, ledger]}}
    game.gdir.save_turn("recon", 1, rep)
    return rep


@pytest.fixture
def spy_game(tmp_path) -> Game:
    game = make_game(tmp_path / "spy", espionage=True)
    ready_all(game)
    ready_all(game)
    assert game.phase == "negotiation_2"
    _recon(game)
    return game


def test_a_report_copy_is_certified_and_kept_by_the_seller(spy_game):
    game = spy_game
    tid = game.propose_trade("A", "Соло", {"reports": ["r1", "#r2"]}, {"alloy": 6})["id"]
    note = game.inbox("B")["text"]
    assert "донесение r1: Наблюдение → P11, ход 1, найдено, донесение r2: Счётная книга → Сид, ход 1" in note
    assert "units" not in note and "1–3" not in note                 # only the head before the accept
    answer = game.respond_trade("B", tid, "accept")
    assert answer["status"] == "done" and "c1, c2" in answer["text"]
    copies = game.state.players["B"].intel_copies
    assert [c["id"] for c in copies] == ["c1", "c2"] and copies[0]["source"] == "r1" and copies[0]["from"] == "A"
    assert all("truth" not in c["report"] and "shade" not in c["report"] for c in copies)
    assert game.find_report("A", "r1") is not None                     # the seller keeps its report
    text = game.get_intel("B", report_id="c1")["text"]
    assert text.startswith("КОПИЯ донесения Нико, конец Д хода 1, заверено игрой")
    assert len(text.splitlines()[0]) <= tr.COPY_HEAD_LIMIT and len(text) <= tr.COPY_LIMIT + 81
    assert "1–3" in text
    assert "счёт 7" in game.get_intel("B", report_id="c2")["text"]       # a Ledger copy carries the exact score
    assert "c1" in game.get_intel("B")["text"]                         # the list names the copies
    # no resale, reports only in give, own reports only
    assert error(lambda: game.propose_trade("B", "Сид", {"c1": 1})).code == "INVALID"
    assert error(lambda: game.propose_trade("A", "Сид", {}, {"reports": ["r1"]})).code == "INVALID"
    assert error(lambda: game.propose_trade("C", "Нико", {"r1": 1})).code == "UNKNOWN_ID"
    assert error(lambda: game.propose_trade("A", "Сид", {"reports": ["r1", "r2", "r3"]})).code == "INVALID"
    # no intel points, the target learns nothing
    assert not game.state.players["B"].tally.get("intel") and not game.state.players["A"].tally.get("intel")
    events = read_events(game.gdir.journal_path)
    copied = [e for e in events if e["type"] == "intel_copied"]
    assert [(e["copy_id"], e["source"], e["from"], e["to"]) for e in copied] == [("c1", "r1", "A", "B"),
                                                                                  ("c2", "r2", "A", "B")]
    assert not [e for e in view_events(events, seat="C") if e["type"] in ("intel_copied", "trade_settled")]
    # estimate(defender="c1") takes the copy's garrison (docs/SPY_V2.md 6.1)
    est = game.estimate({"attacker": {"assault": 4}, "defender": "c1"}, seat="B")
    assert est["defender"] == {ASSAULT: 3} and est["report"].startswith("по донесению c1")


def test_a_copy_needs_no_recon_files_and_replays(spy_game):
    game = spy_game
    tid = game.propose_trade("A", "Соло", {"r1": 1})["id"]
    game.respond_trade("B", tid, "accept")
    for path in (game.gdir.path / "private" / "recon").glob("*.json"):
        path.unlink()
    assert game.get_intel("B", report_id="c1")["text"].startswith("КОПИЯ")
    op = next(o for o in game.diplomacy_log if o["op"] == "trade_answer")
    assert op["copies"][0]["report"]["facts"]["province"] == "P11" and "truth" not in op["copies"][0]["report"]
    to_next_turn(game)
    assert state_hash(game.state) == state_hash(GameState.from_dict(game.state.to_dict()))
    result = replay(game.gdir)
    assert all(t.ok for t in result.turns)


def test_reports_need_espionage_and_the_stat(game, spy_game):
    assert error(lambda: game.propose_trade("A", "Соло", {"r1": 1})).code == "NOT_AVAILABLE"
    spy_game.ruleset = dataclasses.replace(spy_game.ruleset, stats={**spy_game.ruleset.stats, "trade.reports": 0})
    assert error(lambda: spy_game.propose_trade("A", "Соло", {"r1": 1})).code == "NOT_AVAILABLE"


# ---------------------------------------------------------------- promises with provinces (docs/TRADE.md 3)

def test_promises_of_provinces_only_in_list_trades(game):
    later = {"give": {"P07": 1}, "want": {"alloy": 5}, "by_turn": 3}
    tid = game.propose_trade("A", "Соло", {}, {}, later=later)["id"]
    assert game.respond_trade("B", tid, "accept")["status"] == "done"
    assert game.state.provinces["P07"].owner == "A"                  # the game never carries a promise out
    text = game.trades("A")["text"]
    assert "обещано: ты → Соло: P07 «" in text and "к ходу 3" in text
    assert "ОБЕЩАНИЯ" not in game.state_text("A") and "promises" not in game.state_json("A")
    err = error(lambda: game.propose_trade("A", "Сид", {}, {}, later={"give": {"r1": 1}, "by_turn": 2}))
    assert err.code == "INVALID"
    game.ruleset = dataclasses.replace(game.ruleset, stats={**game.ruleset.stats, "trade.show_promises": 1})
    assert "ОБЕЩАНИЯ: ты → Соло: P07" in game.state_text("A")
    assert game.state_json("B")["promises"][0]["from"] == "A"


# ---------------------------------------------------------------- rules (docs/TRADE.md 10.2)

def test_the_trade_topic_tells_provinces_reports_and_the_conqueror(tmp_path):
    rs = game_ruleset(make_config(game_id="g", scenario="core:quad4", seats=SEATS, seed=1))
    text = rules_topic(rs, "trade")
    assert len(text) <= TRADE_TOPIC_LIMIT
    for phrase in ("залог", "Пепелище", "стартовую столицу", "Оккупация", "«Завоеватель» не идёт",
                   "заверенную игрой копию c<k>", "не перепродать", "later="):
        assert phrase in text, phrase
    off = dataclasses.replace(rs, stats={**rs.stats, "trade.provinces_per_turn": 0})
    assert "залог" not in rules_topic(off, "trade")


def _scored_game(path) -> Game:
    """slice3 with its own scoring (objectives on, conqueror with via: capture in the pool)."""
    config = slice_config(game_id="t2", scenario="core:slice3", seats=SEATS, seed=7,
                          systems={"espionage": False, "conquest": True, "discoveries": False, "trade": True})
    config["scoring"] = game_ruleset(make_config(game_id="g", scenario="core:slice3", seats=SEATS,
                                                 seed=7)).scenario.scoring
    return Game.create(init_game_dir(path, config)[0], Clock())


def test_a_release_to_another_seat_is_marked_too(tmp_path):
    game = _scored_game(tmp_path)
    assert tr.via_capture(game.ruleset)
    p11 = game.state.provinces["P11"]
    p11.owner = "A"
    p11.status["occupied"] = {"by": "A", "from": "C", "since": 0}
    p11.units = {ASSAULT: 1}
    ready_all(game)
    game.submit("A", [{"kind": "dispose", "province": "P11", "disposition": "liberate", "to": "Соло"}])
    to_next_turn(game)
    p11 = game.state.provinces["P11"]
    assert p11.owner == "B" and p11.status["transferred"] == {"from": "A", "to": "B", "turn": 1}
    assert sc_.objective_progress(game.state, game.ruleset, "B", {"id": "core:conqueror"})[:2] == (False, 0)


def test_empty_copies_and_no_trades_are_not_hashed(game):
    """A state without copies, trades or marks hashes as a state of a game without this step."""
    data = game.state.to_dict()
    for p in data["players"].values():
        assert p.pop("intel_copies") == []
    data.pop("trades"), data.pop("trade_seq")
    assert state_hash(game.state) == state_hash(GameState.from_dict(data))
