# Content packs

**English** · [Русский](../ru/modding.md) · [All docs](README.md)

All game content lives in packs: resources, province types, units, buildings, factions, maps, scenarios and,
since stage 2, tech branches, techs and artifacts. The
engine knows nothing about concrete units or resources, only about resource **roles** and addressable **stats**.
The base pack is `content/packs/core/`; section 2 of the [slice specification](../SLICE.md) (in Russian) is the
reference these rules come from.

## Pack layout

A pack is a folder with `pack.yaml` and YAML files grouped by entity type:

```
mymod/
  pack.yaml
  resources/  province_types/  units/  buildings/  factions/  maps/  scenarios/
  branches/  techs/  artifacts/  lore/      # stage 2 (optional)
```

- Every `*.yaml` file in a type folder is read. A file holds one entity or a list of entities.
- Entities are processed in order of their full id, not of files.
- Other folders and files are ignored. The README mentions an optional `locale/ru.yaml`, but the loader of this
  version does not read it.

`pack.yaml`:

```yaml
id: mymod
version: 0.1.0          # semver: X.Y.Z
title: My mod           # up to 48 characters
author: me              # optional
requires: [core]        # packs that must be loaded together with this one
engine_api: "^1"        # the engine API version is 1
```

## Ids

- Ids inside a pack are short: lowercase letters, digits and `_`, up to 32 characters (`sniper`).
- After loading an entity has the full id `pack:id` (`mymod:sniper`).
- Inside a pack you may refer to its own entities by the short id. Refer to another pack's entities by the full id
  (`core:alloy`).
- Province ids on a map are different: an uppercase letter followed by up to 7 uppercase letters or digits (`P01`).

## Entity fields

Fields marked * are required. `name` is up to 48 characters, `short` (an abbreviation for the state text) up to 12.

| Type | Fields |
|---|---|
| resource | `id`\*, `name`\*, `short`, `role`\* (`build`, `upkeep_food` or `research`) |
| province type | `id`\*, `name`\*, `short`, `income`\* (`{resource: n}`), `defense_mul` (above 0, up to 5; default 1) |
| unit | `id`\*, `name`\*, `short`, `attack`\*, `defense`\*, `cost`\*, `upkeep`\*, `counters` (list of units) |
| building | `id`\*, `name`\*, `short`, `cost`\*, `build_turns`\* (1..10), `effects`, `recruit`, `max_per_province` |
| faction | `id`\*, `name`\*, `lore` (up to 1000 characters), `bonuses`, `penalties`, `starting`\* |
| map | `id`\*, `name`\*, `max_players`\* (1..16), `provinces`\*, `edges`\*, `starts`\* |
| scenario | `id`\*, `name`\*, `map`\*, `factions`\*, `turn_limit`\*, `phase_limits`\*, `message_limits`, `diplomacy_limits`, `overrides` |

Details:

- `turn_limit` of a scenario is 1..200.
- `recruit: true` lets a building recruit units. `max_per_province` can only be 1.
- `starting` of a faction: `resources` (`{resource: n}`), `units` (`{unit: n}`, placed in the capital), `buildings`
  (list, built in the capital), `effects` (start effects, see below).
- A map's `provinces` are `{id, name, type}`; `edges` are pairs `[P01, P02]`; `starts` are `{capital, provinces}`.
  The number of starts must equal `max_players`.
- `phase_limits` of a scenario has `negotiation_1`, `actions` and `negotiation_2`, each in seconds or as `20m`,
  `5s`, `1h`.
- `message_limits` may set `negotiation_1` and `negotiation_2` (each with `per_recipient`, `private`, `public`) and
  `max_chars` (1..4000). Anything not set keeps the default: in N1 6 per recipient, 20 private, 2 public; in N2
  10 per recipient, 10 private, 1 public; 800 characters.
- `diplomacy_limits` may set `gifts_per_phase` and `pact_proposals_per_phase` (0..50 per seat and negotiation
  phase; default 3 and 3).
- Phase and diplomacy limits are match settings: every match's config records them, and they are left out of the
  ruleset hash (changing a default does not make `dah replay` of an older match report changed content).
- Without a setup phase (`setup: none`) a seat gets its configured faction, else the first faction of the
  scenario's `factions`. With one, seats choose (see Stage-2 content below).

## Effects

There are eight effect types (the six below plus `unlock` and `grant_tech`, see Stage-2 content). Each one is
allowed only in some places.

| Effect | Parameters | Where |
|---|---|---|
| `modify` | `target`, `op`, `value` | faction `bonuses`/`penalties`, scenario `overrides` |
| `income` | `resource`, `amount` | building `effects`, faction `bonuses`/`penalties` |
| `add_resource` | `resource`, `amount` | faction `starting.effects` |
| `grant_units` | `unit`, `count`, `where: capital` | faction `starting.effects` |
| `grant_building` | `building`, `where: capital` | faction `starting.effects` |
| `set_flag` | `flag` | faction `starting.effects` |

- `modify` takes a stat path in `target` (see [Stats](#stats)), `op` is `add`, `mul` or `set`.
- `income` is a permanent addition to income: from each building of this kind, or from the faction as a whole.
- Faction bonuses and penalties act all the time (the `passive` hook); start effects act once at game start
  (`on_game_start`).
- `set_flag` only stores the flag on the player; no rule in this version reads it.

## Stats

`modify` changes a stat. In a target, unit, building and resource ids may be short: `unit.assault.attack`.

| Path | Type, bounds | Default | Meaning |
|---|---|---|---|
| `resource.<id>.income_mul` | float 0.5..2.0 | 1.0 | income multiplier of a resource |
| `unit.<id>.attack`, `unit.<id>.defense` | int 0..20 | from content | unit strength |
| `unit.<id>.cost.<res>` | int 0..200 | from content | recruit price |
| `unit.<id>.upkeep.<res>` | int 0..20 | from content | upkeep per turn |
| `building.<id>.cost.<res>` | int 0..300 | from content | building price |
| `p2.hasty_penalty` | float 0..0.5 | 0.15 | strength penalty of moves started in N2 |
| `p2.cost.new_attack` | int 1..5 | 2 | CP price of a new move or retarget in N2 |
| `p2.cancel_refund` | float 0..1 | 0.75 | share of the reserve returned on an N2 cancel |
| `command_points` | int 0..10 | 3 | CP per turn |
| `combat.fortify_bonus` | float 0..1 | 0.25 | defense bonus of `fortify` (the `core` scenarios override it to 0.15) |
| `combat.counter_mul` | float 1..3 | 1.5 | counter multiplier |
| `starvation.loss_frac` | float 0..1 | 0.10 | share of the army lost to starvation |

### Modifier order

Modifiers of one stat are applied in this order:

1. the base value (from content or the engine default);
2. all `add`;
3. all `mul`;
4. `set`: the last one wins, in assembly order (scenario overrides first, then the faction's effects);
5. clamp to the stat's bounds.

A value out of bounds written in the file itself (for example `attack: 99`) is an error. A value that leaves the
bounds only after modifiers is clamped, and the validator gives a warning.

## Stage-2 content: techs, artifacts, abilities, the setup phase

The authoritative description is sections 1–2 and 6 of the [stage-2 specification](../TECH_AND_SETUP.md) (in
Russian); `content/packs/core` uses every field below. A pack without techs, branches and artifacts keeps the
slice's rules: it hashes as before stage 2, and `unit.<id>.move` defaults to 1 there (2 otherwise).

Names may be a string or `{ru, en}` (Russian is shown to players). `lore` may be inline text or a path to a Markdown
file of the pack (`lore/litany.md`); `description` (up to 1000 characters) is a short text for `describe`. Texts
(`lore`, `description`, `name_en`, branch `color`) are left out of the ruleset hash. An effect's kind may be written
as `type:` or `effect:`.

| Type | Folder | Fields |
|---|---|---|
| branch | `branches/` | `id`\*, `name`\*, `order`, `color`, `description` |
| tech | `techs/` | `id`\*, `name`\*, `branch`\*, `tier`\* (1..10), `cost`\* (in the `research` resource), `requires` (all of), `requires_any` (one of), `requires_tier {tier, count, same_branch=true}`, `factions`, `effects`, `unlocks` (informative), `description`, `lore` |
| artifact | `artifacts/` | `id`\*, `name`\*, `power`\* (weight for a fair draft), `cost` (points mode, 0..10), `factions`, `exclusive_with`, `effects`\*, `description`, `lore` |

New fields of existing types:

- resource: `role` may also be `research` (at most one resource; it is spent only on techs, and the validator
  warns about a unit or building priced in it). Without it the match has no techs.
- unit: `move` (1..5, edges through own land), `requires {tech: [...], building: [...]}` (the building in the
  province of recruitment), `factions` (a unique unit), `abilities` (`[siege]` or `[{id: siege, cap: 1.0}]`).
  Abilities: `siege {cap}`, `ignore_fortify`, `garrison {defense}`, `fast {range}`.
- building: `requires {tech: [...]}`, `factions`; `effects` may hold `modify` with `scope: province` (the
  province's own stats) or `scope: owner` (the owner's stats while the building stands).
- faction: `description`; `bonuses`/`penalties` may modify unit move, abilities and tech prices.
- scenario: `setup: none` or `{unique_factions: true, artifacts: draft|points|none, artifact_offer: 3,
  artifact_points: 3, artifact_pool: [...]}` (defaults shown) and `phase_limits.setup` (default 5m). With a setup
  phase seats choose factions in order of preference; a seat that does not choose gets its configured faction, else
  the first free faction of the scenario's list (see [Game rules](rules.md#the-setup-phase)). The host can
  override the mode with `dah game create --artifacts ...`.

Effects added in stage 2:

| Effect | Parameters | Where |
|---|---|---|
| `unlock` | `kind` (`unit`, `building`, `order`), `id` | tech `effects`, artifact `effects` |
| `grant_tech` | `tech` | faction `starting.effects`, artifact `effects` |

Contexts: a tech's effects act like the owner's passive effects (`modify`, `income`, `unlock`, `set_flag`); an
artifact's act for the whole match (`modify`, `income`, `unlock`) plus one-off start effects (`add_resource`,
`grant_units`, `grant_building`, `set_flag`, `grant_tech`); a building allows `income` and scoped `modify`.

Stats added in stage 2:

| Path | Type, bounds | Default | Meaning |
|---|---|---|---|
| `unit.<id>.move` | int 1..5 | 2 (1 without stage-2 content) | edges through own land |
| `unit.<id>.ability.<name>.on` | int 0..1 | 0 (1 if the unit has it) | the ability flag |
| `unit.<id>.ability.siege.cap` | float 0.5..3 | 1.0 | province defense multiplier against siege, at most |
| `unit.<id>.ability.garrison.defense` | int 0..20 | 1 | defense bonus at home |
| `unit.<id>.ability.fast.range` | int 1..5 | 2 | edges through any land |
| `tech.<id>.cost` | int 0..1000 | from content | tech price |
| `tech.branch.<id>.cost_mul` | float 0.1..5 | 1.0 | price multiplier of a branch |
| `province.defense_mul` | float 0.1..5 | 1.0 | extra multiplier of a province (buildings, scope province) |
| `province.income_mul.<res>` | float 0..5 | 1.0 | income multiplier of a province |
| `province.recruit_discount` | float 0..0.9 | 0 | recruit discount, per unit, rounded in the seat's favour |

## Stage-3 content: espionage and conquest

The authoritative description is sections 1, 2.2, 3.3 and 5 of the [stage-3 specification](../SPY_AND_CONQUEST.md)
(in Russian); `content/packs/core` 0.4.0 uses every field below. Everything here is optional: a pack without it, or a
scenario with both systems off, hashes and plays as before stage 3.

- **Switches.** A scenario's `systems: {espionage: bool, conquest: bool}` (default both off); the host may override
  them per match (`dah game create --espionage/--conquest`). While a system is off, entities with
  `requires_system: espionage|conquest` (branches, techs, buildings, artifacts) are dropped from the ruleset (a hidden
  branch hides its techs), and `modify` lines aimed at the system's stats are dropped from techs, artifacts,
  buildings and factions.
- **`espionage/*.yaml`** (needed when espionage is on): an optional `config` — `shade {name, cost, upkeep}` and
  `ward {cost}` (3b) — and `missions: [...]`. A mission has `id`, `name`, `behavior` (`observe_province`,
  `read_ledger`, `read_orders`, `sabotage`), `target` (`province`, `owned_province`, `seat`), `difficulty`,
  `rounds: [min, max]`, `requires: {whisper, tech?}`, `cost` (once), `cost_per_round`, `params` (sabotage:
  `deny_fortify`, `defense_mul`, `income_mul`), `description`.
- **`dispositions/*.yaml`** (needed when conquest is on): the fates of a captured province — `id`, `name`,
  `behavior` (`annex`, `raze` with `params.ruin_turns`, `raid` with `params.ravage_turns`, `release`), `cost`,
  `loot`, `description`.
- **`conquest_value`** (1..5, default 1) on a province type multiplies fate prices and loot (the capital is 3 in
  `core`).
- **Stats** (only while their system is on): `spy.*` (`spy.whisper`, `spy.mirrors`, `spy.shades_max`,
  `spy.rounds_bonus`, `spy.detect.army`, `spy.attribution.base`, `spy.mission.<id>.difficulty|max_rounds`,
  `spy.shade.cost|upkeep.<res>`, ...), `province.mirrors`, `p2.cost.spy`, `p2.cost.recall`, `feint.cp_cost`,
  `disposition.<id>.cost_mul|loot_mul`, `conquest.claim_turns`, `conquest.claim_bonus`, `conquest.loot_cooldown`. The
  full table with ranges is section 1.2 of the specification; `rules(topic="spying")` and `rules(topic="conquest")`
  show a match's numbers.

## Validation

```sh
uv run dah pack validate content/packs/core path/to/mymod   # pass the dependencies together with the mod
```

The validator checks:

1. the JSON Schema of every file (the schemas are in `src/dah/engine/schemas/`);
2. references: every id a pack mentions exists among the loaded packs, and every pack in `requires` is loaded;
3. effects: known type, allowed in this place, correct parameters, a known stat path;
4. resource roles: exactly one resource for each of `build` and `upkeep_food`, at most one for `research`;
5. map graphs: the graph is connected, every start has the same number of provinces of each type within two edges
   of its capital, no province is in two starts;
6. number bounds;
7. texts: a `name` or `lore` that looks like an instruction to agents (`ignore`, `игнорируй`, `system`, `токен`,
   a URL) gives a warning, not an error.

Every scenario the packs define is assembled and checked. Errors are printed as `ERROR file: path: message`,
warnings as `WARN`. The assembled ruleset has a hash (`ruleset_hash`, sha256 of its canonical JSON); the journal
records it, and `dah replay` compares it.

## Loading packs

- By default a match loads every pack in `content/packs/`. Put your pack there as `content/packs/<id>/`.
- Or list packs explicitly: `dah game create --packs content/packs/core --packs path/to/mymod ...`. The flag repeats
  for each pack, and only the listed packs load, so list `core` too.
- Name the scenario by its full id: `--scenario mymod:snipers3`.
- The list of pack folders is saved in the match config, so `dah replay` rebuilds the same ruleset. If the packs
  change after the match, replay reports a ruleset mismatch.

## Example

```yaml
# units/units.yaml
- id: sniper
  name: Снайперы
  attack: 6                      # bounds come from the registry: 0..20
  defense: 1
  cost: {core:alloy: 18}
  upkeep: {core:provisions: 1}
  counters: [core:artillery]     # gets combat.counter_mul against artillery

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
  overrides:                     # only modify is allowed in a scenario
    - {type: modify, target: unit.core:assault.attack, op: add, value: 1}
    - {type: modify, target: p2.hasty_penalty, op: set, value: 0.2}
```

A faction with a bonus, a penalty and a start effect:

```yaml
# factions/forgeborn.yaml
id: forgeborn
name: Рождённые в горне
bonuses:
  - {type: modify, target: resource.core:alloy.income_mul, op: mul, value: 1.2}
penalties:
  - {type: modify, target: unit.core:assault.defense, op: add, value: -1}
starting:
  resources: {core:alloy: 40, core:provisions: 30}
  units: {core:assault: 3}
  buildings: [core:barracks]
  effects:
    - {type: grant_units, unit: core:artillery, count: 1, where: capital}
```
