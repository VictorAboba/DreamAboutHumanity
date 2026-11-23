"""FastAPI server of the game HTTP API (SLICE.md section 7).

Every answer is `{ok: true, ...}` or `{ok: false, error: {code, message}}`. Every authenticated request is
journaled as a `call` event (route, request/response size in chars, duration, status) under the turn and phase
current when it arrived; host traffic (dah-waker, marked by the `X-DAH-Host` header, plus telemetry and spend)
is flagged `host: true`. A background task calls `tick()` on every loaded game once per `tick_interval` seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from dah.game import Clock, Game, GameError
from dah.server.registry import GameRegistry
from dah.server.spectate import build_router as build_spectate_router

log = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 55
DEFAULT_WAIT_SECONDS = 45
MAX_BODY_BYTES = 64_000
# Routes only a host program (dah-waker) calls; with the `X-DAH-Host` header any call is host traffic. Host calls
# do not count as the player's first call and are kept apart from the player's calls in the report.
HOST_ROUTES = frozenset({"/v1/telemetry", "/v1/spend", "/v1/seat/dreamteam_session"})
# A player's phase-status reads: listed as `passive` in the host's `player_calls` (not an answer to a wake).
PASSIVE_ROUTES = frozenset({"/v1/phase", "/v1/wait_phase"})
HOST_HEADER = "x-dah-host"
ADMIN_HEADER = "x-dah-admin-key"
STATUS = {"UNAUTHORIZED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404, "INVALID": 400, "UNKNOWN_ID": 400,
          "TOO_LARGE": 413, "INTERNAL": 500}

Handler = Callable[[Game, str, Request, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


def _json_response(status: int, content: dict[str, Any]) -> tuple[Response, int]:
    text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return Response(text, status_code=status, media_type="application/json"), len(text)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _key_matches(given: str, expected: str | None) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _int_param(request: Request, name: str, default: int | None = None, example: str = "") -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        hint = f". Пример: {example}" if example else ""
        raise GameError("INVALID", f"параметр {name} должен быть целым числом, а не {raw[:20]!r}{hint}") from None


def _is_host(request: Request) -> bool:
    return bool(getattr(request.state, "dah_host", False))


async def _read_body(request: Request) -> bytes:
    """The request body; refuses one over MAX_BODY_BYTES without reading the rest."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise GameError("TOO_LARGE", f"тело запроса длиннее {MAX_BODY_BYTES} байт")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_body(raw: bytes) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GameError("INVALID", "тело запроса — не JSON") from None
    if not isinstance(data, dict):
        raise GameError("INVALID", "тело запроса должно быть JSON-объектом")
    return data


# ---------------------------------------------------------------- route handlers

def whoami(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.whoami(seat)


def rules(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return {"text": game.rules(request.query_params.get("topic"))}


def phase(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.phase_info(seat, host=_is_host(request))


async def wait_phase(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """A player's pending poll is visible to the host (`player_waiting` in the host's phase info)."""
    host = _is_host(request)
    after_seq = _int_param(request, "after_seq")
    max_seconds = max(0, min(MAX_WAIT_SECONDS, _int_param(request, "max_seconds", DEFAULT_WAIT_SECONDS) or 0))
    changed = await game.wait_phase(after_seq, max_seconds, player_seat=None if host else seat)
    return {"changed": changed, **game.phase_info(seat, host=host)}


def state(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    fmt = request.query_params.get("format", "text")
    if fmt == "json":
        return {"state": game.state_json(seat)}
    if fmt != "text":
        raise GameError("INVALID", f"format {fmt[:20]!r} не бывает: text (по умолчанию) или json. "
                                   "Пример: get_state(since_turn=1) или get_state(format=\"json\")")
    example = "get_state(since_turn=1) или get_state()"
    return {"text": game.state_text(seat, _int_param(request, "since_turn", example=example))}


def list_orders(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.orders(seat)


def validate_orders(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.validate(seat, body.get("orders"))


def submit_orders(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.submit(seat, body.get("orders"), body.get("mode", "append"))


def cancel_order(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.cancel(seat, request.path_params["order_id"])


def amend_order(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.amend(seat, request.path_params["order_id"], body)


def estimate(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.estimate(body, seat)


def _first(body: dict[str, Any], *names: str) -> Any:
    """The first of several accepted spellings of one argument (prefs / factions / faction ...)."""
    return next((body[n] for n in names if body.get(n) is not None), None)


def get_setup(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.get_setup(seat)


def choose_faction(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.choose_faction(seat, _first(body, "prefs", "factions", "faction"))


def choose_artifacts(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.choose_artifacts(seat, _first(body, "ids", "artifacts", "artifact"))


def list_techs(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.list_techs(seat)


def describe(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    q = request.query_params
    ref = next((q[n] for n in ("id", "ref", "name") if q.get(n)), None)
    return game.describe(seat, ref)


def send_message(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.send_message(seat, body.get("to"), body.get("text"))


def inbox(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.inbox(seat, _int_param(request, "since"))


def send_gift(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.send_gift(seat, body.get("to"), body.get("resources"))


def list_pacts(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.pacts(seat)


def propose_pact(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.propose_pact(seat, body.get("to"), body.get("turns"))


def respond_pact(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    decision = body.get("decision", body.get("accept"))
    return game.respond_pact(seat, request.path_params["pact_id"], decision)


def leave_pact(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.leave_pact(seat, request.path_params["pact_id"])


def ready(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.set_ready(seat, body.get("confirm_hash"))


def unready(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.unready(seat)


def history(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return game.history(seat, _int_param(request, "turn", example="history(turn=1) или history()"))


def telemetry(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    game.telemetry(seat, body.get("kind"), body.get("detail"))
    return {}


def spend(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """`source: host` also needs the admin key in the `X-DAH-Admin-Key` header."""
    host_allowed = _key_matches(request.headers.get(ADMIN_HEADER, ""), request.app.state.admin_key)
    game.spend(seat, body.get("spent_usd"), body.get("source"), body.get("tokens"), turn=body.get("turn"),
               phase=body.get("phase"), host_allowed=host_allowed)
    return {}


def dreamteam_session(game: Game, seat: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """dah-waker reports its `--dt-session` so the seat's link to the DreamTeam session is journaled."""
    return game.set_dreamteam_session(seat, body.get("dreamteam_session_id"), source="waker")


SEAT_ROUTES: list[tuple[str, str, Handler]] = [
    ("GET", "/v1/whoami", whoami),
    ("GET", "/v1/rules", rules),
    ("GET", "/v1/phase", phase),
    ("GET", "/v1/wait_phase", wait_phase),
    ("GET", "/v1/state", state),
    ("GET", "/v1/orders", list_orders),
    ("POST", "/v1/orders/validate", validate_orders),
    ("POST", "/v1/orders", submit_orders),
    ("DELETE", "/v1/orders/{order_id}", cancel_order),
    ("PATCH", "/v1/orders/{order_id}", amend_order),
    ("POST", "/v1/estimate", estimate),
    ("GET", "/v1/setup", get_setup),
    ("POST", "/v1/setup/faction", choose_faction),
    ("POST", "/v1/setup/artifacts", choose_artifacts),
    ("GET", "/v1/techs", list_techs),
    ("GET", "/v1/describe", describe),
    ("POST", "/v1/messages", send_message),
    ("GET", "/v1/inbox", inbox),
    ("POST", "/v1/gifts", send_gift),
    ("GET", "/v1/pacts", list_pacts),
    ("POST", "/v1/pacts", propose_pact),
    ("POST", "/v1/pacts/{pact_id}/respond", respond_pact),
    ("POST", "/v1/pacts/{pact_id}/leave", leave_pact),
    ("POST", "/v1/ready", ready),
    ("POST", "/v1/unready", unready),
    ("GET", "/v1/history", history),
    ("POST", "/v1/telemetry", telemetry),
    ("POST", "/v1/spend", spend),
    ("POST", "/v1/seat/dreamteam_session", dreamteam_session),
]


# ---------------------------------------------------------------- app

async def _run(handler: Callable[..., Any], *args: Any) -> tuple[int, dict[str, Any]]:
    try:
        result = handler(*args)
        if asyncio.iscoroutine(result):
            result = await result
        return 200, {"ok": True, **result}
    except GameError as exc:
        return STATUS.get(exc.code, 409), _error(exc.code, exc.message)
    except Exception:
        log.exception("request failed")
        return 500, _error("INTERNAL", "внутренняя ошибка сервера")


def create_app(games_dir: str, *, admin_key: str | None = None, clock: Clock = time.time,
               tick_interval: float | None = 1.0, watch_key: str | None = None) -> FastAPI:
    """`admin_key` defaults to $DAH_ADMIN_KEY; `tick_interval=None` disables the background clock (tests).
    `watch_key` ($DAH_WATCH_KEY) is a read-only key for /v1/spectate and /watch at the live level: it opens no other
    route, so the operator need not type the master admin key into a browser."""
    registry = GameRegistry(games_dir, clock)
    admin_key = admin_key if admin_key is not None else os.environ.get("DAH_ADMIN_KEY") or None
    watch_key = watch_key if watch_key is not None else os.environ.get("DAH_WATCH_KEY") or None

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_ticker(registry, tick_interval)) if tick_interval else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # No /docs, /redoc or /openapi.json: every route but the /watch page shell needs a key.
    app = FastAPI(title="DreamAboutHumanity", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
    app.state.admin_key = admin_key
    app.state.spectate_cache = {}
    app.state.watch_key = watch_key
    # Spectator API (/v1/spectate/*) and the /watch page: admin or spectator key only, never a seat key.
    app.include_router(build_spectate_router())

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = "NOT_FOUND" if exc.status_code == 404 else f"HTTP_{exc.status_code}"
        return _json_response(exc.status_code, _error(code, str(exc.detail)))[0]

    def seat_endpoint(route: str, handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            started = time.perf_counter()
            auth = registry.authenticate(_bearer(request))
            if auth is None:
                return _json_response(401, _error("UNAUTHORIZED", "нужен заголовок Authorization: Bearer <ключ>"))[0]
            game, seat = auth
            # The phase the call started in: a phase that the deadline (tick) or this very call (its `ready`)
            # closes is not the phase the player has seen, so the call is not activity in the new one.
            seq_at_arrival = game.phase_seq
            game.tick()
            turn, phase_now = game.state.turn, game.phase
            host = HOST_HEADER in request.headers or route.split(" ", 1)[1] in HOST_ROUTES
            request.state.dah_host = host
            if not host:
                game.note_first_call(seat)
                game.note_player_activity(seat, seq_at_arrival, arrival=True,
                                          passive=route.split(" ", 1)[1] in PASSIVE_ROUTES)
            raw = b""
            try:
                raw = await _read_body(request)
                body = _parse_body(raw)
            except GameError as exc:
                status, content = STATUS.get(exc.code, 400), _error(exc.code, exc.message)
            else:
                status, content = await _run(handler, game, seat, request, body)
            if not host:
                game.note_player_activity(seat, seq_at_arrival)
            response, resp_chars = _json_response(status, content)
            req_chars = len(raw.decode("utf-8", errors="replace")) + len(request.url.query)
            game.record_call(seat, route, req_chars, resp_chars, (time.perf_counter() - started) * 1000, status,
                             turn=turn, phase=phase_now, host=host)
            return response
        return endpoint

    for method, path, handler in SEAT_ROUTES:
        app.add_api_route(path, seat_endpoint(f"{method} {path}", handler), methods=[method])

    @app.get("/v1/admin/games/{game_id}")
    async def admin_game(game_id: str, request: Request) -> Response:
        started = time.perf_counter()
        if not _key_matches(_bearer(request), admin_key):
            return _json_response(403, _error("FORBIDDEN", "нужен админ-ключ"))[0]
        registry.refresh()
        game = registry.games.get(game_id)
        if game is None:
            return _json_response(404, _error("NOT_FOUND", f"нет партии {game_id!r}"))[0]
        game.tick()
        turn, phase_now = game.state.turn, game.phase
        response, resp_chars = _json_response(200, {"ok": True, **game.admin_status()})
        game.record_call("admin", "GET /v1/admin/games/{id}", len(request.url.query), resp_chars,
                         (time.perf_counter() - started) * 1000, 200, turn=turn, phase=phase_now, host=True)
        return response

    return app


async def _ticker(registry: GameRegistry, interval: float) -> None:
    while True:
        registry.tick_all()
        await asyncio.sleep(interval)
