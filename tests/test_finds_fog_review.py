"""Review fixes of stage 5 (core 0.6.0): the marks a find leaves follow ordinary visibility (ABANDONED_LANDS.md 10),
the texts of guards and guarded boons, the ruins' Treasury under `ruins_find`, the Observation status in Russian and
the topic's word on where finds lie."""

from __future__ import annotations

from dah.engine import conquest as cq
from dah.engine import discoveries as disc
from dah.engine import espionage as esp
from dah.engine import resolve
from dah.engine.rules import discoveries_section
from dah.engine.view import render_state_text, view_state, visible_provinces
from test_core060_finds import ALLOY, ASL, CREED, FOOD, MIL, _duel_on_ruins, _events, rs_of


def _entry(view, pid):
    return next(e for e in view["provinces"] if e["id"] == pid)


def _line(text, pid):
    return next(ln for ln in text.splitlines() if ln.startswith(f"{pid} "))


def _scout_into_p10(find: str, units: dict | None = None):
    rs, st, book = _duel_on_ruins({"P10": find})
    st.provinces["P03"].units = dict(units or {MIL: 1})
    order = {"kind": "move", "from": "P03", "to": "P10",
             "units": {u.split(":")[1]: n for u, n in (units or {MIL: 1}).items()}}
    new, rep = resolve(st, {"A": book("A", [order])}, rs, 1, salt=7)
    return rs, st, new, rep


def test_a_find_guard_shows_only_where_the_province_is_seen_the_ruins_guard_everywhere():
    rs, st, new, rep = _scout_into_p10("guarded_depot")
    assert "guard" in new.provinces["P10"].status and new.provinces["P10"].owner is None
    assert "P10" not in visible_provinces(new, rs, "B") and "P10" in visible_provinces(new, rs, "A")
    b = view_state(new, rs, "B")
    assert "guard" not in _entry(b, "P10")
    assert "[стража]" not in _line(render_state_text(new, rs, "B"), "P10")
    assert _entry(view_state(new, rs, "A"), "P10")["guard"] == {"lair": False}
    assert "[стража]" in _line(render_state_text(new, rs, "A"), "P10")
    # the ruins' garrison is the scenario's: public to everyone, seen or not
    assert "P09" not in visible_provinces(new, rs, "B")
    assert _entry(b, "P09")["guard"] == {"lair": False}
    assert "[стража]" in _line(render_state_text(new, rs, "B"), "P09")


def test_a_find_status_shows_only_where_the_province_is_seen():
    for find, key, tag in (("machine_trap", "ravaged_until", "[разорена]"),
                           ("blighted_soil", "ruined_until", "[пепелище до х")):
        rs, st, new, rep = _scout_into_p10(find, {MIL: 2})
        assert new.provinces["P10"].status.get(key) and "P10" not in visible_provinces(new, rs, "B")
        assert key not in (_entry(view_state(new, rs, "B"), "P10").get("status") or {})
        assert tag not in _line(render_state_text(new, rs, "B"), "P10")
        assert key in _entry(view_state(new, rs, "A"), "P10")["status"]
        assert tag in _line(render_state_text(new, rs, "A"), "P10")
        # the Abandoned multiplier (the souls setting, public) stays on the unseen line
        assert "[доход ×0.5]" in _line(render_state_text(new, rs, "B"), "P10")


def test_a_conquest_mark_replaces_a_find_note_and_stale_notes_are_dropped():
    rs, st, new, rep = _scout_into_p10("machine_trap", {MIL: 2})
    prov = new.provinces["P10"]
    assert prov.status[disc.FIND_MARKS] == {"ravaged_until": prov.status["ravaged_until"]}
    cq.drop_find_mark(prov, "ravaged_until")      # a conquest raid set it publicly
    assert disc.FIND_MARKS not in prov.status
    assert "ravaged_until" in _entry(view_state(new, rs, "B"), "P10")["status"]
    # unchanged: once the Ravage ends, step 7 forgets the note
    rs, st, new, rep = _scout_into_p10("machine_trap", {MIL: 2})
    state = new
    for turn in range(2, 6):
        state, _ = resolve(state, {}, rs, turn, salt=7)
    assert "ravaged_until" not in state.provinces["P10"].status
    assert disc.FIND_MARKS not in state.provinces["P10"].status


def test_guard_texts_name_the_ruins_only_for_the_ruins_and_a_guarded_boon_names_its_reward():
    rs, st, new, rep = _scout_into_p10("guarded_depot")
    found = _events(rep, "discovery_found")[0]
    text = disc.DISCOVERY_EVENT_TEXT["discovery_found"](found, rs, lambda s: "ТЫ" if s == "A" else s)
    assert "ничего" not in text and "стража: штурм×2" in text and "награда" in text and "+45" in text, text
    clear = disc.DISCOVERY_EVENT_TEXT["guard_cleared"]
    who = lambda s: "ТЫ"    # noqa: E731
    find_guard = {"province": "P10", "by": "A", "lair": False, "resources": {ALLOY: 45}}
    assert "стража находки уничтожена" in clear(find_guard, rs, who)
    assert "стража руин уничтожена" in clear({**find_guard, "province": "P09"}, rs, who)
    assert "логово зачищено" in clear({**find_guard, "lair": True}, rs, who)


def test_a_guarded_boon_in_the_ruins_keeps_the_treasury():
    rs, st, book = _duel_on_ruins({"P09": "guarded_depot"})
    treasury = list(st.provinces["P09"].status["guard"]["reward"])
    garrison = dict(st.provinces["P09"].units)
    st.provinces["P10"].owner = "A"
    st.provinces["P10"].units = {MIL: 1}
    new, rep = resolve(st, {"A": book("A", [{"kind": "move", "from": "P10", "to": "P09", "units": {"militia": 1}}])},
                       rs, 1, salt=7)
    assert _events(rep, "discovery_found")
    reward = new.provinces["P09"].status["guard"]["reward"]
    assert reward[:len(treasury)] == treasury and len(reward) > len(treasury)
    assert {(e["resource"], e["amount"]) for e in reward[len(treasury):]} == {(ALLOY, 45), (FOOD, 30)}
    assert new.provinces["P09"].units[ASL] == garrison[ASL] + 2
    assert CREED in {e["resource"] for e in reward}


def test_an_observation_shows_the_status_in_russian():
    rs = rs_of("slice3")
    _, st, _ = _duel_on_ruins({})
    text = esp._status_text(st, {"income_mul": 0.5, "ravaged_until": 3, "ruined_until": 4}, "A")
    assert text == "доход ×0.5, разорена до х3, пепелище до х4"
    assert "income_mul" not in text and rs
    report = {"id": 1, "shade": "s1", "target": "P10", "mission": "core:observe", "round": 1, "rounds": 1, "turn": 1,
              "behavior": esp.OBSERVE, "facts": {"province": "P10", "owner": None, "units": {},
                                                 "status": {"income_mul": 0.5}, "discovery": None}}
    line = esp.report_text(report, rs, st, "A")
    assert "статус: доход ×0.5" in line and "income_mul" not in line


def test_the_topic_says_where_finds_lie_when_every_province_holds_one():
    assert "в каждой Заброшенной провинции дальше кольца 1, кроме руин" in discoveries_section(rs_of("slice3")).detail
    sparse = rs_of("slice3", discoveries={"density": 0.66})
    assert "их число тайна" in discoveries_section(sparse).detail
