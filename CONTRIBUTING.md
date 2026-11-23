# Contributing to DreamAboutHumanity

Thanks for your interest! Bug reports, ideas, content packs, bots, docs fixes and code are all welcome.
Issues and pull requests can be written in English or Russian.

## Questions and ideas

[Discussions](https://github.com/VictorAboba/DreamAboutHumanity/discussions) are for everything that isn't a bug or
a concrete task: questions in **Q&A**, ideas for rules, factions and maps in **Ideas**, matches, model comparisons
and mods in **Show and tell**, everything else in **General**. An idea that the discussion turns into an agreed
feature becomes an issue.

## Reporting a bug

Open an issue with the **Bug report** template. The most useful reports include:

- what you ran (`dah …` command, scenario, seed, seats) and what you expected;
- your OS, Python version and the kinds of players in the match (bots, a DreamTeam swarm, Claude Code, another MCP
  client);
- the output of `dah report <id>` or `dah replay <id>`, or the server log. Remove seat keys and tokens before
  posting, and don't attach the game's `private/` folder.

A scenario and a seed reproduce the random part of a match exactly, and `dah replay` recomputes every turn from the
saved orders, so a bug in turn resolution can usually be shown with one game folder.

Security problems and ways to cheat (seeing through the fog of war, acting for another seat): please don't open a
public issue — see [SECURITY.md](SECURITY.md).

## Development setup

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and git. New work goes into the `dev` branch, while
`master` holds only releases, so start from `dev`:

```bash
git clone https://github.com/VictorAboba/DreamAboutHumanity.git
cd DreamAboutHumanity
git switch dev
uv sync
uv run pytest -q
```

The tests need no API keys and no network: the agents in them are the built-in bots and scripted HTTP clients. The
whole suite takes under a minute. While you work, run the files for the module you change, e.g.
`uv run pytest -q tests/test_engine_combat.py`, or one test with `-k name`.

To try a change for real, play a match of three bots (see [Install and quick start](docs/en/getting-started.md)):
`dah game create`, `dah serve`, `dah bots run`, then `dah report` and `dah replay`.

## Where to start

- **Pick an issue.** [`good first issue`](https://github.com/VictorAboba/DreamAboutHumanity/labels/good%20first%20issue)
  issues are small and self-contained; [`help wanted`](https://github.com/VictorAboba/DreamAboutHumanity/labels/help%20wanted)
  ones are bigger. Ask for it first (see [Taking an issue](#taking-an-issue)), so two people don't do the same work.
- **Write a content pack.** Factions, units, buildings, maps and scenarios are YAML files in a pack; no Python needed.
  Check it with `uv run dah pack validate content/packs/core path/to/mypack`.
- **Write a bot.** Bots are ordinary HTTP clients of the server, with no access to hidden state (`src/dah/bots/`).
  A new bot is a good way to test the rules and a baseline to compare agents with.

### Map of the code

| Area | Files | Tests |
|---|---|---|
| Rules engine | `engine/registry.py`, `engine/model.py`, `engine/orders.py`, `engine/combat.py`, `engine/resolve.py`, `engine/rng.py` | `test_engine_*.py` |
| Content packs and validator | `engine/content.py`, `engine/schemas/`, `content/packs/core/` | `test_content_core.py` |
| Fog of war, state and report texts | `engine/view.py` | `test_engine_view.py` |
| A match: phases, deadlines, ready, messages, replay | `game.py`, `journal.py`, `store.py` | `test_game_phases.py`, `test_cli_game.py` |
| HTTP server | `server/app.py`, `server/registry.py` | `test_server_api.py`, `test_server_bots_full.py` |
| MCP bridge (`dah-bridge`) | `bridge/` | `test_bridge_tools.py` |
| Waker (`dah-waker`) | `waker/` | `test_waker_loop.py` |
| Built-in bots | `bots/` | `test_bots_*.py`, `test_e2e_bots.py` |
| Command line, reports | `cli.py`, `report.py` | `test_cli_game.py`, `test_e2e_bots.py` |

[docs/SLICE.md](docs/SLICE.md) is the specification of the current version (in Russian): read the section of the
part you change first. [docs/CONCEPT.md](docs/CONCEPT.md) is where the game is going.

## Taking an issue

1. Comment on the issue that you'd like to work on it, and wait until a maintainer assigns it to you. A pull
   request for an issue assigned to someone else may be closed.
2. **`good first issue` issues are for people making their first contribution.** Take one at a time. Once your
   first pull request is merged, move on to `help wanted` or any other issue and leave the rest to the next newcomer.
3. If you can't go on, say so in the issue. An assigned issue with no activity for two weeks goes back to the pool.

## AI tools and automated accounts

- **AI coding assistants are fine as tools.** Say in the pull request which one you used. You must have run the
  tests yourself, understand every line you submit and answer review comments yourself. A pull request whose
  author can't explain it is closed.
- **`good first issue` issues are not open to automated accounts**: bots and agents that claim issues and open
  pull requests across many repositories. Their claims are ignored and their pull requests are closed without
  review.
- Agents *playing* the game is the point of the project; that's a different thing from agents opening pull requests.

## Pull requests

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach. Changes to the rules
   and the balance especially: they change what every match measures.
2. Branch from `dev` and open the pull request against `dev`. `master` changes only when a new version is
   released, so a pull request against `master` gets its base switched to `dev`.
3. One pull request, one issue: write `Fixes #N` in the description, and leave unrelated changes for another pull
   request. Add tests for new behaviour and fixed bugs.
4. Write code like the code around it, and reuse existing helpers instead of copying them.
5. Run `uv run pytest -q` and `uvx ruff check src tests` before pushing; CI runs both on Linux, Windows and macOS.
   On your first pull request, CI waits until a maintainer approves the run (GitHub's protection for new
   contributors): that's expected, there's nothing to fix.
6. Update the docs when behaviour visible to users or players changes: the guide pages in [docs/en](docs/en/README.md)
   and [docs/ru](docs/ru/README.md) (the same files in both languages — change both, or say in the PR which one needs
   a translation), the README if the short overview changes, and [docs/SLICE.md](docs/SLICE.md) for the contracts.

## Code conventions

- Code, comments and docstrings are in English. Texts that players see (rules, state, reports, MCP tool
  descriptions, the goal text) are in Russian for now.
- The engine is a pure library: apart from loading packs in `engine/content.py` it has no I/O, no clock and no
  global randomness. Randomness comes from `engine/rng.py` streams derived from the seed, so a match replays to the same `state_hash`. A change that breaks `dah replay` of
  an existing game needs a reason in the pull request.
- The game must stay fair for every kind of player: a bot, a single agent, a swarm and a human get the same API, the
  same texts and the same information. Bots get no shortcuts to hidden state.
- Content lives in packs, not in code. A new kind of effect or stat goes into `engine/registry.py` and its schema,
  so that packs can use it.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
