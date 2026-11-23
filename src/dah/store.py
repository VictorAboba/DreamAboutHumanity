"""On-disk layout of one game (`games/<id>/`) and seat keys (only their sha256 is stored).

    games/<id>/config.json           game config without keys
    games/<id>/keys.json             {sha256(key): seat}
    games/<id>/journal.jsonl         public-ish journal (SLICE.md section 6)
    games/<id>/private/runtime.json  live snapshot: state, books, ready, phase timing (restore after restart)
    games/<id>/private/states/turn_<N>.json   state at the start of turn N
    games/<id>/private/orders/turn_<N>.json   {seat: SeatOrders} resolved at the end of turn N
    games/<id>/private/reports/turn_<N>.json  TurnReport of turn N
    games/<id>/private/messages.jsonl         full message texts
    games/<id>/private/bot_keys.json          {seat: {key, bot}} for `dah bots run`
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

GAME_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SEAT_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def make_key(game_id: str, seat: str) -> str:
    return f"dah_{game_id}_{seat}_{secrets.token_urlsafe(18).replace('_', 'x').replace('-', 'y')}"


def write_json(path: Path, data: Any) -> None:
    """Atomic write: a crash never leaves a half-written snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class GameDir:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def game_id(self) -> str:
        return self.path.name

    @property
    def config_path(self) -> Path:
        return self.path / "config.json"

    @property
    def keys_path(self) -> Path:
        return self.path / "keys.json"

    @property
    def journal_path(self) -> Path:
        return self.path / "journal.jsonl"

    @property
    def private(self) -> Path:
        return self.path / "private"

    @property
    def runtime_path(self) -> Path:
        return self.private / "runtime.json"

    @property
    def messages_path(self) -> Path:
        return self.private / "messages.jsonl"

    @property
    def bot_keys_path(self) -> Path:
        return self.private / "bot_keys.json"

    def exists(self) -> bool:
        return self.config_path.is_file()

    def turn_file(self, kind: str, turn: int) -> Path:
        """kind: states | orders | reports."""
        return self.private / kind / f"turn_{turn}.json"

    def save_turn(self, kind: str, turn: int, data: Any) -> None:
        write_json(self.turn_file(kind, turn), data)

    def load_turn(self, kind: str, turn: int) -> Any | None:
        path = self.turn_file(kind, turn)
        return read_json(path) if path.is_file() else None

    def load_config(self) -> dict[str, Any]:
        return read_json(self.config_path)

    def load_key_hashes(self) -> dict[str, str]:
        return read_json(self.keys_path) if self.keys_path.is_file() else {}

    def append_message(self, message: dict[str, Any]) -> None:
        self.messages_path.parent.mkdir(parents=True, exist_ok=True)
        with self.messages_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")

    def load_messages(self) -> list[dict[str, Any]]:
        if not self.messages_path.is_file():
            return []
        lines = self.messages_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


def init_game_dir(games_dir: str | Path, config: dict[str, Any]) -> tuple[GameDir, dict[str, str]]:
    """Create `games/<id>/` with config and hashed keys. Returns the dir and {seat: key} (shown once).

    Keys of `bot` seats are also kept in `private/bot_keys.json` so `dah bots run` can play them.
    """
    game_id = config["game_id"]
    if not GAME_ID_RE.match(game_id):
        raise ValueError(f"game id must match {GAME_ID_RE.pattern}: {game_id!r}")
    gdir = GameDir(Path(games_dir) / game_id)
    if gdir.path.exists():
        raise FileExistsError(f"game directory already exists: {gdir.path}")
    keys: dict[str, str] = {}
    for seat in config["seats"]:
        if not SEAT_RE.match(seat["seat"]):
            raise ValueError(f"seat id must match {SEAT_RE.pattern}: {seat['seat']!r}")
        keys[seat["seat"]] = make_key(game_id, seat["seat"])
    gdir.private.mkdir(parents=True)
    write_json(gdir.config_path, config)
    write_json(gdir.keys_path, {hash_key(k): seat for seat, k in keys.items()})
    bots = {s["seat"]: {"key": keys[s["seat"]], "bot": s["bot"]} for s in config["seats"] if s.get("kind") == "bot"}
    if bots:
        write_json(gdir.bot_keys_path, bots)
    return gdir, keys


def list_game_dirs(games_dir: str | Path) -> list[GameDir]:
    root = Path(games_dir)
    if not root.is_dir():
        return []
    return [GameDir(p) for p in sorted(root.iterdir()) if (p / "config.json").is_file()]
