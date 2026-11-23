"""Regression tests for review findings on diplomacy in the Game layer: number parsing, ready marks and pact
proposals, re-signing after a leave from P1, the inbox cursor, and saving the runtime state before notices."""

from __future__ import annotations

import httpx
import pytest
from test_diplomacy_game import ALLOY, game, journal, ready_all, sign  # noqa: F401  (pytest fixture)

from dah.bridge.client import GameClient
from dah.bridge.tools import Bridge
from dah.engine import build_ruleset, orders_hash
from dah.engine import diplomacy as dip
from dah.game import Game, GameError


# ---------------------------------------------------------------- numbers that pass isdigit() but not int()

@pytest.mark.parametrize("value", ["²", "٣", "9" * 5000])
def test_odd_digit_strings_are_invalid_not_internal_errors(value):
    rs = build_ruleset(None, "slice3")
    with pytest.raises(dip.DiplomacyError) as err:
        dip.parse_resources(rs, {"alloy": value})
    assert err.value.code == "INVALID"
    with pytest.raises(dip.DiplomacyError) as err:
        dip._parse_turns(value)
    assert err.value.code == "INVALID"


def test_game_refuses_odd_digits_in_gifts_pacts_and_estimate(game):  # noqa: F811
    with pytest.raises(GameError) as err:
        game.send_gift("A", "Соло", {"alloy": "²"})
    assert err.value.code == "INVALID"
    with pytest.raises(GameError) as err:
        game.propose_pact("A", "Соло", "²")
    assert err.value.code == "INVALID"
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": {"assault": "²"}, "defender": {}, "province_type": "core:forge"})
    assert err.value.code == "INVALID"
    assert game.send_gift("A", "Соло", {"alloy": " 2 "})["gift"]["resources"] == {ALLOY: 2}


# ---------------------------------------------------------------- a proposal clears the recipient's ready mark

def test_proposal_to_a_ready_seat_clears_its_ready_mark_so_it_can_answer(game):  # noqa: F811
    ready_all(game)                                   # P1 -> actions
    ready_all(game)                                   # actions -> P2
    assert game.phase == "negotiation_2"
    game.set_ready("B", orders_hash(game._book("B")))
    game.set_ready("C", orders_hash(game._book("C")))
    assert "B" in game.ready_seats
    pact_id = game.propose_pact("A", "Соло")["pact"]["id"]
    assert "B" not in game.ready_seats
    closed = game.set_ready("A", orders_hash(game._book("A")))
    assert closed["phase_closed"] is False and game.phase == "negotiation_2"   # B still has to answer
    game.respond_pact("B", pact_id, "accept")
    assert dip.pact_by_id(game.state, pact_id)["status"] == "active"
    (cleared,) = journal(game, "ready_cleared")
    assert cleared["seat"] == "B" and cleared["reason"] == "pact_proposed"


# ---------------------------------------------------------------- re-signing after a leave from P1

def test_pair_can_sign_again_after_a_leave_that_already_took_effect(game):  # noqa: F811
    old = sign(game, "A", "B")
    game.leave_pact("A", old)                         # from P1: no protection in this turn's resolution
    new = game.propose_pact("B", "Нико", 2)["pact"]["id"]
    game.respond_pact("A", new, "accept")
    assert new != old and dip.pact_by_id(game.state, new)["status"] == "active"
    assert dip.pact_between(game.state, "A", "B", game.state.turn)["id"] == new
    ready_all(game)
    ready_all(game)
    ready_all(game)                                   # resolution of turn 1
    assert dip.pact_by_id(game.state, old)["status"] == "left"
    assert dip.pact_by_id(game.state, new)["status"] == "active"


def test_leave_from_p2_still_refuses_a_new_pact_with_a_clear_message(game):  # noqa: F811
    old = sign(game, "A", "B")
    ready_all(game)
    ready_all(game)
    assert game.phase == "negotiation_2"
    game.leave_pact("A", old)                         # still protects in this resolution
    with pytest.raises(GameError) as err:
        game.propose_pact("B", "Нико")
    assert err.value.code == "PACT_EXISTS" and "выход объявлен" in err.value.message


# ---------------------------------------------------------------- inbox cursor

def test_inbox_last_id_never_runs_past_the_newest_entry(game):  # noqa: F811
    game.send_gift("A", "Соло", {"alloy": 1})
    assert game.inbox("B", 20)["last_id"] == 1
    assert game.inbox("B", 1)["last_id"] == 1


def test_bridge_cursor_moves_only_past_entries_it_returned():
    entries = [{"id": n, "text": f"m{n}"} for n in (1, 2, 3, 4)]

    def reply(request: httpx.Request) -> httpx.Response:
        since = int(dict(request.url.params).get("since", 0))
        out = [e for e in entries if e["id"] > since]
        # an old server echoed a too-large `since` back as last_id
        return httpx.Response(200, json={"ok": True, "messages": out, "last_id": out[-1]["id"] if out else since,
                                         "text": "; ".join(e["text"] for e in out) or "новых сообщений нет"})

    bridge = Bridge(GameClient("http://game.test", "dah_g1_A_key", transport=httpx.MockTransport(reply)))
    assert bridge.call("inbox", {})[0].startswith("m1")
    assert bridge.call("inbox", {"since": 20})[0].startswith("новых")
    assert bridge.last_inbox_id == 4
    entries += [{"id": n, "text": f"m{n}"} for n in (5, 6)]
    assert bridge.call("inbox", {})[0].startswith("m5; m6")


# ---------------------------------------------------------------- runtime saved before journal and notices

def test_gift_is_saved_before_the_journal_and_the_notice(game, monkeypatch):  # noqa: F811
    before = game.state.players["B"].resources.get(ALLOY, 0)

    def crash(*args, **kwargs):
        raise RuntimeError("process died")

    monkeypatch.setattr(game.journal, "append", crash)
    with pytest.raises(RuntimeError):
        game.send_gift("A", "Соло", {"alloy": 3})
    monkeypatch.undo()
    again = Game.load(game.gdir, game.clock)
    assert again.state.players["B"].resources.get(ALLOY, 0) == before + 3
    assert again.diplomacy_log and again.diplomacy_log[-1]["op"] == "gift"


def test_proposal_is_saved_before_the_journal_and_the_notice(game, monkeypatch):  # noqa: F811
    monkeypatch.setattr(game.journal, "append", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        game.propose_pact("A", "Соло")
    monkeypatch.undo()
    again = Game.load(game.gdir, game.clock)
    assert [p["status"] for p in again.state.pacts] == ["proposed"]
