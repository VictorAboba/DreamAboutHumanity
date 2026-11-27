"""Map layout for the spectator page: `pos` in map files (validated) and the computed fallback."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from dah.engine import ContentError, build_ruleset
from dah.engine.content import DEFAULT_PACKS_DIR, circular_layout
from dah.game import Game
from slice_helpers import slice_config
from dah.server import create_app
from dah.store import init_game_dir

CORE = DEFAULT_PACKS_DIR / "core"
ADMIN = "admin-secret"


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    dst = tmp_path / "packs" / "core"
    shutil.copytree(CORE, dst)
    return dst


def edit_map(pack: Path, name: str, fn) -> None:
    path = pack / "maps" / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def strip_pos(data) -> None:
    for p in data["provinces"]:
        p.pop("pos", None)


def issues_of(pack: Path, scenario: str = "slice3"):
    with pytest.raises(ContentError) as exc:
        build_ruleset([pack], scenario)
    return exc.value.issues


@pytest.mark.parametrize("scenario,turn_deg", [("slice2", 180), ("slice3", 120)])
def test_core_maps_have_rotation_symmetric_layout(scenario, turn_deg):
    m = build_ruleset(None, scenario).map
    assert not m.layout_computed and set(m.layout) == set(m.provinces)
    assert all(0 <= v <= 100 for xy in m.layout.values() for v in xy)
    assert len(set(m.layout.values())) == len(m.layout)

    def rotate(xy):
        a = math.radians(turn_deg)
        x, y = xy[0] - 50, xy[1] - 50
        return (50 + x * math.cos(a) - y * math.sin(a), 50 + x * math.sin(a) + y * math.cos(a))

    capitals = [m.layout[s.capital] for s in m.starts]
    for c in capitals:  # each capital rotates onto another capital
        r = rotate(c)
        assert min(math.dist(r, o) for o in capitals) < 0.5


@pytest.mark.parametrize("scenario", ["slice2", "slice3"])
def test_core_map_nodes_leave_room_for_labels(scenario):
    """Node r=4.6, a unit badge above-right and the name 8 units below: closer nodes overlap labels and badges."""
    layout = build_ruleset(None, scenario).map.layout
    ids = sorted(layout)
    closest = min(math.dist(layout[a], layout[b]) for i, a in enumerate(ids) for b in ids[i + 1:])
    assert closest >= 17


def test_layout_does_not_change_ruleset_hash(pack):
    for name in ("mirror2", "mirror3"):
        edit_map(pack, name, strip_pos)
    plain = build_ruleset([pack], "slice3")
    assert plain.map.layout_computed and plain.hash == build_ruleset(None, "slice3").hash


def test_pos_out_of_range_is_error(pack):
    edit_map(pack, "mirror3", lambda d: d["provinces"][0].__setitem__("pos", [150, 3]))
    assert any("pos" in i.path and i.file.endswith("mirror3.yaml") for i in issues_of(pack))


def test_pos_wrong_shape_is_error(pack):
    edit_map(pack, "mirror3", lambda d: d["provinces"][1].__setitem__("pos", [10]))
    assert any(i.path.startswith("provinces[1].pos") for i in issues_of(pack))


def test_pos_on_some_provinces_only_is_error(pack):
    edit_map(pack, "mirror3", lambda d: d["provinces"][4].pop("pos"))
    issues = issues_of(pack)
    assert any(i.path == "provinces[4]" and "every province" in i.message for i in issues)


def test_circular_layout_is_deterministic_and_inside_canvas():
    ids = [f"P{i:02d}" for i in range(1, 16)]
    groups = [("P01", "P02"), ("P05", "P06")]
    a, b = circular_layout(ids, groups), circular_layout(list(reversed(ids)), groups)
    assert a == b and set(a) == set(ids)
    assert all(0 <= v <= 100 for xy in a.values() for v in xy) and len(set(a.values())) == len(ids)
    assert math.dist(a["P01"], (50, 50)) > math.dist(a["P03"], (50, 50))  # starts outside, the rest inside
    assert circular_layout(["X"]) == {"X": (50.0, 10.0)}


def test_meta_of_map_without_pos_gets_computed_layout(tmp_path, pack):
    edit_map(pack, "mirror2", strip_pos)
    seats = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "agent"}]
    config = slice_config(game_id="np", scenario="core:slice2", seats=seats, seed=1, pack_dirs=[str(pack)])
    games = tmp_path / "games"
    gdir, _ = init_game_dir(games, config)
    Game.create(gdir)
    client = TestClient(create_app(str(games), admin_key=ADMIN, tick_interval=None))
    meta = client.get("/v1/spectate/np/meta", headers={"Authorization": f"Bearer {ADMIN}"}).json()
    assert meta["ok"] and meta["map"]["layout_computed"] is True
    pos = [tuple(p["pos"]) for p in meta["map"]["provinces"]]
    assert len(pos) == 10 and len(set(pos)) == 10 and all(0 <= v <= 100 for xy in pos for v in xy)
