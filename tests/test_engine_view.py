import json

import pytest

from dah.engine import (DeadlineInfo, SeatOrders, build_ruleset, issue_order_id, new_game_state, render_report_text,
                        render_state_text, resolve, rules_text, submit_orders, view_state, visible_provinces)

AS = "core:assault"


@pytest.fixture(scope="module")
def rs():
    return build_ruleset(None, "slice3", scoring={"territory": 1})     # the slice score; core 0.5.0 view: elsewhere


@pytest.fixture
def st(rs):
    state = new_game_state(rs, [{"seat": "A", "nick": "Нико"}, {"seat": "B", "nick": "Соло"},
                                {"seat": "C", "nick": "Хор#2"}], 3)
    owners = {**dict.fromkeys(["P01", "P02", "P03", "P04"], "A"), **dict.fromkeys(["P05", "P06", "P07", "P08"], "B"),
              **dict.fromkeys(["P09", "P10", "P11", "P12"], "C")}
    for pid, prov in state.provinces.items():
        prov.owner, prov.units, prov.buildings = owners.get(pid), {}, []
    for cap, seat in (("P01", "A"), ("P05", "B"), ("P09", "C")):
        state.provinces[cap].units = {AS: 3}
        state.provinces[cap].buildings = ["core:barracks"]
    state.players["B"].resources["core:alloy"] = 777
    return state


def test_visibility_set(st, rs):
    seen = visible_provinces(st, rs, "A")
    assert seen == {"P01", "P02", "P03", "P04", "P07", "P10", "P13", "P14", "P15"}


def test_fog_in_json_view(st, rs):
    view = view_state(st, rs, "A")
    text = json.dumps(view, ensure_ascii=False)
    assert "777" not in text                                   # foreign resources never shown
    assert set(view["you"]) >= {"resources", "net_income", "command_points"}
    by_id = {p["id"]: p for p in view["provinces"]}
    assert "units" not in by_id["P05"] and "buildings" not in by_id["P05"]     # far: owner and type only
    assert by_id["P05"]["owner"] == "B" and by_id["P05"]["type"] == "core:capital"
    assert by_id["P07"]["units"] == {} and by_id["P01"]["units"] == {AS: 3}
    assert all("orders" not in p for p in view["provinces"])


def test_state_text_full(st, rs):
    book = submit_orders(st, rs, "A", SeatOrders(), [{"kind": "build", "province": "P02", "building": "mine"}],
                         "actions", id_factory=lambda: issue_order_id(st, "A")).book
    st.phase = "actions"
    text = render_state_text(st, rs, "A", DeadlineInfo("14:30Z", 12 * 60), book=book)
    lines = text.splitlines()
    assert lines[0] == "ХОД 1/10 | Д ДЕЙСТВИЯ | до 14:30Z (ещё 12 мин) | ОК 3"
    assert lines[1] == "Казна: Сплав 30 (+8) Кредо 0 (+3) Провиант 15 (+4)"   # start 30/15/0, food 2+4+1 - 3 upkeep
    assert "P01 Столица Аргос [ТЫ] столица штурм×3 | казарма | → P02 P03 P04" in lines
    assert "P05 Столица Тессар [Соло] столица ? | → P06 P07 P08" in lines
    assert "P07 Нивы Лира [Соло] агро пусто | → P02 P05" in lines
    assert "P13 Сердце Кузен [нейтр.] кузн. пусто | → P04 P08 P12 P14 P15" in lines
    assert "777" not in text and "mine" not in text
    assert len(text) < 2000


def test_state_text_since_turn(st, rs):
    st.provinces["P04"].units = {AS: 2}
    book = submit_orders(st, rs, "A", SeatOrders(), [{"kind": "move", "from": "P04", "to": "P13",
                                                     "units": {"assault": 2}}], "actions").book
    st.provinces["P10"].units = {AS: 1}
    c_book = submit_orders(st, rs, "C", SeatOrders(), [{"kind": "move", "from": "P10", "to": "P03",
                                                       "units": {"assault": 1}}], "actions").book
    new, rep = resolve(st, {"A": book, "C": c_book}, rs, 1)
    text = render_state_text(new, rs, "A", since_turn=1, since_state=st, reports=[rep])
    lines = text.splitlines()
    assert lines[0].startswith("ХОД 2/10 | П1 ПЕРЕГОВОРЫ")
    assert lines[1].startswith("Казна:")
    assert any(line.startswith("P13 ") and "[ТЫ]" in line for line in lines)
    assert not any(line.startswith("P05 ") or line.startswith("P01 ") for line in lines)   # unchanged / unseen
    changes = lines[-1]
    assert changes.startswith("ИЗМЕНЕНИЯ с хода 1: ")
    assert "+P13 твоя" in changes and "-P03 потеряна (Хор#2)" in changes and "голода нет" in changes
    report = render_report_text(rep, rs, new, "A")
    assert report.startswith("ОТЧЁТ ХОДА 1") and "P13" in report
    assert "Сплав +" in report                       # own economy line
    assert all(e["seats"] == ["B"] for e in rep.by_seat["B"] if e["kind"] == "economy")
    assert not any(e["kind"] == "economy" and e["seats"] == ["A"] for e in rep.by_seat["B"])


def test_game_over_text(st, rs):
    st.turn = rs.scenario.turn_limit
    new, _ = resolve(st, {}, rs, 1)
    text = render_state_text(new, rs, "A", DeadlineInfo("14:30Z", 30))
    assert "ИГРА ОКОНЧЕНА" in text and "ИТОГ: НИЧЬЯ" in text and "до 14:30Z" not in text


def test_rules_text(rs):
    text = rules_text(rs)
    assert len(text) <= 6000
    for needle in ("Штурмовики", "Бронеход", "Артиллерия", "Шахта", "Ферма", "Казарма", "П2", "PHASE_LOCKED",
                   '"kind":"move"', "2 ОК", "75%", "−15%", "estimate"):
        assert needle in text, needle
    assert len(rules_text(build_ruleset(None, "slice2"))) <= 6000


def test_every_event_kind_renders(st, rs):
    from dah.engine.view import event_text
    units = {AS: 2}
    events = [
        {"kind": "starvation", "losses": units}, {"kind": "retreat_lost", "province": "P01", "units": units},
        {"kind": "destroyed", "province": "P13", "units": units},
        {"kind": "order_rejected", "order": {"id": "o1"}, "reason": "NOT_ENOUGH_UNITS"},
        {"kind": "retreat", "province": "P01", "units": units, "from": "P02"},
        {"kind": "eliminated", "seat": "B"}, {"kind": "fortify_broken", "province": "P01"},
        {"kind": "captured", "province": "P13", "from": None, "to": "A"},
        {"kind": "edge_battle", "edge": ["P04", "P13"], "province": None,
         "sides": [{"owner": "A", "units": units, "losses": {}, "won": True, "strength": 1.0},
                   {"owner": "B", "units": units, "losses": units, "won": False, "strength": 0.5}]},
    ]
    texts = [event_text(e, rs, st, "A") for e in events]
    assert texts[0] == "голод: потеряно штурм×2"
    assert texts[5] == "выбыл Соло" and "к ТЫ" in texts[7]
    assert all(texts)
