"""Fog of war, compact Russian state text (with diff since a turn), turn reports and rules text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import diplomacy as dip
from . import registry
from .content import Ruleset
from .model import GameState, SeatOrders
from .orders import cp_left as _cp_left
from .resolve import TurnReport, income_of, scores, upkeep_of, visible_provinces
from .techs import public_techs, stat_key

PHASE_LABELS = {
    "setup": "ФАЗА СТАРТА",
    "negotiation_1": "П1 ПЕРЕГОВОРЫ",
    "actions": "Д ДЕЙСТВИЯ",
    "negotiation_2": "П2 ПЕРЕГОВОРЫ",
    "resolution": "РАСЧЁТ",
    "game_over": "ИГРА ОКОНЧЕНА",
}


@dataclass(frozen=True)
class DeadlineInfo:
    deadline: str            # e.g. "14:30Z"
    seconds_left: int


# ---------------------------------------------------------------- JSON view

def view_state(state: GameState, ruleset: Ruleset, seat: str, book: SeatOrders | None = None) -> dict[str, Any]:
    """What `seat` may know: own resources, all owners (public map), units/buildings only near own land."""
    me = state.players[seat]
    seen = visible_provinces(state, ruleset, seat)
    key = stat_key(state, ruleset, seat)
    income, upkeep = income_of(state, ruleset, seat, key), upkeep_of(state, ruleset, seat, key)
    provinces = []
    for pid, p in sorted(state.provinces.items()):
        entry: dict[str, Any] = {"id": pid, "name": p.name, "type": p.type, "owner": p.owner,
                                 "neighbors": list(ruleset.map.adjacency[pid]), "visible": pid in seen}
        if pid in seen:
            entry.update(units=dict(p.units), buildings=list(p.buildings), fortified=p.fortified)
        if p.owner == seat:
            entry["building_now"] = [{"building": b.building, "turns_left": b.turns_left}
                                     for b in state.pending_builds if b.province == pid]
        provinces.append(entry)
    sc = scores(state, ruleset)
    you: dict[str, Any] = {"seat": seat, "nick": me.nick, "faction": me.faction, "alive": me.alive,
                           "resources": dict(me.resources),
                           "net_income": {r: income[r] - upkeep[r] for r in ruleset.resources},
                           "command_points": _cp_left(state, seat, book) if book else me.command_points}
    if ruleset.techs or ruleset.artifacts:
        # own techs in full (others: only the public count below); own artifacts (others never, until game end)
        you["techs"] = list(me.techs)
        you["researching"] = ({"tech": me.researching, "progress": me.research.get(me.researching, 0),
                               "cost": ruleset.tech_cost(me.researching, key)}
                              if me.researching in ruleset.techs else None)
        you["research"] = dict(me.research)
        you["artifacts"] = list(me.artifacts)
    players = []
    for s, p in state.players.items():
        entry: dict[str, Any] = {"seat": s, "nick": p.nick, "alive": p.alive, "score": sc[s], "faction": p.faction}
        if ruleset.techs:
            entry["techs_count"] = len(public_techs(ruleset, p))     # artifact grants stay hidden
        if state.phase == "game_over" and (ruleset.techs or ruleset.artifacts):
            entry["techs"] = list(p.techs)              # revealed after the game (TECH_AND_SETUP.md 1.4, 2)
            entry["artifacts"] = list(p.artifacts)
        players.append(entry)
    return {
        "turn": state.turn, "turn_limit": ruleset.scenario.turn_limit, "phase": state.phase,
        "you": you,
        "players": players,
        "provinces": provinces,
        "pacts": [{**dip.public_pact(pc), **({"id": pc["id"]} if seat in (pc["a"], pc["b"]) else {})}
                  for pc in dip.active_pacts(state)],
        "pact_proposals": [{"id": pc["id"], "from": pc["a"], "to": pc["b"], "turns": pc["turns"]}
                           for pc in state.pacts if pc["status"] == dip.PROPOSED and seat in (pc["a"], pc["b"])],
        "result": state.result,
    }


# ---------------------------------------------------------------- text

def _owner_tag(state: GameState, seat: str, owner: str | None) -> str:
    if owner is None:
        return "нейтр."
    return "ТЫ" if owner == seat else state.players[owner].nick


def _units_text(ruleset: Ruleset, units: Mapping[str, int]) -> str:
    return " ".join(f"{ruleset.short(u)}×{n}" for u, n in sorted(units.items()) if n) or "пусто"


def _province_line(ruleset: Ruleset, state: GameState, seat: str, entry: Mapping[str, Any]) -> str:
    head = f"{entry['id']} {entry['name']} [{_owner_tag(state, seat, entry['owner'])}] {ruleset.short(entry['type'])}"
    if not entry["visible"]:
        parts = [f"{head} ?"]
    else:
        parts = [f"{head} {_units_text(ruleset, entry['units'])}" + (" укр." if entry["fortified"] else "")]
        building = [ruleset.buildings[b].name.lower() for b in entry["buildings"]]
        building += [f"строится {ruleset.buildings[b['building']].name.lower()} ({b['turns_left']})"
                     for b in entry.get("building_now", [])]
        if building:
            parts.append(", ".join(building))
    parts.append("→ " + " ".join(entry["neighbors"]))
    return " | ".join(parts)


def _header(state: GameState, ruleset: Ruleset, view: Mapping[str, Any], deadline: DeadlineInfo | None) -> str:
    parts = [f"ХОД {state.turn}/{ruleset.scenario.turn_limit}", PHASE_LABELS.get(state.phase, state.phase)]
    if deadline and state.phase != "game_over":
        left = deadline.seconds_left
        rest = f"ещё {left} с" if left < 120 else f"ещё {left // 60} мин"
        parts.append(f"до {deadline.deadline} ({rest})")
    parts.append(f"ОК {view['you']['command_points']}")
    return " | ".join(parts)


def _treasury(ruleset: Ruleset, view: Mapping[str, Any]) -> str:
    you = view["you"]
    line = "Казна: " + " ".join(f"{ruleset.resources[r].name} {you['resources'].get(r, 0)} "
                                f"({you['net_income'][r]:+d})" for r in ruleset.resources)
    if ruleset.techs and ruleset.research_resource:
        active = you.get("researching")
        now = (f"исследуется «{ruleset.techs[active['tech']].name}» {active['progress']}/{active['cost']}"
               if active else "исследование не выбрано")
        line += f"\nНаука: изучено {len(you.get('techs', []))}; {now} (list_techs)"
    if you.get("artifacts"):
        line += "\nАртефакты: " + ", ".join(ruleset.artifacts[a].name for a in you["artifacts"]
                                             if a in ruleset.artifacts)
    return line


def _score_line(view: Mapping[str, Any], seat: str, ruleset: Ruleset) -> str:
    # Factions are public once the setup phase is over (TECH_AND_SETUP.md 2); in a one-faction game they say nothing.
    show = len(ruleset.scenario.factions) > 1 and view["phase"] != "setup"

    def tag(p: Mapping[str, Any]) -> str:
        parts = (["ты"] if p["seat"] == seat else []) + (
            [ruleset.factions[p["faction"]].name] if show and p.get("faction") in ruleset.factions else [])
        return f" ({', '.join(parts)})" if parts else ""

    items = [f"{p['nick']}{tag(p)} {p['score']}"
             + (f" тех.{p['techs_count']}" if "techs_count" in p else "") + ("" if p["alive"] else " выбыл")
             for p in view["players"]]
    return "Счёт: " + ", ".join(items)


def render_state_text(state: GameState, ruleset: Ruleset, seat: str, deadline_info: DeadlineInfo | None = None,
                      since_turn: int | None = None, since_state: GameState | None = None,
                      reports: Sequence[TurnReport] = (), book: SeatOrders | None = None) -> str:
    """Compact text for an LLM player.

    Full mode lists every province (hidden units shown as "?"). With ``since_turn`` and ``since_state``
    (the state at the start of that turn) only the treasury, changed own/visible provinces and an
    ИЗМЕНЕНИЯ section are given; ``reports`` are the turn reports from ``since_turn`` on. ``book``
    (the seat's current orders) makes the header show the CP left.
    """
    view = view_state(state, ruleset, seat, book)
    lines = [_header(state, ruleset, view, deadline_info), _treasury(ruleset, view)]
    if state.result:
        lines.append(_result_line(state, seat))
    entries = view["provinces"]
    pacts = _pacts_line(state, seat, view)
    if since_turn is not None and since_state is not None:
        old = {e["id"]: e for e in view_state(since_state, ruleset, seat)["provinces"]}
        entries = [e for e in entries if e["visible"] and _changed(e, old[e["id"]])]
        lines += [_province_line(ruleset, state, seat, e) for e in entries]
        lines += [pacts] if pacts else []
        lines.append(_changes_line(state, ruleset, seat, since_turn, since_state, reports))
    else:
        lines += [_province_line(ruleset, state, seat, e) for e in entries]
        lines.append(_score_line(view, seat, ruleset))
        lines += [pacts] if pacts else []
        if since_turn is not None:
            lines.append(_changes_line(state, ruleset, seat, since_turn, None, reports))
    return "\n".join(lines)


def _pact_terms(pact: Mapping[str, Any], seat: str, state: GameState) -> str:
    text = f"до хода {pact['until_turn']} вкл." if pact.get("until_turn") else "бессрочно"
    if pact.get("leaving"):
        text += (f", {_owner_tag(state, seat, pact['leaving'])} выходит: конец перед расчётом хода "
                 f"{pact['ends_before_turn']}")
    return text


def _pacts_line(state: GameState, seat: str, view: Mapping[str, Any]) -> str:
    """Active pacts (public: everyone sees who is allied with whom; ids only for one's own) and own proposals."""
    items = []
    for pc in view["pacts"]:
        sides = "—".join(_owner_tag(state, seat, s) for s in pc["seats"])
        items.append(f"{sides}{' [' + pc['id'] + ']' if pc.get('id') else ''} {_pact_terms(pc, seat, state)}")
    for pr in view["pact_proposals"]:
        terms = f"на {pr['turns']} х." if pr["turns"] else "бессрочно"
        if pr["to"] == seat:
            items.append(f"тебе предлагает {_owner_tag(state, seat, pr['from'])} [{pr['id']}] {terms} — "
                         "respond_pact до конца хода")
        else:
            items.append(f"ты предложил {_owner_tag(state, seat, pr['to'])} [{pr['id']}] {terms}, ждёт ответа")
    return "ПАКТЫ: " + "; ".join(items) if items else ""


def _changed(new: Mapping[str, Any], old: Mapping[str, Any]) -> bool:
    keys = ("owner", "visible", "units", "buildings", "fortified", "building_now")
    return any(new.get(k) != old.get(k) for k in keys)


def _result_line(state: GameState, seat: str) -> str:
    assert state.result is not None
    winners = state.result["winners"]
    names = ", ".join(state.players[w].nick for w in winners) or "никто"
    verdict = "ТЫ ПОБЕДИЛ" if winners == [seat] else ("НИЧЬЯ" if seat in winners else "поражение")
    return f"ИТОГ: {verdict}; победители: {names}"


def _changes_line(state: GameState, ruleset: Ruleset, seat: str, since_turn: int, since_state: GameState | None,
                  reports: Sequence[TurnReport]) -> str:
    items: list[str] = []
    if since_state is not None:
        for pid, prov in sorted(state.provinces.items()):
            was = since_state.provinces[pid].owner
            if prov.owner == was:
                continue
            if prov.owner == seat:
                items.append(f"+{pid} твоя")
            elif was == seat:
                items.append(f"-{pid} потеряна ({_owner_tag(state, seat, prov.owner)})")
            else:
                items.append(f"{pid} перешла к {_owner_tag(state, seat, prov.owner)}")
    starving = False
    for rep in reports:
        if rep.turn < since_turn:
            continue
        for e in rep.by_seat.get(seat, []):
            if since_state is None and e["kind"] == "captured":
                items.append(f"{e['province']} перешла к {_owner_tag(state, seat, e['to'])}")
            elif e["kind"] in ("battle", "edge_battle", "starvation", "build_done", "eliminated",
                               "retreat_lost", "order_rejected", "fortify_broken", "pact_bounce", "pact_joint",
                               "pact_broken", "pact_expired", "pact_ended", "pact_lapsed", "gift", "pact_signed",
                               "pact_leave", "tech_completed"):
                starving |= e["kind"] == "starvation"
                items.append(event_text(e, ruleset, state, seat))
    if not starving:
        items.append("голода нет")
    return f"ИЗМЕНЕНИЯ с хода {since_turn}: " + "; ".join(items)


# ---------------------------------------------------------------- report text

def event_text(e: Mapping[str, Any], ruleset: Ruleset, state: GameState, seat: str) -> str:
    def who(owner: str | None) -> str:
        return _owner_tag(state, seat, owner)

    kind = e["kind"]
    if kind in _DIPLOMACY_TEXT:
        return _DIPLOMACY_TEXT[kind](e, ruleset, who)
    if kind in ("battle", "edge_battle"):
        where = f"на ребре {'-'.join(e['edge'])}" if kind == "edge_battle" else f"в {e['province']}"
        sides = "; ".join(f"{who(s['owner'])}{' победа' if s['won'] else ''}: {_units_text(ruleset, s['units'])}"
                          f" потери {_units_text(ruleset, s['losses']) if s['losses'] else 'нет'}"
                          for s in e["sides"])
        return f"бой {where}{' (укр.)' if e.get('fortified') else ''}: {sides}"
    simple = {
        "captured": lambda: f"{e['province']} перешла от {who(e['from'])} к {who(e['to'])}",
        "retreat": lambda: f"отход в {e['province']}: {_units_text(ruleset, e['units'])}",
        "retreat_lost": lambda: f"отступать некуда из {e['province']}: погибли {_units_text(ruleset, e['units'])}",
        "destroyed": lambda: f"в {e['province']} уничтожены {_units_text(ruleset, e['units'])}",
        "recruited": lambda: f"нанято в {e['province']}: {ruleset.short(e['unit'])}×{e['count']}",
        "build_started": lambda: f"заложено {ruleset.buildings[e['building']].name.lower()} в {e['province']}",
        "build_done": lambda: f"построено {ruleset.buildings[e['building']].name.lower()} в {e['province']}",
        "fortify_broken": lambda: f"укрепление {e['province']} сорвано: войска ушли",
        "starvation": lambda: f"голод: потеряно {_units_text(ruleset, e['losses'])}",
        "eliminated": lambda: f"выбыл {who(e['seat'])}",
        "order_rejected": lambda: f"приказ {e['order']['id']} не выполнен: {e['reason']}",
        "economy": lambda: "доход " + " ".join(f"{ruleset.resources[r].name} +{e['income'][r]}/-{e['upkeep'][r]}"
                                               for r in ruleset.resources)
        + (f"; в исследование +{e['research']['added']} ({e['research']['progress']}/{e['research']['cost']})"
           if e.get("research") else ""),
        "research_started": lambda: f"начато исследование «{_tech_name(ruleset, e['tech'])}»",
        "tech_completed": lambda: f"изучена технология «{_tech_name(ruleset, e['tech'])}»",
    }
    return simple[kind]()


def _tech_name(ruleset: Ruleset, tech: str) -> str:
    return ruleset.techs[tech].name if tech in ruleset.techs else tech


def _pair(e: Mapping[str, Any], who: Any) -> str:
    return " и ".join(who(s) for s in e["seats"])


def _gift_text(e: Mapping[str, Any], ruleset: Ruleset, who: Any) -> str:
    amounts = ", ".join(f"{ruleset.resources[r].name if r in ruleset.resources else r} {n}"
                        for r, n in (e.get("resources") or {}).items())
    return f"подношение: {who(e['sender'])} → {who(e['recipient'])}" + (f" ({amounts})" if amounts else "")


def _bounce_text(e: Mapping[str, Any], ruleset: Ruleset, who: Any) -> str:
    where = f"встреча на ребре {'-'.join(e['edge'])}" if e.get("edge") else f"вход в {e['province']}"
    return (f"пакт соблюдён: {where}, войска {who(e.get('seat'))} ({_units_text(ruleset, e['units'])}) вернулись в "
            f"{e['from']} без боя")


def _terms(e: Mapping[str, Any]) -> str:
    return f"на {e['turns']} х." if e.get("turns") else "бессрочно"


_DIPLOMACY_TEXT = {
    "gift": _gift_text,
    "pact_bounce": _bounce_text,
    "pact_joint": lambda e, rs, who: (f"{e['province']}: союзники по пакту ({_pair(e, who)}) вошли вместе, держит "
                                      f"{who(e['holder'])}, остальные вернулись домой"),
    "pact_signed": lambda e, rs, who: f"пакт заключён: {_pair(e, who)} ({_terms(e)})",
    "pact_leave": lambda e, rs, who: (f"{who(e['seat'])} выходит из пакта ({_pair(e, who)}): конец перед расчётом "
                                      f"хода {e['ends_before_turn']}"),
    "pact_broken": lambda e, rs, who: (f"пакт нарушен: {_pair(e, who)}; нарушил "
                                       f"{', '.join(who(s) for s in e['broken_by'])}"),
    "pact_expired": lambda e, rs, who: f"срок пакта истёк: {_pair(e, who)}",
    "pact_ended": lambda e, rs, who: (f"пакт окончен: {_pair(e, who)} — "
                                      f"{'выход вступил в силу' if e.get('reason') == 'left' else 'сторона выбыла'}"),
    "pact_proposed": lambda e, rs, who: f"предложен пакт {e['pact']}: {who(e['sender'])} → {_pair(e, who)} ({_terms(e)})",
    "pact_rejected": lambda e, rs, who: f"пакт {e['pact']} отклонён ({_pair(e, who)})",
    "pact_withdrawn": lambda e, rs, who: f"предложение пакта {e['pact']} отозвано ({_pair(e, who)})",
    "pact_lapsed": lambda e, rs, who: f"предложение пакта {e['pact']} не принято до конца хода ({_pair(e, who)})",
}


def render_report_text(report: TurnReport, ruleset: Ruleset, state: GameState, seat: str) -> str:
    """Report of one resolved turn as seen by `seat` (state is used only for nicks)."""
    lines = [f"ОТЧЁТ ХОДА {report.turn}"]
    lines += [f"- {event_text(e, ruleset, state, seat)}" for e in report.by_seat.get(seat, [])]
    sc = report.public.get("scores", {})
    lines.append("Счёт: " + ", ".join(f"{_owner_tag(state, seat, s)} {n}" for s, n in sorted(sc.items())))
    if report.public.get("result"):
        lines.append(_result_line(state, seat))
    return "\n".join(lines)

