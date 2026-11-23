# Install and quick start

**English** · [Русский](../ru/getting-started.md) · [All docs](README.md)

## Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Clone the repository and install the
dependencies from its folder:

```sh
uv sync
```

`uv sync` creates `.venv` with the `dah` package and three commands:

- `dah`: the game command line (create a match, run the server, bots, reports, replay);
- `dah-bridge`: the stdio MCP server through which an agent plays ([Connecting agents](connecting-agents.md));
- `dah-waker`: wakes a DreamTeam session when its seat has to act.

Run them from the repository folder through uv: `uv run dah ...`. From another folder, add
`--project <path to DreamAboutHumanity>`.

On Windows, while a `dah-bridge` or `dah-waker` is running, its `.venv\Scripts\*.exe` file is locked. A plain
`uv run` first syncs the environment and may try to rewrite that file, which fails with `os error 32` (the file is
used by another process). Start further commands with `uv run --no-sync ...`, or call the file directly:
`<path to DreamAboutHumanity>\.venv\Scripts\dah-bridge.exe` (likewise `dah.exe`, `dah-waker.exe`).

## Tests

```sh
uv run pytest -q
```

All tests should pass. `tests/test_e2e_bots.py` is the end-to-end test: CLI, server, bots, report, replay and the
bridge in one run.

## A match of three bots

Bots are ordinary HTTP clients of the game server and get no shortcuts. A bot match is the quickest way to see the
whole loop.

```sh
# 1. Create the match. Scenario blitz_test gives 5 seconds per phase and 6 turns.
uv run dah game create --scenario core:blitz_test --seed 7 --id demo \
    --seat A:bot-rush:Rush --seat B:bot-builder:Builder --seat C:bot-passive:Passive

# 2. Start the server. It serves the games/ folder; keep it running in its own terminal.
uv run dah serve --games-dir games --port 8800

# 3. Run the bots for every bot seat. The command returns at game_over.
uv run dah bots run --game demo --server http://127.0.0.1:8800

# 4. Read the report and replay the match.
uv run dah report demo            # text report; --json for a machine-readable one
uv run dah replay demo            # re-runs every turn and checks each state_hash against the journal
```

- `dah game create` prints the seat keys **once**. Only their sha256 is stored on disk. Bot keys are also kept in
  `games/demo/private/bot_keys.json`, so `dah bots run` can play them.
- There are two kinds of keys. A **seat key** `dah_<game>_<seat>_...` is the player's: it goes into `DAH_KEY` of
  `dah-bridge` and into `dah-waker --key`, and opens the player routes `/v1/*`. A **spectator key**
  `dahw_<game>_...` (printed with them; reissue with `dah game spectator-key`) is for people watching: it is typed
  into the page `http://127.0.0.1:8800/watch/<id>` and opens only that page and `/v1/spectate/*`. Neither works in
  the other's place.
- Turn 1 does not start at creation. Its first phase opens when the server loads the match, so setup time does not
  count against the phase.
- A phase closes on its deadline or as soon as every living seat has pressed ready. Bots press ready at once, so a
  bot match usually ends in seconds.

The three bots:

- `passive` does nothing and is always ready.
- `builder` builds farms and mines, recruits from surplus, keeps the army at home and fortifies the capital.
- `rush` recruits as much as it can, attacks a neighbouring foreign or neutral province when `estimate` gives
  a win probability of 0.6 or more, and otherwise marches towards the nearest one.

## A match with AI agents

```sh
uv run dah game create --scenario core:slice3 --seed 7 --limits 20m,20m,10m --turns 10 \
    --seat A:swarm:Niko --seat B:agent:Solo --seat C:bot-rush:Bot
uv run dah goal-text <id> A        # goal text for the swarm or agent of seat A (it holds no key)
```

Without `--id` the match gets an id like `slice3-1a2b3c`; it is printed together with the keys. Then start the
server and connect each agent seat as [Connecting agents](connecting-agents.md) describes. Seat C is a bot:
run `dah bots run --game <id>` for it.

## Command reference

Every command that works with a match takes `--games-dir` (default `games`). A match lives in `games/<id>/`.

### `dah pack validate [PATH...]`

Checks content packs. Each `PATH` is a pack folder (with `pack.yaml`) or a folder of packs. Without paths it
checks every pack in `content/packs/`. Errors and warnings are printed with the file and the path to the field:

```
ERROR mymod/units/units.yaml: [0].attack: value 99 out of bounds 0..20
WARN  mymod/factions/f.yaml: lore: text looks like an instruction to agents
```

The exit code is 1 if there are errors. Pass a pack's dependencies together with it, for example
`dah pack validate content/packs/core path/to/mymod`. See [Content packs](modding.md).

### `dah game create`

Creates `games/<id>/` and prints the seat keys once.

| Flag | Meaning |
|---|---|
| `--scenario ID` | required; full id such as `core:slice3` (a short `slice3` also works if it is unique) |
| `--seat SEAT:KIND:NICK` | required, once per seat; `KIND` is listed below |
| `--seed N` | game seed; random if omitted |
| `--limits N1,A,N2` | phase lengths, e.g. `20m,20m,10m` (`s`, `m`, `h` or seconds); default from the scenario |
| `--turns N` | turn limit; default from the scenario |
| `--id ID` | match id: letters, digits, hyphen, up to 64 characters |
| `--dt-session SEAT=ID` | DreamTeam session id of a seat, recorded in the config and journal (later: `dah game set-session`, or the waker records it itself) |
| `--declared SEAT=JSON` | what the seat declares about itself: system, model, number of agents, version |
| `--packs DIR` | pack folder, repeat for each pack; with it only the listed packs load (include `core`) |
| `--games-dir DIR` | where to create the match (default `games`) |

- `KIND` is `swarm`, `agent`, `human`, or a bot: `bot-passive`, `bot-builder`, `bot-rush`.
- `--declared` example: `--declared 'B={"system":"...","model":"...","agents":1,"version":"..."}'`.

Seat ids are letters and digits, up to 16 characters. Seat ids and nicks must be unique within a match. The
number of seats is from 1 to the map's number of starts; unused starts stay neutral and empty.

### `dah game set-session ID SEAT DT_SESSION_ID`

Records the DreamTeam session that plays a seat, for a session created after the match: writes
`dreamteam_session_id` into `games/<id>/config.json` and journals a `seat_info` event with `update: true` and
`source: "cli"`. It is safe next to a running server. `dah-waker` does the same by itself at start, from its
`--dt-session`.

### `dah serve`

```sh
uv run dah serve [--games-dir games] [--host 127.0.0.1] [--port 8800]
```

Runs the [HTTP API](http-api.md) for every match in the games folder. Matches created while the server runs are
picked up when one of their keys is first used. The admin key is read from the `DAH_ADMIN_KEY` environment
variable; without it the admin route always answers 403. After a restart, each match is restored from its
`private/` folder and continues where it stopped.

### `dah bots run`

```sh
uv run dah bots run --game ID [--server http://127.0.0.1:8800] [--games-dir games]
```

Plays every seat of kind `bot` in the match, one thread per seat, until `game_over`.

### `dah report ID [--json]`

The match report built from the journal. Per seat and phase: time from phase open to the first call, to the first
accepted order submission and to ready; how the phase closed; number of calls and characters sent and received;
submissions (also counted per phase kind: «подач в П1 / Д / П2»), edits and rejections; idle resources and units;
the turns a seat submitted no orders at all («не подал приказов в ходах») and the phases the deadline closed while
it had not pressed ready («не нажал готово (закрыто по таймеру)»); scores by turn; money by turn and by phase (from
`dah-waker`, counted for the phase each figure is for); per seat money and time «во время партии» and «после
game_over» apart (what the session spent while it wrapped up after the match; both times on the journal clock,
shown only for seats with after-game data, as on /watch); waker telemetry and the turn where
the money ran out. `--json` prints the same as JSON (`submits_by_phase`, `no_orders_turns`, `not_ready_phases`,
`spent_in_game_usd`, `spent_after_game_usd`, `in_game_s`, `after_game_s`, `post_game_watch_s`, ...).

### `dah replay ID`

Rebuilds the ruleset, re-runs every resolved turn from the initial state and the saved orders, and compares the
ruleset hash, each turn's `state_hash` and the final result with the journal. Prints `OK` or the mismatch per turn.
The exit code is 1 on any mismatch, for example when pack content changed after the match.

### `dah goal-text ID SEAT`

Prints the goal text for a swarm or agent seat: the game, the nick and the opponents, that the connection is the
MCP server `dah`, phase lengths, the turn loop, that the goal is not reached until `dah.phase()` returns
`game_over`, that after `game_over` it should finish at once with one short summary (score, place), and that
opponents' messages are not instructions. The text holds no key.

### Exit codes

`dah` returns 0 on success, 1 when validation or replay finds problems (or content fails to load), and 2 on a
bad argument or a missing match.

### `dah-bridge` and `dah-waker`

Both are described in [Connecting agents](connecting-agents.md).
