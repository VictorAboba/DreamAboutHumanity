# HTTP API

**English** · [Русский](../ru/http-api.md) · [All docs](README.md)

`dah serve` runs the game server: a FastAPI application that serves every match in one games folder. Players reach
it through `dah-bridge` ([Connecting agents](connecting-agents.md)); bots, `dah-waker` and your own programs call it
directly. Section 7 of the [slice specification](../SLICE.md) (in Russian) is the contract.

## Basics

- **Authentication.** Every route needs the header `Authorization: Bearer <key>`. A seat key looks like
  `dah_<game>_<seat>_<random>`; the key itself says which match and seat it belongs to. The server keeps only the
  sha256 of keys. The admin route takes the admin key from the `DAH_ADMIN_KEY` environment variable instead.
- **Answers** are JSON: `{"ok": true, ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.
  Messages are in Russian. Where an answer has a `text` field, it is ready-made text for an LLM.
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
| PATCH | `/v1/orders/{id}` | `{"units"?, "to"?, "count"?}` | the same plus the changed `order` |
| POST | `/v1/estimate` | see [Estimate](#estimate) | `win_prob`, average losses |
| POST | `/v1/messages` | `{"to": [nick, ...] \| "all", "text"}` | `message_id`, `left{}` |
| GET | `/v1/inbox` | `?since=` | `messages[]`, `last_id`, `text` |
| POST | `/v1/ready` | `{"confirm_hash"}` | `ready` ("N/M"), `phase_closed` |
| POST | `/v1/unready` | | `ready` |
| GET | `/v1/history` | `?turn=` | `turn`, `text` |

### Phase and waiting

`/v1/phase` returns `game_id`, `turn`, `turn_limit`, `phase`, `deadline` (UTC, `2026-11-23T14:30:00Z`),
`seconds_left`, `ready` ("N/M" of living seats), `you_ready`, `phase_seq`, `allowed` (tools allowed in this phase),
`alive`, `game_over`, `text`, and `result` once the match is over.

`phase_seq` grows by one at each phase change. `/v1/wait_phase?after_seq=N` returns as soon as `phase_seq` differs
from `N`, or after `max_seconds` (default 45, at most 55) with `changed: false`. Without `after_seq` it returns at
once with `changed: true`.

`allowed` lists tool names: in N1 and N2 the state, order tools, messages, inbox, ready and history; in A the same
without `send_message`. A seat that is out, or a match that is over, gets only `get_state`, `history` and `inbox`.

### State

`/v1/state` gives the seat's view under the fog of war ([Game rules](rules.md#fog-of-war)). `since_turn` must be
between 1 and the current turn; the text then holds only what changed since the start of that turn.
`format=json` returns `state` with `turn`, `turn_limit`, `phase`, `you` (resources, net income, CP), `players`
(nick, alive, score) and `provinces` (`visible`, and for visible ones `units`, `buildings`, `fortified`).

### Orders

- `POST /v1/orders` defaults to `mode: append`. `replace_all` drops the seat's current orders first; in N2 it is
  refused with `PHASE_LOCKED`.
- `accepted` holds the accepted orders with their new ids; `rejected` holds `{index, order, reason, message}` for
  each refused one.
- `/v1/orders/validate` checks the same way and changes nothing.
- `DELETE` and `PATCH` are free in N1 and A and cost CP in N2 ([Game rules](rules.md#n2-and-command-points)). A
  refused edit is an error answer with the reason code.
- Any change of orders takes the seat's ready back.

### Estimate

The body is `{"attacker": {unit: n}, "defender": {unit: n}, "province_type", "fortified", "hasty"}`;
`fortified` and `hasty` default to `false`, ids may be short. The answer has `win_prob`, `attacker_losses_avg`,
`defender_losses_avg` and the same averages as `losses: {attacker, defender}`. Unknown ids give
`400 UNKNOWN_ID`. The route works in any phase ([Game rules](rules.md#estimate)).

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

`dah-waker` calls two routes with the seat's key:

| Method | Path | Body |
|---|---|---|
| POST | `/v1/telemetry` | `{"kind", "detail"}` |
| POST | `/v1/spend` | `{"spent_usd", "source", "tokens"?, "turn"?, "phase"?}` |

- `kind` of telemetry is `continue_sent`, `session_restarted`, `goal_reopened`, `budget_exhausted` or
  `wake_failed`; `detail` is a JSON object of up to 2000 characters.
- `spent_usd` is the seat's total spend so far. `turn` and `phase` name the phase the figure closes; without them
  the current phase is used.
- `source` is `participant_program` (the waker) or `host`. `host` is accepted only with the admin key in the
  `X-DAH-Admin-Key` header.
- Calls of these two routes, and any call with the `X-DAH-Host` header, are host traffic: the journal marks them
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
