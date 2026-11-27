"""Measurement m5 fixes: pact renewal, ready in the negotiation phases, short list_techs, own sent messages in the
inbox, and clearer answers for the refusals seen in m5 (PACT_EXISTS, estimate, respond_pact, history)."""

from __future__ import annotations

import pytest

from dah.bots.base import Bot
from dah.bots.view import parse_state
from dah.engine import diplomacy as dip
from dah.engine import orders_hash
from dah.game import Game, GameError, replay
from dah.journal import read_events
from dah.report import build_report, render_report
from dah.store import init_game_dir
from slice_helpers import slice_config

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"},
         {"seat": "C", "nick": "Сид", "kind": "agent"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def game(tmp_path) -> Game:
    config = slice_config(game_id="m5", scenario="core:slice3", seats=SEATS, seed=7)
    return Game.create(init_game_dir(tmp_path, config)[0], FakeClock())


def ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def next_turn(game: Game) -> None:
    for _ in range(3):
        ready_all(game)


def sign(game: Game, a: str, b: str, turns=None) -> str:
    pact_id = game.propose_pact(a, game.state.players[b].nick, turns)["pact"]["id"]
    game.respond_pact(b, pact_id, "accept")
    return pact_id


def journal(game: Game, kind: str) -> list[dict]:
    return [e for e in read_events(game.gdir.journal_path) if e["type"] == kind]


# ---------------------------------------------------------------- 1. pact renewal

def test_renewal_extends_the_pact_from_its_end_without_gap_or_overlap(game):
    pact_id = sign(game, "A", "B", turns=2)                    # turns 1..2
    assert dip.pact_by_id(game.state, pact_id)["until_turn"] == 2
    out = game.propose_pact("B", "Нико", 3)                     # either side may propose the renewal
    renewal = out["pact"]["id"]
    assert renewal != pact_id and out["pact"]["renews"] == pact_id
    assert "с хода 3 до хода 5 включительно" in out["text"]
    notice = game.inbox("A")["messages"][-1]
    assert notice["system"] and "продлить пакт" in notice["text"] and renewal in notice["text"]
    assert "продлить" in game.pacts("A")["text"] and renewal in game.pacts("A")["text"]
    accepted = game.respond_pact("A", renewal, "accept")
    pact = dip.pact_by_id(game.state, pact_id)
    assert pact["until_turn"] == 5 and pact["since_turn"] == 1 and pact["status"] == "active"
    assert pact["renewals"] == [{"id": renewal, "turns": 3, "turn": 1, "phase": "negotiation_1", "from_turn": 3}]
    assert dip.pact_by_id(game.state, renewal)["status"] == dip.RENEWED
    assert accepted["pact"]["id"] == pact_id and "до хода 5" in accepted["text"]
    (ev,) = journal(game, "pact_renewed")
    assert (ev["pact_id"], ev["renewal_id"], ev["from_turn"], ev["until_turn"]) == (pact_id, renewal, 3, 5)
    # public: everyone (C too) learns that the pact was renewed and until when, never the ids
    c_notice = game.inbox("C")["messages"][-1]
    assert c_notice["event"] == "pact_renewed" and "Пакт продлён" in c_notice["text"] and "до хода 5" in c_notice["text"]
    public = game.pacts("C")["public"]
    assert public == [{"seats": ["Нико", "Соло"], "turns": 2, "until_turn": 5, "leaving": None,
                       "ends_before_turn": None, "renewed": 1}]
    assert "продлён 1 раз" in game.state_text("C") and pact_id not in game.state_text("C")
    # the pact protects in every resolution 1..5 and expires after turn 5, not after turn 2
    from dah.engine import GameState
    from dah.server import spectate as spectate_mod
    for turn in range(1, 6):
        assert game.state.turn == turn and dip.pact_between(game.state, "A", "B", turn) is not None
        next_turn(game)
        if turn == 1:
            (row,) = spectate_mod._pacts(GameState.from_dict(game.gdir.load_turn("states", 2)), False)
            assert row["renewed"] == 1 and row["until_turn"] == 5 and "id" not in row
    assert dip.pact_by_id(game.state, pact_id)["status"] == "expired"
    assert [e["turn"] for e in journal(game, "pact_expired")] == [5]
    # the turn report: C sees the renewal without ids, the sides with them
    report = game.gdir.load_turn("reports", 1)
    (seen_c,) = [e for e in report["by_seat"]["C"] if e["kind"] == "pact_renewed"]
    assert "pact" not in seen_c and "renewal" not in seen_c and seen_c["until_turn"] == 5
    (seen_a,) = [e for e in report["by_seat"]["A"] if e["kind"] == "pact_renewed"]
    assert seen_a["pact"] == pact_id and seen_a["renewal"] == renewal
    assert all("pact" not in e and "renewal" not in e for e in report["public"]["diplomacy"])
    assert "пакт продлён: Нико и Соло — до хода 5 вкл." in game.history("C", 1)["text"]
    # the delayed spectator: the same public fact, no ids (spectate._public_events / _pacts)
    from dah.server import spectate
    (spect,) = [e for e in spectate._public_events(report, game, game.state) if e["kind"] == "pact_renewed"]
    assert "pact" not in spect and "renewal" not in spect and "продлён" in spect["text"]
    totals = build_report(read_events(game.gdir.journal_path))["totals"]
    assert (totals["A"]["pacts_signed"], totals["A"]["pacts_renewed"], totals["C"]["pacts_renewed"]) == (1, 1, 0)
    assert "заключено 1, продлено 1" in render_report(build_report(read_events(game.gdir.journal_path)))


def test_renewal_in_the_last_turn_and_replay(game):
    pact_id = sign(game, "A", "B", turns=1)                    # protects turn 1 only
    ready_all(game)
    ready_all(game)                                            # П2 of the last turn: still renewable
    renewal = game.propose_pact("A", "Соло", 2)["pact"]["id"]
    game.respond_pact("B", renewal, "accept")
    ready_all(game)
    assert game.state.turn == 2 and dip.pact_by_id(game.state, pact_id)["status"] == "active"
    assert not journal(game, "pact_expired")
    next_turn(game)
    next_turn(game)
    assert dip.pact_by_id(game.state, pact_id)["status"] == "expired" and journal(game, "pact_expired")[0]["turn"] == 3
    rep = replay(game.gdir)
    assert rep.ok, [(t.turn, t.expected, t.actual) for t in rep.turns]


def test_indefinite_pact_cannot_be_renewed_and_other_pact_exists_cases_say_what_to_do(game):
    pact_id = sign(game, "A", "B")
    with pytest.raises(GameError) as err:
        game.propose_pact("B", "Нико", 3)
    assert err.value.code == "PACT_EXISTS" and "бессрочный" in err.value.message and pact_id in err.value.message
    # crossing proposals: the other side's proposal is waiting — accept it instead of proposing
    offer = game.propose_pact("A", "Сид", 2)["pact"]["id"]
    with pytest.raises(GameError) as err:
        game.propose_pact("C", "Нико", 2)
    assert err.value.code == "PACT_EXISTS" and f'respond_pact(id="{offer}", decision="accept")' in err.value.message
    with pytest.raises(GameError) as err:
        game.propose_pact("A", "Сид", 2)
    assert f'leave_pact(id="{offer}")' in err.value.message
    game.respond_pact("C", offer, "accept")
    # a pending renewal: its proposer may withdraw it, the partner answers it
    renewal = game.propose_pact("C", "Нико", 1)["pact"]["id"]
    with pytest.raises(GameError) as err:
        game.propose_pact("C", "Нико", 1)
    assert "продлить" in err.value.message and f'leave_pact(id="{renewal}")' in err.value.message
    with pytest.raises(GameError) as err:
        game.propose_pact("A", "Сид", 1)
    assert f'respond_pact(id="{renewal}", decision="accept")' in err.value.message
    game.leave_pact("C", renewal)                                # withdrawn by its proposer
    assert dip.pact_by_id(game.state, renewal)["status"] == "withdrawn"


def test_leave_cancels_a_pending_renewal_and_ends_an_accepted_one(game):
    pact_id = sign(game, "A", "B", turns=2)
    renewal = game.propose_pact("A", "Соло", 2)["pact"]["id"]
    out = game.leave_pact("B", pact_id)                         # P1: no longer protects in this resolution
    assert renewal in out["text"]
    assert dip.pact_by_id(game.state, renewal)["status"] == dip.CANCELLED
    (ev,) = journal(game, "pact_renewal_cancelled")
    assert ev["renewal_id"] == renewal and ev["pact_id"] == pact_id
    assert any(m["event"] == "pact_renewal_cancelled" for m in game.inbox("A")["messages"])
    with pytest.raises(GameError):
        game.respond_pact("B", renewal, "accept")
    # the leave took effect in P1: the left pact is not renewed, a brand-new pact may be proposed instead
    fresh = game.propose_pact("A", "Соло", 2)["pact"]
    assert "renews" not in fresh and fresh["status"] == "proposed"


def test_leave_after_an_accepted_renewal_ends_the_pact_by_the_notice_rule(game):
    pact_id = sign(game, "A", "B", turns=1)
    game.respond_pact("B", game.propose_pact("A", "Соло", 5)["pact"]["id"], "accept")
    assert dip.pact_by_id(game.state, pact_id)["until_turn"] == 6
    next_turn(game)
    ready_all(game)
    ready_all(game)                                             # П2 of turn 2
    game.leave_pact("A", pact_id)                              # still protects in turn 2, ends before turn 3
    ready_all(game)
    assert dip.pact_by_id(game.state, pact_id)["status"] == "active"
    next_turn(game)
    pact = dip.pact_by_id(game.state, pact_id)
    assert pact["status"] == "left" and pact["ended_turn"] == 3


def test_unanswered_renewal_lapses_and_the_pact_still_expires_on_time(game):
    pact_id = sign(game, "A", "B", turns=1)
    renewal = game.propose_pact("A", "Соло", 2)["pact"]["id"]
    next_turn(game)
    assert dip.pact_by_id(game.state, renewal)["status"] == "lapsed"
    assert dip.pact_by_id(game.state, pact_id)["status"] == "expired"
    assert any("Предложение продлить пакт" in m["text"] for m in game.inbox("A")["messages"])


def test_accepting_an_active_pact_again_is_not_an_error(game):
    pact_id = sign(game, "A", "B", turns=3)
    out = game.respond_pact("B", pact_id, "accept")            # a teammate of the swarm answered already
    assert out["already"] is True and "уже принят" in out["text"]
    assert len(journal(game, "pact_accepted")) == 1
    with pytest.raises(GameError) as err:
        game.respond_pact("A", game.propose_pact("A", "Сид", 2)["pact"]["id"], "accept")
    assert "сделал ты" in err.value.message and "leave_pact" in err.value.message


class _FakeDiplomacy:
    def __init__(self) -> None:
        self.proposed: list[tuple[str, int | None]] = []

    def propose_pact(self, to, turns):
        self.proposed.append((to, turns))
        return {}

    def respond_pact(self, pact_id, decision):
        return {}

    def send_gift(self, to, resources):
        return {}

    def leave_pact(self, pact_id):
        return {}


def test_bots_renew_a_fixed_pact_in_its_last_turn(game):
    pact_id = sign(game, "A", "B", turns=2)
    api = _FakeDiplomacy()
    bot = Bot("A", 1)
    view = parse_state(game.state_json("A"), "A")
    assert bot.renew_expiring_pacts(view, api) == [] and api.proposed == []        # turn 1 of 2: not yet
    next_turn(game)
    view = parse_state(game.state_json("A"), "A")
    assert bot.renew_expiring_pacts(view, api) == ["B"] and api.proposed == [("B", 2)]
    assert bot.renew_expiring_pacts(view, api) == []                                 # once per pact and turn
    renewal = game.propose_pact("A", "Соло", 2)["pact"]["id"]
    view = parse_state(game.state_json("B"), "B")
    assert Bot("B", 2).renew_expiring_pacts(view, _FakeDiplomacy()) == []          # B was asked already
    assert any(p.get("renews") == pact_id and p["id"] == renewal for p in view.pact_proposals)


# ---------------------------------------------------------------- 2. ready in the negotiation phases

def test_ready_in_p1_needs_no_hash_and_orders_phases_say_the_exact_call(game):
    info = game.phase_info("A")
    assert "orders_hash" not in info and "ready() — без confirm_hash" in info["text"]
    assert game.set_ready("A", None)["ready"] == "1/3"
    assert game.set_ready("B", "a-stale-hash-from-last-turn")["ready"] == "2/3"
    game.set_ready("C", "")
    assert game.phase == "actions"
    info = game.phase_info("A")
    current = info["orders_hash"]
    assert current == orders_hash(game._book("A")) and f'ready(confirm_hash="{current}")' in info["text"]
    with pytest.raises(GameError) as err:
        game.set_ready("A", None)
    assert err.value.code == "STALE_HASH" and f'ready(confirm_hash="{current}")' in err.value.message
    game.submit("A", [{"kind": "fortify", "province": next(p.id for p in game.state.owned("A"))}])
    with pytest.raises(GameError) as err:
        game.set_ready("A", current)
    new = orders_hash(game._book("A"))
    assert "устарел" in err.value.message and f'ready(confirm_hash="{new}")' in err.value.message
    assert "1 приказ" in err.value.message
    assert game.set_ready("A", game.phase_info("A")["orders_hash"])["ready"] == "1/3"
    ready_all(game)
    info = game.phase_info("B")
    assert game.phase == "negotiation_2" and info["orders_hash"] in info["text"] and "П2" in info["text"]


def test_rules_and_goal_text_explain_ready_per_phase(game):
    from dah.cli import goal_text

    assert "П1 — ready() без хэша" in game.rules() and len(game.rules()) <= 6000
    goal = goal_text(game.config, "B")
    assert "П1: dah.ready() без confirm_hash" in goal and "не жди таймера" in goal


# ---------------------------------------------------------------- 3. list_techs short by default

def test_list_techs_is_short_by_default_and_full_on_request(game):
    short, full = game.list_techs("A"), game.list_techs("A", full=True)
    assert short["full"] is False and full["full"] is True
    assert {t["status"] for t in short["techs"]} <= {"researched", "active", "available"}
    assert short["locked"] == sum(1 for t in full["techs"] if t["status"] == "locked") > 0
    assert "list_techs(full=true)" in short["text"] and "Доступно сейчас" in short["text"]
    assert len(short["text"]) * 1.5 < len(full["text"])
    assert all(set(t) <= {"id", "name", "branch", "tier", "cost", "progress", "status", "effects", "unlocks"}
               for t in short["techs"])


# ---------------------------------------------------------------- 4. own sent messages

def test_inbox_shows_own_sent_messages_but_never_others_private_ones(game):
    game.send_message("A", ["Соло"], "мир?")
    game.send_message("B", ["Сид"], "тайна")
    game.send_message("C", "all", "всем привет")
    a = game.inbox("A")
    assert [(m["from"], m.get("own", False)) for m in a["messages"]] == [("ты", True), ("Сид", False)]
    assert "ты → Соло: мир?" in a["text"] and "тайна" not in a["text"]
    assert [m["id"] for m in game.inbox("A", since=1)["messages"]] == [2]      # delta semantics kept
    b = game.inbox("B")
    assert [(m["from"], m.get("own", False)) for m in b["messages"]] == [("Нико", False), ("ты", True),
                                                                         ("Сид", False)]
    assert "<<untrusted" not in b["messages"][1]["text"]


# ---------------------------------------------------------------- 5. refusals seen in m5

def test_history_of_the_turn_in_progress_is_an_answer(game):
    out = game.history("A", 1)
    assert out["turn"] is None and "ещё идёт" in out["text"]
    next_turn(game)
    ready_all(game)
    ready_all(game)
    out = game.history("A", 2)                                   # П2 of turn 2 asks for turn 2
    assert out["turn"] is None and "history(turn=1)" in out["text"]
    with pytest.raises(GameError) as err:
        game.history("A", 7)
    assert err.value.code == "NOT_FOUND"


def test_estimate_takes_province_ids_for_visible_armies(game):
    own, near = next((p, n) for p in game.state.owned("A") for n in game.ruleset.map.adjacency[p.id]
                     if game.state.provinces[n].owner != "A")
    own.units = {"core:assault": 4}
    game.state.provinces[near].units = {}
    out = game.estimate({"attacker": own.id.lower(), "defender": near}, "A")   # no province_type: the defender's
    assert out["win_prob"] == 1.0 and out["province_type"] == game.state.provinces[near].type
    far = next(pid for pid, p in game.state.provinces.items() if p.owner == "C"
               and all(game.state.provinces[n].owner != "A" for n in game.ruleset.map.adjacency[pid]))
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": own.id, "defender": far}, "A")
    assert "туманом" in err.value.message
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": {"asault": 2}, "defender": {"?": 1}, "province_type": own.id}, "A")
    assert "похоже на: assault" in err.value.message
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": {"assault": 2}, "defender": {"?": 1}, "province_type": own.id}, "A")
    assert "предполагаемый состав" in err.value.message


def test_renewal_without_turns_makes_the_pact_indefinite_and_the_last_turn_is_hinted(game):
    pact_id = sign(game, "A", "B", turns=1)
    assert "последний ход срока" in game.state_text("A") and "последний ход срока" in game.pacts("A")["text"]
    assert "последний ход срока" not in game.state_text("C")          # only the sides get the hint
    renewal = game.propose_pact("B", "Нико")["pact"]["id"]
    assert "бессрочно, начиная с хода 2" in game.inbox("A")["messages"][-1]["text"]
    game.respond_pact("A", renewal, "accept")
    assert dip.pact_by_id(game.state, pact_id)["until_turn"] is None
    next_turn(game)
    next_turn(game)
    assert dip.pact_by_id(game.state, pact_id)["status"] == "active"
