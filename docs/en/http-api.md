# HTTP API

**English** · [Русский](../ru/http-api.md) · [All docs](README.md)

`dah serve` runs the game server: a FastAPI application that serves every match in one games folder. Players reach
it through `dah-bridge` ([Connecting agents](connecting-agents.md)); bots, `dah-waker` and your own programs call it
directly. Section 7 of the [slice specification](../SLICE.md) (in Russian) is the contract.

## Basics

- **Authentication.** Every route needs the header `Authorization: Bearer <key>`. A seat key looks like
  `dah_<game>_<seat>_<random>`; the key itself says which match and seat it belongs to. The server keeps only the
  sha256 of keys. The admin route takes the admin key from the `DAH_ADMIN_KEY` environment variable instead.
  A spectator key (`dahw_<game>_<random>`) opens only `/watch/<id>` and `/v1/spectate/*`, never these routes; a
  seat key does not open the spectator page.
- **Answers** are JSON: `{"ok": true, ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.
  Messages are in Russian. Where an answer has a `text` field, it is ready-made text for an LLM. A refusal of
  `/v1/estimate`, `/v1/state`, `/v1/history` or `/v1/messages` says what was wrong and, where the call itself was
  wrong, ends with a correct call (`Пример: estimate(attacker={"core:assault": 4}, ...)`).
- **Request body**: a JSON object, at most 64,000 bytes (else `413 TOO_LARGE`). A body that is not a JSON object
  gives `400 INVALID`.
- **No docs routes.** `/docs`, `/redoc` and `/openapi.json` are switched off.
- **Journal.** Every authenticated request is written to the match journal as a `call` event: route, request and
  response size in characters, duration, status, and the turn and phase current when it arrived.

### Error codes

| HTTP | Codes |
|---|---|
| 400 | `INVALID` (bad parameter, order or body), `UNKNOWN_ID` |
| 401 | `UNAUTHORIZED`: no key or an unknown key |
| 403 | `FORBIDDEN`: admin key needed |
| 404 | `NOT_FOUND`: no such route, match or turn report |
| 409 | the request is well-formed but refused now (see below) |
| 413 | `TOO_LARGE` |
| 500 | `INTERNAL` |

409 codes: `PHASE_LOCKED`, `STALE_HASH`, `NOT_ENOUGH_CP`, `LIMIT_EXCEEDED`, `TOO_LONG`, and the order reasons
(`NOT_ADJACENT`, `NOT_ENOUGH_UNITS`, ...) when a cancel or an amendment is refused.

Submitting and validating orders answer 200 even when some orders are refused: the refusals are in the answer.

## Player routes

| Method | Path | Parameters / body | Answer |
|---|---|---|---|
| GET | `/v1/whoami` | | `game_id`, `seat`, `nick`, `faction`, `kind`, `alive`, `turn`, `phase` |
| GET | `/v1/rules` | `?topic=` | `text` |
| GET | `/v1/phase` | | see below |
| GET | `/v1/wait_phase` | `?after_seq=&max_seconds=45` | `changed` plus the fields of `/v1/phase` |
| GET | `/v1/state` | `?format=text\|json&since_turn=` | `text`, or `state` for `json` |
| GET | `/v1/orders` | | `orders[]`, `orders_hash`, `cp_left`, `reserved{}`, `text` |
| POST | `/v1/orders/validate` | `{"orders": [...]}` | `results[{ok, cp_cost, reason?, message?}]`, `ok_count` |
| POST | `/v1/orders` | `{"orders": [...], "mode"}` | `accepted[]`, `rejected[]`, `orders_hash`, `cp_left` |
| DELETE | `/v1/orders/{id}` | | `order_id`, `cp_cost`, `orders_hash`, `cp_left` |
| PATCH | `/v1/orders/{id}` | `{"units"?, "unit"?, "count"?, "to"?}` | the same plus the changed `order` |
| POST | `/v1/estimate` | see [Estimate](#estimate) | `win_prob`, average losses |
| POST | `/v1/messages` | `{"to": [nick, ...] \| "all", "text"}` | `message_id`, `left{}` |
| GET | `/v1/inbox` | `?since=` | `messages[]`, `last_id`, `text` |
| POST | `/v1/ready` | `{"confirm_hash"}` | `ready` ("N/M"), `phase_closed` |
| POST | `/v1/unready` | | `ready` |
| GET | `/v1/history` | `?turn=` | `turn`, `text` (before turn 1 is resolved, without `turn`: `turn: null` and a hint) |

### Phase and waiting

`/v1/phase` returns `game_id`, `turn`, `turn_limit`, `phase`, `deadline` (UTC, `2026-11-23T14:30:00Z`),
`seconds_left`, `ready` ("N/M" of living seats), `you_ready`, `phase_seq`, `allowed` (tools allowed in this phase),
`alive`, `game_over`, `text`, and `result` once the match is over.

`phase_seq` grows by one at each phase change. `/v1/wait_phase?after_seq=N` returns as soon as `phase_seq` differs
from `N`, or after `max_seconds` (default 45, at most 55) with `changed: false`. Without `after_seq` it returns at
once with `changed: true`.

`allowed` lists tool names: in N1 and N2 the state, order tools, messages, inbox, ready and history; in A the same
without `send_message`. A seat that is out, or a match that is over, gets only `get_state`, `history` and `inbox`.

For host calls (the `X-DAH-Host` header, which `dah-waker` sends), `/v1/phase` and `/v1/wait_phase` add fields
about the player of that seat: `player_waiting` (a player's long-poll of `/v1/wait_phase` is pending now, or was
pending when the current phase opened, so it returns with the new phase) and `player_active_s_ago` (seconds since
the player's last own call, if that call started in the current phase; else `null`: a call that started in the
previous phase, such as the player's `ready` that closed it, does not count), `player_first_active_s_ago`
(seconds since the player's first own call of the current phase, same rule) and `player_last_call`
(`{"phase_seq", "s_ago"}` of the player's last own call in any phase, or `null`; the waker tells by it whether a
wake was answered before the phase closed), and `player_calls`: the player's last 64 own calls, oldest first,
`[{"phase_seq", "s_ago", "passive"}]` by the time each call came in, `passive` for a phase-status read (`/v1/phase`,
`/v1/wait_phase`); the waker finds there the first call after a wake that is more than waiting. These presence
fields are saved with the match's runtime state, so a server restart keeps them. Players and spectators never get these fields, and a host call shows
only its own seat's player.

### State

`/v1/state` gives the seat's view under the fog of war ([Game rules](rules.md#fog-of-war)). With `since_turn`
between 1 and the current turn the text holds only what changed since the start of that turn; `since_turn=0` (or
none) gives the whole state; a later turn is `400 INVALID`.
`format=json` returns `state` with `turn`, `turn_limit`, `phase`, `you` (resources, net income, CP), `players`
(nick, alive, score) and `provinces` (`visible`, and for visible ones `units`, `buildings`, `fortified`).

### Orders

- `POST /v1/orders` defaults to `mode: append`. `replace_all` drops the seat's current orders first; in N2 it is
  refused with `PHASE_LOCKED`.
- `accepted` holds the accepted orders with their new ids; `rejected` holds `{index, order, reason, message}` for
  each refused one.
- `/v1/orders/validate` checks the same way and changes nothing.
- `orders` is a list of order objects, or the same list as one JSON string
  (`{"orders": "[{\"kind\":\"move\",...}]"}`); a single order object counts as a list of one, and an order that
  is itself a JSON string is decoded too.
- A move takes its units flat, `{"kind": "move", "from": "P01", "to": "P04", "unit": "assault", "count": 2}`, or
  nested, `"units": {"assault": 2}` (also a list `[{"unit": "assault", "count": 2}]` or a JSON string). Units may be
  full or short ids or state labels. The server normalizes every form to the nested one before checking and
  storing, so the journal and replay do not depend on the form. An empty or missing `units` is refused with
  `INVALID`, and the message shows what arrived and both workarounds (flat form, `orders` as a JSON string).
  A field sent as `null` counts as absent (in orders and patches); with non-empty nested `units`, a stray
  `count` is ignored; with a valid flat `unit`/`count`, nested `units` that hold no positive count are ignored.
- The `PATCH` body is the patch: `{"unit": "assault", "count": 1}`, `{"units": {...}}`, `{"to": "P03"}` or
  `{"count": 1}` for a recruit; `{"patch": <object or JSON string>}` is accepted too.
- `DELETE` and `PATCH` are free in N1 and A and cost CP in N2 ([Game rules](rules.md#n2-and-command-points)). A
  refused edit is an error answer with the reason code.
- Any change of orders takes the seat's ready back.

### Estimate

The body is `{"attacker": {unit: n}, "defender": {unit: n}, "province_type", "fortified", "hasty"}`;
`fortified` and `hasty` default to `false`. A unit may be its full id (`core:assault`), its short id (`assault`),
its label in the state text (`штурм`) or its name; a side may also be a list `[{"unit": ..., "count": n}]`.
`province_type` may be a type id (`core:agri`, `agri`), its label in the state text (`агро`, `кузн.`) or name, or
a province id (`P07`) or name, which stands for that province's type. The answer has `win_prob`,
`attacker_losses_avg`, `defender_losses_avg`, the same averages as `losses: {attacker, defender}` and the resolved
`province_type`. Unknown ids give `400 UNKNOWN_ID` with the list of valid ones; a missing or malformed field gives
`400 INVALID`; both messages end with a correct example call. The route works in any phase
([Game rules](rules.md#estimate)).

### Ready

`POST /v1/ready` needs `confirm_hash` equal to the seat's current `orders_hash`, otherwise `409 STALE_HASH` (the
message names the current hash). When the last living seat presses ready, the phase closes at once and the answer
has `phase_closed: true`.

### Messages

`POST /v1/messages` works only in N1 and N2. `to` is a nick, a list of nicks or `"all"`. `left` shows what is left
in this phase: `private`, `public` and `per_recipient` by nick. The limits are in
[Game rules](rules.md#messages).

`GET /v1/inbox` returns messages addressed to the seat. Their `id` counts only those messages (1, 2, ...); pass the
last one as `since` to get only newer ones. Each `text` is wrapped as `<<untrusted from=Nick>>…<</untrusted>>`.

## Host routes

`dah-waker` calls three routes with the seat's key:

| Method | Path | Body |
|---|---|---|
| POST | `/v1/telemetry` | `{"kind", "detail"}` |
| POST | `/v1/spend` | `{"spent_usd", "source", "tokens"?, "turn"?, "phase"?}` |
| POST | `/v1/seat/dreamteam_session` | `{"dreamteam_session_id"}` |

- `kind` of telemetry is `wake_sent`, `wake_answered`, `wake_unanswered`, `continue_sent` (earlier wakers),
  `continue_skipped`, `session_restarted`, `goal_reopened`, `budget_exhausted`, `wake_failed`,
  `session_stopped_by_waker` or `post_game_finished`; `detail` is a JSON object
  of up to 2000 characters.
- `spent_usd` is the seat's total spend so far. `turn` and `phase` name the phase the figure is for (the one that
  just closed); without them the figure is a baseline taken before any phase closed. The journal's `spend_report`
  keeps both sides: `for_turn`/`for_phase` (`null` for a baseline) and `at_turn`/`at_phase`, the phase current
  when the server received it. The report counts money by the "for" fields.
- After game_over `phase` may be `after_game_over` (`turn` optional, the last turn by default; before game_over it
  is 400 `INVALID`): the spend of the session while it wraps up. It is journaled like any figure (`for_phase:
  "after_game_over"`), counts in no turn or phase, and the report shows it apart (`spent_after_game_usd`).
- `/v1/seat/dreamteam_session` records which DreamTeam session plays the seat: it writes `dreamteam_session_id`
  into the match's `config.json` and journals a `seat_info` event with `update: true`, `source: "waker"` and the
  `previous` id. The same id again changes nothing (`changed: false`). The waker calls it once at start.
- `source` is `participant_program` (the waker) or `host`. `host` is accepted only with the admin key in the
  `X-DAH-Admin-Key` header.
- Calls of these three routes, and any call with the `X-DAH-Host` header, are host traffic: the journal marks them
  `host: true`, they do not count as the player's first call in a phase, and the report counts them apart.

## Admin route

```
GET /v1/admin/games/{id}
Authorization: Bearer <DAH_ADMIN_KEY>
```

Returns the full status of a match: config, `ruleset_hash`, turn, phase, `phase_seq`, deadline, ready count, for
each seat its resources, orders, `orders_hash`, CP left and ready flag, scores, result, number of messages and
`state_hash`. Without a matching admin key the answer is `403 FORBIDDEN`; if `DAH_ADMIN_KEY` is not set, always.

## The server

- `dah serve --games-dir games --host 127.0.0.1 --port 8800`. Each match is a folder `games/<id>/`.
- A background task closes phases on their deadlines once a second.
- New matches in the games folder are loaded when one of their keys is first used (the folder is rescanned at most
  every 2 seconds) or on an admin request. A match's first phase opens when the server loads it.
- After a restart each match is restored from `private/runtime.json` and the message log.

## Example

```sh
KEY=dah_demo_A_...
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8800/v1/phase
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"orders": [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 2}], "mode": "append"}' \
  http://127.0.0.1:8800/v1/orders
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"confirm_hash": "<orders_hash from the previous answer>"}' http://127.0.0.1:8800/v1/ready
```
