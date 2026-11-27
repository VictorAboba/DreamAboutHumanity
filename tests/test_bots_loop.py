"""Bot phase loop against a scripted fake API (no server)."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from dah.bots import run_bots
from dah.bots.builder import BuilderBot
from dah.bots.client import ApiError
from dah.bots.passive import PassiveBot
from dah.bots.rush import RushBot


def make_state(phase: str) -> dict[str, Any]:
    provinces = {
        "P01": {"type": "capital", "owner": "A", "units": {"assault": 3}, "buildings": ["barracks"]},
        "P02": {"type": "forge", "owner": "A", "units": {}, "buildings": []},
        "P07": {"type": "forge", "owner": "B", "units": {"assault": 1}, "buildings": []},
    }
    edges = [["P01", "P02"], ["P01", "P07"], ["P02", "P07"]]
    you = {"resources": {"alloy": 40, "provisions": 30}, "command_points": 3}
    return {"ok": True, "state": {"turn": 1, "phase": phase, "you": you, "provinces": provinces, "edges": edges}}


class FakeApi:
    def __init__(self, phases: list[str], win_prob: float = 0.9, cp_left: int = 3) -> None:
        self.phases = phases
        self.index = 0
        self.win_prob = win_prob
        self.cp_left = cp_left
        self.calls: list[tuple[str, Any]] = []
        self.orders_list: list[dict] = []

    def _status(self) -> dict[str, Any]:
        phase = self.phases[self.index] if self.index < len(self.phases) else "game_over"
        return {"ok": True, "turn": 1, "phase": phase, "phase_seq": self.index, "you_ready": False,
                "game_over": phase == "game_over"}

    def whoami(self):
        return {"ok": True, "seat": "A", "nick": "Нико"}

    def phase(self):
        return self._status()

    def wait_phase(self, after_seq, max_seconds):
        self.index += 1
        return {"changed": True, **self._status()}

    def state(self):
        return make_state(phase=self._status()["phase"])

    def orders(self):
        return {"ok": True, "orders": self.orders_list, "orders_hash": f"h{len(self.orders_list)}",
                "cp_left": self.cp_left}

    def submit_orders(self, orders, mode):
        self.calls.append(("submit", (mode, orders)))
        self.orders_list = orders if mode == "replace_all" else self.orders_list + orders
        return {"ok": True, "accepted": orders, "rejected": [], "orders_hash": f"h{len(self.orders_list)}"}

    def estimate(self, attacker, defender, province_type, fortified, hasty):
        return self.win_prob if province_type == "forge" else 0.0

    def send_message(self, to, text):
        self.calls.append(("message", (to, text)))
        raise ApiError("LIMIT", "message limit")  # must not break the phase

    def ready(self, confirm_hash):
        self.calls.append(("ready", (self._status()["phase"], confirm_hash)))
        return {"ok": True}


PHASES = ["negotiation_1", "actions", "negotiation_2"]


def readies(api: FakeApi) -> list[tuple]:
    return [args for name, args in api.calls if name == "ready"]


def test_passive_readies_every_phase_until_game_over():
    api = FakeApi(PHASES * 2)
    PassiveBot("A", 1).run(api, threading.Event())
    assert readies(api) == [(p, "h0") for p in PHASES * 2]
    assert [args for name, args in api.calls if name == "submit"] == [("replace_all", [])] * 2


def test_builder_greets_submits_and_confirms_hash():
    api = FakeApi(PHASES)
    BuilderBot("A", 1).run(api, threading.Event())
    names = [name for name, _ in api.calls]
    assert names == ["message", "ready", "submit", "ready", "ready"]
    submitted = next(args for name, args in api.calls if name == "submit")[1]
    assert readies(api)[1] == ("actions", f"h{len(submitted)}")   # confirms the hash of what it submitted


def test_rush_appends_p2_attack_when_cp_allow():
    api = FakeApi(["negotiation_2"], cp_left=2)
    RushBot("A", 1).run(api, threading.Event())
    submits = [args for name, args in api.calls if name == "submit"]
    assert submits == [("append", [{"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 3}}])]
    assert readies(api) == [("negotiation_2", "h1")]


def test_stop_event_ends_loop_immediately():
    api = FakeApi(PHASES)
    stop = threading.Event()
    stop.set()
    PassiveBot("A", 1).run(api, stop)
    assert api.calls == []


def test_run_bots_rejects_unknown_bot_type(tmp_path):
    (tmp_path / "private").mkdir()
    keys = {"C": {"key": "dah_x_C_1", "bot": "berserk"}}
    (tmp_path / "private" / "bot_keys.json").write_text(json.dumps(keys), encoding="utf-8")
    with pytest.raises(ValueError, match="berserk"):
        run_bots(tmp_path, "http://127.0.0.1:1")


def test_run_bots_stops_on_event_without_server(tmp_path):
    (tmp_path / "private").mkdir()
    keys = {"C": {"key": "dah_x_C_1", "bot": "rush"}, "D": {"key": "dah_x_D_1", "bot": "passive"}}
    (tmp_path / "private" / "bot_keys.json").write_text(json.dumps(keys), encoding="utf-8")
    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    run_bots(tmp_path, "http://127.0.0.1:1", stop)  # connection refused -> retry loop -> stop
    timer.join()
