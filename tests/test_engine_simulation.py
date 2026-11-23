"""Whole games driven by a seeded scripted player: determinism and replay from serialized data."""

import json

from dah.engine import (GameState, SeatOrders, build_ruleset, issue_order_id, new_game_state, render_report_text,
                        render_state_text, resolve, state_hash, submit_orders)
from dah.engine.rng import stream


def scripted_orders(state, rs, seat, seed):
    rng = stream(seed, state.turn, "test_bot", seat)
    items = []
    for prov in state.owned(seat):
        if any(rs.buildings[b].recruit for b in prov.buildings):
            items.append({"kind": "recruit", "province": prov.id, "unit": rng.choice(sorted(rs.units)), "count": 1})
        if prov.units:
            unit = rng.choice(sorted(prov.units))
            items.append({"kind": "move", "from": prov.id, "to": rng.choice(rs.map.adjacency[prov.id]),
                          "units": {unit: prov.units[unit]}})
        if rng.random() < 0.3:
            items.append({"kind": "build", "province": prov.id, "building": rng.choice(["mine", "farm"])})
    book = submit_orders(state, rs, seat, SeatOrders(), items, "actions", id_factory=lambda: issue_order_id(state, seat))
    p2 = submit_orders(state, rs, seat, book.book, [{"kind": "fortify", "province": state.owned(seat)[0].id}],
                       "negotiation_2", id_factory=lambda: issue_order_id(state, seat)) if state.owned(seat) else book
    return p2.book


def play(seed, scenario="slice3", seats=3):
    rs = build_ruleset(None, scenario)
    state = new_game_state(rs, [{"seat": s, "nick": f"Бот{s}"} for s in "ABC"[:seats]], seed)
    history = []
    while state.phase != "game_over":
        books = {seat: scripted_orders(state, rs, seat, seed) for seat, p in state.players.items() if p.alive}
        history.append((json.dumps(state.to_dict()), {s: b.to_dict() for s, b in books.items()}))
        before = state
        state, report = resolve(state, books, rs, seed)
        history[-1] += (state_hash(state),)
        json.dumps(report.to_dict())
        for seat in state.players:          # every event kind must render
            render_report_text(report, rs, state, seat)
            render_state_text(state, rs, seat, since_turn=before.turn, since_state=before, reports=[report])
    return rs, state, history


def test_same_seed_same_game():
    _, end1, h1 = play(5)
    _, end2, h2 = play(5)
    assert [h[2] for h in h1] == [h[2] for h in h2]
    assert state_hash(end1) == state_hash(end2)
    assert end1.result is not None and end1.phase == "game_over"


def test_replay_from_serialized_data():
    rs, _, history = play(9, "slice2", 2)
    for state_json, books, expected in history:
        state = GameState.from_dict(json.loads(state_json))
        new, _ = resolve(state, {s: SeatOrders.from_dict(b) for s, b in books.items()}, rs, 9)
        assert state_hash(new) == expected


def test_games_reach_the_end_within_turn_limit():
    for seed in range(4):
        rs, end, history = play(seed)
        assert len(history) <= rs.scenario.turn_limit
        assert end.result["reason"] in ("turn_limit", "last_standing")
        assert all(v >= 0 for p in end.players.values() for v in p.resources.values())
        assert all(n > 0 for prov in end.provinces.values() for n in prov.units.values())
        assert all(not prov.units for prov in end.provinces.values() if prov.owner is None)
