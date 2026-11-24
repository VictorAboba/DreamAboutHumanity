"""Review of the m5 fixes: renewal answers (accept on the pact id, a pact no longer in force, rejection), the
renewal hint in the state text, PACT_EXISTS without a contradicting example, estimate's side from province owners,
the Д ready hint, and what agents are told about own messages and tool costs."""

from __future__ import annotations

import pytest

from dah.bridge.tools import TOOLS
from dah.cli import goal_text
from dah.engine import diplomacy as dip
from dah.game import GameError
from test_m5_fixes import game, next_turn, ready_all, sign  # noqa: F401  (fixture and helpers)

SPECS = {t.name: t.description for t in TOOLS}


# ---------------------------------------------------------------- RENEW-1: accept on the pact id while a renewal waits

def test_accepting_the_pact_id_while_its_renewal_waits_points_to_the_renewal(game):  # noqa: F811
    pact_id = sign(game, "A", "B", turns=1)
    renewal = game.propose_pact("A", "Соло", 2)["pact"]["id"]
    with pytest.raises(GameError) as err:
        game.respond_pact("B", pact_id, "accept")
    assert err.value.code == "INVALID" and f'respond_pact(id="{renewal}", decision="accept")' in err.value.message
    assert "отвечать больше не нужно" not in err.value.message
    assert dip.pact_by_id(game.state, renewal)["status"] == dip.PROPOSED
    # the proposer of the renewal has nothing to answer: its accept of the pact itself stays idempotent
    assert game.respond_pact("A", pact_id, "accept")["already"] is True
    game.respond_pact("B", renewal, "accept")
    assert dip.pact_by_id(game.state, pact_id)["until_turn"] == 3


# ---------------------------------------------------------------- RENEW-2: no "действует" for a pact not in force

def test_accepting_a_pact_whose_leave_took_effect_is_not_reported_as_in_force(game):  # noqa: F811
    pact_id = sign(game, "A", "B", turns=3)
    game.leave_pact("A", pact_id)                               # from П1: no protection in this resolution already
    assert not dip.in_force(dip.pact_by_id(game.state, pact_id), game.state.turn)
    with pytest.raises(GameError) as err:
        game.respond_pact("B", pact_id, "accept")
    assert err.value.code == "INVALID" and "больше не действует" in err.value.message
    assert "до хода 3 включительно" not in err.value.message


def test_accepting_a_pact_with_an_announced_leave_says_so(game):  # noqa: F811
    pact_id = sign(game, "A", "B", turns=3)
    ready_all(game)
    ready_all(game)                                             # П2: a leave takes effect from the next turn
    game.leave_pact("A", pact_id)
    out = game.respond_pact("B", pact_id, "accept")
    assert out["already"] is True and "выход объявлен" in out["text"]


def test_accepting_a_renewed_entry_of_an_ended_pact_is_refused(game):  # noqa: F811
    pact_id = sign(game, "A", "B", turns=1)
    renewal = game.propose_pact("A", "Соло", 1)["pact"]["id"]
    game.respond_pact("B", renewal, "accept")                    # now until turn 2
    assert game.respond_pact("B", renewal, "accept")["already"] is True
    next_turn(game)
    next_turn(game)                                             # turn 3: the pact expired
    assert dip.pact_by_id(game.state, pact_id)["status"] == "expired"
    with pytest.raises(GameError) as err:
        game.respond_pact("B", renewal, "accept")
    assert "больше не действует" in err.value.message and "действует до хода" not in err.value.message


# ---------------------------------------------------------------- RENEW-3: the renewal hint of the state text

def test_state_hint_for_a_renewed_pact_and_for_a_pending_renewal(game):  # noqa: F811
    pact_id = sign(game, "A", "B", turns=1)
    renewal = game.propose_pact("A", "Соло", 1)["pact"]["id"]
    b_text = game.state_text("B")
    assert f'respond_pact(id="{renewal}", decision="accept")' in b_text
    assert "продлить: propose_pact" not in b_text                # it would fail with PACT_EXISTS
    assert f"твоё продление {renewal} ждёт ответа" in game.state_text("A")
    game.respond_pact("B", renewal, "accept")                    # until turn 2
    next_turn(game)
    a_text = game.state_text("A")
    assert "(продлён 1 раз(а))" in a_text and "последний ход срока, продлить: propose_pact" in a_text
    assert pact_id in a_text and "последний ход срока" in game.pacts("A")["text"]


# ---------------------------------------------------------------- RENEW-5: rejecting a renewal

def test_rejecting_a_renewal_tells_the_proposer_the_pact_still_holds(game):  # noqa: F811
    pact_id = sign(game, "A", "B", turns=2)
    renewal = game.propose_pact("A", "Соло", 2)["pact"]["id"]
    out = game.respond_pact("B", renewal, "reject")
    assert "продление" in out["text"] and "до хода 2 включительно" in out["text"]
    notice = game.inbox("A")["messages"][-1]
    assert notice["event"] == "pact_rejected" and f"продление пакта {pact_id} ({renewal})" in notice["text"]
    assert "по-прежнему действует до хода 2 включительно" in notice["text"]
    assert dip.pact_by_id(game.state, pact_id)["status"] == "active"


# ---------------------------------------------------------------- AUX-3: PACT_EXISTS has no propose example

def test_pact_exists_does_not_append_a_propose_example(game):  # noqa: F811
    offer = game.propose_pact("A", "Сид", 2)["pact"]["id"]
    with pytest.raises(GameError) as err:
        game.propose_pact("C", "Нико", 2)
    assert err.value.code == "PACT_EXISTS" and offer in err.value.message
    assert "Пример" not in err.value.message and err.value.message.endswith("своё предлагать не нужно")
    pact_id = sign(game, "A", "B")
    with pytest.raises(GameError) as err:
        game.propose_pact("B", "Нико", 3)
    assert "бессрочный" in err.value.message and "propose_pact(to=" not in err.value.message
    with pytest.raises(GameError) as err:                        # other refusals keep their example
        game.propose_pact("A", "Никто", 2)
    assert "Пример: propose_pact" in err.value.message and pact_id


# ---------------------------------------------------------------- AUX-1: estimate's side from province owners

def test_estimate_puts_the_caller_on_the_side_of_its_own_province(game):  # noqa: F811
    capital = next(p for p in game.state.owned("A") if game.ruleset.province_types[p.type].defense_mul > 1)
    enemy = next(iter(game.ruleset.map.adjacency[capital.id]))
    game.state.provinces[enemy].owner = "B"                       # an enemy army next to A's capital
    game.state.players["A"].techs = ["core:drill_doctrine", "core:fortification"]   # A's bonuses must stay A's
    capital.units = {"core:assault": 4}
    game.state.provinces[enemy].units = {"core:assault": 4}
    body = {"attacker": enemy, "defender": capital.id}
    inferred = game.estimate(body, "A")
    explicit = game.estimate({**body, "side": "defender"}, "A")
    wrong = game.estimate({**body, "side": "attacker"}, "A")
    assert inferred["side"] == "defender" and inferred["win_prob"] == explicit["win_prob"]
    assert inferred["win_prob"] == 0.0 < wrong["win_prob"]        # not "the capital probably falls"
    assert "defender" in inferred["note"] and "по владельцу провинции" in inferred["note"]
    # an enemy army against a composition: the caller defends
    assert game.estimate({"attacker": enemy, "defender": {"assault": 4}, "province_type": capital.id}, "A")["side"] == "defender"
    # own army attacking: attacker, as before
    assert game.estimate({"attacker": capital.id, "defender": enemy}, "A")["side"] == "attacker"
    assert "по умолчанию" in game.estimate({"attacker": {"assault": 2}, "defender": {"assault": 1},
                                            "province_type": capital.id}, "A")["note"]


# ---------------------------------------------------------------- AUX-2: the Д hint does not promise a stale hash

def test_actions_hint_says_to_take_the_hash_from_submit_orders(game):  # noqa: F811
    ready_all(game)
    assert game.phase == "actions"
    text = game.phase_info("A")["text"]
    assert "orders_hash из ответа submit_orders" in text and "если приказы уже поданы" in text
    hint_hash = game.phase_info("A")["orders_hash"]
    out = game.submit("A", [{"kind": "fortify", "province": next(p.id for p in game.state.owned("A"))}])
    assert out["orders_hash"] != hint_hash
    assert game.set_ready("A", out["orders_hash"])["ready"] == "1/3"


# ---------------------------------------------------------------- AUX-5, AUX-6, RENEW-4: what agents read

def test_tool_descriptions_and_goal_text(game):  # noqa: F811
    assert "~5 раз" not in SPECS["list_techs"] and "вдвое" in SPECS["list_techs"]
    assert "from: \"ты\"" in SPECS["inbox"] and "inbox_id" in SPECS["send_message"]
    assert "продлить" in SPECS["propose_pact"] and "к концу нынешнего срока" in SPECS["propose_pact"]
    assert "продления" in SPECS["respond_pact"] and "свой ID" in SPECS["respond_pact"]
    assert "продлени" in SPECS["leave_pact"]
    assert "from \"ты\"" in goal_text(game.config, "B")


def test_send_message_returns_the_inbox_number_of_the_message(game):  # noqa: F811
    game.send_message("C", ["Нико"], "привет")
    pact = game.propose_pact("B", "Нико", 2)["pact"]["id"]      # a [ИГРА] notice in A's inbox
    out = game.send_message("A", ["Соло"], "мир?")
    assert out["message_id"] == 1 and out["inbox_id"] == 3
    entry = game.inbox("A")["messages"][out["inbox_id"] - 1]
    assert entry["own"] is True and entry["id"] == out["inbox_id"] and pact
    assert game.inbox("A", since=out["inbox_id"])["messages"] == []
