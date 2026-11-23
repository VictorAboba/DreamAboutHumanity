"""Game: phases with an injectable clock, ready/confirm_hash, orders, messages, journal (SLICE.md section 5).

A Game wraps the pure engine and owns everything time- and seat-related. Every mutation is journaled and the
live snapshot is written to `private/runtime.json`, so `Game.load` restores a game after a server restart.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from dah.engine import (DeadlineInfo, GameState, Ruleset, SeatOrders, TurnReport, amend_order, build_ruleset,
                        cancel_order, cp_left, estimate, issue_order_id, new_game_state, orders_hash,
                        render_report_text, render_state_text, reserved_resources, resolve, rules_text, scores,
                        state_hash, submit_orders, view_state)
from dah.engine.model import canonical_json
from dah.engine.view import PHASE_LABELS
from dah.journal import Journal, read_events
from dah.store import GameDir, write_json, read_json

Clock = Callable[[], float]

PLAYABLE_PHASES = ("negotiation_1", "actions", "negotiation_2")
NEGOTIATION_PHASES = ("negotiation_1", "negotiation_2")
P2 = "negotiation_2"
GAME_OVER = "game_over"
NEXT_PHASE = {"negotiation_1": "actions", "actions": "negotiation_2"}
SEAT_KINDS = ("swarm", "agent", "human", "bot")
TELEMETRY_KINDS = ("continue_sent", "session_restarted", "goal_reopened", "budget_exhausted", "wake_failed")
SPEND_SOURCES = ("participant_program", "host")
MAX_TELEMETRY_DETAIL_CHARS = 2000

READ_TOOLS = ["get_state", "history", "inbox"]
ORDER_TOOLS = ["list_orders", "validate_orders", "submit_orders", "cancel_order", "amend_order", "estimate"]
ALLOWED = {
    "negotiation_1": ["get_state", *ORDER_TOOLS, "send_message", "inbox", "ready", "unready", "history"],
    "actions": ["get_state", *ORDER_TOOLS, "inbox", "ready", "unready", "history"],
    "negotiation_2": ["get_state", *ORDER_TOOLS, "send_message", "inbox", "ready", "unready", "history"],
}

RULES_TOPICS = {
    "turn": "ХОД", "phases": "ХОД", "resources": "РЕСУРСЫ", "provinces": "ПРОВИНЦИИ", "units": "ЮНИТЫ",
    "buildings": "ЗДАНИЯ", "orders": "ПРИКАЗЫ", "p2": "П2", "cp": "П2", "combat": "РАСЧЁТ",
    "resolution": "РАСЧЁТ", "victory": "ПОБЕДА", "fog": "ВИДИМОСТЬ", "visibility": "ВИДИМОСТЬ",
    "messages": "СООБЩЕНИЯ",
}

# A `DAH` token (Latin or Cyrillic А/Н, spaces inside) in any brackets would pass for a host wake-up message.
# It is matched after NFKC normalization (fullwidth letters) with format characters (zero-width) removed.
_DAH_RE = re.compile(r"(?<!\w)D\s*[AА]\s*[HН](?!\w)", re.IGNORECASE)
_BRACKET_CATEGORIES = frozenset({"Ps", "Pe", "Pi", "Pf"})


class GameError(Exception):
    """A refused request: `code` is a stable reason id, `message` is Russian text for the player."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- config and ruleset

def make_config(*, game_id: str, scenario: str, seats: list[dict[str, Any]], seed: int,
                phase_limits: dict[str, int] | None = None, turn_limit: int | None = None,
                pack_dirs: list[str] | None = None) -> dict[str, Any]:
    """Game config (SLICE.md section 5). Limits default to the scenario's. Seats: {seat, nick, kind, bot?,
    declared?, dreamteam_session_id?, faction?}."""
    rs = build_ruleset(pack_dirs, scenario)
    limits = dict(rs.scenario.phase_limits)
    limits.update(phase_limits or {})
    normalized = []
    for s in seats:
        if s.get("kind") not in SEAT_KINDS:
            raise ValueError(f"seat {s.get('seat')!r}: kind must be one of {', '.join(SEAT_KINDS)}")
        if (s["kind"] == "bot") != bool(s.get("bot")):
            raise ValueError(f"seat {s['seat']!r}: a bot type is required exactly for kind 'bot'")
        entry = {"seat": s["seat"], "nick": s["nick"], "kind": s["kind"], "declared": dict(s.get("declared") or {}),
                 "dreamteam_session_id": s.get("dreamteam_session_id")}
        for optional in ("bot", "faction"):
            if s.get(optional):
                entry[optional] = s[optional]
        normalized.append(entry)
    return {"game_id": game_id, "seed": seed, "scenario": rs.scenario.id, "pack_dirs": pack_dirs,
            "seats": normalized, "phase_limits": limits, "message_limits": rs.scenario.message_limits,
            "turn_limit": turn_limit or rs.scenario.turn_limit}


def game_ruleset(config: dict[str, Any]) -> Ruleset:
    """The scenario's ruleset with this game's turn and phase limits (so rules text matches the game)."""
    rs = build_ruleset(config.get("pack_dirs"), config["scenario"])
    scenario = replace(rs.scenario, turn_limit=config["turn_limit"], phase_limits=dict(config["phase_limits"]),
                       message_limits=config["message_limits"])
    return replace(rs, scenario=scenario)


def _seat_specs(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"seat": s["seat"], "nick": s["nick"], "faction": s.get("faction")} for s in config["seats"]]


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def escape_untrusted(text: str) -> str:
    """Neutralize wrapper markers and `[DAH]`-like tags inside a player's text."""
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")
    text = text.replace("<<", "‹‹").replace(">>", "››")
    out, pos = [], 0
    for match in _DAH_RE.finditer(text):
        start, end = max(pos, _bracket_edge(text, match.start(), -1)), _bracket_edge(text, match.end(), 1)
        out += [text[pos:start], "(DAH?)"]
        pos = end
    return "".join(out) + text[pos:]


def _bracket_edge(text: str, index: int, step: int) -> int:
    """Extend a tag boundary over spaces and one bracket-like character next to it (if there is one)."""
    i = index if step > 0 else index - 1
    while 0 <= i < len(text) and text[i].isspace():
        i += step
    if 0 <= i < len(text) and (unicodedata.category(text[i]) in _BRACKET_CATEGORIES or text[i] in "<>"):
        return i + 1 if step > 0 else i
    return index


def wrap_untrusted(nick: str, text: str) -> str:
    return f"<<untrusted from={escape_untrusted(nick)}>>{escape_untrusted(text)}<</untrusted>>"


def _rule_sections(text: str) -> list[str]:
    """Split rules text into sections; a section starts at a line whose first word is upper-case (ЮНИТЫ, П2...)."""
    sections: list[list[str]] = []
    for line in text.splitlines():
        first = line.split(maxsplit=1)[0].rstrip(":") if line.strip() else ""
        if len(first) >= 2 and first.isupper() or not sections:
            sections.append([])
        sections[-1].append(line)
    return ["\n".join(s).strip() for s in sections]


def _resolve_future(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)


# ---------------------------------------------------------------- game

class Game:
    def __init__(self, gdir: GameDir, config: dict[str, Any], ruleset: Ruleset, journal: Journal, clock: Clock,
                 state: GameState) -> None:
        self.gdir = gdir
        self.config = config
        self.ruleset = ruleset
        self.journal = journal
        self.clock = clock
        self.state = state
        self.books: dict[str, SeatOrders] = {}
        self.ready_seats: set[str] = set()
        self.phase_seq = 0
        self.phase_opened_at = clock()
        self.deadline: float | None = None
        self.first_called: set[str] = set()
        self.msg_counts: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.seats = {s["seat"]: s for s in config["seats"]}
        self.nick_to_seat = {s["nick"]: s["seat"] for s in config["seats"]}
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        self._waiters_lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------------------------------------

    @classmethod
    def create(cls, gdir: GameDir, clock: Clock = time.time, *, start: bool = True) -> Game:
        """Set up a game in an initialized dir (see store.init_game_dir): journal, turn-1 snapshot.

        With `start`, П1 of turn 1 opens at once; otherwise it opens on `start()` (the server calls it when it
        first loads the game, so operator setup time does not count against the phase).
        """
        config = gdir.load_config()
        rs = game_ruleset(config)
        state = new_game_state(rs, _seat_specs(config), config["seed"])
        game = cls(gdir, config, rs, Journal(gdir.journal_path, clock), clock, state)
        game.journal.append("game_created", config=config, ruleset_hash=rs.hash, seed=config["seed"],
                            rules_chars=len(game.rules()))
        for s in config["seats"]:
            game.journal.append("seat_info", seat=s["seat"], kind=s["kind"], declared=s["declared"],
                                dreamteam_session_id=s.get("dreamteam_session_id"))
        gdir.save_turn("states", 1, state.to_dict())
        if start:
            game.start()
        else:
            game._save()
        return game

    @property
    def started(self) -> bool:
        return self.phase_seq > 0

    def start(self) -> None:
        """Open П1 of turn 1 (no-op once the game has started)."""
        if not self.started:
            self._open_phase()

    @classmethod
    def load(cls, gdir: GameDir, clock: Clock = time.time) -> Game:
        """Restore a running (or finished) game from `private/runtime.json` and the message log."""
        config = gdir.load_config()
        rt = read_json(gdir.runtime_path)
        game = cls(gdir, config, game_ruleset(config), Journal(gdir.journal_path, clock), clock,
                   GameState.from_dict(rt["state"]))
        game.books = {s: SeatOrders.from_dict(b) for s, b in rt["books"].items()}
        game.ready_seats = set(rt["ready"])
        game.phase_seq = rt["phase_seq"]
        game.phase_opened_at = rt["phase_opened_at"]
        game.deadline = rt["deadline"]
        game.first_called = set(rt["first_called"])
        game.msg_counts = rt["msg_counts"]
        game.messages = gdir.load_messages()
        return game

    def _save(self) -> None:
        write_json(self.gdir.runtime_path, {
            "state": self.state.to_dict(), "books": {s: b.to_dict() for s, b in self.books.items()},
            "ready": sorted(self.ready_seats), "phase_seq": self.phase_seq, "phase_opened_at": self.phase_opened_at,
            "deadline": self.deadline, "first_called": sorted(self.first_called), "msg_counts": self.msg_counts,
        })

    # --- phases ---------------------------------------------------------------------------------------

    @property
    def phase(self) -> str:
        return self.state.phase

    @property
    def game_over(self) -> bool:
        return self.state.phase == GAME_OVER

    def alive_seats(self) -> list[str]:
        return [s for s, p in self.state.players.items() if p.alive]

    def seconds_left(self) -> int | None:
        if self.deadline is None:
            return None
        return max(0, int(round(self.deadline - self.clock())))

    def tick(self) -> bool:
        """Close the current phase if its deadline has passed. True if a phase changed."""
        if self.phase in PLAYABLE_PHASES and self.deadline is not None and self.clock() >= self.deadline:
            self._close_phase("deadline")
            return True
        return False

    def _open_phase(self) -> None:
        now = self.clock()
        self.phase_seq += 1
        self.ready_seats.clear()
        self.first_called.clear()
        self.msg_counts = {}
        self.phase_opened_at = now
        self.deadline = now + self.config["phase_limits"][self.phase]
        self.journal.append("phase_open", turn=self.state.turn, phase=self.phase, deadline=iso_time(self.deadline))
        self._save()
        self._notify()

    def _close_phase(self, reason: str) -> None:
        phase = self.phase
        self.journal.append("phase_close", turn=self.state.turn, phase=phase, reason=reason)
        if phase == "actions":
            self.journal.append("recon", turn=self.state.turn)  # scouting step: empty in the slice
        if phase == P2:
            self._resolve_turn()
        else:
            self.state.phase = NEXT_PHASE[phase]
            self._open_phase()

    def _resolve_turn(self) -> None:
        turn = self.state.turn
        self.state.phase = "resolution"
        books = {seat: self.books.get(seat, SeatOrders()) for seat in self.alive_seats()}
        self.gdir.save_turn("orders", turn, {s: b.to_dict() for s, b in books.items()})
        new_state, report = resolve(self.state, books, self.ruleset, self.config["seed"])
        self.gdir.save_turn("reports", turn, report.to_dict())
        self.journal.append(
            "turn_resolved", turn=turn, state_hash=state_hash(new_state), scores=report.public["scores"],
            idle_resources={s: v["resources"] for s, v in report.idle.items()},
            idle_units={s: v["units"] for s, v in report.idle.items()},
            autopilot=sorted(s for s, b in books.items() if not b.orders))
        self.state = new_state
        self.books = {}
        if new_state.phase == GAME_OVER:
            result = new_state.result or {}
            self.journal.append("game_over", winners=result.get("winners", []), reason=result.get("reason"),
                                scores=result.get("scores", {}), turn=turn)
            self.phase_seq += 1
            self.ready_seats.clear()
            self.deadline = None
            self.phase_opened_at = self.clock()
            self._save()
            self._notify()
        else:
            self.gdir.save_turn("states", new_state.turn, new_state.to_dict())
            self._open_phase()

    def _notify(self) -> None:
        with self._waiters_lock:
            waiters, self._waiters = self._waiters, []
        for loop, fut in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_resolve_future, fut)

    async def wait_phase(self, after_seq: int | None, max_seconds: float) -> bool:
        """Wait until `phase_seq` differs from `after_seq` (or the timeout). True if it changed."""
        if after_seq is None or after_seq != self.phase_seq:
            return True
        loop = asyncio.get_running_loop()
        entry = (loop, loop.create_future())
        with self._waiters_lock:
            self._waiters.append(entry)
        try:
            await asyncio.wait_for(entry[1], timeout=max_seconds)
        except TimeoutError:
            pass
        finally:
            with self._waiters_lock:
                if entry in self._waiters:
                    self._waiters.remove(entry)
        return self.phase_seq != after_seq

    def phase_info(self, seat: str) -> dict[str, Any]:
        alive = self.alive_seats()
        ready = f"{len(self.ready_seats & set(alive))}/{len(alive)}"
        seat_alive = self.state.players[seat].alive
        info: dict[str, Any] = {
            "game_id": self.config["game_id"], "turn": self.state.turn, "turn_limit": self.config["turn_limit"],
            "phase": self.phase, "deadline": iso_time(self.deadline) if self.deadline is not None else None,
            "seconds_left": self.seconds_left(), "ready": ready, "you_ready": seat in self.ready_seats,
            "phase_seq": self.phase_seq,
            "allowed": ALLOWED.get(self.phase, READ_TOOLS) if seat_alive else READ_TOOLS,
            "alive": seat_alive, "game_over": self.game_over,
        }
        if self.game_over:
            info["result"] = self.state.result
        info["text"] = self._phase_text(info)
        return info

    def _phase_text(self, info: dict[str, Any]) -> str:
        parts = [f"Ход {info['turn']}/{info['turn_limit']}", PHASE_LABELS.get(self.phase, self.phase)]
        if self.game_over:
            winners = ", ".join(self.state.players[w].nick for w in (self.state.result or {}).get("winners", []))
            return " | ".join(parts + [f"победители: {winners or 'никто'}", "game_over"])
        if self.deadline is not None:
            parts.append(f"до {datetime.fromtimestamp(self.deadline, timezone.utc):%H:%MZ} "
                         f"(ещё {info['seconds_left']} с)")
        parts.append(f"готово {info['ready']}" + (", ты готов" if info["you_ready"] else ", ты не готов"))
        return " | ".join(parts)

    # --- reading --------------------------------------------------------------------------------------

    def whoami(self, seat: str) -> dict[str, Any]:
        player = self.state.players[seat]
        return {"game_id": self.config["game_id"], "seat": seat, "nick": player.nick, "faction": player.faction,
                "kind": self.seats[seat]["kind"], "alive": player.alive, "turn": self.state.turn,
                "phase": self.phase}

    def rules(self, topic: str | None = None) -> str:
        text = rules_text(self.ruleset) + "\n\n" + self._messages_rules()
        if not topic:
            return text
        key = RULES_TOPICS.get(topic.strip().lower(), topic.strip()).upper()
        found = [s for s in _rule_sections(text) if s.split(maxsplit=1)[0].rstrip(":").upper() == key]
        if not found:
            topics = ", ".join(sorted(RULES_TOPICS))
            return f"Тема {topic!r} не найдена (темы: {topics}); ниже правила целиком.\n\n{text}"
        return "\n".join(found)

    def _messages_rules(self) -> str:
        lim = self.config["message_limits"]
        parts = []
        for phase, label in (("negotiation_1", "П1"), ("negotiation_2", "П2")):
            p = lim.get(phase, {})
            per = f", ≤{p['per_recipient']} одному адресату" if p.get("per_recipient") else ""
            parts.append(f"{label}: ≤{p.get('private', 0)} личных{per}, ≤{p.get('public', 0)} публичных")
        return (f"СООБЩЕНИЯ: только в фазах переговоров; to — список ников или \"all\"; "
                f"{'; '.join(parts)}; ≤{lim.get('max_chars', 800)} символов. Личное сообщение нескольким адресатам "
                "считается по одному на каждого.")

    def state_text(self, seat: str, since_turn: int | None = None) -> str:
        info = None
        if self.deadline is not None:
            hhmm = f"{datetime.fromtimestamp(self.deadline, timezone.utc):%H:%MZ}"
            info = DeadlineInfo(hhmm, self.seconds_left() or 0)
        since_state, reports = None, []
        if since_turn is not None:
            if not 1 <= since_turn <= self.state.turn:
                raise GameError("INVALID", f"since_turn должен быть от 1 до {self.state.turn}")
            snapshot = self.gdir.load_turn("states", since_turn)
            since_state = GameState.from_dict(snapshot) if snapshot else None
            reports = [TurnReport.from_dict(r) for t in range(since_turn, self.state.turn + 1)
                       if (r := self.gdir.load_turn("reports", t)) is not None]
        return render_state_text(self.state, self.ruleset, seat, info, since_turn, since_state, reports,
                                 self._book(seat))

    def state_json(self, seat: str) -> dict[str, Any]:
        return view_state(self.state, self.ruleset, seat, self._book(seat))

    def history(self, seat: str, turn: int | None = None) -> dict[str, Any]:
        last = self.state.turn if self.game_over else self.state.turn - 1
        turn = last if turn is None else turn
        raw = self.gdir.load_turn("reports", turn) if 1 <= turn <= last else None
        if raw is None:
            raise GameError("NOT_FOUND", f"отчёта хода {turn} нет (рассчитаны ходы 1..{last})"
                            if last >= 1 else "ещё ни один ход не рассчитан")
        report = TurnReport.from_dict(raw)
        return {"turn": turn, "text": render_report_text(report, self.ruleset, self.state, seat)}

    # --- orders ---------------------------------------------------------------------------------------

    def _book(self, seat: str) -> SeatOrders:
        return self.books.get(seat) or SeatOrders()

    def orders(self, seat: str) -> dict[str, Any]:
        book = self._book(seat)
        player = self.state.players[seat]
        lines = [self._order_line(o.to_dict()) for o in book.orders] or ["приказов нет (армии стоят)"]
        return {"orders": [o.to_dict() for o in book.orders], "orders_hash": orders_hash(book),
                "cp_left": cp_left(self.state, seat, book),
                "reserved": reserved_resources(self.ruleset, player.faction, book), "text": "\n".join(lines)}

    def _order_line(self, o: dict[str, Any]) -> str:
        short = self.ruleset.short
        hasty = " [П2, наспех]" if o["phase_added"] == P2 and o["kind"] == "move" else ""
        if o["kind"] == "move":
            units = " ".join(f"{short(u)}×{n}" for u, n in o["units"].items())
            return f"{o['id']} move {o['from']}→{o['to']} {units}{hasty}"
        if o["kind"] == "recruit":
            return f"{o['id']} recruit {o['province']} {short(o['unit'])}×{o['count']}"
        if o["kind"] == "build":
            return f"{o['id']} build {o['province']} {short(o['building'])}"
        return f"{o['id']} fortify {o['province']}"

    def _orders_changed(self, seat: str) -> None:
        if seat in self.ready_seats:
            self.ready_seats.discard(seat)
            self.journal.append("ready_cleared", seat=seat, turn=self.state.turn, phase=self.phase)

    @staticmethod
    def _items(items: Any) -> list[Any]:
        if not isinstance(items, list):
            raise GameError("INVALID", "orders должен быть списком приказов")
        return items

    def validate(self, seat: str, items: Any) -> dict[str, Any]:
        items = self._items(items)
        res = submit_orders(self.state, self.ruleset, seat, self._book(seat), items, self.phase)
        results = []
        for check in res.results:
            entry = check.to_dict()
            entry.pop("order", None)  # dry-run ids are meaningless to the player
            results.append(entry)
        rejected = [{"order": items[i], "reason": c.reason} for i, c in enumerate(res.results) if not c.ok]
        self.journal.append("orders_validated", seat=seat, ok_count=len(items) - len(rejected), rejected=rejected)
        return {"results": results, "ok_count": len(items) - len(rejected)}

    def submit(self, seat: str, items: Any, mode: str = "append") -> dict[str, Any]:
        items = self._items(items)
        book = self._book(seat)
        phase = self.phase
        res = submit_orders(self.state, self.ruleset, seat, book, items, phase, mode,
                            id_factory=lambda: issue_order_id(self.state, seat))
        accepted = [c for c in res.results if c.ok and c.order is not None]
        rejected = [{"index": i, "order": items[i], "reason": c.reason, "message": c.message}
                    for i, c in enumerate(res.results) if not c.ok]
        if orders_hash(res.book) != orders_hash(book) or res.book.cp_spent != book.cp_spent:
            self.books[seat] = res.book
            self._orders_changed(seat)
        if phase == P2:
            for c in accepted:
                self.journal.append("order_edit_p2", seat=seat, op="add", order_id=c.order.id, cp_cost=c.cp_cost)
        new_hash = orders_hash(self._book(seat))
        self.journal.append("orders_submitted", seat=seat, accepted=[c.order.id for c in accepted],
                            rejected=[{"order": r["order"], "reason": r["reason"]} for r in rejected],
                            orders_hash=new_hash, phase=phase, mode=mode)
        self._save()
        return {"accepted": [c.order.to_dict() for c in accepted], "rejected": rejected, "orders_hash": new_hash,
                "cp_left": cp_left(self.state, seat, self._book(seat))}

    def cancel(self, seat: str, order_id: str) -> dict[str, Any]:
        res = cancel_order(self.state, self.ruleset, seat, self._book(seat), order_id, self.phase)
        return self._apply_edit(seat, "cancel", order_id, res)

    def amend(self, seat: str, order_id: str, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise GameError("INVALID", "patch должен быть объектом {units?, to?, count?}")
        res = amend_order(self.state, self.ruleset, seat, self._book(seat), order_id, patch, self.phase)
        return self._apply_edit(seat, "amend", order_id, res)

    def _apply_edit(self, seat: str, op: str, order_id: str, res: Any) -> dict[str, Any]:
        if not res.ok:
            raise GameError(res.reason or "INVALID", res.message)
        phase = self.phase
        self.books[seat] = res.book
        self._orders_changed(seat)
        edit_type = "order_edit_p2" if phase == P2 else "order_edit"
        self.journal.append(edit_type, seat=seat, op=op, order_id=order_id, cp_cost=res.cp_cost,
                            turn=self.state.turn, phase=phase)
        self._save()
        out = {"order_id": order_id, "cp_cost": res.cp_cost, "orders_hash": orders_hash(res.book),
               "cp_left": cp_left(self.state, seat, res.book)}
        if res.order is not None:
            out["order"] = res.order.to_dict()
        return out

    def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        attacker, defender = body.get("attacker"), body.get("defender")
        ptype = body.get("province_type")
        if not isinstance(attacker, dict) or not isinstance(defender, dict) or not isinstance(ptype, str):
            raise GameError("INVALID", "нужно {attacker:{юнит:n}, defender:{юнит:n}, province_type, fortified, hasty}")
        try:
            out = estimate(self.ruleset, attacker, defender, ptype, bool(body.get("fortified", False)),
                           bool(body.get("hasty", False)))
        except KeyError as exc:
            raise GameError("UNKNOWN_ID", str(exc.args[0]) if exc.args else "неизвестный id") from None
        except (TypeError, ValueError):
            raise GameError("INVALID", "число юнитов должно быть целым") from None
        out["losses"] = {"attacker": out["attacker_losses_avg"], "defender": out["defender_losses_avg"]}
        return out

    # --- ready ----------------------------------------------------------------------------------------

    def _require_active(self, seat: str) -> None:
        if self.phase not in PLAYABLE_PHASES:
            raise GameError("PHASE_LOCKED", "сейчас нет открытой фазы" if not self.game_over else "игра окончена")
        if not self.state.players[seat].alive:
            raise GameError("PHASE_LOCKED", "место выбыло из игры")

    def ready_text(self) -> str:
        alive = self.alive_seats()
        return f"{len(self.ready_seats & set(alive))}/{len(alive)}"

    def set_ready(self, seat: str, confirm_hash: Any) -> dict[str, Any]:
        self._require_active(seat)
        current = orders_hash(self._book(seat))
        if confirm_hash != current:
            raise GameError("STALE_HASH", f"набор приказов не совпадает с confirm_hash; актуальный orders_hash: "
                                          f"{current}")
        closed = False
        if seat not in self.ready_seats:
            self.ready_seats.add(seat)
            self.journal.append("ready", seat=seat, turn=self.state.turn, phase=self.phase)
            ready = self.ready_text()
            if set(self.alive_seats()) <= self.ready_seats:
                self._close_phase("all_ready")
                closed = True
            else:
                self._save()
        else:
            ready = self.ready_text()
        return {"ready": ready, "phase_closed": closed}

    def unready(self, seat: str) -> dict[str, Any]:
        self._require_active(seat)
        if seat in self.ready_seats:
            self.ready_seats.discard(seat)
            self.journal.append("unready", seat=seat, turn=self.state.turn, phase=self.phase)
            self._save()
        return {"ready": self.ready_text()}

    # --- messages -------------------------------------------------------------------------------------

    def _limits(self) -> dict[str, Any]:
        return self.config["message_limits"].get(self.phase, {})

    def _counts(self, seat: str) -> dict[str, Any]:
        return self.msg_counts.setdefault(seat, {"private": 0, "public": 0, "to": {}})

    def messages_left(self, seat: str) -> dict[str, Any]:
        lim, used = self._limits(), self._counts(seat)
        left: dict[str, Any] = {"private": max(0, lim.get("private", 0) - used["private"]),
                                "public": max(0, lim.get("public", 0) - used["public"])}
        if lim.get("per_recipient"):
            left["per_recipient"] = {self.state.players[s].nick: max(0, lim["per_recipient"] - used["to"].get(s, 0))
                                     for s in self.state.players if s != seat}
        return left

    def send_message(self, seat: str, to: Any, text: Any) -> dict[str, Any]:
        self._require_active(seat)
        if self.phase not in NEGOTIATION_PHASES:
            raise GameError("PHASE_LOCKED", "сообщения — только в фазах переговоров (П1, П2)")
        max_chars = self.config["message_limits"].get("max_chars", 800)
        if not isinstance(text, str) or not text.strip():
            raise GameError("INVALID", "text — непустая строка")
        if len(text) > max_chars:
            raise GameError("TOO_LONG", f"сообщение длиннее {max_chars} символов ({len(text)})")
        recipients = self._recipients(seat, to)
        lim, used = self._limits(), self._counts(seat)
        if recipients is None:
            if used["public"] + 1 > lim.get("public", 0):
                raise GameError("LIMIT_EXCEEDED", f"лимит публичных сообщений в фазе: {lim.get('public', 0)}")
        else:
            if used["private"] + len(recipients) > lim.get("private", 0):
                raise GameError("LIMIT_EXCEEDED", f"лимит личных сообщений в фазе: {lim.get('private', 0)}, "
                                                  f"осталось {lim.get('private', 0) - used['private']}")
            per = lim.get("per_recipient")
            full = [r for r in recipients if per and used["to"].get(r, 0) + 1 > per]
            if full:
                nicks = ", ".join(self.state.players[r].nick for r in full)
                raise GameError("LIMIT_EXCEEDED", f"лимит {per} сообщений одному адресату исчерпан: {nicks}")
        if recipients is None:
            used["public"] += 1
        else:
            used["private"] += len(recipients)
            for r in recipients:
                used["to"][r] = used["to"].get(r, 0) + 1
        record = {"id": len(self.messages) + 1, "ts": round(self.clock(), 3), "turn": self.state.turn,
                  "phase": self.phase, "from": seat, "to": "all" if recipients is None else recipients, "text": text}
        self.gdir.append_message(record)
        self.messages.append(record)
        self.journal.append("message", **{"from": seat, "to": record["to"], "chars": len(text),
                                          "phase": self.phase, "turn": self.state.turn})
        self._save()
        sent = sum(1 for m in self.messages if m["from"] == seat)  # own count: the global id would leak traffic
        return {"message_id": sent, "left": self.messages_left(seat)}

    def _recipients(self, seat: str, to: Any) -> list[str] | None:
        if to == "all":
            return None
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list) or not to or not all(isinstance(n, str) for n in to):
            raise GameError("INVALID", "to — список ников или \"all\"")
        out: list[str] = []
        for nick in to:
            target = self.nick_to_seat.get(nick) or (nick if nick in self.seats else None)
            if target is None:
                raise GameError("UNKNOWN_ID", f"нет игрока с ником {nick!r}")
            if target == seat:
                raise GameError("INVALID", "нельзя писать самому себе")
            if target not in out:
                out.append(target)
        return out

    def inbox(self, seat: str, since: int | None = None) -> dict[str, Any]:
        """Messages addressed to the seat, numbered 1, 2, ... per recipient (`since` is that number), so the ids
        say nothing about messages between other players."""
        players = self.state.players
        received = [m for m in self.messages
                    if m["from"] != seat and (m["to"] == "all" or seat in m["to"])]
        out = []
        for number, m in enumerate(received, start=1):
            if since is not None and number <= since:
                continue
            out.append({"id": number, "turn": m["turn"], "phase": m["phase"], "from": players[m["from"]].nick,
                        "to": "all" if m["to"] == "all" else [players[s].nick for s in m["to"]],
                        "public": m["to"] == "all", "text": wrap_untrusted(players[m["from"]].nick, m["text"])})
        last_id = out[-1]["id"] if out else (since or 0)
        lines = [f"#{m['id']} ход {m['turn']} {PHASE_LABELS.get(m['phase'], m['phase'])} "
                 f"{'всем' if m['public'] else 'лично'}: {m['text']}" for m in out]
        return {"messages": out, "last_id": last_id, "text": "\n".join(lines) or "новых сообщений нет"}

    # --- host telemetry and per-call journaling --------------------------------------------------------

    def telemetry(self, seat: str, kind: Any, detail: Any) -> None:
        if kind not in TELEMETRY_KINDS:
            raise GameError("INVALID", f"kind должен быть одним из {', '.join(TELEMETRY_KINDS)}")
        if detail is not None and (not isinstance(detail, dict)
                                   or len(canonical_json(detail)) > MAX_TELEMETRY_DETAIL_CHARS):
            raise GameError("INVALID", f"detail — JSON-объект не длиннее {MAX_TELEMETRY_DETAIL_CHARS} символов")
        self.journal.append("telemetry", seat=seat, kind=kind, detail=detail, turn=self.state.turn,
                            phase=self.phase)

    def spend(self, seat: str, spent_usd: Any, source: Any, tokens: Any = None, *, turn: Any = None,
              phase: Any = None, host_allowed: bool = False) -> None:
        """Spend so far. `turn`/`phase` name the phase the figure closes (default: the current one);
        `source: host` is accepted only from the host (`host_allowed`)."""
        if isinstance(spent_usd, bool) or not isinstance(spent_usd, (int, float)) or spent_usd < 0:
            raise GameError("INVALID", "spent_usd — неотрицательное число")
        if source not in SPEND_SOURCES:
            raise GameError("INVALID", f"source должен быть одним из {', '.join(SPEND_SOURCES)}")
        if source == "host" and not host_allowed:
            raise GameError("FORBIDDEN", "source host — только с админ-ключом")
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
            raise GameError("INVALID", "tokens — неотрицательное целое")
        if turn is None and phase is None:
            turn, phase = self.state.turn, self.phase
        elif (isinstance(turn, bool) or not isinstance(turn, int) or not 1 <= turn <= self.state.turn
              or phase not in PLAYABLE_PHASES):
            raise GameError("INVALID", f"turn — номер хода 1..{self.state.turn}, phase — одна из "
                                       f"{', '.join(PLAYABLE_PHASES)}")
        fields: dict[str, Any] = {"seat": seat, "spent_usd": float(spent_usd), "source": source,
                                  "turn": turn, "phase": phase}
        if tokens is not None:
            fields["tokens"] = tokens
        self.journal.append("spend_report", **fields)

    def note_first_call(self, seat: str) -> None:
        if seat not in self.first_called and self.phase in PLAYABLE_PHASES:
            self.first_called.add(seat)
            self.journal.append("first_call_in_phase", seat=seat, turn=self.state.turn, phase=self.phase)
            self._save()

    def record_call(self, seat: str, route: str, req_chars: int, resp_chars: int, ms: float, status: int, *,
                    turn: int, phase: str, host: bool = False) -> None:
        """Journal one API call under the phase current when it arrived; `host` marks dah-waker traffic."""
        fields: dict[str, Any] = {"seat": seat, "route": route, "req_chars": req_chars, "resp_chars": resp_chars,
                                  "ms": round(ms, 1), "status": status, "turn": turn, "phase": phase}
        if host:
            fields["host"] = True
        self.journal.append("call", **fields)

    def admin_status(self) -> dict[str, Any]:
        seats = {}
        for seat, p in self.state.players.items():
            book = self._book(seat)
            seats[seat] = {**self.seats[seat], "alive": p.alive, "resources": p.resources,
                           "orders": [o.to_dict() for o in book.orders], "orders_hash": orders_hash(book),
                           "cp_left": cp_left(self.state, seat, book), "ready": seat in self.ready_seats}
        return {"game_id": self.config["game_id"], "config": self.config, "ruleset_hash": self.ruleset.hash,
                "turn": self.state.turn, "phase": self.phase, "phase_seq": self.phase_seq,
                "deadline": iso_time(self.deadline) if self.deadline is not None else None,
                "seconds_left": self.seconds_left(), "ready": self.ready_text(), "seats": seats,
                "scores": scores(self.state, self.ruleset), "result": self.state.result,
                "messages": len(self.messages), "state_hash": state_hash(self.state)}


# ---------------------------------------------------------------- replay

@dataclass(frozen=True)
class ReplayTurn:
    turn: int
    expected: str
    actual: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


@dataclass(frozen=True)
class Replay:
    """Outcome of `replay()`: ruleset hash, per-turn state hashes and the final result, journal vs re-run."""
    ruleset_expected: str | None
    ruleset_actual: str
    turns: list[ReplayTurn]
    result_expected: dict[str, Any] | None   # {winners, scores} of the journal's game_over, if any
    result_actual: dict[str, Any] | None

    @property
    def ruleset_ok(self) -> bool:
        return self.ruleset_expected == self.ruleset_actual

    @property
    def result_ok(self) -> bool:
        return self.result_expected == self.result_actual

    @property
    def ok(self) -> bool:
        return self.ruleset_ok and self.result_ok and all(t.ok for t in self.turns)


def replay(gdir: GameDir) -> Replay:
    """Rebuild the ruleset, re-run every resolved turn from the initial state and saved orders, compare the
    ruleset hash, every turn's state hash and the final result with the journal."""
    config = gdir.load_config()
    rs = game_ruleset(config)
    events = read_events(gdir.journal_path)
    created = next((e for e in events if e["type"] == "game_created"), {})
    over = next((e for e in events if e["type"] == "game_over"), None)
    state = new_game_state(rs, _seat_specs(config), config["seed"])
    expected = {e["turn"]: e["state_hash"] for e in events if e["type"] == "turn_resolved"}
    turns = []
    for turn in sorted(expected):
        raw = gdir.load_turn("orders", turn)
        if raw is None:
            raise FileNotFoundError(f"no saved orders for turn {turn}")
        state, _ = resolve(state, {s: SeatOrders.from_dict(b) for s, b in raw.items()}, rs, config["seed"])
        turns.append(ReplayTurn(turn, expected[turn], state_hash(state)))
    result_expected = {"winners": over["winners"], "scores": over["scores"]} if over else None
    result_actual = ({"winners": state.result["winners"], "scores": state.result["scores"]}
                     if state.result else None)
    return Replay(created.get("ruleset_hash"), rs.hash, turns, result_expected, result_actual)
