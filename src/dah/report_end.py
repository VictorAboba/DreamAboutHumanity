"""Core 0.8.3 (decision 34, measurement m12): «заплачено и потеряно из-за конца» — per seat, what it paid for that
could only have paid off on a turn after the real end of the game (the hidden last turn of quad4 came first).

`end_losses(events, report)` returns None before game_over, else {"end_turn": N, "seats": {seat: block}, "game":
{total, owed}} where a block of a seat alive at the end is

- `works`: builds still in progress at the end with what was paid: [{province, building, paid, shares_paid, shares,
  great}]. From the game_over's `unfinished` (journals of core 0.8.3 on: every build, ordinary ones included); in an
  older journal rebuilt from the great works' events (`work_started` with its first share or its price, `build_paid`,
  no `work_completed`, the province not taken from the seat after the start) — ordinary builds are not journaled
  there, `works_note` says so;
- `shades`: Shades hired for a price (not the free ones) that never played a mission round by the cutoff (no mission,
  or only one whose first round falls after it — given in П2 of the last turn): [{shade, turn, cost}];
- `rounds`: mission rounds paid (a mission's price covers its whole term, SPY_V2.md) for turns after the end, of
  missions still running on the last turn (caught, void or done on it included — a round after the end could not
  be played anyway; a mission that ended earlier lost its rounds to that end, not to the game's): [{shade, mission,
  target, first_turn, rounds, lost, cost, ended}];
- `research`: research points put into techs not finished ({tech: points}, `research_resource`), from the
  game_over's `unfinished`; an older journal names the techs started and not completed (`research_unpriced`);
- `promises`: goods promised to the seat in a trade (`later`) due after the end and not delivered by the end — the
  undelivered part only (`goods`, {resources?, provinces?}; the delivery matched as in dah.report_trade: the
  promiser's one-sided transfers and trades marked `fulfils`): [{from, goods, by_turn, trade}];
- `total`: the resources the seat paid in all of the above but the promises ({resource: n}; the research points are in
  it under their resource); `owed`: the promises' undelivered resources — owed to the seat, not paid by it, so apart.

A seat eliminated before the end is not in `seats`: its game ended at its elimination, so what it lost there is lost
to the elimination (its provinces went with their builds), not to the end. It gets a block of its own in
`at_elimination` {seat: block + {"turn"}} with the same rules and its elimination turn as the cutoff (research and
builds from `unfinished` too: the state keeps them), so the reader sees it, apart from the end's losses.

Prices: the journal's own `cost` (core 0.8.3 on: `shade_hired.cost`, `mission_started.cost` and `round_cost`); an
older journal is priced by its ruleset (game.pick_ruleset on the journaled hash, the seat's faction), `priced`
"ruleset" then; without one the price is unknown ({}) and `priced` is None.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

FREE = "free"


def _short(full_id: Any) -> str:
    return str(full_id).rsplit(":", 1)[-1]


def _add(acc: dict[str, int], amounts: Any) -> None:
    for r, n in (amounts or {}).items():
        if isinstance(n, (int, float)) and not isinstance(n, bool) and n:
            acc[r] = acc.get(r, 0) + int(n)


def _mul(amounts: dict[str, int], k: int) -> dict[str, int]:
    return {r: n * k for r, n in amounts.items() if n * k}


@lru_cache(maxsize=8)
def _ruleset(config_json: str, journaled: str | None) -> Any:
    try:
        from dah.game import pick_ruleset
        pick = pick_ruleset(json.loads(config_json), journaled)
    except Exception:           # a config the ruleset cannot be built from: prices unknown
        return None
    return pick.ruleset if pick.matches else None


class _Prices:
    """Prices of an older journal (no `cost` in its events), read from the ruleset it was played under."""

    def __init__(self, created: dict[str, Any] | None, factions: dict[str, Any]) -> None:
        self.created = created or {}
        self.factions = factions
        self._rs: Any = ...
        self.used = False

    def rs(self) -> Any:
        if self._rs is ...:
            cfg = self.created.get("config")
            self._rs = _ruleset(json.dumps(cfg, sort_keys=True, ensure_ascii=False), self.created.get(
                "ruleset_hash")) if isinstance(cfg, dict) else None
        return self._rs

    def shade(self, seat: str) -> dict[str, int]:
        self.used = True
        rs = self.rs()
        if rs is None:
            return {}
        from dah.engine import espionage as esp
        return dict(esp.shade_cost(rs, self.factions.get(seat)))

    def round(self, mission: Any) -> dict[str, int]:
        self.used = True
        rs = self.rs()
        m = rs.missions.get(mission) if rs is not None else None
        return dict(m.cost_per_round) if m is not None else {}


def _block() -> dict[str, Any]:
    return {"works": [], "shades": [], "rounds": [], "research": {}, "promises": [], "total": {}, "owed": {}}


def _owed(events: list[dict[str, Any]], report: dict[str, Any]) -> dict[tuple[Any, Any, Any], dict[str, Any]]:
    """(trade id, giver, taker) -> the undelivered part of that promise (dah.report_trade's delivery matching)."""
    trade = report.get("trade")
    if trade is None:
        from dah.report_trade import trade_report
        trade = trade_report(events, list(report.get("seats") or {})) or {}
    rows = ((trade.get("game") or {}).get("promises") or {}).get("rows") or []
    return {(r.get("trade"), r.get("from"), r.get("to")): r.get("owed") or {} for r in rows}


def end_losses(events: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any] | None:
    over = next((e for e in events if e.get("type") == "game_over"), None)
    if over is None:
        return None
    end = int(over.get("turn") or 0)
    seats = list(report.get("seats") or {})
    created = next((e for e in events if e.get("type") == "game_created"), None)
    factions = {s: (report["seats"][s] or {}).get("faction") for s in seats}
    prices = _Prices(created, factions)
    out_turn: dict[str, int] = {}
    works: dict[tuple[str, str, str], dict[str, Any]] = {}
    shades: dict[tuple[str, str], dict[str, Any]] = {}
    missions: list[dict[str, Any]] = []
    running: dict[tuple[str, str], dict[str, Any]] = {}
    promises: list[dict[str, Any]] = []
    started_research: dict[str, dict[str, int]] = {}
    for e in events:
        kind = e.get("type")
        seat = e.get("seat")
        if kind == "turn_resolved":
            for s in e.get("eliminated") or []:
                out_turn.setdefault(s, int(e.get("turn") or 0))
        elif kind == "work_started" and seat:
            shares = int(e.get("shares") or 1)
            paid: dict[str, int] = {}
            _add(paid, e.get("paid") if shares > 1 else e.get("cost"))
            works[(seat, str(e.get("province")), str(e.get("building")))] = {
                "province": e.get("province"), "building": e.get("building"), "paid": paid,
                "shares_paid": 1, "shares": shares, "great": True, "turn": e.get("turn")}
        elif kind == "build_paid" and seat:
            w = works.get((seat, str(e.get("province")), str(e.get("building"))))
            if w is not None:
                _add(w["paid"], e.get("paid"))
                w["shares_paid"] = int(e.get("share") or w["shares_paid"] + 1)
        elif kind == "work_completed" and seat:
            works.pop((seat, str(e.get("province")), str(e.get("building"))), None)
        elif kind == "province_captured" and e.get("from"):
            # the build went with the province (lost to the war, not to the end)
            for key in [k for k in works if k[0] == e["from"] and k[1] == e.get("province")]:
                works.pop(key)
        elif kind == "shade_hired" and seat and not e.get(FREE):
            shades[(seat, str(e.get("shade")))] = {"shade": e.get("shade"), "turn": e.get("turn"),
                                                   "cost": dict(e["cost"]) if "cost" in e else None}
        elif kind == "mission_started" and seat:
            m = {"seat": seat, "shade": e.get("shade"), "mission": e.get("mission"), "target": e.get("target"),
                 "first_turn": int(e.get("first_turn") or e.get("turn") or 0), "rounds": int(e.get("rounds") or 1),
                 "round_cost": dict(e["round_cost"]) if "round_cost" in e else None, "ended": None,
                 "ended_turn": None}
            missions.append(m)
            running[(seat, str(e.get("shade")))] = m
        elif kind == "mission_end" and seat:
            m = running.pop((seat, str(e.get("shade"))), None)
            if m is not None:
                m.update(ended=e.get("reason"), ended_turn=int(e.get("turn") or 0))
        elif kind == "promise_made":
            promises.append(e)
        elif kind == "research_started" and seat:
            started_research.setdefault(seat, {})[str(e.get("tech"))] = int(e.get("turn") or 0)
        elif kind == "tech_completed" and seat:
            started_research.get(seat, {}).pop(str(e.get("tech")), None)
    unfinished = over.get("unfinished") if isinstance(over.get("unfinished"), dict) else None
    owed = _owed(events, report) if promises else {}

    def block(seat: str, cutoff: int) -> dict[str, Any]:
        b = _block()
        if unfinished is not None:
            mine = unfinished.get(seat) or {}
            b["works"] = [{k: w.get(k) for k in ("province", "building", "paid", "shares_paid", "shares", "great")}
                          for w in mine.get("builds") or []]
            if mine.get("research"):
                b["research"] = dict(mine["research"])
                b["research_resource"] = mine.get("research_resource")
        else:
            b["works"] = [{k: w[k] for k in ("province", "building", "paid", "shares_paid", "shares", "great")}
                          for (s, _, _), w in sorted(works.items()) if s == seat]
            b["works_note"] = "only great works (older journal)"
            if started_research.get(seat):
                b["research_unpriced"] = sorted(started_research[seat])
        for (s, sid), sh in sorted(shades.items()):
            # used = some mission of it played a round by the cutoff (one given in П2 of the last turn starts after it)
            used = any(m["seat"] == s and str(m["shade"]) == sid and m["first_turn"] <= cutoff for m in missions)
            if s == seat and not used and int(sh["turn"] or 0) <= cutoff:
                cost = sh["cost"] if sh["cost"] is not None else prices.shade(seat)
                b["shades"].append({"shade": sh["shade"], "turn": sh["turn"], "cost": cost})
        for m in missions:
            if m["seat"] != seat or (m["ended_turn"] is not None and m["ended_turn"] < cutoff):
                continue
            lost = m["first_turn"] + m["rounds"] - 1 - cutoff
            if lost <= 0:
                continue
            per = m["round_cost"] if m["round_cost"] is not None else prices.round(m["mission"])
            b["rounds"].append({"shade": m["shade"], "mission": m["mission"], "target": m["target"],
                                "first_turn": m["first_turn"], "rounds": m["rounds"], "lost": lost,
                                "cost": _mul(per, lost), "ended": m["ended"]})
        for p in promises:
            if p.get("to") == seat and int(p.get("by_turn") or 0) > cutoff:
                left = owed.get((p.get("trade_id"), p.get("seat"), p.get("to")), {})
                if left:            # delivered early in full: nothing lost
                    b["promises"].append({"from": p.get("seat"), "goods": left, "by_turn": p.get("by_turn"),
                                          "trade": p.get("trade_id")})
        for w in b["works"]:
            _add(b["total"], w["paid"])
        for x in (*b["shades"], *b["rounds"]):
            _add(b["total"], x["cost"])
        if b["research"] and b.get("research_resource"):
            _add(b["total"], {b["research_resource"]: sum(b["research"].values())})
        for p in b["promises"]:
            _add(b["owed"], p["goods"].get("resources"))
        b["total"] = dict(sorted(b["total"].items()))
        b["owed"] = dict(sorted(b["owed"].items()))
        return b

    out: dict[str, Any] = {"end_turn": end, "seats": {}, "at_elimination": {}, "game": {"total": {}, "owed": {}}}
    for seat in seats:
        if seat in out_turn and out_turn[seat] <= end:
            b = block(seat, out_turn[seat])
            b["turn"] = out_turn[seat]
            out["at_elimination"][seat] = b
        else:
            b = block(seat, end)
            out["seats"][seat] = b
            _add(out["game"]["total"], b["total"])
            _add(out["game"]["owed"], b["owed"])
    out["game"]["total"] = dict(sorted(out["game"]["total"].items()))
    out["game"]["owed"] = dict(sorted(out["game"]["owed"].items()))
    out["priced"] = "journal" if not prices.used else ("ruleset" if prices.rs() is not None else None)
    return out


def _amounts(amounts: dict[str, Any]) -> str:
    return " ".join(f"{_short(r)} {n}" for r, n in sorted(amounts.items())) or "0"


def _block_text(b: dict[str, Any], nick: dict[str, str]) -> str:
    parts = []
    if b["works"]:
        parts.append("стройки " + "; ".join(
            f"{_short(w['building'])} в {w['province']} ({w['shares_paid']}/{w['shares']} дол., "
            f"{_amounts(w['paid'])})" for w in b["works"]))
    if b["shades"]:
        parts.append("Тени без задания " + "; ".join(f"{s['shade']} (нанята на ходу {s['turn']}, "
                                                      f"{_amounts(s['cost'] or {})})" for s in b["shades"]))
    if b["rounds"]:
        parts.append("раунды после конца " + "; ".join(
            f"{r['shade']} {_short(r['mission'])} → {nick.get(str(r['target']), r['target'])} (ходы {r['first_turn']}–"
            f"{r['first_turn'] + r['rounds'] - 1}, потеряно {r['lost']}, {_amounts(r['cost'])}"
            + (f"; задание кончилось: {r['ended']}" if r["ended"] else "") + ")" for r in b["rounds"]))
    if b["research"]:
        parts.append("исследования " + ", ".join(f"{_short(t)} {n}" for t, n in b["research"].items())
                     + (f" ({_short(b['research_resource'])})" if b.get("research_resource") else ""))
    if b.get("research_unpriced"):
        parts.append("начатые исследования (вклад в журнале не записан) " + ", ".join(
            _short(t) for t in b["research_unpriced"]))
    body = "; ".join(parts) or "ничего"
    body += f"; всего {_amounts(b['total'])}" if b["total"] else ""
    if b["promises"]:       # owed to the seat, not paid by it: apart from «всего»
        body += "; не получено по обещаниям со сроком после конца (причиталось, не заплачено) " + "; ".join(
            f"от {nick.get(str(p['from']), p['from'])} к ходу {p['by_turn']}: " + _goods(p["goods"])
            for p in b["promises"])
    return body


def _goods(goods: dict[str, Any]) -> str:
    parts = [_amounts(goods["resources"])] if goods.get("resources") else []
    if goods.get("provinces"):
        parts.append("провинции " + ", ".join(str(x) for x in goods["provinces"]))
    return " + ".join(parts) or "0"


def end_lines(block: dict[str, Any], nick: dict[str, str]) -> list[str]:
    """Russian lines of the report text."""
    lines = ["", f"ЗАПЛАЧЕНО И ПОТЕРЯНО ИЗ-ЗА КОНЦА (после хода {block['end_turn']}; только отчёт, местам не "
                 "показывается):"]
    for s, b in block["seats"].items():
        lines.append(f"{s} {nick.get(s, s)}: {_block_text(b, nick)}")
    for s, b in block["at_elimination"].items():
        lines.append(f"{s} {nick.get(s, s)} выбыл на ходу {b['turn']} — потеряно при выбытии, не из-за конца: "
                     f"{_block_text(b, nick)}")
    notes = []
    if any(b.get("works_note") for b in (*block["seats"].values(), *block["at_elimination"].values())):
        notes.append("старый журнал: стройки — только великие, вклад в исследования не записан")
    if block.get("priced") == "ruleset":
        notes.append("цены Теней и раундов — по правилам партии (в журнале их нет)")
    elif block.get("priced") is None:
        notes.append("цены Теней и раундов неизвестны")
    owed = block["game"].get("owed")
    lines.append(f"всего по партии: {_amounts(block['game']['total'])}" + (f" ({'; '.join(notes)})" if notes else "")
                 + (f"; не получено по обещаниям: {_amounts(owed)}" if owed else ""))
    return lines
