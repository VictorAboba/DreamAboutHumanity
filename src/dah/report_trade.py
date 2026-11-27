"""Trade figures of the game report (docs/TRADE.md section 13; decision 18: the checking side counts): built from the
journal only.

`trade_report(events, seats)` returns None when the journal has no trade events, else {"seats": {...}, "game": {...}}:

- per seat (13.1): offers made and received, accepts (as proposer, as answerer), rejects, withdrawals, replaced,
  countered and lapsed offers, settled trades, `failed_by_own_lack` (the proposer could no longer pay at the accept),
  `accept_rate` (offers received it accepted), the median seconds from an offer to its answer, bargaining chains
  (`reply_to` / `replaces`) and the concession along them (the change of the value asked from the first offer to the
  last), provinces sold / bought, report copies sold / bought, the value traded out / in (reference prices),
  `terms_of_trade` (value got / value given over resource parts; None when nothing was given), promises (13.3),
  reciprocity of one-sided transfers, `kingmaking`.
- game (13.2-13.3): rates of one-resource-for-one-resource trades per pair (median, min, max, n; by turn), the
  reference prices, provinces (price, buyback, status, `transfer_recapture`, `attacked_promised`,
  `promised_move_rejected`), report copies (by mission, sold to their own target), `manual_swaps`, the promises in
  total.

Reference prices: `p_r = 1 / share of r in the seats' stocks summed over the turns`, alloy = 1. The journal has no
income per resource, so the stocks of `turn_resolved` stand in for it (an approximation, docs/TRADE.md «Реализация:
уточнения», шаг C). The free-text promises of 13.3.3 need the messages' text, which the journal does not keep (only
their length): they stay with the checking side.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any

RECAPTURE_WINDOW = 3    # turns: a province given away and taken back by its giver (report constant, not a stat)
GIFT_WINDOW = 3         # turns: a one-sided transfer counts as repaid when the other side sends anything back in it
KINGMAKING_TURNS = 3    # the last turns of the game in which a seat last by score gives value away
# kingmaking also counts what is not resources (docs/TRADE.md 6.2, 13.2), at fixed report prices in alloy: a province
# given (a trade or a release to another seat) as the bots' province price, a report copy as their copy price
KINGMAKING_PROVINCE_VALUE = 12.0
KINGMAKING_COPY_VALUE = 5.0
LAST_TURNS = 2          # promises due in the last turns are counted apart
TRADE_EVENTS = frozenset({"trade_proposed", "trade_accepted", "trade_rejected", "trade_withdrawn", "trade_replaced",
                          "trade_countered", "trade_lapsed", "trade_settled", "trade_failed", "promise_made",
                          "province_transferred", "intel_copied"})


def _short(full_id: Any) -> str:
    return str(full_id).rsplit(":", 1)[-1]


def _res(side: Any) -> dict[str, int]:
    return {_short(r): int(n) for r, n in ((side or {}).get("resources") or {}).items()}


def _seat() -> dict[str, Any]:
    return {"proposed": 0, "received": 0, "accepted_as_proposer": 0, "accepted_as_answerer": 0, "rejected": 0,
            "withdrawn": 0, "replaced": 0, "countered": 0, "lapsed": 0, "settled": 0, "failed_by_own_lack": 0,
            "accept_rate": None, "answer_median_s": None, "chains": 0, "chain_rounds": None, "concession": None,
            "provinces_sold": 0, "provinces_bought": 0, "reports_sold": 0, "reports_bought": 0,
            "traded_out": 0.0, "traded_in": 0.0, "terms_of_trade": None,
            "promises_made": 0, "promises_kept": 0, "promises_partial": 0, "promises_broken": 0, "promises_void": 0,
            "promises_kept_rate": None, "promises_partial_rate": None, "promises_last_turns": 0,
            "one_sided_received": 0, "one_sided_sent": 0, "repaid_within_3": 0, "repaid_value_ratio": None,
            "kingmaking": 0.0, "kingmaking_provinces": 0, "kingmaking_copies": 0}


def prices(events: list[dict[str, Any]]) -> dict[str, float]:
    """Reference prices: 1 / the share of a resource in all stocks summed over the turns, alloy = 1."""
    total: Counter[str] = Counter()
    for e in events:
        if e.get("type") == "turn_resolved":
            for stock in (e.get("resources") or {}).values():
                for r, n in stock.items():
                    total[_short(r)] += max(0, int(n))
    all_ = sum(total.values())
    if not all_:
        return {}
    raw = {r: all_ / n for r, n in total.items() if n > 0}
    base = raw.get("alloy") or min(raw.values())
    return {r: round(v / base, 3) for r, v in sorted(raw.items())}


def _value(amounts: dict[str, int], price: dict[str, float]) -> float:
    return sum(n * price.get(r, 1.0) for r, n in amounts.items())


def trade_report(events: list[dict[str, Any]], seats: list[str]) -> dict[str, Any] | None:
    if not any(e.get("type") in TRADE_EVENTS for e in events):
        return None
    price = prices(events)
    per: dict[str, dict[str, Any]] = {s: _seat() for s in seats}

    def seat(s: Any) -> dict[str, Any]:
        return per.setdefault(str(s), _seat())

    offers: dict[str, dict[str, Any]] = {}          # trade id -> trade_proposed
    answered_s: dict[str, list[float]] = defaultdict(list)
    received_answered: Counter[str] = Counter()
    accepted_received: Counter[str] = Counter()
    settled: list[tuple[dict[str, Any], dict[str, Any]]] = []     # (settled event, offer)
    transfers: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    gifts: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    promises: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    releases: list[dict[str, Any]] = []
    rejected_moves: Counter[str] = Counter()
    scores_by_turn: dict[int, dict[str, int]] = {}
    last_turn = 0
    out_turn: dict[str, int] = {}       # seat -> the turn it was eliminated
    for e in events:
        kind = e.get("type")
        if kind == "trade_proposed":
            offers[e["trade_id"]] = e
            seat(e["seat"])["proposed"] += 1
            seat(e["to"])["received"] += 1
        elif kind in ("trade_accepted", "trade_rejected"):
            offer = offers.get(e["trade_id"])
            if offer is None:
                continue
            if e.get("ts") is not None and offer.get("ts") is not None:
                answered_s[e["seat"]].append(float(e["ts"]) - float(offer["ts"]))
            received_answered[e["seat"]] += 1
            if kind == "trade_accepted":
                accepted_received[e["seat"]] += 1
                seat(e["seat"])["accepted_as_answerer"] += 1
                seat(offer["seat"])["accepted_as_proposer"] += 1
            else:
                seat(e["seat"])["rejected"] += 1
        elif kind == "trade_withdrawn":
            seat(e["seat"])["withdrawn"] += 1
        elif kind in ("trade_replaced", "trade_countered"):
            offer = offers.get(e["trade_id"])
            if offer is not None:
                seat(offer["seat"])["replaced" if kind == "trade_replaced" else "countered"] += 1
        elif kind == "trade_lapsed":
            offer = offers.get(e["trade_id"])
            if offer is not None:
                seat(offer["seat"])["lapsed"] += 1
        elif kind == "trade_settled":
            offer = offers.get(e["trade_id"])
            if offer is not None:
                settled.append((e, offer))
                for s in e.get("seats") or ():
                    seat(s)["settled"] += 1
        elif kind == "trade_failed":
            failed.append(e)
            if e.get("reason") == "proposer_lacks" and e.get("short_side"):
                seat(e["short_side"])["failed_by_own_lack"] += 1
        elif kind in ("pact_ended", "claim_ended") and e.get("reason") == "eliminated" and e.get("seat"):
            out_turn.setdefault(str(e["seat"]), int(e.get("turn") or 0))     # journals before `eliminated` was written
        elif kind == "promise_made":
            promises.append(e)
        elif kind == "province_transferred":
            transfers.append(e)
        elif kind == "intel_copied":
            copies.append(e)
        elif kind == "province_captured":
            captures.append(e)
        elif kind == "gift":
            gifts.append(e)
        elif kind == "disposition" and e.get("behavior") == "release" and e.get("owner_after") not in (
                None, e.get("seat")):
            releases.append(e)
        elif kind == "order_rejected" and e.get("reason") == "PROVINCE_PROMISED":
            rejected_moves[str(e.get("seat"))] += 1
        elif kind == "turn_resolved":
            for s in e.get("eliminated") or ():
                out_turn.setdefault(str(s), int(e["turn"]))
            scores_by_turn[int(e["turn"])] = {str(k): int(v or 0) for k, v in (e.get("scores") or {}).items()}
            last_turn = max(last_turn, int(e["turn"]))
        elif kind == "game_over":
            last_turn = max(last_turn, int(e.get("turn") or 0))
    for s, t in per.items():
        if received_answered[s]:
            t["accept_rate"] = round(accepted_received[s] / max(1, t["received"]), 3)
        if answered_s[s]:
            t["answer_median_s"] = round(median(answered_s[s]), 1)
    _chains(offers, per, price)

    # transfers of value: (turn, giver, taker, resources, one-sided?, trade id, fulfils)
    flows: list[tuple[int, str, str, dict[str, int], bool, str | None, str | None]] = []
    for g in gifts:
        flows.append((int(g.get("turn") or 0), str(g["seat"]), str(g["to"]), _res({"resources": g["resources"]}), True,
                      None, None))
    rates: dict[str, list[float]] = defaultdict(list)
    rates_by_turn: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    got: dict[str, float] = defaultdict(float)
    gave: dict[str, float] = defaultdict(float)
    for e, offer in settled:
        a, b = str(offer["seat"]), str(offer["to"])
        give, want = _res(e.get("give")), _res(e.get("want"))
        turn = int(e.get("turn") or 0)
        one_way = not (e.get("want") or {}) or not (e.get("give") or {})
        later = offer.get("later")
        mark = offer.get("fulfils")
        if give:
            flows.append((turn, a, b, give, one_way and not later, offer["trade_id"], mark))
        if want:
            flows.append((turn, b, a, want, one_way and not later, offer["trade_id"], mark))
        for x, y, side in ((a, b, give), (b, a, want)):
            v = _value(side, price)
            seat(x)["traded_out"] += v
            seat(y)["traded_in"] += v
            gave[x] += v
            got[y] += v
        if len(give) == 1 and len(want) == 1 and not (e.get("give") or {}).get("provinces") \
                and not (e.get("want") or {}).get("provinces"):
            (r1, n1), (r2, n2) = next(iter(give.items())), next(iter(want.items()))
            key, rate = (f"{r1}→{r2}", n2 / n1) if r1 <= r2 else (f"{r2}→{r1}", n1 / n2)
            rates[key].append(rate)
            rates_by_turn[key][turn].append(rate)
    for s, t in per.items():
        t["traded_out"], t["traded_in"] = round(t["traded_out"], 2), round(t["traded_in"], 2)
        if gave[s]:
            t["terms_of_trade"] = round(got[s] / gave[s], 3)

    provinces = _provinces(transfers, captures, failed, offers, settled, price, per)
    reports = _copies(copies, per)
    swaps = _manual_swaps(gifts)
    _reciprocity(flows, per, price)
    given = ([(int(t.get("turn") or 0), str(t.get("from")), "provinces") for t in transfers]
             + [(int(r.get("turn") or 0), str(r.get("seat")), "provinces") for r in releases]
             + [(int(c.get("turn") or 0), str(c.get("from")), "copies") for c in copies])
    _kingmaking(flows, scores_by_turn, last_turn, per, price, given)
    promise_rows = _promises(promises, flows, transfers, last_turn, per, out_turn)
    for s, t in per.items():
        t["promised_move_rejected"] = rejected_moves.get(s, 0)
    kept = sum(1 for p in promise_rows if p["status"] == "kept")
    counted = sum(1 for p in promise_rows if p["status"] != "void")
    game = {
        "prices": price,
        "rates": {k: {"median": round(median(v), 3), "min": round(min(v), 3), "max": round(max(v), 3), "n": len(v),
                      "by_turn": {str(t): round(median(x), 3) for t, x in sorted(rates_by_turn[k].items())}}
                  for k, v in sorted(rates.items())},
        "settled": len(settled), "proposed": len(offers), "failed": dict(Counter(e.get("reason") for e in failed)),
        "provinces": provinces, "reports": reports, "manual_swaps": swaps,
        "promises": {"made": len(promise_rows), "kept": kept, "kept_rate": round(kept / counted, 3) if counted else None,
                     "void": sum(1 for p in promise_rows if p["status"] == "void"), "rows": promise_rows},
        "promised_move_rejected": sum(rejected_moves.values()),
    }
    return {"seats": per, "game": game}


def _chains(offers: dict[str, dict[str, Any]], per: dict[str, dict[str, Any]], price: dict[str, float]) -> None:
    """Bargaining chains: offers linked by `reply_to` / `replaces`; rounds = offers in the chain; the concession of
    the chain's first proposer = the value it asked at first minus the value it asked last (its own offers)."""
    parent = {tid: (o.get("reply_to") or o.get("replaces")) for tid, o in offers.items()}
    children = {p for p in parent.values() if p}
    rounds: dict[str, list[int]] = defaultdict(list)
    concession: dict[str, list[float]] = defaultdict(list)
    for tid in offers:
        if tid in children or not parent.get(tid):
            continue
        chain = [tid]
        while parent.get(chain[-1]) in offers:
            chain.append(parent[chain[-1]])
        chain.reverse()
        first = offers[chain[0]]
        who = str(first["seat"])
        rounds[who].append(len(chain))
        own = [offers[x] for x in chain if offers[x]["seat"] == who]
        if len(own) > 1:
            concession[who].append(round(_value(_res(own[0].get("want")), price)
                                         - _value(_res(own[-1].get("want")), price), 2))
    for s, t in per.items():
        if rounds.get(s):
            t["chains"] = len(rounds[s])
            t["chain_rounds"] = round(sum(rounds[s]) / len(rounds[s]), 2)
        if concession.get(s):
            t["concession"] = round(sum(concession[s]) / len(concession[s]), 2)


def _provinces(transfers: list[dict[str, Any]], captures: list[dict[str, Any]], failed: list[dict[str, Any]],
               offers: dict[str, dict[str, Any]], settled: list[tuple[dict[str, Any], dict[str, Any]]],
               price: dict[str, float], per: dict[str, dict[str, Any]]) -> dict[str, Any]:
    paid = {}
    for e, offer in settled:
        give, want = e.get("give") or {}, e.get("want") or {}
        if give.get("provinces"):
            paid[e["trade_id"]] = _value(_res(want), price)
        elif want.get("provinces"):
            paid[e["trade_id"]] = _value(_res(give), price)
    rows = []
    recapture = 0
    for t in transfers:
        giver, taker, pid, turn = str(t.get("from")), str(t["to"]), t["province"], int(t.get("turn") or 0)
        per.setdefault(giver, _seat())["provinces_sold"] += 1
        per.setdefault(taker, _seat())["provinces_bought"] += 1
        back = any(c.get("province") == pid and c.get("by") == giver and turn < int(c.get("turn") or 0)
                   <= turn + RECAPTURE_WINDOW for c in captures)
        recapture += back
        rows.append({"province": pid, "from": giver, "to": taker, "turn": turn, "price": paid.get(t.get("trade_id")),
                     "buyback": t.get("status") == "return", "status": t.get("status"), "recaptured": back})
    attacked = 0
    for f in failed:
        if f.get("reason") != "lost" or not f.get("province"):
            continue
        sides = set(f.get("seats") or ())
        attacked += any(c.get("province") == f["province"] and c.get("by") not in sides
                        and int(c.get("turn") or 0) == int(f.get("turn") or 0) for c in captures)
    return {"transfers": rows, "transfer_recapture": recapture, "attacked_promised": attacked}


def _copies(copies: list[dict[str, Any]], per: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_mission: Counter[str] = Counter()
    to_target = 0
    for c in copies:
        per.setdefault(str(c.get("from")), _seat())["reports_sold"] += 1
        per.setdefault(str(c.get("to")), _seat())["reports_bought"] += 1
        by_mission[_short(c.get("mission"))] += 1
        to_target += str(c.get("target")) == str(c.get("to"))
    return {"copies": len(copies), "by_mission": dict(by_mission), "sold_to_target": to_target}


def _manual_swaps(gifts: list[dict[str, Any]]) -> dict[str, Any]:
    """Gifts both ways between a pair within one turn (the barter of m8 by hand): who paid first, whether the second
    answered."""
    by_turn: dict[tuple[int, frozenset[str]], list[dict[str, Any]]] = defaultdict(list)
    for g in gifts:
        by_turn[(int(g.get("turn") or 0), frozenset((str(g["seat"]), str(g["to"]))))].append(g)
    rows = []
    for (turn, _pair), items in sorted(by_turn.items(), key=lambda kv: (kv[0][0], sorted(kv[0][1]))):
        senders = {str(g["seat"]) for g in items}
        if len(senders) < 2:
            continue
        first = min(items, key=lambda g: g.get("seq", 0))
        rows.append({"turn": turn, "first": str(first["seat"]), "second": str(first["to"]), "answered": True})
    return {"count": len(rows), "rows": rows}


def _reciprocity(flows: list[tuple[int, str, str, dict[str, int], bool, str | None, str | None]],
                 per: dict[str, dict[str, Any]], price: dict[str, float]) -> None:
    """13.3.2: a one-sided transfer A→B is repaid when any transfer B→A comes within GIFT_WINDOW turns."""
    repaid_value: dict[str, list[float]] = defaultdict(list)
    for turn, a, b, amounts, one_sided, _tid, _mark in flows:
        if not one_sided:
            continue
        per.setdefault(b, _seat())["one_sided_received"] += 1
        per.setdefault(a, _seat())["one_sided_sent"] += 1
        back = [f for f in flows if f[1] == b and f[2] == a and turn <= f[0] <= turn + GIFT_WINDOW]
        if back:
            per[b]["repaid_within_3"] += 1
            sent = _value(amounts, price)
            if sent:
                repaid_value[b].append(sum(_value(f[3], price) for f in back) / sent)
    for s, v in repaid_value.items():
        per[s]["repaid_value_ratio"] = round(sum(v) / len(v), 3)


def _kingmaking(flows: list[tuple[int, str, str, dict[str, int], bool, str | None, str | None]],
                scores: dict[int, dict[str, int]], last_turn: int, per: dict[str, dict[str, Any]],
                price: dict[str, float], given: list[tuple[int, str, str]] = ()) -> None:
    """Value a seat gave away in the last KINGMAKING_TURNS turns while last by the turn's score (the turn before's
    score; elimination is not journaled, so a seat that is out simply stops giving): resources at reference prices,
    `given` provinces (trades, releases to a seat) and report copies at the fixed KINGMAKING_*_VALUE, counted apart
    too."""
    def last(turn: int, a: str) -> bool:
        if not last_turn or turn <= last_turn - KINGMAKING_TURNS:
            return False
        board = scores.get(turn - 1) or {}
        return bool(board) and a in board and board[a] == min(board.values())

    for turn, a, _b, amounts, _one, _tid, _mark in flows:
        if last(turn, a):
            per.setdefault(a, _seat())["kingmaking"] = round(per[a]["kingmaking"] + _value(amounts, price), 2)
    for turn, a, what in given:
        if last(turn, a):
            row = per.setdefault(a, _seat())
            row[f"kingmaking_{what}"] += 1
            value = KINGMAKING_PROVINCE_VALUE if what == "provinces" else KINGMAKING_COPY_VALUE
            row["kingmaking"] = round(row["kingmaking"] + value, 2)


def _promises(promises: list[dict[str, Any]], flows: list[tuple[int, str, str, dict[str, int], bool, str | None,
                                                                    str | None]],
              transfers: list[dict[str, Any]], last_turn: int, per: dict[str, dict[str, Any]],
              out_turn: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """13.3.1: each promised part of `later` -> kept | partial | broken | void (+ early). Resources: the promiser's
    one-sided transfers to the same seat in [answered turn, by_turn] and both parts of trades marked `fulfils` this
    trade; overlapping promises of a pair take them first due, first served. Provinces: a transfer from the promiser to
    the promised seat in the window. Void: due after the last turn played, or not kept and either side was
    eliminated by the due turn (decision of 2026-11-27)."""
    rows = []
    used: dict[int, Counter[str]] = defaultdict(Counter)      # flow index -> amounts already counted
    for p in sorted(promises, key=lambda p: (int(p.get("by_turn") or 0), p.get("seq", 0))):
        giver, taker = str(p["seat"]), str(p["to"])
        start, due = int(p.get("turn") or 0), int(p.get("by_turn") or 0)
        goods = p.get("goods") or {}
        want = _res(goods)
        got: Counter[str] = Counter()
        when = 0
        for i, (turn, a, b, amounts, one_sided, tid, mark) in enumerate(flows):
            if a != giver or b != taker or not start <= turn <= due:
                continue
            if not (one_sided or mark == p.get("trade_id")) or tid == p.get("trade_id"):
                continue
            for r, n in amounts.items():
                need = want.get(r, 0) - got[r]
                take = min(need, n - used[i][r])
                if take > 0:
                    got[r] += take
                    used[i][r] += take
                    when = max(when, turn)
        provs = list(goods.get("provinces") or [])
        passed = [t for t in transfers if t.get("from") == giver and t.get("to") == taker
                  and t["province"] in provs and start <= int(t.get("turn") or 0) <= due]
        if passed:
            when = max(when, max(int(t.get("turn") or 0) for t in passed))
        need_n = len(want) + len(provs)
        full = sum(1 for r, n in want.items() if got[r] >= n) + len({t["province"] for t in passed})
        some = any(got.values()) or bool(passed)
        gone = [(out_turn or {}).get(x) for x in (giver, taker)]
        if last_turn and due > last_turn:
            status = "void"
        elif not (need_n and full == need_n) and any(g is not None and g <= due for g in gone):
            status = "void"
        elif need_n and full == need_n:
            status = "kept"
        elif some:
            status = "partial"
        else:
            status = "broken"
        row = {"trade": p.get("trade_id"), "from": giver, "to": taker, "by_turn": due, "status": status,
               "early": status == "kept" and when < due, "last_turns": bool(last_turn) and due > last_turn - LAST_TURNS}
        rows.append(row)
        t = per.setdefault(giver, _seat())
        t["promises_made"] += 1
        t[f"promises_{status}"] += 1
        t["promises_last_turns"] += row["last_turns"]
    for t in per.values():
        counted = t["promises_made"] - t["promises_void"]
        if counted:
            t["promises_kept_rate"] = round(t["promises_kept"] / counted, 3)
            t["promises_partial_rate"] = round(t["promises_partial"] / counted, 3)
    return rows


def trade_lines(rep: dict[str, Any], nick: dict[str, str]) -> list[str]:
    """Russian lines of the report (ОБМЕН)."""
    game = rep["game"]
    out = ["", "ОБМЕН (по журналу; опорные цены — доли запасов, Сплав = 1):"]
    for seat, t in sorted(rep["seats"].items()):
        if not (t["proposed"] or t["received"] or t["promises_made"] or t["one_sided_received"]):
            continue
        rate = next(iter(game["rates"].items()), None)
        terms = "—" if t["terms_of_trade"] is None else f"{t['terms_of_trade']:.2f}"
        kept = f", сдержано {t['promises_kept']}" if t["promises_made"] else ""
        out.append(f"{seat} {nick.get(seat, seat)}: обмен: предложено {t['proposed']} / принято "
                   f"{t['accepted_as_proposer']} (сам принял {t['accepted_as_answerer']} из {t['received']}) / "
                   f"сорвалось по своей нехватке {t['failed_by_own_lack']}; исполнено {t['settled']}; "
                   + (f"курс {rate[0]} {rate[1]['median']:.2f} (n={rate[1]['n']}); " if rate else "")
                   + f"условия обмена {terms}; провинции продано {t['provinces_sold']} / куплено "
                   f"{t['provinces_bought']}; донесений продано {t['reports_sold']} / куплено {t['reports_bought']}; "
                   f"обещания: {t['promises_made']}{kept}; односторонних получено {t['one_sided_received']}, "
                   f"отдано в ответ {t['repaid_within_3']}"
                   + (f"; kingmaking {t['kingmaking']:.1f}" if t["kingmaking"] else ""))
    pr = game["provinces"]
    out.append(f"партия: исполнено {game['settled']} из {game['proposed']} предложений; сорвано {game['failed'] or '—'}; "
               f"передач провинций {len(pr['transfers'])} (отбиты отдавшим {pr['transfer_recapture']}, атакованы "
               f"третьим {pr['attacked_promised']}); копий донесений {game['reports']['copies']}; ручных встречных "
               f"подношений {game['manual_swaps']['count']}; обещаний {game['promises']['made']}, сдержано "
               f"{game['promises']['kept']}, недействительных {game['promises']['void']}; ход в обещанную провинцию "
               f"отклонён {game['promised_move_rejected']}")
    return out
