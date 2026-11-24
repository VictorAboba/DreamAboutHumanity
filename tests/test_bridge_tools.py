import json
from typing import Any

import anyio
import httpx
import pytest
from mcp import Client

from dah.bridge.client import GameClient
from dah.bridge.main import build_server
from dah.bridge.tools import TOOLS, Bridge

KEY = "dah_g1_A_s3cr3tvalue"


class FakeServer:
    """Records requests; answers with `reply(request)` or a default `{ok, text}`."""

    def __init__(self, reply=None):
        self.requests: list[httpx.Request] = []
        self.reply = reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.reply is not None:
            return self.reply(request)
        return httpx.Response(200, json={"ok": True, "text": f"ответ {request.url.path}"})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_bridge(reply=None) -> tuple[Bridge, FakeServer]:
    server = FakeServer(reply)
    return Bridge(GameClient("http://game.test", KEY, transport=httpx.MockTransport(server))), server


def body(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


ORDER = {"kind": "move", "from": "P01", "to": "P02", "units": {"core:assault": 2}}

MAPPING = [
    ("whoami", {}, "GET", "/v1/whoami", {}, None),
    ("rules", {"topic": "бой"}, "GET", "/v1/rules", {"topic": "бой"}, None),
    ("rules", {}, "GET", "/v1/rules", {}, None),
    ("phase", {}, "GET", "/v1/phase", {}, None),
    ("get_state", {"since_turn": 2, "format": "text"}, "GET", "/v1/state", {"since_turn": "2", "format": "text"},
     None),
    ("list_orders", {}, "GET", "/v1/orders", {}, None),
    ("validate_orders", {"orders": [ORDER]}, "POST", "/v1/orders/validate", {}, {"orders": [ORDER]}),
    ("submit_orders", {"orders": [ORDER], "mode": "append"}, "POST", "/v1/orders", {},
     {"orders": [ORDER], "mode": "append"}),
    ("submit_orders", {"orders": [ORDER]}, "POST", "/v1/orders", {}, {"orders": [ORDER], "mode": "replace_all"}),
    ("submit_orders", {"orders": json.dumps([ORDER])}, "POST", "/v1/orders", {},
     {"orders": [ORDER], "mode": "replace_all"}),
    ("cancel_order", {"id": "o7"}, "DELETE", "/v1/orders/o7", {}, None),
    ("amend_order", {"id": 7, "patch": {"to": "P03"}}, "PATCH", "/v1/orders/7", {}, {"to": "P03"}),
    ("estimate", {"attacker": {"core:armor": 2}, "defender": {"core:assault": 3}, "province_type": "core:forge"},
     "POST", "/v1/estimate", {},
     {"attacker": {"core:armor": 2}, "defender": {"core:assault": 3}, "province_type": "core:forge",
      "fortified": False, "hasty": False}),
    ("send_message", {"to": ["Хор"], "text": "мир?"}, "POST", "/v1/messages", {}, {"to": ["Хор"], "text": "мир?"}),
    ("send_message", {"to": "all", "text": "привет"}, "POST", "/v1/messages", {}, {"to": "all", "text": "привет"}),
    ("inbox", {"since": 4}, "GET", "/v1/inbox", {"since": "4"}, None),
    ("inbox", {}, "GET", "/v1/inbox", {}, None),
    ("send_gift", {"to": "Хор", "resources": {"alloy": 5}}, "POST", "/v1/gifts", {},
     {"to": "Хор", "resources": {"alloy": 5}}),
    ("send_gift", {"to": "Хор", "resources": '{"alloy": 5}'}, "POST", "/v1/gifts", {},
     {"to": "Хор", "resources": {"alloy": 5}}),
    ("list_pacts", {}, "GET", "/v1/pacts", {}, None),
    ("propose_pact", {"to": "Хор", "turns": 3}, "POST", "/v1/pacts", {}, {"to": "Хор", "turns": 3}),
    ("propose_pact", {"to": "Хор"}, "POST", "/v1/pacts", {}, {"to": "Хор", "turns": None}),
    ("respond_pact", {"id": "pact-A-B-1", "decision": "accept"}, "POST", "/v1/pacts/pact-A-B-1/respond", {},
     {"decision": "accept"}),
    ("leave_pact", {"id": "pact-A-B-1"}, "POST", "/v1/pacts/pact-A-B-1/leave", {}, {}),
    ("ready", {"confirm_hash": "abc"}, "POST", "/v1/ready", {}, {"confirm_hash": "abc"}),
    ("unready", {}, "POST", "/v1/unready", {}, {}),
    ("history", {"turn": 2}, "GET", "/v1/history", {"turn": "2"}, None),
    ("history", {}, "GET", "/v1/history", {}, None),
    # stage 2 (TECH_AND_SETUP.md section 4)
    ("ready", {}, "POST", "/v1/ready", {}, {"confirm_hash": None}),
    ("estimate", {"attacker": {"militia": 2}, "defender": {"militia": 1}, "province_type": "forge",
                  "side": "defender", "opponent": "Хор"}, "POST", "/v1/estimate", {},
     {"attacker": {"militia": 2}, "defender": {"militia": 1}, "province_type": "forge", "fortified": False,
      "hasty": False, "side": "defender", "opponent": "Хор"}),
    ("get_setup", {}, "GET", "/v1/setup", {}, None),
    ("choose_faction", {"prefs": ["iron", "ash"]}, "POST", "/v1/setup/faction", {}, {"prefs": ["iron", "ash"]}),
    ("choose_faction", {"prefs": '["iron", "ash"]'}, "POST", "/v1/setup/faction", {}, {"prefs": ["iron", "ash"]}),
    ("choose_faction", {"faction": "iron"}, "POST", "/v1/setup/faction", {}, {"prefs": "iron"}),
    ("choose_artifacts", {"ids": ["ember"]}, "POST", "/v1/setup/artifacts", {}, {"ids": ["ember"]}),
    ("choose_artifacts", {"artifacts": "ember"}, "POST", "/v1/setup/artifacts", {}, {"ids": "ember"}),
    ("list_techs", {}, "GET", "/v1/techs", {}, None),
    ("describe", {"id": "knight"}, "GET", "/v1/describe", {"id": "knight"}, None),
    ("describe", {"name": "Рыцарь"}, "GET", "/v1/describe", {"id": "Рыцарь"}, None),
    ("submit_orders", {"orders": {"kind": "research", "tech": "smithing"}}, "POST", "/v1/orders", {},
     {"orders": [{"kind": "research", "tech": "smithing"}], "mode": "replace_all"}),
    ("amend_order", {"id": "o2", "patch": '{"tech": "drills"}'}, "PATCH", "/v1/orders/o2", {}, {"tech": "drills"}),
    # stage 3 (SPY_AND_CONQUEST.md 6.1)
    ("list_spies", {}, "GET", "/v1/spies", {}, None),
    ("get_intel", {}, "GET", "/v1/intel", {}, None),
    ("get_intel", {"turn": 3, "shade": "s1", "report_id": "r4"}, "GET", "/v1/intel",
     {"turn": "3", "shade": "s1", "report": "r4"}, None),
    ("estimate_mission", {"mission": "observe", "target": "P07", "rounds": 2}, "POST", "/v1/estimate/mission", {},
     {"mission": "observe", "target": "P07", "rounds": 2}),
]


@pytest.mark.parametrize("tool,args,method,path,query,expected_body", MAPPING)
def test_tool_maps_to_http(tool, args, method, path, query, expected_body):
    bridge, server = make_bridge()
    text, is_error = bridge.call(tool, args)
    assert not is_error, text
    assert text == f"ответ {path}"
    request = server.last
    assert request.method == method
    assert request.url.path == path
    assert dict(request.url.params) == query
    assert body(request) == expected_body
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_every_tool_is_mapped():
    assert {t.name for t in TOOLS} == {m[0] for m in MAPPING} | {"wait_phase"}


def test_json_answer_without_text_is_compact_json():
    bridge, _ = make_bridge(lambda r: httpx.Response(200, json={"ok": True, "orders": [], "orders_hash": "h1",
                                                                  "cp_left": 3}))
    text, is_error = bridge.call("list_orders", {})
    assert not is_error
    assert json.loads(text) == {"orders": [], "orders_hash": "h1", "cp_left": 3}
    assert " " not in text


def test_error_answer_is_tool_error():
    bridge, _ = make_bridge(lambda r: httpx.Response(409, json={
        "ok": False, "error": {"code": "STALE_HASH", "message": "приказы изменились"}}))
    text, is_error = bridge.call("ready", {"confirm_hash": "old"})
    assert is_error
    assert text == "Ошибка STALE_HASH: приказы изменились"


def test_bad_arguments_never_reach_server():
    bridge, server = make_bridge()
    for tool, args in [("submit_orders", {"orders": "не json"}), ("submit_orders", {"orders": {"a": 1}}),
                       ("cancel_order", {}), ("nope", {})]:
        text, is_error = bridge.call(tool, args)
        assert is_error, (tool, text)
    assert server.requests == []


def test_key_never_in_outputs():
    def leaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/whoami":
            return httpx.Response(200, json={"ok": True, "text": f"ключ {KEY}", "key": KEY})
        return httpx.Response(401, json={"ok": False, "error": {"code": "unauthorized", "message": f"bad {KEY}"}})

    bridge, _ = make_bridge(leaky)
    for tool in ("whoami", "phase"):
        text, _ = bridge.call(tool, {})
        assert KEY not in text
        assert "***" in text


def test_network_error_hides_key():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect with {KEY}")

    bridge, _ = make_bridge(boom)
    text, is_error = bridge.call("whoami", {})
    assert is_error
    assert KEY not in text


def test_wait_phase_remembers_phase_seq():
    seqs = iter([3, 3, 4, 5])

    def reply(request: httpx.Request) -> httpx.Response:
        seq = next(seqs)
        return httpx.Response(200, json={"ok": True, "changed": request.url.path == "/v1/wait_phase",
                                         "turn": 1, "phase": "actions", "phase_seq": seq})

    bridge, server = make_bridge(reply)
    text, is_error = bridge.call("wait_phase", {})
    assert not is_error
    assert json.loads(text)["changed"] is True
    assert server.last.url.path == "/v1/phase"

    bridge.call("wait_phase", {})
    assert server.last.url.path == "/v1/wait_phase"
    assert dict(server.last.url.params) == {"after_seq": "3", "max_seconds": "45"}

    bridge.call("wait_phase", {"max_seconds": 10})
    assert dict(server.last.url.params) == {"after_seq": "3", "max_seconds": "10"}
    assert bridge.last_phase_seq == 4

    bridge.call("wait_phase", {"max_seconds": 999})
    assert dict(server.last.url.params) == {"after_seq": "4", "max_seconds": "55"}


def test_phase_tool_also_updates_seq():
    bridge, server = make_bridge(lambda r: httpx.Response(200, json={"ok": True, "phase_seq": 9}))
    bridge.call("phase", {})
    bridge.call("wait_phase", {})
    assert server.last.url.params["after_seq"] == "9"


def test_mcp_server_lists_all_tools_and_calls_them():
    bridge, server = make_bridge()

    async def scenario() -> None:
        async with Client(build_server(bridge)) as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == [t.name for t in TOOLS]
            for tool in listed.tools:
                assert tool.description
                assert tool.input_schema["type"] == "object"
            submit = next(t for t in listed.tools if t.name == "submit_orders")
            assert submit.input_schema["properties"]["orders"]["type"] == "array"
            result = await client.call_tool("whoami", {})
            assert not result.is_error
            assert result.content[0].text == "ответ /v1/whoami"

    anyio.run(scenario)
    assert server.last.url.path == "/v1/whoami"
