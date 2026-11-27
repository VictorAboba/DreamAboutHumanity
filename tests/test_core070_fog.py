"""Core 0.7.0 (decision 27, measurement m8), part 2: army fog, the idle-Shade reminder, spend in the setup phase, the
eliminated bot's quiet stop and the joint entry of pact partners."""

from __future__ import annotations

import copy
import logging
import threading

import pytest

from dah.bots.passive import PassiveBot
from dah.bots.view import parse_state
from dah.engine import (SeatOrders, build_ruleset, issue_order_id, new_game_state, resolve, rules_topic,
                        submit_orders, view_state, visible_provinces)
from dah.engine import diplomacy as dip
from dah.engine import espionage as esp
from dah.engine.content import LEGACY_PACKS_DIR
from dah.engine.rules import discoveries_section
from dah.engine.view import render_state_text
from dah.game import Game, GameError, make_config
from dah.journal import read_events
from dah.report import build_report
from dah.server.spectate import DELAYED, GameCache, snapshot
from dah.store import init_game_dir
from dah.waker.text import wake_text
from test_bots_loop import FakeApi
from test_review_stage2 import FakeClock

CORE060 = LEGACY_PACKS_DIR / "core-0.6.0"
AS = "core:assault"


def _seats(n: int) -> list[dict]:
    return [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(n)]


def _book(st, rs, seat: str, items: list[dict]) -> SeatOrders:
    st.phase = "actions"
    res = submit_orders(st, rs, seat, SeatOrders(), items, "actions", id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def _far_fight(rs):
    """quad4, 3 seats: B attacks a province of C that A cannot see (neither A's nor next to A's land)."""
    st = new_game_state(rs, _seats(3), 3)
    seen_a = visible_provinces(st, rs, "A")
    for src in sorted(p.id for p in st.owned("B")):
        for dst in sorted(rs.map.adjacency[src]):
            if dst not in seen_a and st.provinces[dst].owner is None:
                st.provinces[dst].owner, st.provinces[dst].units = "C", {AS: 1}
                st.provinces[src].units = {AS: 6}
                return st, src, dst
    raise AssertionError("no province out of A's sight next to B")


# ---------------------------------------------------------------- 8. army fog

def test_beyond_sight_a_rival_army_shows_only_its_presence():
    rs = build_ruleset(None, "quad4")
    assert rs.stat("fog.army_presence") == 1
    st, src, dst = _far_fight(rs)
    entry = next(e for e in view_state(st, rs, "A")["provinces"] if e["id"] == dst)
    assert entry["visible"] is False and entry["army"] is True and "units" not in entry
    st.provinces[dst].units = {}
    entry = next(e for e in view_state(st, rs, "A")["provinces"] if e["id"] == dst)
    assert entry["army"] is False
    line = next(x for x in render_state_text(st, rs, "A").splitlines() if x.startswith(dst))
    assert "армии нет" in line and "штурм" not in line
    # neutral land beyond sight: no presence mark (no seat's army)
    neutral = next(e for e in view_state(st, rs, "A")["provinces"] if not e["visible"] and e["owner"] is None)
    assert "army" not in neutral and "units" not in neutral
    # the own seat and the seats that see it get the sizes as before
    own = next(e for e in view_state(st, rs, "C")["provinces"] if e["id"] == dst)
    assert own["units"] == {} and "army" not in own


def test_legacy_rulesets_keep_the_question_mark():
    rs = build_ruleset([str(CORE060)], "quad4")
    assert rs.stat("fog.army_presence") == 0
    st, src, dst = _far_fight(rs)
    entry = next(e for e in view_state(st, rs, "A")["provinces"] if e["id"] == dst)
    assert "army" not in entry and "units" not in entry
    line = next(x for x in render_state_text(st, rs, "A").splitlines() if x.startswith(dst))
    assert " ? |" in line
    assert "есть ли армия" not in rules_topic(rs, "fog") and "есть ли армия" in rules_topic(build_ruleset(None, "quad4"),
                                                                                           "fog")


@pytest.mark.parametrize("sid", ["slice3", "slice2", "quad4", "blitz_spy", "blitz_score", "blitz_finds"])
def test_every_espionage_scenario_has_the_army_fog(sid):
    rs = build_ruleset(None, sid)
    assert rs.system("espionage") and rs.stat("fog.army_presence") == 1


def test_a_battle_out_of_sight_stays_out_of_the_turn_report():
    rs = build_ruleset(None, "quad4")
    st, src, dst = _far_fight(rs)
    book = _book(st, rs, "B", [{"kind": "move", "from": src, "to": dst, "units": {"assault": 6}}])
    new, rep = resolve(st, {"B": book}, rs, 3)
    assert new.provinces[dst].owner == "B"
    battles = [e for e in rep.events if e["kind"] == "battle"]
    assert battles and all(e["province"] == dst for e in battles)
    for seat in ("B", "C"):
        assert any(e["kind"] == "battle" for e in rep.by_seat[seat])
    assert not [e for e in rep.by_seat["A"] if e["kind"] in ("battle", "retreat", "destroyed")]
    # the capture itself is public (ownership), without any unit count
    assert all("units" not in e and "sides" not in e for e in rep.by_seat["A"] if e.get("province") == dst)


def test_bots_read_only_the_fogged_view():
    rs = build_ruleset(None, "quad4")
    st, src, dst = _far_fight(rs)
    view = parse_state({"ok": True, "state": view_state(st, rs, "A")}, {"A", "Ник0"})
    assert view.provinces[dst].units is None and view.provinces[dst].army is True
    near = next(p for p in view.provinces.values() if p.owner == "A")
    assert near.units is not None and near.army is None


def test_estimate_and_estimate_mission_use_the_presence(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="f", scenario="core:quad4", seats=_seats(3), seed=3, objectives_salt=9)
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    seen = visible_provinces(game.state, game.ruleset, "A")
    far = next(p for p in sorted(game.state.provinces) if p not in seen and game.state.provinces[p].owner == "B")
    with pytest.raises(GameError) as err:
        game.estimate({"attacker": far, "defender": {"assault": 2}, "province_type": "forge"}, "A")
    assert "армия в" in err.value.message and "размер скрыт" in err.value.message and "наблюдение" in err.value.message
    rs, st = game.ruleset, game.state
    shade = esp.idle_shades(st, "A")[0]
    out = esp.estimate_mission(st, rs, "A", "observe", far, 1, shade=shade)
    assert "не видна" not in out["text"]
    old =build_ruleset([str(CORE060)], "quad4")
    assert "не видна" in esp.estimate_mission(st, old, "A", "observe", far, 1, shade=shade)["text"]


def test_the_delayed_spectator_sees_no_units(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="s", scenario="core:blitz_score", seats=_seats(3), seed=5, turn_limit=5)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    _skip_setup(game)
    while game.state.turn < 3:                        # turn 1 opens to the delayed spectator, the game goes on
        for seat in game.alive_seats():
            game.set_ready(seat, game.phase_info(seat).get("orders_hash"))
    assert not game.game_over
    snap = snapshot(game, GameCache(), 1, DELAYED)
    assert all("units" not in p and "unit_total" not in p for p in snap["provinces"])
    assert "units" in snapshot(game, GameCache(), 1, "live")["provinces"][0]


# ---------------------------------------------------------------- 9. idle Shade reminder

def _game(tmp_path, scenario: str = "core:quad4", **kw) -> Game:
    config = make_config(game_id="g", scenario=scenario, seats=_seats(3), seed=3, objectives_salt=9, **kw)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock(), start=False)
    game.start()
    return game


def _skip_setup(game: Game) -> None:
    while game.phase == "setup":
        for seat in game.alive_seats():
            game.set_ready(seat, None)


def test_phase_info_reminds_of_an_idle_shade_of_the_seat_only(tmp_path):
    game = _game(tmp_path)
    assert "idle_shades" not in game.phase_info("A")           # setup: no reminder
    _skip_setup(game)
    assert game.phase == "negotiation_1"
    info = game.phase_info("A")
    assert info["idle_shades"] == 1 and "У тебя 1 свободная Тень" in info["text"]
    # another seat's Shades never count: B's Shade goes on a mission, A's count is unchanged
    game.state.players["B"].shades[0]["status"] = "mission"
    assert game.phase_info("A")["idle_shades"] == 1 and "idle_shades" not in game.phase_info("B")
    # the wake text of the turn start carries one line
    text = wake_text(info)
    assert text.count("свободная Тень") == 1 and "dah.list_spies()" in text
    assert "свободн" not in wake_text({**info, "phase": "negotiation_2"})
    assert "свободн" not in wake_text({k: v for k, v in info.items() if k != "idle_shades"})
    assert "свободные Тени" in wake_text({**info, "idle_shades": 2})


def test_no_reminder_without_espionage(tmp_path):
    game = _game(tmp_path, "core:blitz_test")
    _skip_setup(game)
    assert "idle_shades" not in game.phase_info("A")


# ---------------------------------------------------------------- 3. spend in the setup phase

def test_spend_accepts_the_setup_phase_as_the_waker_reports_it(tmp_path):
    game = _game(tmp_path)
    assert game.phase == "setup" and game.phase_info("A")["turn"] == 1
    # the m8 shape: the waker's figure for the closed phase key (turn 1, setup)
    game.spend("A", 0.0, "participant_program")                       # baseline
    game.spend("A", 0.12, "participant_program", turn=1, phase="setup")
    with pytest.raises(GameError) as err:
        game.spend("A", 0.12, "participant_program", turn=2, phase="setup")
    assert err.value.code == "INVALID" and "setup" in err.value.message
    _skip_setup(game)
    game.spend("B", 0.2, "participant_program", turn=1, phase="setup")    # posted after the phase closed
    reports = [e for e in read_events(game.gdir.journal_path) if e["type"] == "spend_report"]
    assert [(r["seat"], r["for_turn"], r["for_phase"]) for r in reports] == [
        ("A", None, None), ("A", 1, "setup"), ("B", 1, "setup")]
    report = build_report(read_events(game.gdir.journal_path))
    assert report["totals"]["A"]["spent_usd"] == 0.12


# ---------------------------------------------------------------- 4. eliminated bot

class _OutApi(FakeApi):
    def __init__(self, locked: bool) -> None:
        super().__init__(["negotiation_1", "actions"])
        self.locked = locked
        self.out = not locked

    def _status(self):
        return {**super()._status(), **({"alive": False, "eliminated": True} if self.out else {"alive": True})}

    def ready(self, confirm_hash):
        if self.locked:
            self.out = True
            from dah.bots.client import ApiError
            raise ApiError("PHASE_LOCKED", "место выбыло из игры")
        return super().ready(confirm_hash)


@pytest.mark.parametrize("locked", [False, True])
def test_an_eliminated_seat_bot_stops_quietly(caplog, locked):
    api = _OutApi(locked)
    with caplog.at_level(logging.INFO, logger="dah.bots.base"):
        PassiveBot("C", 1).run(api, threading.Event())
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("seat eliminated" in r.getMessage() for r in caplog.records)
    if not locked:
        assert api.calls == []                       # never played a phase


# ---------------------------------------------------------------- 5. joint entry of pact partners

def test_equal_partners_entering_together_draw_by_the_game_seed():
    rs = build_ruleset(None, "slice3")
    holders = set()
    for seed in range(1, 9):
        st = new_game_state(rs, [{"seat": s, "nick": f"Ник{s}"} for s in "ABC"], 1)
        for prov in st.provinces.values():
            prov.units, prov.buildings = {}, []
        a_src = next(p for p in sorted(rs.map.adjacency) if st.provinces[p].owner == "A"
                     and any(st.provinces[n].owner is None and any(st.provinces[m].owner == "B"
                                                                    for m in rs.map.adjacency[n])
                             for n in rs.map.adjacency[p]))
        dst = next(n for n in sorted(rs.map.adjacency[a_src]) if st.provinces[n].owner is None
                   and any(st.provinces[m].owner == "B" for m in rs.map.adjacency[n]))
        b_src = next(m for m in sorted(rs.map.adjacency[dst]) if st.provinces[m].owner == "B")
        st.discoveries.pop(dst, None)
        st.provinces[a_src].units, st.provinces[b_src].units = {AS: 2}, {AS: 2}
        op = dip.make_proposal(st, "A", "B", None, "negotiation_1")
        dip.apply_op(st, op)
        dip.apply_op(st, dip.make_response(st, "B", op["id"], "accept", "negotiation_1"))
        books = {"A": _book(st, rs, "A", [{"kind": "move", "from": a_src, "to": dst, "units": {"assault": 2}}]),
                 "B": _book(st, rs, "B", [{"kind": "move", "from": b_src, "to": dst, "units": {"assault": 2}}])}
        again = copy.deepcopy(st)
        new, rep = resolve(st, books, rs, seed)
        new2, _ = resolve(again, books, rs, seed)
        assert new.provinces[dst].owner == new2.provinces[dst].owner in ("A", "B")     # deterministic
        joint = [e for e in rep.events if e["kind"] == "pact_joint"]
        assert joint and joint[0]["holder"] == new.provinces[dst].owner
        holders.add(new.provinces[dst].owner)
    assert holders == {"A", "B"}                     # equal strength: the seeded draw, not the seat order


def test_the_rules_state_who_takes_a_jointly_entered_province():
    rs = build_ruleset(None, "quad4")
    text = discoveries_section(rs).detail
    assert "Союзники вошли вместе — берёт победивший стражу" in text and "Спящий голем" in text
    assert len(text) <= 2500
    assert "жребий партии" in rules_topic(rs, "pacts")
