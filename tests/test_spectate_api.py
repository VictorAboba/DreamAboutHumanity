"""Spectator API (/v1/spectate/*) and the /watch page: access levels, one-turn delay, full reveal, key separation."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from dah.cli import main
from dah.game import Game, make_config
from dah.server import create_app, spectate
from dah.store import GameDir, hash_key, init_game_dir, read_json

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"}]
ADMIN = "admin-secret"
PUBLIC_TEXT = "Привет всем, давайте мирно"
PRIVATE_TEXT = "Соло, секретный пакт <b>против</b> всех"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, tmp_path, turn_limit: int = 3) -> None:
        self.clock = FakeClock()
        config = make_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7, turn_limit=turn_limit)
        self.gdir, keys = init_game_dir(tmp_path, config)
        self.spectator_key = self.gdir.issue_spectator_key()
        self.game = Game.create(self.gdir, self.clock)
        self.client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, clock=self.clock, tick_interval=None))
        self.seat_headers = {seat: {"Authorization": f"Bearer {key}"} for seat, key in keys.items()}
        self.headers = {"admin": {"Authorization": f"Bearer {ADMIN}"},
                        "spectator": {"Authorization": f"Bearer {self.spectator_key}"}}

    def game_now(self) -> Game:
        return self.client.app.state.registry.games["g1"]

    def seat(self, seat: str, method: str, path: str, **kwargs) -> dict:
        data = self.client.request(method, path, headers=self.seat_headers[seat], **kwargs).json()
        assert data["ok"], data
        return data

    def watch(self, who: str, path: str, status: int = 200) -> dict:
        resp = self.client.get(f"/v1/spectate/g1/{path}", headers=self.headers[who])
        assert resp.status_code == status, resp.text
        return resp.json()

    def ready_all(self) -> None:
        for seat in ("A", "B"):
            book = self.seat(seat, "GET", "/v1/orders")
            self.seat(seat, "POST", "/v1/ready", json={"confirm_hash": book["orders_hash"]})

    def finish_turn(self, orders: dict[str, list[dict]] | None = None) -> None:
        """Play П1 → Д (with `orders`) → П2 → resolution."""
        assert self.game_now().phase == "negotiation_1"
        self.ready_all()
        for seat, items in (orders or {}).items():
            self.seat(seat, "POST", "/v1/orders", json={"orders": items, "mode": "replace_all"})
        self.ready_all()
        self.ready_all()


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def _march(game: Game, seat: str) -> tuple[str, str, str]:
    """(capital, own waste next to it, neutral province next to that waste)."""
    rs, st = game.ruleset, game.state
    cap = next(p.id for p in st.owned(seat) if p.type == "core:capital")
    waste = next(n for n in rs.map.adjacency[cap] if st.provinces[n].owner == seat
                 and st.provinces[n].type == "core:waste")
    neutral = next(n for n in rs.map.adjacency[waste] if st.provinces[n].owner is None)
    return cap, waste, neutral


def test_spectator_is_one_turn_behind_and_sees_only_public(env):
    cap, waste, neutral = _march(env.game_now(), "A")
    env.seat("A", "POST", "/v1/messages", json={"to": "all", "text": PUBLIC_TEXT})
    env.seat("A", "POST", "/v1/messages", json={"to": ["Соло"], "text": PRIVATE_TEXT})

    live = env.watch("spectator", "live")
    assert live["level"] == "delayed" and live["access"] == "spectator" and live["last_viewable"] == 0
    assert live["ready"] == "0/2" and live["phase"] == "negotiation_1" and live["timings_allowed"] is False
    start = env.watch("spectator", "turn/0")
    assert start["full"] is False and start["messages"] == []
    assert all(set(p) == {"id", "owner"} for p in start["provinces"])
    assert all("resources" not in p and "units" not in p for p in start["players"])
    assert {p["id"]: p["owner"] for p in start["provinces"]}[cap] == "A"
    assert env.watch("spectator", "turn/1", 403)["error"]["code"] == "NOT_YET"
    env.watch("spectator", "timings", 403)

    env.finish_turn({"A": [{"kind": "move", "from": cap, "to": waste, "units": {"assault": 3}}]})
    env.finish_turn({"A": [{"kind": "move", "from": waste, "to": neutral, "units": {"assault": 3}}]})
    live = env.watch("spectator", "live")
    assert live["resolved_turns"] == 2 and live["last_viewable"] == 1
    assert [h["turn"] for h in live["scores_history"]] == [0, 1]
    env.watch("spectator", "turn/2", 403)
    t1 = env.watch("spectator", "turn/1")
    assert [m["text"] for m in t1["messages"]] == [PUBLIC_TEXT]
    assert t1["messages"][0]["public"] is True and t1["messages"][0]["from_nick"] == "Нико"
    assert "orders" not in t1 and "unit_names" not in t1
    assert all(set(p) == {"id", "owner"} for p in t1["provinces"])
    assert {e["kind"] for e in t1["events"]} <= {"captured", "eliminated"}
    assert all("resources" not in p and "command_points" not in p for p in t1["players"])

    # one turn later the capture of turn 2 becomes visible, still without units
    env.finish_turn()
    t2 = env.watch("spectator", "turn/2")
    captured = [e for e in t2["events"] if e["kind"] == "captured"]
    assert [(e["province"], e["to"]) for e in captured] == [(neutral, "A")]
    assert "Нико" in captured[0]["text"]
    assert {p["id"]: p["owner"] for p in t2["provinces"]}[neutral] == "A"


def test_admin_sees_everything_live(env):
    cap, waste, _ = _march(env.game_now(), "A")
    env.seat("A", "POST", "/v1/messages", json={"to": ["Соло"], "text": PRIVATE_TEXT})
    live = env.watch("admin", "live")
    assert live["level"] == "live" and live["access"] == "admin" and live["last_viewable"] == 1
    assert live["timings_allowed"] is True and live["seconds_left"] is not None
    now = env.watch("admin", "turn/1")
    assert now["in_progress"] is True and now["full"] is True and now["events"] == []
    assert [m["text"] for m in now["messages"]] == [PRIVATE_TEXT] and now["messages"][0]["to_nicks"] == ["Соло"]
    assert {p["id"]: p for p in now["provinces"]}[cap]["units"] == {"core:assault": 3}
    assert all("resources" in p for p in now["players"])
    assert "orders" not in now  # orders of the turn in progress stay hidden until it resolves

    env.finish_turn({"A": [{"kind": "move", "from": cap, "to": waste, "units": {"assault": 3}}]})
    t1 = env.watch("admin", "turn/1")
    assert t1["in_progress"] is False
    assert [o["kind"] for o in t1["orders"]["A"]] == ["move"] and t1["orders"]["B"] == []
    assert any(e["kind"] == "economy" and e["text"].startswith("Нико: ") for e in t1["events"])
    assert {p["id"]: p for p in t1["provinces"]}[waste]["units"] == {"core:assault": 3}
    assert env.watch("admin", "live")["last_viewable"] == 2
    timings = env.watch("admin", "timings")
    first = timings["phases"][0]
    assert first["turn"] == 1 and first["phase"] == "negotiation_1" and first["closed"] == "all_ready"
    assert first["seats"]["A"]["calls"] >= 1 and "ready_s" in first["seats"]["A"]
    assert set(timings["totals"]) == {"A", "B"} and "host_calls" in timings["totals"]["A"]


def test_full_reveal_after_game_over(env):
    cap, waste, _ = _march(env.game_now(), "A")
    env.seat("A", "POST", "/v1/messages", json={"to": ["Соло"], "text": PRIVATE_TEXT})
    env.finish_turn({"A": [{"kind": "move", "from": cap, "to": waste, "units": {"assault": 3}}]})
    env.finish_turn()
    env.finish_turn()
    assert env.game_now().game_over
    live = env.watch("spectator", "live")
    assert live["game_over"] is True and live["level"] == "revealed" and live["last_viewable"] == 3
    assert live["result"]["reason"] == "turn_limit" and live["timings_allowed"] is True
    t1 = env.watch("spectator", "turn/1")
    assert t1["full"] is True and [m["text"] for m in t1["messages"]] == [PRIVATE_TEXT]
    assert "orders" in t1 and {p["id"]: p for p in t1["provinces"]}[waste]["units"] == {"core:assault": 3}
    last = env.watch("spectator", "turn/3")
    assert last["result"]["reason"] == "turn_limit" and last["in_progress"] is False
    env.watch("spectator", "turn/4", 404)
    assert env.watch("spectator", "timings")["phases"]
    assert env.watch("admin", "live")["last_viewable"] == 3


def test_keys_are_separated(env):
    for headers in ({}, env.seat_headers["A"], env.seat_headers["B"], {"Authorization": "Bearer dahw_g1_wrong"}):
        for path in ("meta", "live", "turn/0", "timings"):
            resp = env.client.get(f"/v1/spectate/g1/{path}", headers=headers)
            assert resp.status_code == 401 and resp.json()["error"]["code"] == "UNAUTHORIZED"
    for path in ("/v1/whoami", "/v1/phase", "/v1/state", "/v1/inbox"):
        assert env.client.get(path, headers=env.headers["spectator"]).status_code == 401
    assert env.client.get("/v1/admin/games/g1", headers=env.headers["spectator"]).status_code == 403
    # an unknown game is 401 for everyone but the admin (no probing of game ids)
    assert env.client.get("/v1/spectate/nope/live", headers=env.headers["spectator"]).status_code == 401
    assert env.client.get("/v1/spectate/nope/live", headers=env.headers["admin"]).status_code == 404
    assert env.client.get("/v1/spectate/g1/turn/x", headers=env.headers["admin"]).status_code == 400
    stored = read_json(env.gdir.spectator_path)
    assert stored == {"sha256": hash_key(env.spectator_key)}
    assert env.spectator_key not in env.gdir.keys_path.read_text(encoding="utf-8")


def test_spectate_calls_are_not_journaled(env):
    before = env.gdir.journal_path.stat().st_size
    for _ in range(3):
        env.watch("spectator", "live")
        env.watch("admin", "turn/1")
    assert env.gdir.journal_path.stat().st_size == before


def test_meta_has_map_layout_and_seats(env):
    meta = env.watch("spectator", "meta")
    assert meta["game_id"] == "g1" and meta["scenario"]["id"] == "core:slice2" and meta["turn_limit"] == 3
    assert meta["level"] == "delayed" and meta["level_label"] == "Зритель (задержка 1 ход)"
    provinces = {p["id"]: p for p in meta["map"]["provinces"]}
    assert len(provinces) == 10 and meta["map"]["layout_computed"] is False
    assert provinces["P01"]["pos"] == [10.0, 50.0] and provinces["P01"]["capital"] is True
    assert ["P01", "P02"] in meta["map"]["edges"] and len(meta["map"]["edges"]) == 13
    # players never see their opponents' kind or bot type, so the delayed spectator does not either
    assert [(s["seat"], s["nick"]) for s in meta["seats"]] == [("A", "Нико"), ("B", "Соло")]
    assert all(not {"kind", "bot", "declared"} & set(s) for s in meta["seats"])
    assert {s["capital"] for s in meta["seats"]} == {"P01", "P05"}
    admin = env.watch("admin", "meta")
    assert admin["level_label"] == "Админ (вживую)"
    assert [(s["seat"], s["kind"]) for s in admin["seats"]] == [("A", "agent"), ("B", "swarm")]


def test_meta_reveals_seat_kinds_after_game_over(env):
    for _ in range(3):
        env.finish_turn()
    meta = env.watch("spectator", "meta")
    assert meta["level"] == "revealed"
    assert [(s["seat"], s["kind"], s["bot"]) for s in meta["seats"]] == [("A", "agent", None), ("B", "swarm", None)]


def test_delayed_message_ids_are_contiguous_while_running(tmp_path):
    env = Env(tmp_path, turn_limit=5)
    env.seat("A", "POST", "/v1/messages", json={"to": "all", "text": "первое"})
    for _ in range(2):
        env.seat("A", "POST", "/v1/messages", json={"to": ["Соло"], "text": PRIVATE_TEXT})
    env.seat("A", "POST", "/v1/messages", json={"to": "all", "text": "второе"})
    env.finish_turn()
    env.seat("B", "POST", "/v1/messages", json={"to": ["Нико"], "text": PRIVATE_TEXT})
    env.seat("B", "POST", "/v1/messages", json={"to": "all", "text": "третье"})
    env.finish_turn()
    env.finish_turn()
    t1, t2 = env.watch("spectator", "turn/1"), env.watch("spectator", "turn/2")
    assert t1["level"] == "delayed"
    assert [(m["id"], m["text"]) for m in t1["messages"]] == [(1, "первое"), (2, "второе")]
    assert [(m["id"], m["text"]) for m in t2["messages"]] == [(3, "третье")]
    assert [m["id"] for m in env.watch("admin", "turn/2")["messages"]] == [5, 6]  # full levels keep the global id


def test_watch_page_is_served_without_key(env):
    resp = env.client.get("/watch/g1")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert "sessionStorage" in html and "Authorization" in html and "/v1/spectate/" in html
    assert env.spectator_key not in html and ADMIN not in html
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in html  # player texts go through textContent only
    assert "http://" not in html.replace('"http://www.w3.org/2000/svg"', "") and "https://" not in html  # no CDN
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["cache-control"] == "no-store"
    for label in ("Лента", "Переписка", "Счёт", "Тайминги", "Вживую", "Повтор"):
        assert label in html


def test_cli_game_create_prints_spectator_key_and_watch_url(tmp_path, capsys):
    code = main(["game", "create", "--scenario", "core:slice2", "--seat", "A:agent:Нико", "--seat", "B:bot-rush:Бот",
                 "--seed", "3", "--id", "w1", "--games-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    key = next(word for line in out.splitlines() for word in line.split() if word.startswith("dahw_w1_"))
    assert "/watch/w1" in out
    gdir = GameDir(tmp_path / "w1")
    assert read_json(gdir.spectator_path) == {"sha256": hash_key(key)}
    for path in (gdir.config_path, gdir.journal_path, gdir.keys_path):
        assert key not in path.read_text(encoding="utf-8")
    client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, tick_interval=None))
    meta = client.get("/v1/spectate/w1/meta", headers={"Authorization": f"Bearer {key}"}).json()
    assert meta["ok"] and meta["access"] == "spectator"


def test_cli_spectator_key_and_server_option(tmp_path, capsys):
    assert main(["game", "create", "--scenario", "core:slice2", "--seat", "A:agent:Нико", "--seat", "B:agent:Соло",
                 "--id", "w2", "--games-dir", str(tmp_path), "--server", "http://127.0.0.1:8850/"]) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8850/watch/w2" in out
    old = next(word for line in out.splitlines() for word in line.split() if word.startswith("dahw_w2_"))
    assert main(["game", "spectator-key", "w2", "--games-dir", str(tmp_path), "--server", "http://h:8851"]) == 0
    out = capsys.readouterr().out
    new = next(word for line in out.splitlines() for word in line.split() if word.startswith("dahw_w2_"))
    assert new != old and "http://h:8851/watch/w2" in out
    assert read_json(GameDir(tmp_path / "w2").spectator_path) == {"sha256": hash_key(new)}
    assert main(["game", "spectator-key", "nope", "--games-dir", str(tmp_path)]) == 2


def test_watch_page_csp_allows_only_its_own_script(env):
    resp = env.client.get("/watch/g1")
    csp = resp.headers["content-security-policy"]
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
    bodies = re.findall(r"<script>(.*?)</script>", resp.text, re.S)
    assert len(bodies) == 1 and resp.text.count("<script") == 1
    digest = base64.b64encode(hashlib.sha256(bodies[0].encode("utf-8")).digest()).decode("ascii")
    assert f"'sha256-{digest}'" in script_src


def test_watch_key_is_live_and_read_only(tmp_path):
    env = Env(tmp_path)
    client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, watch_key="watch-secret", clock=env.clock,
                                   tick_interval=None))
    headers = {"Authorization": "Bearer watch-secret"}
    live = client.get("/v1/spectate/g1/live", headers=headers).json()
    assert live["ok"] and live["level"] == "live" and live["access"] == "watcher" and live["last_viewable"] == 1
    assert client.get("/v1/spectate/g1/timings", headers=headers).status_code == 200
    assert client.get("/v1/spectate/nope/live", headers=headers).status_code == 404
    assert client.get("/v1/admin/games/g1", headers=headers).status_code == 403
    assert client.get("/v1/whoami", headers=headers).status_code == 401


def test_reissued_spectator_key_revokes_the_old_one(env):
    env.watch("spectator", "live")
    new_key = env.gdir.issue_spectator_key()
    assert env.client.get("/v1/spectate/g1/live", headers=env.headers["spectator"]).status_code == 401
    resp = env.client.get("/v1/spectate/g1/live", headers={"Authorization": f"Bearer {new_key}"})
    assert resp.status_code == 200 and resp.json()["access"] == "spectator"


def test_live_caches_start_scores(env, monkeypatch):
    first = env.watch("spectator", "live")["scores_history"]
    calls = []
    original = GameDir.load_turn

    def counting(self, kind, turn):
        calls.append((kind, turn))
        return original(self, kind, turn)

    monkeypatch.setattr(GameDir, "load_turn", counting)
    for _ in range(3):
        assert env.watch("spectator", "live")["scores_history"] == first
    assert ("states", 1) not in calls


def test_timings_rebuild_is_throttled_while_running(env, monkeypatch):
    builds = []
    original = spectate.build_timings

    def counting(path):
        builds.append(path)
        return original(path)

    monkeypatch.setattr(spectate, "build_timings", counting)
    env.watch("admin", "timings")
    env.seat("A", "GET", "/v1/phase")  # the journal grows
    env.watch("admin", "timings")
    assert len(builds) == 1  # rebuilt at most once per TIMINGS_MIN_AGE
    monkeypatch.setattr(spectate, "TIMINGS_MIN_AGE", 0.0)
    env.seat("A", "GET", "/v1/phase")
    phases = env.watch("admin", "timings")["phases"]
    assert len(builds) == 2 and phases[0]["seats"]["A"]["calls"] >= 2
    env.watch("admin", "timings")  # journal unchanged: cached
    assert len(builds) == 2
