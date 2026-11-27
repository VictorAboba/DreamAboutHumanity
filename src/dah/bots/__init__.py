"""Bots: HTTP clients of the game API that play bot seats (SLICE.md section 10)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from dah.bots.base import Bot, seat_seed
from dah.bots.builder import BuilderBot
from dah.bots.client import ApiClient
from dah.bots.diplomat import DiplomatBot
from dah.bots.passive import PassiveBot
from dah.bots.rush import RushBot
from dah.bots.scout import ScoutBot

BOT_TYPES: dict[str, type[Bot]] = {cls.name: cls for cls in (PassiveBot, BuilderBot, RushBot, DiplomatBot,
                                                                       ScoutBot)}

__all__ = ["BOT_TYPES", "Bot", "BuilderBot", "DiplomatBot", "PassiveBot", "RushBot", "ScoutBot", "run_bots"]


def run_bots(game_dir: str | Path, server_url: str, stop_event: threading.Event | None = None) -> None:
    """Play every seat in `<game_dir>/private/bot_keys.json`, one thread each, until game_over or stop."""
    game_dir = Path(game_dir)
    entries = json.loads((game_dir / "private" / "bot_keys.json").read_text(encoding="utf-8"))
    unknown = sorted({entry["bot"] for entry in entries.values()} - BOT_TYPES.keys())
    if unknown:
        raise ValueError(f"unknown bot type(s): {', '.join(unknown)}")
    stop = stop_event or threading.Event()

    def play(key: str, bot: Bot) -> None:
        api = ApiClient(server_url, key)
        try:
            bot.run(api, stop)
        finally:
            api.close()

    threads = [
        threading.Thread(
            target=play,
            args=(entry["key"], BOT_TYPES[entry["bot"]](seat, seat_seed(game_dir.name, seat))),
            name=f"dah-bot-{seat}",
            daemon=True,
        )
        for seat, entry in sorted(entries.items())
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
