# Content packs

**English** · [Русский](../ru/modding.md) · [All docs](README.md)

All game content lives in packs: resources, province types, units, buildings, factions, maps and scenarios. The
engine knows nothing about concrete units or resources, only about resource **roles** and addressable **stats**.
The base pack is `content/packs/core/`; section 2 of the [slice specification](../SLICE.md) (in Russian) is the
reference these rules come from.

## Pack layout

A pack is a folder with `pack.yaml` and YAML files grouped by entity type:

```
mymod/
  pack.yaml
  resources/  province_types/  units/  buildings/  factions/  maps/  scenarios/
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
| resource | `id`\*, `name`\*, `short`, `role`\* (`build` or `upkeep_food`) |
| province type | `id`\*, `name`\*, `short`, `income`\* (`{resource: n}`), `defense_mul` (above 0, up to 5; default 1) |
| unit | `id`\*, `name`\*, `short`, `attack`\*, `defense`\*, `cost`\*, `upkeep`\*, `counters` (list of units) |
| building | `id`\*, `name`\*, `short`, `cost`\*, `build_turns`\* (1..10), `effects`, `recruit`, `max_per_province` |
| faction | `id`\*, `name`\*, `lore` (up to 1000 characters), `bonuses`, `penalties`, `starting`\* |
| map | `id`\*, `name`\*, `max_players`\* (1..16), `provinces`\*, `edges`\*, `starts`\* |
| scenario | `id`\*, `name`\*, `map`\*, `factions`\*, `turn_limit`\*, `phase_limits`\*, `message_limits`, `overrides` |

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
- The first faction of a scenario's `factions` is the one seats get.

## Effects

There are six effect types. Each one is allowed only in some places.

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
| `combat.fortify_bonus` | float 0..1 | 0.25 | defense bonus of `fortify` |
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

## Validation

```sh
uv run dah pack validate content/packs/core path/to/mymod   # pass the dependencies together with the mod
```

The validator checks:

1. the JSON Schema of every file (the schemas are in `src/dah/engine/schemas/`);
2. references: every id a pack mentions exists among the loaded packs, and every pack in `requires` is loaded;
3. effects: known type, allowed in this place, correct parameters, a known stat path;
4. resource roles: exactly one resource for each role (`build`, `upkeep_food`);
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
