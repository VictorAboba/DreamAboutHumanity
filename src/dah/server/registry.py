"""Loaded games of one games directory and seat-key authentication."""

from __future__ import annotations

import logging
import time

from dah.game import Clock, Game
from dah.store import hash_key, list_game_dirs

log = logging.getLogger(__name__)

KEY_PREFIX = "dah_"
REFRESH_INTERVAL_SECONDS = 2.0


class GameRegistry:
    def __init__(self, games_dir: str, clock: Clock = time.time, *,
                 refresh_interval: float = REFRESH_INTERVAL_SECONDS) -> None:
        self.games_dir = games_dir
        self.clock = clock
        self.refresh_interval = refresh_interval
        self.games: dict[str, Game] = {}
        self._keys: dict[str, tuple[str, str]] = {}   # sha256(key) -> (game_id, seat)
        self._refreshed_at = time.monotonic()
        self.refresh()

    def refresh(self) -> None:
        """Load games that appeared on disk since the last scan (restored from `private/`).

        A game created by `dah game create` has not opened its first phase yet; it opens now.
        """
        self._refreshed_at = time.monotonic()
        for gdir in list_game_dirs(self.games_dir):
            if gdir.game_id in self.games or not gdir.runtime_path.is_file():
                continue
            game = Game.load(gdir, self.clock)
            game.start()
            self.games[gdir.game_id] = game
            self._keys.update({h: (gdir.game_id, seat) for h, seat in gdir.load_key_hashes().items()})

    def authenticate(self, key: str) -> tuple[Game, str] | None:
        """The key's game and seat. An unknown well-formed key rescans the disk at most once per interval."""
        if not key.startswith(KEY_PREFIX):
            return None
        digest = hash_key(key)
        if digest not in self._keys and time.monotonic() - self._refreshed_at >= self.refresh_interval:
            self.refresh()
        found = self._keys.get(digest)
        return (self.games[found[0]], found[1]) if found else None

    def get(self, game_id: str) -> Game | None:
        """A loaded game by id; an unknown id rescans the disk at most once per interval."""
        if game_id not in self.games and time.monotonic() - self._refreshed_at >= self.refresh_interval:
            self.refresh()
        return self.games.get(game_id)

    def tick_all(self) -> None:
        for game in list(self.games.values()):
            try:
                game.tick()
            except Exception:  # one broken game must not stop the others' clocks
                log.exception("tick failed for game %s", game.config["game_id"])
