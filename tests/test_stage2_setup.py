"""Setup phase (TECH_AND_SETUP.md section 2): engine functions, the Game phase, journal, replay and CLI flags."""

from __future__ import annotations

import json

import pytest

from dah.cli import main
from dah.engine import (SetupChoiceError, check_artifacts, check_faction_prefs, draft_offers, orders_hash,
                        resolve_setup, setup_config, view_state)
from dah.game import Game, GameError, make_config, replay
from dah.journal import read_events
from dah.store import GameDir, init_game_dir, read_json
from stage2_helpers import PACKS, FakeClock, ruleset

SEATS = [{"seat": "A", "nick": "Альфа", "kind": "agent"}, {"seat": "B", "nick": "Бета", "kind": "swarm"}]


def new_game(tmp_path, clock, scenario="duel_draft", seed=7, game_id="s1", **kwargs) -> Game:
    config = make_config(game_id=game_id, scenario=scenario, seats=SEATS, seed=seed, pack_dirs=PACKS, **kwargs)
    gdir, _ = init_game_dir(tmp_path, config)
    return Game.create(gdir, clock)


def events(game: Game, kind: str | None = None) -> list[dict]:
    out = read_events(game.gdir.journal_path)
    return [e for e in out if kind is None or e["type"] == kind]


def ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        if game.phase == "setup":
            game.set_ready(seat, None)
        elif not game.game_over:
            game.set_ready(seat, orders_hash(game._book(seat)))


# ---------------------------------------------------------------- engine functions

def test_setup_config_from_scenario_and_host_overrides():
    assert setup_config(ruleset("duel_plain")) is None
    assert setup_config(ruleset("duel_plain"), "none") is None
    on = setup_config(ruleset("duel_plain"), "draft")           # the host turns a setup on
    assert on["artifacts"] == "draft" and on["unique_factions"] is True and on["artifact_offer"] == 3
    cfg = setup_config(ruleset("duel_draft"), "points", points=5)
    assert cfg["artifacts"] == "points" and cfg["artifact_points"] == 5 and cfg["artifact_offer"] == 2
    assert setup_config(ruleset("duel_draft"), offer=4)["artifact_offer"] == 4
    with pytest.raises(ValueError):
        setup_config(ruleset("duel_draft"), "lottery")
    with pytest.raises(ValueError):
        setup_config(ruleset("duel_draft"), offer=0)


def test_draft_offers_are_seeded_balanced_and_disjoint():
    rs = ruleset()
    cfg = setup_config(rs)
    offers = draft_offers(rs, cfg, ["A", "B"], 7)
    assert offers == draft_offers(rs, cfg, ["B", "A"], 7)
    assert all(len(o) == 2 for o in offers.values()) and not set(offers["A"]) & set(offers["B"])
    power = [sum(rs.artifacts[a].power for a in o) for o in offers.values()]
    assert max(power) - min(power) == 0
    assert len({tuple(draft_offers(rs, cfg, ["A", "B"], seed)["A"]) for seed in range(20)}) > 1
    many = draft_offers(rs, {**cfg, "artifact_offer": 5}, ["A", "B", "C"], 1)   # pool of 6: offers overlap
    assert all(len(set(o)) == 5 for o in many.values())
    assert draft_offers(rs, {**cfg, "artifacts": "points"}, ["A"], 1) == {"A": []}


def test_faction_prefs_and_artifact_checks():
    rs = ruleset()
    assert check_faction_prefs(rs, "ash") == ["t2:ash"]
    assert check_faction_prefs(rs, ["Пепельный Двор", "t2:iron", "ash"]) == ["t2:ash", "t2:iron"]
    with pytest.raises(SetupChoiceError) as err:
        check_faction_prefs(rs, ["elves"])
    assert err.value.code == "UNKNOWN_ID"
    draft = setup_config(rs)
    assert check_artifacts(rs, draft, ["t2:cache", "t2:seal"], ["cache"]) == ["t2:cache"]
    for ids, code in ((["ember"], "NOT_OFFERED"), (["cache", "seal"], "INVALID"), ([], "INVALID"),
                      (["nothing"], "UNKNOWN_ID")):
        with pytest.raises(SetupChoiceError) as err:
            check_artifacts(rs, draft, ["t2:cache", "t2:seal"], ids)
        assert err.value.code == code
    points = setup_config(rs, "points")
    assert check_artifacts(rs, points, [], ["ember", "warband"]) == ["t2:ember", "t2:warband"]
    for ids, code in ((["ember", "seal"], "NOT_ENOUGH_POINTS"), (["cache", "seal"], "EXCLUSIVE")):
        with pytest.raises(SetupChoiceError) as err:
            check_artifacts(rs, points, [], ids)
        assert err.value.code == code
    with pytest.raises(SetupChoiceError):
        check_artifacts(rs, setup_config(rs, "none") or {"artifacts": "none"}, [], ["cache"])


def test_unique_faction_clash_is_settled_by_seed_and_loser_gets_next_pref():
    rs = ruleset()
    cfg = setup_config(rs, "none")
    seats = [{"seat": "A"}, {"seat": "B"}]
    choices = {"A": {"factions": ["t2:ash", "t2:iron"]}, "B": {"factions": ["t2:ash"]}}
    winners = set()
    for seed in range(30):
        out = resolve_setup(rs, cfg, seed, seats, choices, {})
        assert out == resolve_setup(rs, cfg, seed, seats, choices, {})
        assert {out["A"]["faction"], out["B"]["faction"]} == {"t2:ash", "t2:iron"}
        winner = next(s for s in "AB" if out[s]["faction"] == "t2:ash")
        assert out["AB".replace(winner, "")]["faction_conflict"] is True
        winners.add(winner)
    assert winners == {"A", "B"}
    # a seat that chose comes before one that did not; without choices: the scenario's list, unique
    out = resolve_setup(rs, cfg, 3, seats, {"B": {"factions": ["t2:iron"]}}, {})
    assert out["B"]["faction"] == "t2:iron" and out["A"]["faction"] == "t2:ash" and out["A"]["auto_faction"]
    out = resolve_setup(rs, cfg, 3, seats, {}, {})
    assert sorted(r["faction"] for r in out.values()) == ["t2:ash", "t2:iron"]
    shared = resolve_setup(rs, {**cfg, "unique_factions": False}, 3, seats, choices, {})
    assert shared["A"]["faction"] == shared["B"]["faction"] == "t2:ash"


def test_artifact_results_defaults_and_faction_limits():
    rs = ruleset()
    draft = setup_config(rs)
    seats = [{"seat": "A", "faction": "ash"}, {"seat": "B", "faction": "iron"}]
    offers = {"A": ["t2:banner", "t2:cache"], "B": ["t2:seal", "t2:warband"]}
    out = resolve_setup(rs, draft, 1, seats, {"A": {"artifacts": ["t2:banner"]}}, offers)
    assert out["A"]["faction"] == "t2:ash"
    assert out["A"]["artifacts"] == ["t2:cache"] and out["A"]["dropped"] == ["t2:banner"]   # banner: iron only
    assert out["B"]["artifacts"] == ["t2:seal"] and out["B"]["auto_artifacts"]
    points = setup_config(rs, "points")
    auto = resolve_setup(rs, points, 1, seats, {}, {})
    assert auto["A"]["artifacts"] == ["t2:cache", "t2:ember"]          # id order within budget 3
    assert auto["B"]["artifacts"] == ["t2:banner", "t2:cache"] or sum(
        rs.artifacts[a].cost for a in auto["B"]["artifacts"]) <= 3


# ---------------------------------------------------------------- the Game phase

def test_game_with_setup_starts_in_setup_phase(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    assert game.config["setup"]["artifacts"] == "draft" and game.config["phase_limits"]["setup"] == 120
    assert (game.phase, game.state.turn, game.phase_seq, game.seconds_left()) == ("setup", 1, 1, 120)
    assert events(game, "phase_open")[0]["phase"] == "setup"
    info = game.phase_info("A")
    assert "choose_faction" in info["allowed"] and "submit_orders" not in info["allowed"]
    assert "ФАЗА СТАРТА" in info["text"]
    view = game.get_setup("A")
    assert view["open"] and view["artifacts_mode"] == "draft" and len(view["offer"]) == 2
    assert [f["id"] for f in view["factions"]] == ["t2:iron", "t2:ash"]
    assert view["offer"] != game.get_setup("B")["offer"]
    with pytest.raises(GameError) as err:
        game.submit("A", [{"kind": "research", "tech": "smithing"}])
    assert err.value.code == "PHASE_LOCKED"
    with pytest.raises(GameError):
        game.send_message("A", "all", "привет")
    assert game.validate("A", [{"kind": "research", "tech": "smithing"}])["results"][0]["reason"] == "PHASE_LOCKED"


def test_choices_are_private_ready_and_close(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    offer_a = game.get_setup("A")["offer"][0]["id"]
    res = game.choose_faction("A", ["ash", "iron"])
    assert res["factions"] == ["t2:ash", "t2:iron"]
    game.choose_artifacts("A", [offer_a])
    assert game.get_setup("B")["your_choice"] == {"factions": None, "artifacts": None}   # A's choice is private
    assert "t2:ash" not in str(game.get_setup("B")["your_choice"])
    game.set_ready("A", None)
    game.choose_faction("A", "ash")                       # a change clears ready
    assert "A" not in game.ready_seats
    with pytest.raises(GameError) as err:
        game.set_ready("A", "stale")
    assert err.value.code == "STALE_HASH"
    game.set_ready("A", game.setup_hash("A"))
    game.choose_faction("B", ["iron"])
    game.set_ready("B", None)
    assert game.phase == "negotiation_1" and game.state.turn == 1
    a, b = game.state.players["A"], game.state.players["B"]
    assert (a.faction, b.faction) == ("t2:ash", "t2:iron") and a.artifacts == [offer_a]
    assert len(b.artifacts) == 1 and b.artifacts[0] in game.setup_offers["B"]    # default: first offered
    closed = events(game, "setup_closed")[0]
    assert closed["factions"] == {"A": "t2:ash", "B": "t2:iron"} and closed["reason"] == "all_ready"
    assert closed["results"]["B"]["auto_artifacts"] is True
    choices = events(game, "setup_choice")
    assert [(e["seat"], e["kind"]) for e in choices] == [("A", "factions"), ("A", "artifacts"), ("A", "factions"),
                                                         ("B", "factions")]
    assert game.gdir.load_turn("states", 1)["players"]["A"]["faction"] == "t2:ash"
    assert read_json(game.gdir.private / "setup.json")["result"]["A"]["faction"] == "t2:ash"
    after = game.get_setup("B")
    assert not after["open"] and after["result"]["faction"] == "t2:iron"
    assert after["players"] == [{"seat": "A", "nick": "Альфа", "faction": "t2:ash"},
                                {"seat": "B", "nick": "Бета", "faction": "t2:iron"}]
    assert "artifacts" not in str(after["players"])
    with pytest.raises(GameError) as err:
        game.choose_faction("A", "iron")
    assert err.value.code == "PHASE_LOCKED"
    view = view_state(game.state, game.ruleset, "B")
    assert view["you"]["artifacts"] == b.artifacts and a.artifacts[0] not in str(view)   # hidden from others
    assert next(p for p in view["players"] if p["seat"] == "A")["faction"] == "t2:ash"   # factions are public
    assert game.whoami("A")["artifacts"] == [offer_a]


def test_setup_deadline_gives_defaults(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    clock.advance(121)
    assert game.tick() and game.phase == "negotiation_1"
    close = events(game, "phase_close")[0]
    assert close["phase"] == "setup" and close["reason"] == "deadline" and close["not_ready"] == ["A", "B"]
    factions = sorted(p.faction for p in game.state.players.values())
    assert factions == ["t2:ash", "t2:iron"]
    for seat, p in game.state.players.items():
        assert p.artifacts == [next(a for a in game.setup_offers[seat]
                                    if not game.ruleset.artifacts[a].factions or
                                    p.faction in game.ruleset.artifacts[a].factions)]


def test_load_mid_setup_keeps_offers_and_choices(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock)
    game.choose_faction("B", "ash")
    loaded = Game.load(game.gdir, clock)
    assert loaded.phase == "setup" and loaded.setup_offers == game.setup_offers
    assert loaded.setup_choices == {"B": {"factions": ["t2:ash"]}}
    for seat in "AB":
        loaded.set_ready(seat, None)
    assert loaded.state.players["B"].faction == "t2:ash"


def test_points_mode_game(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock, scenario="duel_points")
    view = game.get_setup("A")
    assert view["artifacts_mode"] == "points" and view["artifact_points"] == 3 and len(view["offer"]) == 6
    with pytest.raises(GameError) as err:
        game.choose_artifacts("A", ["ember", "seal"])
    assert err.value.code == "NOT_ENOUGH_POINTS"
    game.choose_artifacts("A", ["tithe", "warband"])
    game.choose_faction("A", "ash")
    game.choose_faction("B", "ash")                       # unique_factions: false in this scenario
    ready_all(game)
    assert game.state.players["A"].artifacts == ["t2:tithe", "t2:warband"]
    assert game.state.players["B"].faction == "t2:ash"


def test_scenario_without_setup_is_the_slice(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock, scenario="duel_plain")
    assert "setup" not in game.config and "setup" not in game.config["phase_limits"]
    assert game.phase == "negotiation_1" and game.get_setup("A")["enabled"] is False
    assert "artifacts" not in game.whoami("A")
    with pytest.raises(GameError):
        game.choose_faction("A", "iron")
    assert [e["type"] for e in events(game)][:4] == ["game_created", "seat_info", "seat_info", "phase_open"]


def test_host_override_turns_setup_on(tmp_path):
    clock = FakeClock()
    game = new_game(tmp_path, clock, scenario="duel_plain", artifacts="points", artifact_points=2)
    assert game.phase == "setup" and game.config["setup"]["artifact_points"] == 2
    assert game.config["phase_limits"]["setup"] == 300


# ---------------------------------------------------------------- research through Game, journal, replay

def play(tmp_path, seed=7, game_id="s1") -> Game:
    clock = FakeClock()
    game = new_game(tmp_path, clock, seed=seed, game_id=game_id)
    game.choose_faction("A", ["iron"])
    game.choose_faction("B", ["ash"])
    game.choose_artifacts("A", [next(a for a in game.setup_offers["A"] if a != "t2:ember")])
    ready_all(game)
    plan = {1: {"A": "smithing", "B": "drills"}, 2: {"A": "fortification", "B": "fortification"},
            3: {"A": "doctrine"}}
    while not game.game_over:
        if game.phase == "actions":
            for seat, tech in plan.get(game.state.turn, {}).items():
                out = game.submit(seat, [{"kind": "research", "tech": tech}])
                assert not out["rejected"], out
            if game.state.turn == 2:
                cap = next(p.id for p in game.state.owned("B") if p.type == "t2:capital")
                game.submit("B", [{"kind": "build", "province": cap, "building": "temple"}])
        ready_all(game)
    return game


def test_game_research_journal_and_replay(tmp_path):
    game = play(tmp_path)
    started = [(e["seat"], e["tech"], e["turn"]) for e in events(game, "research_started")]
    done = [(e["seat"], e["tech"], e["turn"]) for e in events(game, "tech_completed")]
    assert ("A", "t2:smithing", 1) in started and ("A", "t2:smithing", 1) in done
    assert ("B", "t2:drills", 1) in done and ("A", "t2:doctrine", 3) in started
    techs = game.list_techs("A")
    assert techs["enabled"] and "t2:doctrine" in techs["researched"] and "НАУКА" in techs["text"]
    assert game.list_techs("B")["income"] == 2 + 3 + 3          # capital, shrine, temple
    rep = replay(game.gdir)
    assert rep.ok and len(rep.turns) == 8
    report = game.gdir.load_turn("reports", 1)
    assert report["public"]["techs_count"] == {"A": 1, "B": 1}
    assert "Наука:" in game.state_text("A") and "тех." in game.state_text("A")
    # after game_over the others' techs and artifacts are revealed
    other = next(p for p in game.state_json("B")["players"] if p["seat"] == "A")
    assert other["artifacts"] == game.state.players["A"].artifacts and "t2:doctrine" in other["techs"]


def test_same_seed_and_choices_give_the_same_game(tmp_path):
    one = play(tmp_path / "one", seed=11)
    two = play(tmp_path / "two", seed=11)
    hashes = [[e["state_hash"] for e in events(g, "turn_resolved")] for g in (one, two)]
    assert hashes[0] == hashes[1]
    assert events(one, "setup_closed")[0]["results"] == events(two, "setup_closed")[0]["results"]


def test_replay_detects_a_changed_setup_result(tmp_path):
    game = play(tmp_path)
    assert replay(game.gdir).ok
    path = game.gdir.journal_path
    lines = []
    for e in read_events(path):
        if e["type"] == "setup_closed":
            e["results"]["A"]["faction"], e["results"]["B"]["faction"] = "t2:ash", "t2:iron"
        lines.append(json.dumps(e, ensure_ascii=False))
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    assert replay(game.gdir).ok is False


def test_describe_finds_any_content(tmp_path):
    game = new_game(tmp_path, FakeClock())
    knight = game.describe("A", "Рыцарь")
    assert knight["kind"] == "unit" and knight["id"] == "t2:knight" and knight["requires_tech"] == ["t2:smithing"]
    assert game.describe("A", "drills")["effects"] == ["доход «Сплав» ×1.5"]
    assert game.describe("A", "t2:iron")["bonuses"] == ["цена технологий ветки «Война» ×0.5"]
    assert "Знамя" in game.describe("A", "banner")["text"]
    assert game.describe("A", "ram")["abilities"] == {"ignore_fortify": {}, "siege": {"cap": 1.0}}
    with pytest.raises(GameError) as err:
        game.describe("A", "dragon")
    assert err.value.code == "NOT_FOUND"


# ---------------------------------------------------------------- CLI

def test_cli_create_with_artifact_flags(tmp_path, capsys):
    code = main(["game", "create", "--scenario", "duel_draft", "--packs", PACKS[0], "--seat", "A:agent:Альфа",
                 "--seat", "B:swarm:Бета", "--seed", "3", "--id", "c1", "--games-dir", str(tmp_path),
                 "--artifacts", "points", "--artifact-points", "4", "--artifact-offer", "3", "--setup-limit", "90s"])
    assert code == 0
    out = capsys.readouterr().out
    assert "фаза старта" in out and "бюджет 4" in out
    gdir = GameDir(tmp_path / "c1")
    config = gdir.load_config()
    assert config["setup"]["artifacts"] == "points" and config["setup"]["artifact_points"] == 4
    assert config["setup"]["artifact_offer"] == 3 and config["phase_limits"]["setup"] == 90
    created = read_events(gdir.journal_path)[0]
    assert created["config"]["setup"] == config["setup"]
    game = Game.load(gdir, FakeClock())
    game.start()
    assert game.phase == "setup" and game.seconds_left() == 90


def test_cli_create_without_setup_keeps_the_old_config(tmp_path, capsys):
    code = main(["game", "create", "--scenario", "core:blitz_test", "--seat", "A:agent:Альфа", "--seat", "B:swarm:Бета",
                 "--seed", "3", "--id", "c2", "--games-dir", str(tmp_path)])
    assert code == 0
    config = GameDir(tmp_path / "c2").load_config()
    assert "setup" not in config and set(config["phase_limits"]) == {"negotiation_1", "actions", "negotiation_2"}
    assert "фаза П1" in capsys.readouterr().out
