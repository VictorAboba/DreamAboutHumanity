"""Espionage v2 figures of the game report (docs/SPY_V2.md section 11): built from the journal only.

`spy_v2(events, seats)` returns None when the journal has no Shade missions, else
{"seats": {seat: {...}}, "game": {...}, "m10": [{name, value, target, ok}]}:

- per seat: missions by mission and the share of deep ones (secret / read_orders / sabotage); missions started after
  the seat reached the intel cap (a denial `cap`, or a running total at the cap); missions by turn buckets 1-4 / 5-8 /
  9-12; idle Shade-turns (held Shades beyond those that played a round, up to ACTIVE_SLOTS a turn); needless
  Observations (`mission_started.in_sight`); intel points by kind and by target, denials by reason (the pact ones
  apart); empty missions per mission; mean informativeness per mission; catches by C_loc band; Wards set and Shades
  caught at a Ward; expected catches (the sum of `p_det` of the rounds played) against the actual ones; reports
  turned into decisions (a) a P2 move / fortify of the same turn at the Observation's province (any P2 edit for a
  report on a seat), (b) an attack on the Observation's province within 2 turns, (c) estimate(defender="rN");
  sabotage: points cut by the cap, effects credited to another strike, builds stopped, revolts, strikes with a
  battle, strikes without effect; read_orders catches at S1.
- game: pact breaks the victim had read beforehand (its read_orders on the breaker that turn) and its P2 reaction;
  builds stopped by sabotage (whose, left unfinished).
- m10: the criteria of section 11 for this game.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

DEEP = ("secret", "read_orders", "sabotage")
BUCKETS = ((1, 4, "1-4"), (5, 8, "5-8"), (9, 12, "9-12"))
ACTIVE_SLOTS = 2                # spy.active_base of the core 0.8.0 scenarios: the slots an idle Shade could fill
DECISION_TURNS = 2              # (b): an attack on the observed province within this many turns
C_BANDS = ((None, 0, "≤0"), (1, 1, "1"), (2, 2, "2"), (3, None, "3+"))


def _short(full_id: Any) -> str:
    return str(full_id).rsplit(":", 1)[-1]


def _intel_cap() -> int:
    from dah.engine.registry import stat_spec
    return int(stat_spec("score.intel.cap").default)


def _band(c: Any) -> str:
    c = int(c or 0)
    for lo, hi, name in C_BANDS:
        if (lo is None or c >= lo) and (hi is None or c <= hi):
            return name
    return "?"


def _seat() -> dict[str, Any]:
    return {"missions": Counter(), "deep_share": None, "after_cap": 0, "by_bucket": Counter(), "idle_shade_turns": 0,
            "needless_observe": 0, "points_by_kind": Counter(), "points_by_target": Counter(),
            "denied": Counter(), "empty": Counter(), "ended": Counter(), "info": defaultdict(list),
            "caught_by_c": Counter(), "rounds_by_c": Counter(), "wards": 0, "caught_at_ward": 0,
            "expected_caught": 0.0, "caught": 0, "decisions": Counter(), "sabotage": Counter(),
            "read_s1": [0, 0]}


def spy_v2(events: list[dict[str, Any]], seats: list[str]) -> dict[str, Any] | None:
    if not any(e.get("type") == "mission_started" for e in events):
        return None
    cap = _intel_cap()
    out: dict[str, dict[str, Any]] = {s: _seat() for s in seats}

    def seat(s: Any) -> dict[str, Any] | None:
        return out.get(s) if isinstance(s, str) else None

    capped: dict[str, int] = {}                     # seat -> turn it reached the cap
    hired: dict[str, list[int]] = defaultdict(list)  # seat -> turn of each Shade it got (0: from the start)
    lost: dict[str, list[int]] = defaultdict(list)
    busy: dict[tuple[str, int], set[str]] = defaultdict(set)   # (seat, turn) -> Shades that played a round
    started: list[tuple[str, int, str, str, bool]] = []         # (seat, turn, mission, target, in_sight)
    reports: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    summaries: dict[tuple[str, int], dict[str, Any]] = {}
    p2_edits: Counter[tuple[str, int]] = Counter()
    estimates: list[dict[str, Any]] = []
    breaks: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    completed: set[tuple[str, str, str]] = set()
    turn, last_turn = 1, 1
    for e in events:
        kind = e.get("type")
        if isinstance(e.get("turn"), int):
            turn = e["turn"]
            last_turn = max(last_turn, turn)
        t = seat(e.get("seat"))
        if kind == "shade_hired" and t is not None:
            hired[e["seat"]].append(int(e.get("turn") or 0))
        elif kind == "mission_started" and t is not None:
            mid = _short(e.get("mission"))
            t["missions"][mid] += 1
            st_turn = int(e.get("turn") or turn)
            started.append((e["seat"], st_turn, mid, str(e.get("target")), bool(e.get("in_sight"))))
            for lo, hi, name in BUCKETS:
                if lo <= st_turn <= hi:
                    t["by_bucket"][name] += 1
            if e["seat"] in capped and st_turn > capped[e["seat"]]:
                t["after_cap"] += 1
            t["needless_observe"] += bool(e.get("in_sight"))
        elif kind == "spy_round" and t is not None:
            if "p_det" not in e:           # a void mission plays no round
                continue
            busy[(e["seat"], int(e.get("turn") or turn))].add(str(e.get("shade")))
            band = _band(e.get("C_loc"))
            t["rounds_by_c"][band] += 1
            t["expected_caught"] += float(e.get("p_det") or 0.0)
            if e.get("detected"):
                t["caught"] += 1
                t["caught_by_c"][band] += 1
                t["caught_at_ward"] += bool(e.get("ward"))
            if _short(e.get("mission")) == "read_orders" and int(e.get("S") or 0) == 1:
                t["read_s1"][1] += 1
                t["read_s1"][0] += bool(e.get("detected"))
        elif kind == "intel_report" and t is not None:
            t["info"][_short(e.get("mission"))].append(float(e.get("informativeness") or 0.0))
            reports.append(e)
        elif kind == "intel_copied":
            # core 0.8.0 trade (docs/TRADE.md 2.3): a bought copy reads as the buyer's report of the report's turn,
            # in the turn it was bought (only for the forewarned breaks below)
            copies.append({"seat": e.get("to"), "mission": e.get("mission"), "target": e.get("target"),
                           "turn": e.get("turn"), "report_turn": e.get("report_turn")})
        elif kind == "mission_end" and t is not None:
            mid = _short(e.get("mission"))
            t["ended"][mid] += 1
            t["empty"][mid] += e.get("reason") == "empty"
            if e.get("reason") == "caught":
                lost[e["seat"]].append(int(e.get("turn") or turn))
        elif kind == "intel_point" and t is not None:
            t["points_by_kind"][e.get("kind") or "each_turn"] += int(e.get("points") or 1)
            t["points_by_target"][str(e.get("target"))] += int(e.get("points") or 1)
            if int(e.get("total") or 0) >= cap and e["seat"] not in capped:
                capped[e["seat"]] = int(e.get("turn") or turn)
        elif kind == "intel_denied" and t is not None:
            t["denied"][str(e.get("reason"))] += 1
            if e.get("reason") == "cap" and e["seat"] not in capped:
                capped[e["seat"]] = int(e.get("turn") or turn)
        elif kind == "ward_set" and t is not None:
            t["wards"] += 1
        elif kind == "order_edit_p2" and t is not None:
            p2_edits[(e["seat"], int(e.get("turn") or turn))] += 1
        elif kind == "orders_summary" and t is not None:
            summaries[(e["seat"], int(e.get("turn") or turn))] = e
            for m in e.get("moves") or []:
                if m.get("pact") == "break" and m.get("owner"):
                    breaks.append({"seat": e["seat"], "victim": m["owner"], "province": m.get("to"),
                                   "turn": int(e.get("turn") or turn)})
        elif kind == "estimate_report" and t is not None:
            estimates.append(e)
        elif kind == "sabotage_effect" and t is not None:
            effects = list(e.get("effects") or [])
            sab = t["sabotage"]
            sab["strikes"] += 1
            sab["no_effect"] += not effects
            sab["battle"] += "battle" in effects
            sab["stall"] += "stall" in effects
            sab["revolt"] += "revolt" in effects
            sab["points"] += bool(e.get("intel_point"))
            if e.get("reason") == "cap":
                sab["cut_by_cap"] += 1
            elif e.get("reason") == "taken":
                sab["taken_by_another"] += 1
        elif kind == "build_sabotaged":
            stopped.append(e)
        elif kind == "work_completed":
            completed.add((str(e.get("seat")), str(e.get("province")), str(e.get("building"))))
    # idle Shade-turns: Shades held at the recon of a turn (got before it, not lost before it) beyond those that played
    # a round, while a slot was free (ACTIVE_SLOTS)
    for s, t in out.items():
        for turn_ in range(1, last_turn + 1):
            held = sum(1 for x in hired.get(s, ()) if x < turn_) - sum(1 for x in lost.get(s, ()) if x < turn_)
            playing = len(busy.get((s, turn_), ()))
            t["idle_shade_turns"] += max(0, min(held, ACTIVE_SLOTS) - playing)
    # reports turned into decisions (decision 18: counted here, offline)
    for r in reports:
        s, r_turn, target = r["seat"], int(r.get("turn") or 0), str(r.get("target"))
        t = out.get(s)
        if t is None:
            continue
        province = target if _short(r.get("mission")) == "observe" else None
        same = summaries.get((s, r_turn)) or {}
        if province is not None:
            if any(m.get("p2") and m.get("to") == province for m in same.get("moves") or []) or \
                    any(f.get("p2") and f.get("province") == province for f in same.get("fortify") or []):
                t["decisions"]["p2_same_turn"] += 1
            if any(m.get("to") == province for k in range(DECISION_TURNS + 1)
                   for m in (summaries.get((s, r_turn + k)) or {}).get("moves") or []):
                t["decisions"]["attack_within_2"] += 1
        elif p2_edits.get((s, r_turn)):
            t["decisions"]["p2_same_turn"] += 1
    for e in estimates:
        t = out.get(e["seat"])
        if t is not None:
            t["decisions"]["estimate_by_report"] += 1
    # pact breaks the victim read beforehand (its read_orders on the breaker that turn) and its P2 reaction
    warned = []
    for b in breaks:
        read = any(r["seat"] == b["victim"] and _short(r.get("mission")) == "read_orders"
                   and str(r.get("target")) == b["seat"] and int(r.get("turn") or 0) == b["turn"] for r in reports)
        bought = any(c["seat"] == b["victim"] and _short(c.get("mission")) == "read_orders"
                     and str(c.get("target")) == b["seat"] and int(c.get("report_turn") or 0) == b["turn"]
                     and int(c.get("turn") or 0) == b["turn"] for c in copies)
        if not (read or bought):
            continue
        same = summaries.get((b["victim"], b["turn"])) or {}
        reacted = bool(p2_edits.get((b["victim"], b["turn"]))) or any(
            f.get("p2") for f in same.get("fortify") or [])
        warned.append({**b, "reacted": reacted, **({"via": "copy"} if bought and not read else {})})
    for t in out.values():
        deep = sum(t["missions"][m] for m in DEEP)
        total = sum(t["missions"].values())
        t["deep_share"] = round(deep / total, 3) if total else None
        t["info"] = {m: round(sum(v) / len(v), 3) for m, v in sorted(t["info"].items()) if v}
        t["expected_caught"] = round(t["expected_caught"], 2)
        for k in ("missions", "by_bucket", "points_by_kind", "points_by_target", "denied", "empty", "ended",
                  "caught_by_c", "rounds_by_c", "decisions", "sabotage"):
            t[k] = dict(sorted(t[k].items()))
    stops = [{"seat": e.get("seat"), "province": e.get("province"), "building": e.get("building"),
              "turn": e.get("turn"),
              "unfinished": (str(e.get("seat")), str(e.get("province")), str(e.get("building"))) not in completed}
             for e in stopped]
    game = {"warned_breaks": warned, "builds_stopped": stops, "turns": last_turn}
    return {"seats": out, "game": game, "m10": _m10(out, game, last_turn)}


def _m10(out: dict[str, dict[str, Any]], game: dict[str, Any], turns: int) -> list[dict[str, Any]]:
    """The m10 criteria of docs/SPY_V2.md 11 for one game."""
    rows: list[dict[str, Any]] = []

    def row(name: str, value: Any, target: str, ok: bool | None) -> None:
        rows.append({"name": name, "value": value, "target": target, "ok": ok})

    missions = Counter()
    for t in out.values():
        missions.update(t["missions"])
    total = sum(missions.values())
    deep = sum(missions[m] for m in DEEP) / total if total else None
    row("глубоких миссий", None if deep is None else round(deep, 3), "≥ 0.40", None if deep is None else deep >= 0.40)
    row("у каждого места задание после предела", {s: t["after_cap"] for s, t in out.items()}, "≥ 1 у каждого",
        all(t["after_cap"] >= 1 for t in out.values()))
    early = sum(t["by_bucket"].get("1-4", 0) for t in out.values())
    late = sum(t["by_bucket"].get("9-12", 0) for t in out.values())
    row("заданий в ходы 9–12 от ходов 1–4", None if not early else round(late / early, 3), "≥ 0.50",
        None if not early or turns < 9 else late / early >= 0.50)
    row("лишних наблюдений на место", {s: t["needless_observe"] for s, t in out.items()}, "≤ 1",
        all(t["needless_observe"] <= 1 for t in out.values()))
    obs_end = sum(t["ended"].get("observe", 0) for t in out.values())
    obs_empty = sum(t["empty"].get("observe", 0) for t in out.values())
    empty = obs_empty / obs_end if obs_end else None
    row("пустых observe", None if empty is None else round(empty, 3), "≤ 0.30",
        None if empty is None else empty <= 0.30)
    two = [s for s, t in out.items() if len([x for x, n in t["points_by_target"].items() if n > 0]) >= 2]
    row("очки разведки с двух соперников у места", two, "≥ 1 места", bool(two))
    links = sum(sum(t["decisions"].values()) for t in out.values())
    row("связок «донесение → решение»", links, "≥ 2", links >= 2)
    wards = sum(t["wards"] for t in out.values())
    # S2 comes from a pack's tech: the report does not name it (decision 13); a Ward settles it, else it is open
    row("Застава (или S2 — по технологиям места)", wards, "≥ 1", True if wards else None)
    s1 = [sum(t["read_s1"][0] for t in out.values()), sum(t["read_s1"][1] for t in out.values())]
    rate = s1[0] / s1[1] if s1[1] else None
    row("поимок read_orders при S1", None if rate is None else round(rate, 3), "≤ 0.30",
        None if rate is None else rate <= 0.30)
    return rows


def spy_v2_lines(block: dict[str, Any], nick: dict[str, str]) -> list[str]:
    """Russian lines of the report text (in «ШПИОНАЖ И ЗАВОЕВАНИЕ»)."""

    def counts(d: dict[str, Any]) -> str:
        return ", ".join(f"{k} {v}" for k, v in d.items()) or "—"

    lines = []
    for s, t in block["seats"].items():
        lines.append(
            f"{s} {nick.get(s, s)} v2: глубоких {'—' if t['deep_share'] is None else round(100 * t['deep_share'])}%"
            f", после предела {t['after_cap']}, по ходам {counts(t['by_bucket'])}, простой Теней {t['idle_shade_turns']}"
            f" Тень-ход., лишних наблюдений {t['needless_observe']}; очки по видам {counts(t['points_by_kind'])}, по "
            f"целям {counts(t['points_by_target'])}, отказы {counts(t['denied'])}; пустые {counts(t['empty'])}; "
            f"информативность {counts(t['info'])}; поимки по C_лок "
            + (", ".join(f"C{b} {t['caught_by_c'].get(b, 0)}/{n}" for b, n in t["rounds_by_c"].items()) or "—")
            + f"; Застав {t['wards']}, пойман у Заставы {t['caught_at_ward']}; поимок ожидалось "
            f"{t['expected_caught']}, было {t['caught']}; донесение → решение {counts(t['decisions'])}; Саботаж "
            f"{counts(t['sabotage'])}")
    g = block["game"]
    warned = "; ".join(f"ход {w['turn']} {nick.get(w['seat'], w['seat'])} → {nick.get(w['victim'], w['victim'])} "
                       f"{w['province']} ({'реакция в П2' if w['reacted'] else 'без реакции'})"
                       for w in g["warned_breaks"]) or "—"
    stops = "; ".join(f"ход {x['turn']} {nick.get(str(x['seat']), x['seat'])} {_short(x['building'])} в "
                      f"{x['province']}{' (не достроено)' if x['unfinished'] else ''}"
                      for x in g["builds_stopped"]) or "—"
    lines.append(f"предупреждённые нарушения: {warned}; стройки, остановленные саботажем: {stops}")
    marks = {True: "да", False: "нет", None: "—"}
    lines.append("критерии m10: " + "; ".join(f"{r['name']} {r['value']} ({r['target']}: {marks[r['ok']]})"
                                               for r in block["m10"]))
    return lines
