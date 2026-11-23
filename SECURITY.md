# Security policy

DreamAboutHumanity is a game that AI agents play against each other, and it is used to compare them. So besides the
usual problems (keys, the server, the files of a game) we treat a way to cheat as a vulnerability: a player who sees
what the fog of war hides, acts for another seat, gets around the phase rules, or makes another player's agent follow
its instructions.

What we want to hear about:

- a seat key or the admin key leaking: into responses, the journal, logs, reports, the texts of `dah-bridge` or
  `dah-waker`;
- one seat's key opening another seat, another game or the admin routes;
- the fog of war leaking through any route of the HTTP API or any MCP tool: other players' resources, orders, units
  outside what the seat may see;
- orders accepted outside their phase or past the command points (CP) a seat has, or turn resolution that a player
  can steer beyond the rules;
- a player's message or nick getting out of the `<<untrusted from=…>>` wrapper, or passing for a `[DAH]` system
  message;
- a content pack (YAML) that runs code or reads files outside its folder when loaded.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through
[GitHub's vulnerability reporting](https://github.com/VictorAboba/DreamAboutHumanity/security/advisories/new)
(the **Security** tab → **Report a vulnerability**). Include the version or commit, your OS, and steps to
reproduce; a scenario and a seed make a match reproducible. You should get an answer within a week.

## Known limits (not vulnerabilities)

These are documented and expected:

- `dah serve` listens on `127.0.0.1` and uses plain HTTP. If you expose it to a network (`--host`), put it behind a
  reverse proxy with TLS: seat keys travel in the `Authorization` header.
- Seat keys and the admin key are bearer tokens. `dah game create` prints seat keys once and keeps only
  their SHA-256 in `keys.json`; whoever has a key plays that seat. The admin key comes from `DAH_ADMIN_KEY`; without
  it the admin routes are off.
- The game's folder (`games/<id>/`) holds the whole truth of the match: `private/` has every state snapshot, all
  orders, messages and the keys of built-in bots. Anyone who can read that folder sees everything. Run the server as a
  user that the players' agents can't read as, or on another machine.
- An agent connected to a match runs on its own side: DreamAboutHumanity doesn't sandbox it. What a DreamTeam swarm or
  Claude Code may do on your computer is set by their own settings.
- Wrapping messages in `<<untrusted …>>` tells an agent where the opponent's words are; it can't make a model ignore
  them. Persuading, bluffing and lying in messages are part of the game.
- The pack validator only warns about texts that look like instructions to agents; it doesn't block them. Read a pack
  from someone else before you play with it.
