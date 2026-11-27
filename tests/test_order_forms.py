"""Measurement m3: orders whose nested `units` object gets lost by an MCP client.

Accepted on input (normalized to the canonical nested form before validation and storage):
flat move {"unit", "count"}, `units` as a list of pairs or a JSON string, unit labels, `orders` as one JSON string
(engine, HTTP API, bridge), and P2 amend patches in the same forms. An empty `units` explains both workarounds.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from dah.bridge.client import GameClient
from dah.bridge.tools import TOOLS, Bridge
from dah.engine import SeatOrders, amend_order, build_ruleset, new_game_state, orders_hash, rules_text, submit_orders
from dah.engine.model import P2
from dah.game import Game, replay
from slice_helpers import slice_config
from dah.server import create_app
from dah.store import init_game_dir

ACT = "actions"
NESTED = {"kind": "move", "from": "P01", "to": "P02", "units": {"core:assault": 2}}
FLAT = {"kind": "move", "from": "P01", "to": "P02", "unit": "assault", "count": 2}
SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"}]


@pytest.fixture(scope="module")
def rs():
    return build_ruleset(None, "slice2")


@pytest.fixture
def st(rs):
    state = new_game_state(rs, [{"seat": "A", "nick": "Аня"}, {"seat": "B", "nick": "Боб"}], 1)
    for pid, prov in state.provinces.items():
        prov.owner = "A" if pid in ("P01", "P02", "P03", "P04") else "B" if pid in ("P05", "P06", "P07", "P08") else None
        prov.units, prov.buildings = {}, []
    state.provinces["P01"].units = {"core:assault": 3, "core:armor": 1}
    return state


def submit(st, rs, items, phase=ACT, book=None):
    return submit_orders(st, rs, "A", book or SeatOrders(), items, phase)


def canonical(st, rs, item):
    res = submit(st, rs, [item])
    assert res.results[0].ok, res.results[0].message
    return res.book.orders[0].to_dict()


# ---------------------------------------------------------------- engine: accepted forms

@pytest.mark.parametrize("item", [
    FLAT,
    {**FLAT, "unit": "штурм"},                                         # state label
    {**FLAT, "unit": "Штурмовики"},                                    # unit name
    {**FLAT, "unit": "core:assault", "count": "2"},                    # full id, count as a string
    {**FLAT, "units": {}},                                             # flat wins over the lost nested object
    {**FLAT, "units": {"assault": 2}},                                 # both, agreeing
    {"kind": "move", "from": "P01", "to": "P02", "units": [{"unit": "assault", "count": 2}]},
    {"kind": "move", "from": "P01", "to": "P02", "units": [["assault", 1], ["assault", 1]]},
    {"kind": "move", "from": "P01", "to": "P02", "units": '{"assault": 2}'},
    json.dumps(FLAT),                                                  # the order itself as a JSON string
    json.dumps(NESTED),
    {**NESTED, "kind": "Move"},
])
def test_every_form_normalizes_to_the_nested_order(st, rs, item):
    assert canonical(st, rs, item) == canonical(st, rs, NESTED)
    assert canonical(st, rs, item)["units"] == {"core:assault": 2}


def test_nested_with_several_unit_types_still_works(st, rs):
    order = canonical(st, rs, {"kind": "move", "from": "P01", "to": "P02", "units": {"assault": 2, "armor": 1}})
    assert order["units"] == {"core:armor": 1, "core:assault": 2}


def test_flat_and_nested_give_the_same_orders_hash(st, rs):
    assert orders_hash(submit(st, rs, [FLAT]).book) == orders_hash(submit(st, rs, [NESTED]).book)


@pytest.mark.parametrize("item, needle", [
    ({**FLAT, "units": {"assault": 1}}, "расходятся"),
    ({"kind": "move", "from": "P01", "to": "P02", "count": 2}, "плоская форма"),
    ({**FLAT, "count": 0}, "count"),
    ({**FLAT, "unit": "дракон"}, "неизвестный юнит"),
])
def test_bad_flat_forms_are_explained(st, rs, item, needle):
    check = submit(st, rs, [item]).results[0]
    assert not check.ok and needle in check.message


# ---------------------------------------------------------------- engine: empty units

@pytest.mark.parametrize("units, shown", [({}, "units={}"), ([], "units=[]"), ("", 'units=""'), (None, "не задано")])
def test_empty_units_message_names_what_came_and_both_workarounds(st, rs, units, shown):
    item = {"kind": "move", "from": "P01", "to": "P02"}
    if units is not None:
        item["units"] = units
    check = submit(st, rs, [item]).results[0]
    assert check.reason == "INVALID"
    assert shown in check.message
    assert '"unit":"assault","count":2' in check.message              # the flat form
    assert "JSON-строкой" in check.message and 'orders="[{' in check.message


# ---------------------------------------------------------------- engine: P2 amend

def test_p2_amend_with_a_flat_patch(st, rs):
    book = submit(st, rs, [NESTED]).book
    oid = book.orders[0].id
    for patch in ({"unit": "assault", "count": 1}, '{"unit": "assault", "count": 1}',
                  {"units": [{"unit": "штурм", "count": 1}]}, '{"units": {"assault": 1}}'):
        res = amend_order(st, rs, "A", book, oid, patch, P2)
        assert res.ok and res.cp_cost == 1, (patch, res.message)
        assert res.order.units == {"core:assault": 1} and not res.order.hasty
    more = amend_order(st, rs, "A", book, oid, {"unit": "assault", "count": 3, "to": "P03"}, P2)
    assert more.ok and more.cp_cost == 2 and more.order.hasty and more.order.dst == "P03"


def test_amend_empty_units_explains_patch_workarounds(st, rs):
    book = submit(st, rs, [NESTED]).book
    res = amend_order(st, rs, "A", book, book.orders[0].id, {"units": {}}, P2)
    assert res.reason == "INVALID" and "units={}" in res.message
    assert 'patch={"unit":"assault","count":1}' in res.message and "JSON-строкой" in res.message
    recruit = submit(st, rs, [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 1}])
    assert recruit.results[0].reason == "NO_RECRUIT_BUILDING"                  # P01 has no barracks here
    fort = submit(st, rs, [{"kind": "fortify", "province": "P03"}]).book
    assert amend_order(st, rs, "A", fort, fort.orders[0].id, {"unit": "assault", "count": 1}, P2).reason == "INVALID"


# ---------------------------------------------------------------- HTTP API

class Env:
    def __init__(self, tmp_path) -> None:
        config = slice_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7)
        self.gdir, keys = init_game_dir(tmp_path, config)
        self.now = 1_700_000_000.0
        Game.create(self.gdir, lambda: self.now)
        self.client = TestClient(create_app(str(tmp_path), admin_key="adm", clock=lambda: self.now,
                                            tick_interval=None))
        self.headers = {seat: {"Authorization": f"Bearer {key}"} for seat, key in keys.items()}

    def ok(self, seat: str, method: str, path: str, **kwargs) -> dict:
        data = self.client.request(method, path, headers=self.headers[seat], **kwargs).json()
        assert data["ok"], data
        return data

    def to_actions(self) -> None:
        for seat in ("A", "B"):
            self.ok(seat, "POST", "/v1/ready", json={"confirm_hash": self.ok(seat, "GET", "/v1/orders")["orders_hash"]})


# seed 7 on slice2: A holds P05 (3 assault) next to P06
HTTP_NESTED = {"kind": "move", "from": "P05", "to": "P06", "units": {"assault": 2}}
HTTP_FLAT = {"kind": "move", "from": "P05", "to": "P06", "unit": "штурм", "count": 2}


def test_http_orders_as_json_string_and_flat(tmp_path):
    env = Env(tmp_path)
    env.to_actions()
    val = env.ok("A", "POST", "/v1/orders/validate", json={"orders": json.dumps([HTTP_FLAT])})
    assert val["results"] == [{"ok": True, "cp_cost": 0}]
    sub = env.ok("A", "POST", "/v1/orders", json={"orders": json.dumps([HTTP_NESTED]), "mode": "replace_all"})
    def body(order: dict) -> dict:
        return {k: v for k, v in order.items() if k != "id"}      # ids are new on every submit

    nested = body(sub["accepted"][0])
    assert nested["units"] == {"core:assault": 2}
    flat = env.ok("A", "POST", "/v1/orders", json={"orders": [HTTP_FLAT], "mode": "replace_all"})
    assert body(flat["accepted"][0]) == nested
    one = env.ok("A", "POST", "/v1/orders", json={"orders": HTTP_FLAT, "mode": "replace_all"})   # a lone object
    assert body(one["accepted"][0]) == nested
    bad = env.ok("A", "POST", "/v1/orders/validate",
                 json={"orders": [{"kind": "move", "from": "P05", "to": "P06", "units": {}}]})
    msg = bad["results"][0]["message"]
    assert "units={}" in msg and '"unit":"assault"' in msg and "JSON-строкой" in msg
    err = env.client.post("/v1/orders", headers=env.headers["A"], json={"orders": "не json"}).json()
    assert err["error"]["code"] == "INVALID" and "JSON-строкой" in err["error"]["message"]


def test_http_p2_amend_flat_and_string_patch(tmp_path):
    env = Env(tmp_path)
    env.to_actions()
    oid = env.ok("A", "POST", "/v1/orders", json={"orders": [HTTP_FLAT]})["accepted"][0]["id"]
    env.to_actions()
    assert env.ok("A", "GET", "/v1/phase")["phase"] == "negotiation_2"
    amended = env.ok("A", "PATCH", f"/v1/orders/{oid}", json={"unit": "assault", "count": 1})
    assert amended["cp_cost"] == 1 and amended["order"]["units"] == {"core:assault": 1}
    again = env.ok("A", "PATCH", f"/v1/orders/{oid}", json={"patch": '{"to": "P07"}'})
    assert again["order"]["to"] == "P07" and again["order"]["phase_added"] == P2


# ---------------------------------------------------------------- bridge

def _bridge():
    sent: list[dict] = []

    def server(request: httpx.Request) -> httpx.Response:
        sent.append({"path": request.url.path, "body": json.loads(request.content) if request.content else None})
        return httpx.Response(200, json={"ok": True, "text": "ок"})

    return Bridge(GameClient("http://game.test", "dah_k", transport=httpx.MockTransport(server))), sent


def test_bridge_passes_json_string_orders_and_patches_as_objects():
    bridge, sent = _bridge()
    for tool in ("validate_orders", "submit_orders"):
        text, is_error = bridge.call(tool, {"orders": json.dumps([FLAT])})
        assert not is_error, text
        assert sent[-1]["body"]["orders"] == [FLAT]
    assert not bridge.call("submit_orders", {"orders": FLAT})[1]           # a lone order object
    assert sent[-1]["body"]["orders"] == [FLAT]
    assert not bridge.call("amend_order", {"id": "o1", "patch": '{"unit": "assault", "count": 1}'})[1]
    assert sent[-1] == {"path": "/v1/orders/o1", "body": {"unit": "assault", "count": 1}}


def test_bridge_schemas_and_descriptions_show_the_flat_form_first():
    tools = {t.name: t for t in TOOLS}
    for name in ("validate_orders", "submit_orders"):
        spec = tools[name]
        orders = spec.schema["properties"]["orders"]
        assert orders["type"] == "array" and "unit" in orders["items"]["properties"]
        assert orders["description"].index('"unit": "assault"') < orders["description"].index('"units"')
        assert "JSON-строкой" in orders["description"] and "JSON-строкой" in spec.description
    patch = tools["amend_order"].schema["properties"]["patch"]
    assert {"unit", "count", "units", "to"} <= set(patch["properties"])
    assert '"unit": "assault"' in tools["amend_order"].description


def test_bridge_end_to_end_against_the_real_server(tmp_path):
    env = Env(tmp_path)
    env.to_actions()

    def forward(request: httpx.Request) -> httpx.Response:
        resp = env.client.request(request.method, request.url.path, params=dict(request.url.params),
                                  content=request.content, headers={**env.headers["A"],
                                                                    "Content-Type": "application/json"})
        return httpx.Response(resp.status_code, content=resp.content, headers={"Content-Type": "application/json"})

    bridge = Bridge(GameClient("http://game.test", "dah_k", transport=httpx.MockTransport(forward)))
    text, is_error = bridge.call("submit_orders", {"orders": json.dumps([HTTP_FLAT])})
    assert not is_error and '"core:assault":2' in text
    text, is_error = bridge.call("validate_orders", {"orders": [{"kind": "move", "from": "P05", "to": "P06",
                                                                 "units": {}}]})
    assert "units={}" in text and "JSON-строкой" in text


# ---------------------------------------------------------------- journal / replay

def _play(tmp_path, items) -> tuple[Game, object]:
    config = slice_config(game_id="g1", scenario="core:slice2", seats=SEATS, seed=7)
    gdir, _ = init_game_dir(tmp_path, config)
    clock = {"now": 1_700_000_000.0}
    game = Game.create(gdir, lambda: clock["now"])
    while not game.game_over:
        if game.phase == ACT and game.state.turn == 1:
            sub = game.submit("A", items)
            assert not sub["rejected"], sub
        clock["now"] += 3600
        game.tick()
    return game, gdir


def test_flat_orders_are_journaled_and_replayed_like_nested(tmp_path):
    _, gdir_flat = _play(tmp_path / "flat", json.dumps([HTTP_FLAT]))
    _, gdir_nested = _play(tmp_path / "nested", [HTTP_NESTED])
    saved_flat, saved_nested = gdir_flat.load_turn("orders", 1), gdir_nested.load_turn("orders", 1)
    assert saved_flat == saved_nested and saved_flat["A"]["orders"][0]["units"] == {"core:assault": 2}
    assert replay(gdir_flat).ok and replay(gdir_nested).ok


# ---------------------------------------------------------------- rules text

@pytest.mark.parametrize("scenario", ["slice2", "slice3"])
def test_rules_text_shows_flat_form_and_stays_short(scenario):
    text = rules_text(build_ruleset(None, scenario))
    assert len(text) <= 6000
    assert '"unit":"assault","count":2' in text and "JSON-строкой" in text
    assert text.index('"unit":"assault","count":2') < text.index('"units":{')


# ---------------------------------------------------------------- review fixes: null-filling clients, edge inputs

NULLS = {"unit": None, "count": None, "province": None, "building": None}


@pytest.mark.parametrize("item", [
    {**NESTED, **NULLS},                                               # every schema property filled with null
    {**NESTED, **NULLS, "units": {"assault": 2}},
    {**NESTED, "count": 2},                                            # nested units plus a stray count
    {**NESTED, "count": 5},                                            # stray count is ignored, not compared
    {**FLAT, "units": {"assault": 0}},                                 # flat wins: nested keys kept, values lost
    {**FLAT, "units": {"assault": 0, "armor": "0"}},
    {**FLAT, "units": [["assault", 0]]},
])
def test_null_filled_and_lossy_moves_are_accepted(st, rs, item):
    assert canonical(st, rs, item)["units"] == {"core:assault": 2}


@pytest.mark.parametrize("item, needle", [
    ({**FLAT, "count": "²"}, "count"),                                 # non-ASCII digit: INVALID, not ValueError
    ({**FLAT, "count": "①"}, "count"),
    ({**FLAT, "count": 1e20}, "count"),                                # huge float: refused as before
    ({**FLAT, "count": "99999999999"}, "count"),
    ({"kind": "move", "from": "P01", "to": "P02", "units": [["assault", -1], ["assault", 3]]}, "≥ 0"),
    ({"kind": "move", "from": "P01", "to": "P02", "units": [["assault", "x"]]}, "≥ 0"),
    ({**FLAT, "units": {"assault": 1, "armor": 0}}, "расходятся"),     # a positive nested part must still agree
    ({"kind": "move", "from": "P01", "to": "P02", "count": 2, "unit": None, "units": None}, "плоская форма"),
])
def test_edge_inputs_are_rejected_as_invalid(st, rs, item, needle):
    check = submit(st, rs, [item]).results[0]
    assert not check.ok and check.reason == "INVALID" and needle in check.message, check.message


def test_recruit_count_with_unicode_digit_is_invalid(st, rs):
    check = submit(st, rs, [{"kind": "recruit", "province": "P01", "unit": "assault", "count": "²"}]).results[0]
    assert check.reason == "INVALID" and "count" in check.message


def test_p2_amend_with_a_null_filled_patch(st, rs):
    book = submit(st, rs, [NESTED]).book
    oid = book.orders[0].id
    for patch in ({"to": "P03", "unit": None, "count": None, "units": None}, {"to": "P03", "units": None},
                  '{"to": "P03", "unit": null, "count": null, "units": null}'):
        res = amend_order(st, rs, "A", book, oid, patch, P2)
        assert res.ok, (patch, res.message)
        assert res.order.dst == "P03" and res.order.units == {"core:assault": 2} and res.order.hasty
    fewer = amend_order(st, rs, "A", book, oid, {"to": None, "unit": None, "count": None, "units": {"assault": 1}}, P2)
    assert fewer.ok and fewer.order.dst == "P02" and fewer.order.units == {"core:assault": 1} and fewer.cp_cost == 1
    stray = amend_order(st, rs, "A", book, oid, {"units": {"assault": 1}, "count": 1}, P2)
    assert stray.ok and stray.order.units == {"core:assault": 1}
    empty = amend_order(st, rs, "A", book, oid, {"to": None, "units": None}, P2)
    assert not empty.ok and empty.reason == "INVALID"


def test_bridge_orders_errors_show_both_workarounds():
    bridge, sent = _bridge()
    for args in ({"orders": {"from": "P01", "to": "P02"}}, {"orders": "не json"}, {"orders": 5}):
        text, is_error = bridge.call("submit_orders", args)
        assert is_error and '"unit": "assault"' in text and 'orders="[{' in text, text
    text, is_error = bridge.call("amend_order", {"id": "o1", "patch": [1]})
    assert is_error and 'patch={"unit": "assault"' in text
    assert sent == []
