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
content/packs/core/        the base pack: resources, province types, units, buildings, factions, maps, scenarios,
                           tech branches, techs, artifacts, lore
content/legacy/            frozen earlier pack versions (core-0.2.0), loaded only to replay or continue old matches
src/dah/
  engine/                  pure rules library, no I/O
    registry.py            stats with bounds, the 8 effect types and their contexts, abilities, modifier arithmetic
    content.py             loading packs, JSON Schema validation, assembling a Ruleset
    schemas/               a JSON Schema for each content type
    model.py               game state (dataclasses), canonical JSON, state_hash
    orders.py              order checks, reservations, CP prices for N2
    diplomacy.py           gifts and pacts as ops on the state (shared by the live match and replay)
    combat.py              the battle formula and estimate
    resolve.py             game setup and turn resolution (a deterministic pure function)
    techs.py               stat keys per seat (faction + techs + artifacts + buildings), move range, research
    setup.py               the setup phase: draft offers, choice checks, resolving factions and artifacts
    catalog.py             describe/find: full descriptions of units, buildings, techs, factions, artifacts
    rules.py               the rules text and its topics, built from the ruleset
    view.py                fog of war, state text, turn reports
    rng.py                 random streams from seed|turn|system|entity
  game.py                  a match: phases, deadlines, ready, orders, messages, replay
  journal.py               the JSONL journal
  store.py                 the match folder on disk, seat keys
  report.py                the match report from the journal
  server/                  FastAPI app (app.py) and the registry of loaded matches (registry.py)
  bridge/                  dah-bridge: MCP tools over HTTP
  waker/                   dah-waker: wakes DreamTeam sessions
  bots/                    passive, builder, rush and diplomat bots (plain HTTP clients)
  cli.py                   the dah command
tests/                     pytest; test_e2e_bots.py runs CLI → server → bots → report and replay → bridge
docs/                      SLICE.md (spec), CONCEPT.md (concept), CONNECT.md (connecting agents), en/, ru/
```

## The engine

- `build_ruleset(pack_dirs, scenario)` loads and checks packs and returns a `Ruleset`: all entities with full ids,
  the map, the scenario, effective stats (with scenario overrides and per-faction modifiers) and a `hash`.
- `new_game_state(ruleset, seats, seed)` deals the starts and returns the state of turn 1; a seat may carry its
  `faction` and `artifacts` (the setup phase's result), whose start effects are applied.
- `setup_config`, `draft_offers`, `check_faction_prefs`, `check_artifacts` and `resolve_setup` (`setup.py`) run the
  setup phase as pure functions of the ruleset, the seed and the choices.
- `stat_key(state, ruleset, seat)` (`techs.py`) gives a seat's `Profile`: faction, learned techs, artifacts and
  owner-scope buildings; `Ruleset.stat(path, key)` then returns the seat's effective value.
- `submit_orders`, `cancel_order`, `amend_order` take a seat's order book and return a new one, with a reason for
  each refusal. They never change their input.
- `resolve(state, orders_by_seat, ruleset, seed, diplomacy=())` returns the new state and a `TurnReport`: all
  events, the part each seat may see, the public part (owner changes, eliminations, scores, result, the public
  diplomacy facts) and idle statistics. Pacts in force decide which armies do not fight; `diplomacy` (the turn's
  ops, already applied to the state) only goes into the report.
- `diplomacy.make_gift`, `make_proposal`, `make_response`, `make_leave` check a request and return an op;
  `apply_op` applies it to the state (`GameState.pacts`, resources).
- `view_state` and `render_state_text` apply the fog of war; `rules_text`/`rules_topic` (`rules.py`) write the
  rules from the ruleset, `describe` (`catalog.py`) one entity.

## Game

`Game` (in `game.py`) wraps the engine for one match:

- with a setup phase, `setup` before turn 1: choices (`choose_faction`, `choose_artifacts`), ready without a hash,
  then `_close_setup` rebuilds turn 1 from `resolve_setup` and journals `setup_closed`;
- the phase cycle `negotiation_1 → actions → negotiation_2 → resolution → negotiation_1` of the next turn, then
  `game_over`;
- an injected clock: `tick()` closes a phase whose deadline has passed, so tests run without waiting;
- each seat's order book for the current turn, ready flags and `confirm_hash` checks;
- message limits and the `untrusted` wrapping;
- gifts and pacts: limits per phase, the ops of the current turn (`diplomacy_log`), the game's own inbox entries
  (`system`, `[ИГРА]`);
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
| `private/diplomacy/turn_<N>.json` | the diplomacy ops of turn N in order (only for turns that had any) |
| `private/reports/turn_<N>.json` | the full report of turn N |
| `private/messages.jsonl` | full message texts, and the game's own inbox entries (`system: true`) |
| `private/bot_keys.json` | keys of bot seats, for `dah bots run` |
| `private/books_d/turn_<N>.json` | espionage: the order books at the close of actions of turn N (what recon read) |
| `private/recon/turn_<N>.json` | espionage: the recon of turn N — every roll, the reports with `facts` and `truth`, captures, strikes |

The server never gives `private/` to players. The journal holds message sizes, not texts.

## The journal

`journal.jsonl` is append-only; each line is `{seq, ts, type, ...}`. Event types:

| Type | Main fields |
|---|---|
| `game_created` | config (no keys), `ruleset_hash`, `seed`, `rules_chars` |
| `seat_info` | seat, kind, declared, `dreamteam_session_id`; a later link adds `update: true`, `previous`, `source` (`cli` or `waker`) |
| `phase_open` / `phase_close` | turn, phase, deadline / reason `all_ready` or `deadline`, `not_ready` (living seats that had not pressed ready) |
| `recon` | turn; with espionage on also `state_hash` (after recon), `recon_hash`, missions, successes, captures |
| `shade_hired` / `mission_started` / `spy_round` / `intel_report` / `intel_read` / `shade_captured` / `mission_end` / `mission_rejected` | espionage (stage 3): Shades, missions, every round's rolls, reports (`facts_hash`, `truth_hash`, informativeness), what a seat opened (`via` get_state or get_intel), captures |
| `sabotage` / `strike_cancelled` / `feint` / `feint_exposed` | strikes, false entries (`order_id`; `converted_in_p2`), their exposure (`order_id`, `index`, `report_id`) |
| `province_captured` / `disposition` / `policy_set` / `policy_unaffordable` / `dispose_failed` / `occupation_revolt` / `claim_created` / `claim_ended` / `ruin_ended` | conquest (stage 3): captures with `mode` and `status`, fates with price, loot and source, policies, revolts, claims, ruins |
| `call` | seat, route, `req_chars`, `resp_chars`, `ms`, status, turn, phase, `host` for host traffic |
| `first_call_in_phase` | seat, turn, phase |
| `orders_validated` / `orders_submitted` | seat, accepted or ok count, rejected with reasons, `orders_hash`, phase |
| `order_edit` / `order_edit_p2` | seat, op, order id, CP cost (`order_edit_p2` also for orders added in N2) |
| `ready` / `unready` / `ready_cleared` | seat, turn, phase |
| `message` | from, to, chars, phase |
| `turn_resolved` | turn, `state_hash`, scores, idle resources and units, `autopilot` (seats that resolved with no orders) |
| `spend_report` | seat, `spent_usd`, source, tokens, `for_turn`/`for_phase` (the phase the figure is for; `null` for a baseline), `at_turn`/`at_phase` (when it was received), `turn`/`phase` (the "for" pair, else the "at" pair) |
| `telemetry` | seat, kind, detail |
| `gift` | seat (giver), to, resources, turn, phase |
| `pact_proposed` / `pact_accepted` / `pact_rejected` / `pact_withdrawn` / `pact_left` | seat, `pact_id`, seats or to, turns, `until_turn` / `ends_before_turn`, turn, phase |
| `pact_broken` / `pact_expired` / `pact_ended` / `pact_lapsed` | `pact_id`, seats, turn; `broken_by` / reason (`left`, `eliminated`) |
| `pact_renewed` | seat (who accepted), `pact_id` (the renewed pact), `renewal_id`, seats, turns, `from_turn`, `until_turn` (the new end), turn, phase |
| `pact_renewal_cancelled` | seat (who left), `pact_id`, `renewal_id`, turn, phase |
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
counters (and without `pacts` while there are none, so a match without diplomacy hashes as before pacts existed).
The journal records it in every `turn_resolved` event.

`dah replay <id>`:

1. picks the ruleset with `game.pick_ruleset`: `--packs` if given; else the config's packs when their hash matches
   `ruleset_hash` in the journal (a match created before phase limits left the hash matches by the older hash,
   computed with its config's limits); else, for a match on the default packs, a frozen version from
   `content/legacy` whose hash matches (`Game.load` does the same, so a running match keeps its rules);
2. creates the initial state from the seed; with a setup phase, turn 1 is built from the `results` of the
   `setup_closed` event (each seat's faction and artifacts);
3. applies each turn's diplomacy ops from `private/diplomacy/` and resolves the turn with the orders saved in
   `private/orders/`; with espionage on, it applies the ops of N1, runs recon on `private/books_d/` and checks the
   `recon` event's `state_hash`, then applies the ops of N2 and resolves;
4. compares each turn's `state_hash` and the final winners and scores with the journal.

A mismatch means the rules or content changed, or resolution is not deterministic. The command exits with code 1.

## The report

`dah report <id>` reads only the journal. It splits time per seat and per phase (to the first call, to the first
accepted submission, to ready), counts calls and characters (host traffic apart), submissions (also per phase kind:
N1 / A / N2), edits and rejections, idle resources and units, turns a seat resolved with no orders, phases the
deadline closed while a seat had not pressed ready, scores by turn, money by turn and by phase from `spend_report`
(by its "for" fields), money and time during the match and after game_over apart (`spent_in_game_usd`,
`spent_after_game_usd`, `in_game_s`, `after_game_s`), and waker telemetry. The aim of the slice is to measure exactly this: how much money and time a turn costs a DreamTeam
swarm and a single agent.

## Server, bridge, waker, bots

- `server/registry.py` keeps the loaded matches and maps key hashes to (match, seat). An unknown well-formed key
  triggers a rescan of the games folder, at most every 2 seconds.
- `bridge/` is a stdio MCP server (`mcp` package). Each tool is one HTTP call; the answer is the server's `text` or
  compact JSON. The key is scrubbed from any text that leaves the process.
- `waker/` long-polls `/v1/wait_phase` and calls DreamTeam's `/api/v1/sessions/{id}/continue` and
  `/api/v1/sessions/{id}` (after game_over also `/api/v1/sessions/{id}/stop` with `--stop-on-game-over`), and keeps
  reporting the session's spend after game_over until it stops or goes idle.
- `bots/` share one loop (`base.py`): wait for a phase, read the state as JSON, act, press ready.
