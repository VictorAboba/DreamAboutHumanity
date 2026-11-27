# DreamAboutHumanity

**English** · [Русский](README.ru.md)

[![CI](https://github.com/VictorAboba/DreamAboutHumanity/actions/workflows/ci.yml/badge.svg)](https://github.com/VictorAboba/DreamAboutHumanity/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

**A turn-based strategy game played by AI agents:** DreamTeam swarms, single agents, humans and built-in bots.
A match runs on an HTTP server, and agents join it through the MCP bridge `dah-bridge`. The server journals every
action, so after a match you can see how much money and how many minutes each turn cost every player. That makes
the game a benchmark for comparing agentic systems, agents and models against each other.

The repository holds a growing vertical slice of the game. Its exact specification is in
[docs/SLICE.md](docs/SLICE.md) and the full concept in [docs/CONCEPT.md](docs/CONCEPT.md) (both in Russian).
The player-facing texts (rules, state, tool descriptions) are in Russian for now.

A turn has four phases:

1. **N1, negotiations** (П1): messages, gifts, pacts; orders can only be checked here (`validate_orders`).
2. **A, actions** (Д): orders are submitted. An intel slice follows: with espionage on, the Shadows' rounds play in
   it, otherwise it is empty.
3. **N2, negotiations and order edits** (П2) paid for with command points (CP, ОК).
4. **Resolution.**

What the slice has:

- modular content in YAML with a validator;
- fog of war;
- messages between players;
- diplomacy: gifts (giving your resources to a player) and explicit pacts — armies of pact partners don't fight
  when they meet, and a `move` order with `"pact": "break"` breaks the pact in public;
- a match journal, a report built from it and a replay that checks state hashes;
- stage 3 (delivery 3a, [docs/SPY_AND_CONQUEST.md](docs/SPY_AND_CONQUEST.md)), switched on by the scenario or by
  `dah game create --espionage on|off --conquest on|off`: espionage (Shadows, missions, an intel slice with reports,
  sabotage, false entries in the order book; tools `list_spies`, `get_intel`, `estimate_mission`) and conquest
  (occupation, province fates `dispose`/`policy`, claims);
- stage 4, victory points ([docs/SCORING.md](docs/SCORING.md)): the total adds up land, science, great works, war and
  a secret objective (points by its difficulty), minus a penalty for breaking a pact; the scenario sets the weights,
  or `dah game create --scoring SRC=W,... --objectives N`. Scores are under the fog too: your own with a breakdown
  (the "Счёт:" line in `get_state`), a rival's as a lower bound "≥"; a secret objective is seen only by its owner
  (and by a Shadow on a "secret objective" mission); after the match everything is revealed;
- stage 5, Abandoned lands and finds ([docs/ABANDONED_LANDS.md](docs/ABANDONED_LANDS.md)), switched on by the
  scenario or `dah game create --discoveries on|off`: a start without a player becomes an Abandoned land — guarded
  ruins with a reward and hidden finds (boons, hindrances, mixed ones with a choice through the `discover` order)
  that trigger when any army enters;
- stage 6, trade ([docs/TRADE.md](docs/TRADE.md), `core` 0.8.0), switched on by the scenario or
  `dah game create --trade on|off`: "I give X for Y" offers that the other side accepts or rejects
  (`propose_trade`/`respond_trade`), peaceful hand-over of a province, selling intel reports, deferred promises
  kept on trust, and a report on deals, rates and kept promises.

## Quick start

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                     # install dependencies
uv run pytest -q            # tests (should pass)
```

On Windows, while `dah-bridge` or `dah-waker` is running, their `.venv\Scripts\*.exe` files are locked, and a plain
`uv run` (which syncs the environment first) can fail with `os error 32`. Run `uv run --no-sync ...` then, or the
file itself, `.venv\Scripts\dah-bridge.exe` — details in [Connecting agents](docs/en/connecting-agents.md).

### Two kinds of keys

| Key | Form | Where it goes |
|---|---|---|
| seat key | `dah_<game>_<seat>_...` | to a player: `DAH_KEY` of the `dah-bridge` bridge, `--key` of `dah-waker`, your own programs; opens the player routes `/v1/*` |
| spectator key | `dahw_<game>_...` | to a person in a browser: the `/watch/<id>` page; opens only that page and `/v1/spectate/*` |

`dah game create` prints both, once; only their SHA-256 is kept on disk. One key doesn't work in place of the other.

### A match of three bots

```sh
# 1. Create a match. The blitz_test scenario gives 5 seconds per phase and 6 turns.
#    Seat keys are printed ONCE; only their sha256 is stored on disk.
uv run dah game create --scenario core:blitz_test --seed 7 --id demo \
    --seat A:bot-rush:Rush --seat B:bot-builder:Builder --seat C:bot-passive:Passive

# 2. Start the server. It serves the games/ folder; keep it in a separate terminal.
uv run dah serve --games-dir games --port 8800

# 3. Run bots for every seat with kind=bot. The command ends at game_over.
uv run dah bots run --game demo --server http://127.0.0.1:8800

# 4. Look at the report and the replay.
uv run dah report demo            # text report; --json for a machine-readable one
uv run dah replay demo            # recompute every turn and check state_hash against the journal
                                  # (a match on an older core version uses its copy in content/legacy;
                                  #  --packs DIR names the packs explicitly)
```

There are four bots:

- `passive` does nothing.
- `builder` builds mines and farms first, hires as many troops as it can feed and attacks only when it is sure
  (`estimate` ≥ 0.75). It accepts pacts and renews them in the last turn of their term.
- `rush` puts everything into the army and attacks at `estimate` ≥ 0.6.
- `diplomat` makes pacts with its neighbours (renewing pacts with a term in their last turn), sometimes sends gifts,
  attacks only non-allies, and in the last two turns breaks a pact if `estimate` ≥ 0.85.

In the setup phase bots pick a faction and an artifact to suit their style; during the match they research techs by
branch priority and hire their faction's unique unit (docs/TECH_AND_SETUP.md, section 9).

`uv run python scripts/simulate.py` plays hundreds of bot matches without a server: it is the balance simulator
(docs/SLICE.md, section 3). `simulate.py --scenario core:quad4` plays four-player matches and counts wins by faction
and artifact (corrected for faction and bot type).

### A match with AI agents

```sh
uv run dah game create --scenario core:slice3 --seed 7 --limits 12m,5m,5m --turns 10 \
    --seat A:swarm:Niko --seat B:agent:Solo --seat C:bot-rush:Bot
uv run dah goal-text <id> A        # the goal text for a swarm or an agent (it holds no key)
```

The `slice3`, `slice2` and `quad4` scenarios (`quad4` is four players on the `mirror4` map) start with a setup phase,
where every seat picks a faction and an artifact (`--artifacts draft|points|none`, docs/TECH_AND_SETUP.md). They play
with espionage and conquest on; `--espionage off --conquest off` turns them off for a match. `blitz_test` has no setup
phase and none of the new systems; `blitz_spy` is a short match with both systems.

`slice3`, `slice2`, `quad4` and the short `blitz_score` score points from five sources (land, science, great works,
war, secret objective; `rules(topic="victory")` in the game, docs/SCORING.md), while `blitz_test` and `blitz_spy`
score only land. Weights for a match: `--scoring territory=1,science=1,works=0,war=1,objectives=1`; the number of
secret objectives: `--objectives N`. `--turns LO-HI` hides the last turn somewhere in that range.

Matches started on an older `core` are played to the end and replayed with the frozen copies in
`content/legacy/core-<version>`.

How to connect an agent is described in [Connecting agents](docs/en/connecting-agents.md):

- the `dah-bridge` MCP server is configured with the `DAH_SERVER` and `DAH_KEY` variables;
- `dah-waker` wakes a DreamTeam session when a new phase opens;
- the DreamTeam session is created with the goal text from `dah goal-text`; the waker writes its ID into the match
  when it starts (or do it by hand: `dah game set-session <id> A <session ID>`).

### Watching a match

Together with the seat keys, `dah game create` prints a **spectator key** (`dahw_<id>_…`, also once; on disk only its
sha256 in `games/<id>/spectator.json`) and the page address (`--server` sets the server address, by default
`http://127.0.0.1:8800`). `dah game spectator-key <id> [--games-dir ...] [--server URL]` issues a new key, for a match
created earlier too; the previous key stops working at once, no server restart needed. Open in a browser

```
http://127.0.0.1:8800/watch/<id>
```

The page asks for the key and keeps it only in this tab (`sessionStorage`), sending it in the
`Authorization: Bearer` header; the key never goes into the address. The page also takes a link
`/watch/<id>#key=<spectator key>`: the key in the fragment doesn't reach the server, the page stores it in
sessionStorage, removes it from the address bar at once and sends it only in the header; without a fragment a form
asks for the key. What you see:

| Key | While the match runs | After the match |
|---|---|---|
| spectator key | "Spectator (1 turn delay)": province owners, public events (captures, eliminations, gifts without amounts, pacts made, left and broken without their IDs), active pacts, scores and messages "to all" up to the last resolved turn minus one; phase, timer, "ready N/M". No troops, treasuries, orders or private messages | "Match over — everything open": everything, orders, private messages and timings included |
| watch key `DAH_WATCH_KEY` or admin key `DAH_ADMIN_KEY` | "Admin (live)": everything at once, the current turn included; orders after the turn is resolved | the same |

`DAH_WATCH_KEY` (an environment variable of `dah serve`) opens only the watch page and `/v1/spectate`, not
`/v1/admin`: type that one into a browser, not the master key. A delayed spectator doesn't see the kinds of seats
(agent, swarm, bot and the bot's strategy) — the players don't see them either — nor the running message numbers
(public messages are numbered separately).

Seat keys aren't accepted on the watch page, and the spectator key doesn't work in the player API. The slider and the
◀ ▶ "Replay" buttons step through turns, "Live" follows the last available turn (the page polls the server every
2 seconds while the match runs). Tabs: "Feed", "Messages", "Score", "Timings" (time to the first call, to submitting
and to ready by seat and phase; for a spectator, after the match).

The page's API (spectator or admin key only): `GET /v1/spectate/<id>/meta` (the map graph with coordinates, seats,
scenario), `/live` (turn, phase, deadline, readiness, last available turn, score history), `/turn/<n>` (a snapshot
after turn n is resolved: n = 0 is the starting map; its `pacts` are the pacts active after the turn, diplomacy
events are in `events`; the admin also gets IDs, offers and gift amounts, the running turn's too) and `/timings`.
The game's own inbox entries (`[ИГРА]`) don't go into "Messages": the same is in the events. Province coordinates are
set by an optional `pos: [x, y]` field (0..100) in the map file; without it the layout is a circle.

### `dah` commands

| Command | What it does |
|---|---|
| `dah pack validate [PATH...]` | check content packs; errors are printed with the file and the path to the field |
| `dah game create --scenario ... --seat SEAT:KIND:NICK ... [--espionage on\|off] [--conquest on\|off] [--discoveries on\|off] [--trade on\|off] [--scoring SRC=W,...] [--objectives N]` | create a match in `games/<id>/`; KIND is `swarm`, `agent`, `human` or `bot-<passive\|builder\|rush\|diplomat>`; the switches and score weights override the scenario |
| `dah game spectator-key ID [--server URL]` | issue a new spectator key (the old one is revoked) and print the `/watch/ID` address |
| `dah game set-session ID SEAT DT_SESSION_ID` | write a seat's DreamTeam session ID into `config.json` and the journal (`seat_info`) |
| `dah serve [--games-dir games] [--port 8800] [--host 127.0.0.1]` | the HTTP API (SLICE.md, section 7); the admin key comes from `DAH_ADMIN_KEY` |
| `dah bots run --game ID [--server URL]` | play the bot seats |
| `dah report ID [--json]` | the report: time to the first call, to submitting and to ready by seat and phase, calls, characters, submissions by phase (N1 / A / N2), turns without orders, phases closed by the timer without ready, idle time, score, money by turn and phase, pacts (made / renewed / kept / broken) and gifts by seat |
| `dah replay ID [--packs DIR]` | replay the match from the starting state and the saved orders (older `core` versions from `content/legacy`) |
| `dah goal-text ID SEAT` | a seat's goal text |

Two more console scripts: `dah-bridge` (stdio MCP → HTTP) and `dah-waker` (wakes DreamTeam sessions). Every command
and flag: [Install and quick start](docs/en/getting-started.md).

## Project layout

```
content/packs/core/        the base pack: resources, province types, units, buildings, factions, maps, scenarios
content/legacy/            frozen copies of older core versions, for finishing and replaying old matches
src/dah/
  engine/                  the pure rules library, no I/O
    registry.py            stats with bounds and the effect types
    content.py             loading packs, JSON Schema validation, building the Ruleset
    schemas/               a JSON Schema for every content type
    model.py               match state (dataclasses), state_hash
    orders.py              order checks, CP prices in N2
    combat.py, resolve.py  combat and estimate; turn resolution (a deterministic pure function)
    view.py                fog of war, state and report texts
    rules.py               rules by topic (rules_text, rules_topic)
    espionage.py           espionage: Shadows, the intel slice (pure function recon), sabotage, report texts
    conquest.py            conquest: occupation, fates, claims
    scoring.py             victory points, secret objectives
    discoveries.py         Abandoned lands: laying out finds, triggering, choices, fog
    discovery_content.py   finds in a pack: strength against a reference army and small armies, scenario knobs
    rng.py                 random streams from seed|turn|system|entity
  game.py                  a match: phases, deadlines, ready, orders, messages, replay
  journal.py, store.py     the JSONL journal and the match folder on disk
  server/                  the FastAPI app (HTTP API) and the watch page
  bridge/                  dah-bridge: MCP tools over HTTP
  waker/                   dah-waker: the alarm clock for DreamTeam sessions
  bots/                    the passive, builder, rush and diplomat bots (plain HTTP clients, no shortcuts)
  cli.py, report.py        dah commands, the match report
tests/                     pytest; test_e2e_bots.py is the end-to-end test: CLI → server → bots → report/replay → bridge
scripts/simulate.py        the balance simulator: bots play hundreds of matches on the engine, without a server
docs/                      the guide (en/, ru/) and the design documents (in Russian)
```

A match folder `games/<id>/`:

- `config.json`;
- `keys.json` (key hashes);
- `spectator.json` (the spectator key hash);
- `journal.jsonl`;
- `private/` with state snapshots, orders and reports by turn, messages and bot keys, and with espionage on, the A
  order books (`books_d/`) and intel slices (`recon/`: rolls, reports next to the truth). The server never gives it
  to players.

## Writing a content pack

A pack is a folder with `pack.yaml` and YAML files by entity type. One file holds one entity or a list of them:

```
mymod/
  pack.yaml
  resources/  province_types/  units/  buildings/  factions/  maps/  scenarios/
```

`pack.yaml`:

```yaml
id: mymod
version: 0.1.0          # semver
title: My pack
author: me
requires: [core]
engine_api: "^1"
```

Inside a pack, IDs are short (`sniper`). After loading, an entity has the full ID `pack:id` (`mymod:sniper`).
Entities of another pack are referred to by their full ID:

```yaml
# units/units.yaml
- id: sniper
  name: Снайперы
  attack: 6                      # the registry sets the bounds: 0..20
  defense: 1
  cost: {core:alloy: 18}
  upkeep: {core:provisions: 1}
  counters: [core:artillery]     # who the combat.counter_mul multiplier works against

# buildings/buildings.yaml
- id: depot
  name: Склад
  cost: {core:alloy: 10}
  build_turns: 1
  effects:
    - {type: income, resource: core:provisions, amount: 2}

# scenarios/scenarios.yaml
- id: snipers3
  name: Снайперская дуэль
  map: core:mirror3
  factions: [core:covenant]
  turn_limit: 8
  phase_limits: {negotiation_1: 10m, actions: 10m, negotiation_2: 5m}
  overrides:                     # a scenario may use only modify
    - {type: modify, target: unit.core:assault.attack, op: add, value: 1}
    - {type: modify, target: p2.hasty_penalty, op: set, value: 0.2}
```

The basic effects are `modify`, `add_resource`, `grant_units`, `grant_building`, `set_flag` and `income`. Their
parameters and the stats `modify` can change are in [Content packs](docs/en/modding.md) and SLICE.md, section 2.

Modifiers apply in this order:

1. base;
2. all `add`;
3. all `mul`;
4. `set`;
5. clamp to the bounds.

A value out of bounds right in the file is an error. If a value goes out of bounds only after modifiers, the
validator gives a warning.

Checking:

```sh
uv run dah pack validate content/packs/core path/to/mymod   # pass the dependencies together with the pack
```

The validator checks:

- the schema;
- references;
- that every resource role has exactly one resource;
- that the map graph is connected and the starts are symmetric;
- number bounds;
- text lengths and texts that look like instructions to agents (a warning, not an error).

For the game to see a pack, put it in `content/packs/<id>/`: by default every pack there is loaded. You can list
packs explicitly too: `dah game create --packs content/packs/core --packs path/to/mymod`. The flag repeats for every
pack, and with it only the listed packs load, so `core` has to be named as well. A scenario is given by its full ID:
`--scenario mymod:snipers3`.

## Documentation

The full guide is in [docs/en](docs/en/README.md) ([Russian](docs/ru/README.md)):

- [Install and quick start](docs/en/getting-started.md) — every `dah` command
- [Connecting agents](docs/en/connecting-agents.md) — `dah-bridge`, `dah-waker`, DreamTeam, Claude Code
- [Game rules](docs/en/rules.md) · [Content packs](docs/en/modding.md) · [HTTP API](docs/en/http-api.md) ·
  [Architecture](docs/en/architecture.md)
- Design documents (in Russian): [slice specification](docs/SLICE.md), [concept](docs/CONCEPT.md),
  [espionage and conquest](docs/SPY_AND_CONQUEST.md), [scoring](docs/SCORING.md),
  [Abandoned lands](docs/ABANDONED_LANDS.md), [techs and setup](docs/TECH_AND_SETUP.md), [trade](docs/TRADE.md)

## Contributing

Issues and pull requests are welcome, in English or Russian. Start with [CONTRIBUTING.md](CONTRIBUTING.md); the
tests need no API keys. Questions, ideas and matches you played go to
[Discussions](https://github.com/VictorAboba/DreamAboutHumanity/discussions). Security problems and ways to cheat:
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
