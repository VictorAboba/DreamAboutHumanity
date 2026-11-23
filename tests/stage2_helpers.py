"""Shared helpers for the stage-2 tests (fixture pack tests/packs/t2, TECH_AND_SETUP.md)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

import yaml

from dah.engine import GameState, Ruleset, build_ruleset, new_game_state

PACK = Path(__file__).parent / "packs" / "t2"
PACKS = [str(PACK)]
# core 0.2.0 frozen (the slice without stage-2 content): old packs must build and hash as before
CORE020 = [str(Path(__file__).resolve().parents[1] / "content" / "legacy" / "core-0.2.0")]
CORE020_HASHES = {"slice3": "97c0b106", "slice2": "41fe1492", "blitz_test": "ee39558b"}


def ruleset(scenario: str = "duel_draft") -> Ruleset:
    return build_ruleset(PACKS, scenario)


def state(rs: Ruleset, factions: tuple[str, str] = ("iron", "ash"), seed: int = 1,
          artifacts: dict[str, list[str]] | None = None) -> GameState:
    seats = [{"seat": "A", "nick": "Альфа", "faction": factions[0]}, {"seat": "B", "nick": "Бета", "faction": factions[1]}]
    for s in seats:
        if artifacts and s["seat"] in artifacts:
            s["artifacts"] = [f"t2:{a}" for a in artifacts[s["seat"]]]
    return new_game_state(rs, seats, seed)


def capital(st: GameState, seat: str) -> str:
    return next(p.id for p in st.owned(seat) if p.type == "t2:capital")


def own(st: GameState, seat: str, ptype: str) -> str:
    return next(p.id for p in st.owned(seat) if p.type == f"t2:{ptype}")


def center(st: GameState, rs: Ruleset, seat: str) -> str:
    """The neutral province next to the seat's shrine."""
    shrine = own(st, seat, "shrine")
    return next(n for n in rs.map.adjacency[shrine] if st.provinces[n].owner is None)


def pack_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "t2"
    shutil.copytree(PACK, dst)
    return dst


def edit_yaml(path: Path, change: Callable[[Any], Any]) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    result = change(data)
    path.write_text(yaml.safe_dump(result if result is not None else data, allow_unicode=True), encoding="utf-8")


def by_id(items: list[dict[str, Any]], entity_id: str) -> dict[str, Any]:
    return next(i for i in items if i["id"] == entity_id)


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
