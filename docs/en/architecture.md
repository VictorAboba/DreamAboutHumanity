# Architecture

**English** · [Русский](../ru/architecture.md) · [All docs](README.md)

```
 agent / swarm ──MCP stdio──► dah-bridge ──┐
 DreamTeam hub ◄──continue── dah-waker ────┤
 bots (dah bots run) ──────────────────────┼─HTTP /v1─► dah serve (server/app.py)
 your program ─────────────────────────────┘                │
                                                  GameRegistry ─► Game (game.py) ─► engine (pure rules)
                                                                     │
                                                    games/<id>/: config.json, keys.json, journal.jsonl, private/
```

- Everything a player does goes through the HTTP API. Bots use the same API as agents and get no shortcuts.
- The engine is a pure library: rules in, state out, no clock, no network, no files except reading packs.
- `Game` adds what depends on time and seats: phases, deadlines, ready, order books, messages, the journal.
- The server is a thin layer over `Game`: authentication, JSON in and out, a `call` event per request.

## Code map

```
content/packs/core/        the base pack: resources, province types, units, buildings, faction, maps, scenarios
src/dah/
  engine/                  pure rules library, no I/O
    registry.py            stats with bounds, the 6 effect types, modifier arithmetic
    content.py             loading packs, JSON Schema validation, assembling a Ruleset
    schemas/               a JSON Schema for each content type
    model.py               game state (dataclasses), canonical JSON, state_hash
    orders.py              order checks, reservations, CP prices for N2
    combat.py              the battle formula and estimate
    resolve.py             game setup and turn resolution (a deterministic pure function)
    view.py                fog of war, state text, turn reports, rules text
    rng.py                 random streams from seed|turn|system|entity
  game.py                  a match: phases, deadlines, ready, orders, messages, replay
  journal.py               the JSONL journal
  store.py                 the match folder on disk, seat keys
  report.py                the match report from the journal
  server/                  FastAPI app (app.py) and the registry of loaded matches (registry.py)
  bridge/                  dah-bridge: MCP tools over HTTP
  waker/                   dah-waker: wakes DreamTeam sessions
  bots/                    passive, builder and rush bots (plain HTTP clients)
  cli.py                   the dah command
tests/                     pytest; test_e2e_bots.py runs CLI → server → bots → report and replay → bridge
docs/                      SLICE.md (spec), CONCEPT.md (concept), CONNECT.md (connecting agents), en/, ru/
```

## The engine

- `build_ruleset(pack_dirs, scenario)` loads and checks packs and returns a `Ruleset`: all entities with full ids,
  the map, the scenario, effective stats (with scenario overrides and per-faction modifiers) and a `hash`.
- `new_game_state(ruleset, seats, seed)` deals the starts and returns the state of turn 1.
- `submit_orders`, `cancel_order`, `amend_order` take a seat's order book and return a new one, with a reason for
  each refusal. They never change their input.
- `resolve(state, orders_by_seat, ruleset, seed)` returns the new state and a `TurnReport`: all events, the part
  each seat may see, the public part (owner changes, eliminations, scores, result) and idle statistics.
- `view_state` and `render_state_text` apply the fog of war; `rules_text` writes the rules from the ruleset.

## Game

`Game` (in `game.py`) wraps the engine for one match:

- the phase cycle `negotiation_1 → actions → negotiation_2 → resolution → negotiation_1` of the next turn, then
  `game_over`;
- an injected clock: `tick()` closes a phase whose deadline has passed, so tests run without waiting;
- each seat's order book for the current turn, ready flags and `confirm_hash` checks;
- message limits and the `untrusted` wrapping;
- `phase_seq` and `wait_phase`, which wakes waiting requests on every phase change.

Every change is written to the journal, and the live snapshot to `private/runtime.json`. `Game.load` restores a
match from it after a server restart.

## The match folder

`dah game create` makes `games/<id>/`:

| Path | Content |
|---|---|
| `config.json` | the match config without keys: id, seed, scenario, pack folders, seats, limits |
| `keys.json` | `{sha256(key): seat}` |
| `journal.jsonl` | the journal, one event per line |
| `private/runtime.json` | live snapshot: state, order books, ready flags, phase timing |
| `private/states/turn_<N>.json` | the state at the start of turn N |
| `private/orders/turn_<N>.json` | the order books resolved at the end of turn N |
| `private/reports/turn_<N>.json` | the full report of turn N |
| `private/messages.jsonl` | full message texts |
| `private/bot_keys.json` | keys of bot seats, for `dah bots run` |

The server never gives `private/` to players. The journal holds message sizes, not texts.

## The journal

`journal.jsonl` is append-only; each line is `{seq, ts, type, ...}`. Event types:

| Type | Main fields |
|---|---|
| `game_created` | config (no keys), `ruleset_hash`, `seed`, `rules_chars` |
| `seat_info` | seat, kind, declared, `dreamteam_session_id` |
| `phase_open` / `phase_close` | turn, phase, deadline / reason `all_ready` or `deadline` |
| `recon` | turn (the empty intel slice) |
| `call` | seat, route, `req_chars`, `resp_chars`, `ms`, status, turn, phase, `host` for host traffic |
| `first_call_in_phase` | seat, turn, phase |
| `orders_validated` / `orders_submitted` | seat, accepted or ok count, rejected with reasons, `orders_hash`, phase |
| `order_edit` / `order_edit_p2` | seat, op, order id, CP cost (`order_edit_p2` also for orders added in N2) |
| `ready` / `unready` / `ready_cleared` | seat, turn, phase |
| `message` | from, to, chars, phase |
| `turn_resolved` | turn, `state_hash`, scores, idle resources and units, seats on autopilot |
| `spend_report` | seat, `spent_usd`, source, turn, phase, tokens |
| `telemetry` | seat, kind, detail |
| `game_over` | winners, reason (`last_standing` or `turn_limit`), scores, turn |

A crash in the middle of a write leaves at most one torn line; readers skip it and the writer continues after it.

## Determinism

- All randomness comes from `rng.stream(seed, turn, system, entity)`: a `random.Random` seeded with the sha256 of
  `"{seed}|{turn}|{system}|{entity}"`. There is no global randomness. Starts are dealt from one stream, each
  battle has its own stream per province or edge.
- `estimate` uses a fixed seed, so the same question always gets the same answer.
- Content is processed in order of full ids, not of files.
- Bots are deterministic from their seat seed (sha256 of game id and seat).

So the same ruleset, seed and orders always give the same match.

## state_hash and replay

`state_hash` is the sha256 of the state's canonical JSON (sorted keys, no spaces), without the per-seat order id
counters. The journal records it in every `turn_resolved` event.

`dah replay <id>`:

1. rebuilds the ruleset from the config and compares its hash with `ruleset_hash` in the journal;
2. creates the initial state from the seed;
3. resolves each turn with the orders saved in `private/orders/`;
4. compares each turn's `state_hash` and the final winners and scores with the journal.

A mismatch means the rules or content changed, or resolution is not deterministic. The command exits with code 1.

## The report

`dah report <id>` reads only the journal. It splits time per seat and per phase (to the first call, to the first
accepted submission, to ready), counts calls and characters (host traffic apart), submissions, edits and
rejections, idle resources and units, autopilot turns, scores by turn, money by turn from `spend_report`, and
waker telemetry. The aim of the slice is to measure exactly this: how much money and time a turn costs a DreamTeam
swarm and a single agent.

## Server, bridge, waker, bots

- `server/registry.py` keeps the loaded matches and maps key hashes to (match, seat). An unknown well-formed key
  triggers a rescan of the games folder, at most every 2 seconds.
- `bridge/` is a stdio MCP server (`mcp` package). Each tool is one HTTP call; the answer is the server's `text` or
  compact JSON. The key is scrubbed from any text that leaves the process.
- `waker/` long-polls `/v1/wait_phase` and calls DreamTeam's `/api/v1/sessions/{id}/continue` and
  `/api/v1/sessions/{id}`.
- `bots/` share one loop (`base.py`): wait for a phase, read the state as JSON, act, press ready.
