"""Regression tests for the stage-2 live-play and spec review findings (ENG-2..4, LIVE-1..10, SPEC-1)."""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

import pytest

from dah.bots.builder import BuilderBot
from dah.bots.rush import RushBot
from dah.engine import (Order, SeatOrders, build_ruleset, cancel_order, new_game_state, resolve, rules_text,
                        submit_orders)
from dah.engine.orders import order_cost
from dah.engine.setup import resolve_setup, setup_config
from dah.engine.techs import move_problem
from dah.game import Game, GameError, make_config
from dah.store import init_game_dir
from stage2_helpers import CORE020, FakeClock

SEATS = [{"seat": "A", "nick": "Al", "kind": "agent"}, {"seat": "B", "nick": "Be", "kind": "agent"}]
WATCH = Path(__file__).parent.parent / "src" / "dah" / "server" / "watch.html"


def _slice3(factions: tuple[str, str, str] = ("covenant", "litany", "synclite")):
    rs = build_ruleset(None, "slice3")
    st = new_game_state(rs, [{"seat": s, "nick": s, "faction": f} for s, f in zip("ABC", factions)], 1)
    return rs, st


# ---------------------------------------------------------------- ENG-2: research is fixed after Д

def test_research_order_cannot_be_cancelled_in_p2():
    rs, st = _slice3()
    st.players["A"].researching = "core:deep_drilling"
    st.players["A"].research["core:deep_drilling"] = 4
    book = submit_orders(st, rs, "A", SeatOrders(), [{"kind": "research", "tech": "core:agro_vats"}], "actions").book
    oid = book.orders[0].id
    res = cancel_order(st, rs, "A", book, oid, "negotiation_2")
    assert not res.ok and res.reason == "PHASE_LOCKED" and res.book is book and res.cp_cost == 0
    assert cancel_order(st, rs, "A", book, oid, "actions").ok           # in Д it is free, as before
    new, _ = resolve(st, {"A": book}, rs, 1)
    assert new.players["A"].researching == "core:agro_vats"


# ---------------------------------------------------------------- ENG-3: recruit discount per unit, seat's favour

def test_recruit_discount_rounds_the_unit_price_down_and_ignores_batching():
    rs, st = _slice3()
    cap = next(p for p in st.owned("A") if p.buildings)
    cap.buildings = sorted(cap.buildings + ["core:warehouse"])

    def cost(unit: str, count: int) -> int:
        return order_cost(rs, "core:covenant", Order("x", "recruit", province=cap.id, unit=unit, count=count),
                          st)["core:alloy"]

    base = rs.unit_cost("core:assault", "core:covenant")["core:alloy"]
    one = cost("core:assault", 1)
    assert one == base - math.ceil(base * 0.2)            # 12 alloy, 20 %: 9.6 -> 9
    assert cost("core:assault", 3) == 3 * one             # one order of 3 == three orders of 1
    militia = rs.unit_cost("core:militia", "core:covenant")["core:alloy"]
    assert cost("core:militia", 1) == militia - math.ceil(militia * 0.2)


# ---------------------------------------------------------------- ENG-4: only the freed budget is refilled

def test_points_mode_refills_only_the_cost_of_dropped_artifacts():
    rs = build_ruleset(None, "slice3")
    setup = setup_config(rs, "points")
    budget = int(setup["artifact_points"])
    unfit = next(a for a, d in rs.artifacts.items() if d.factions and "core:covenant" not in d.factions
                 and d.cost < budget)

    def final(arts: list[str]) -> list[str]:
        out = resolve_setup(rs, setup, 1, [{"seat": "A"}], {"A": {"factions": ["core:covenant"], "artifacts": arts}},
                            {})
        return out["A"]["artifacts"]

    got = final([unfit])
    assert unfit not in got
    assert sum(rs.artifacts[a].cost for a in got) <= rs.artifacts[unfit].cost < budget


# ---------------------------------------------------------------- LIVE-2: NOT_ADJACENT always says the reach

def test_not_adjacent_names_the_reach_of_a_range_1_stack():
    rs, st = _slice3(("litany", "covenant", "synclite"))
    adj = rs.map.adjacency
    src = next(p.id for p in st.owned("A"))
    mid = sorted(adj[src])[0]
    dst = next(p for p in sorted(adj[mid]) if p != src and p not in adj[src])
    st.provinces[mid].owner = st.provinces[dst].owner = "A"
    msg = move_problem(st, rs, "A", src, dst, ["core:assault"])
    assert msg is not None and "дальность этого стека — 1 ребро" in msg and "assault move 1" in msg


# ---------------------------------------------------------------- SPEC-1: core 0.2.0 keeps one edge per move

@pytest.mark.parametrize("packs, ok", [(None, True), (CORE020, False)])
def test_two_edge_move_through_own_land_only_with_stage2_content(tmp_path, packs, ok):
    config = make_config(game_id="g", scenario="core:blitz_test", seats=SEATS, seed=7, pack_dirs=packs)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock())
    assert game.ruleset.move_default == (2 if ok else 1)
    res = game.validate("A", [{"kind": "move", "from": "P05", "to": "P02", "unit": "assault", "count": 1}])
    assert res["results"][0]["ok"] is ok
    if not ok:
        assert res["results"][0]["reason"] == "NOT_ADJACENT"
        assert "по своей земле" not in rules_text(game.ruleset)


# ---------------------------------------------------------------- LIVE-1 and LIVE-3: Game texts

def test_setup_text_names_the_choice_and_ready_without_hash_says_it_is_required(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="s", scenario="core:slice3", seats=SEATS + [{"seat": "C", "nick": "Ce",
                                                                              "kind": "agent"}],
                         seed=3, artifacts="points")
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    game.start()
    assert game.phase == "setup"
    assert "Твой выбор: фракции —, артефакты —" in game.get_setup("A")["text"]
    game.choose_faction("A", ["ash_choir", "growth"])
    game.choose_artifacts("A", ["trail_maps"])
    text = game.get_setup("A")["text"]
    assert "Твой выбор: фракции ash_choir «" in text and "trail_maps «" in text and "['" not in text
    for seat in "ABC":
        game.set_ready(seat, None)                   # ready() without a hash is fine in setup
    assert game.phase != "setup"
    with pytest.raises(GameError) as err:
        game.set_ready("A", None)
    assert err.value.code == "STALE_HASH" and "confirm_hash обязателен" in err.value.message


# ---------------------------------------------------------------- LIVE-5 and LIVE-6: the watch page

def test_watch_page_names_research_orders_and_counts_all_techs_at_full_level():
    html = WATCH.read_text(encoding="utf-8")
    assert 'o.kind === "research"' in html and 'nameOf("tech_names", o.tech)' in html
    assert "s.full && p.techs ? p.techs.length" in html


# ---------------------------------------------------------------- LIVE-10: bots press ready again

class ClearedReadyApi:
    """One negotiation phase: after the bot's ready, a pact is proposed to it (clearing its ready) once."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.waits = 0
        self.you_ready = False
        self.proposals: list[dict[str, Any]] = []
        self.over = False

    def _status(self) -> dict[str, Any]:
        if self.over:
            return {"phase": "game_over", "phase_seq": 9, "game_over": True}
        return {"phase": "negotiation_1", "phase_seq": 1, "you_ready": self.you_ready, "game_over": False}

    def whoami(self):
        return {"seat": "A", "nick": "Al"}

    def phase(self):
        return self._status()

    def wait_phase(self, after_seq, max_seconds):
        self.waits += 1
        if self.waits == 1:          # a proposal arrives: the server clears the bot's ready, same phase
            self.you_ready = False
            self.proposals = [{"id": "p1", "from": "B", "to": "A", "turns": 2}]
        elif self.waits > 3 or (self.waits > 1 and self.you_ready):
            self.over = True
        return self._status()

    def state(self):
        provinces = {"P01": {"type": "capital", "owner": "A", "units": {"assault": 1}, "buildings": []}}
        return {"state": {"turn": 1, "phase": "negotiation_1", "provinces": provinces, "edges": [],
                          "you": {"resources": {"alloy": 0, "provisions": 10}, "command_points": 3},
                          "pact_proposals": list(self.proposals)}}

    def orders(self):
        return {"orders": [], "orders_hash": "h0", "cp_left": 3}

    def submit_orders(self, orders, mode):
        return {"orders_hash": "h0"}

    def estimate(self, *args):
        return 0.0

    def send_message(self, to, text):
        self.calls.append(("message", text))

    def respond_pact(self, pact_id, decision):
        self.calls.append(("respond", (pact_id, decision)))
        self.proposals = []

    def ready(self, confirm_hash):
        self.calls.append(("ready", confirm_hash))
        self.you_ready = True
        return {"ok": True}


@pytest.mark.parametrize("bot_cls, answers", [(BuilderBot, True), (RushBot, False)])
def test_bot_presses_ready_again_after_a_pact_proposal_clears_it(bot_cls, answers):
    api = ClearedReadyApi()
    bot_cls("A", 1).run(api, threading.Event())
    readies = [c for c in api.calls if c[0] == "ready"]
    assert readies == [("ready", "h0"), ("ready", "h0")]
    assert (("respond", ("p1", "accept")) in api.calls) is answers
    assert len([c for c in api.calls if c[0] == "message"]) <= 1     # the phase is not replayed
