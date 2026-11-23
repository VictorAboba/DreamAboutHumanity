"""Independent randomness streams: one ``random.Random`` per (seed, turn, system, entity)."""

from __future__ import annotations

import hashlib
import random


def stream(game_seed: int | str, turn: int, system: str, entity_id: str) -> random.Random:
    key = hashlib.sha256(f"{game_seed}|{turn}|{system}|{entity_id}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(key, "big"))
