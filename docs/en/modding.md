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
  objectives/                             # stage 4 (optional)
  discoveries/                            # stage 5 (optional)
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

There are twelve effect types (the six below, `unlock` and `grant_tech` of Stage-2 content, and `grant_artifact`,
`lose_units`, `spawn_neutral`, `set_status` of finds, see Stage-5 content). Each one is
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
| `combat.fortify_persists` | int 0..1 | 0 | 1: a fortification lasts while the owner's units stay in the province (the `core` 0.5.0 scenarios set 1) |
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
  `ward {cost}` (3b), `shade.free` (0..3: idle Shades every seat holds from the start, within the limit; `core`
  0.6.0 sets 1) — and `missions: [...]`. A mission has `id`, `name`, `behavior` (`observe_province`,
  `read_ledger`, `read_objective`, `read_orders`, `sabotage`), `target` (`province`, `owned_province`, `seat`),
  `difficulty`, `rounds: [min, max]`, `requires: {whisper, tech?}`, `cost` (once), `cost_per_round`, `params`
  (sabotage: `deny_fortify`, `defense_mul`, `income_mul`), `requires_system` (e.g. `scoring.objectives` for
  `read_objective`: without the system the mission does not exist), `description`.
- **`dispositions/*.yaml`** (needed when conquest is on): the fates of a captured province — `id`, `name`,
  `behavior` (`annex`, `raze` with `params.ruin_turns`, `raid` with `params.ravage_turns`, `release`), `cost`,
  `loot`, `description`.
- **`conquest_value`** (1..5, default 1) on a province type multiplies fate prices and loot (the capital is 3 in
  `core`).
- **Stats** (only while their system is on): `spy.*` (`spy.whisper`, `spy.mirrors`, `spy.shades_max`,
  `spy.rounds_bonus`, `spy.detect.army`, `spy.attribution.base`, `spy.mission.<id>.difficulty|max_rounds`,
  `spy.shade.cost|upkeep.<res>`, `spy.shades_free`, ...), `province.mirrors`, `p2.cost.spy`, `p2.cost.recall`, `feint.cp_cost`,
  `disposition.<id>.cost_mul|loot_mul`, `conquest.claim_turns`, `conquest.claim_bonus`, `conquest.loot_cooldown`. The
  full table with ranges is section 1.2 of the specification; `rules(topic="spying")` and `rules(topic="conquest")`
  show a match's numbers.

## Stage-4 content: score sources, great works, secret objectives

The authoritative description is sections 1–3 and 11 of the [scoring specification](../SCORING.md) (in Russian);
`content/packs/core` 0.5.0 uses every field below. Everything is optional: a scenario without `scoring` scores land
only (1 per province, +3 per capital) and hashes as before.

- **Weights.** A scenario's `scoring: {territory, science, works, war, objectives}` (each 0..5; a source not named is
  0; some source other than `objectives` must be > 0). The host may override per match: `dah game create --scoring
  territory=1,science=1,works=0,war=1,objectives=1 --objectives N`. A source with weight 0 is an off system
  `scoring.<src>`: entities with `requires_system: scoring.<src>` are hidden and `modify` lines on its `score.*`
  stats are dropped.
- **Buildings** gain `score` (0..10: points while ready in an own full province; stat `building.<id>.score`),
  `max_per_owner` (1..10: the `build` order is refused with `ALREADY_BUILT` once a seat has that many, ready or
  building, in all its provinces) and `allowed_types` (province types where it may be built). A building with
  `score > 0` is a great work: its start and completion are public events; the validator warns when its
  `build_turns` < 2.
- **`objectives/*.yaml`**: `id`, `name`, `kind` (engine kinds: `hold_type {type, count}`, `hold_count {count}`,
  `hold_foreign {count}`, `capital_other {count}`, `techs_branch {branch, count}`, `great_work {count}`,
  `rival_capital_taken` — targets a rival seat), `params`, `points` (1..8), `difficulty` (optional: `easy`,
  `medium`, `hard`; points must grow with the tier, a missing tier next to set ones is a warning), `maps` (empty:
  any), `players {min, max}`, `requires_system`, `description`, `lore`. A scenario with objectives on sets `objectives: {dealt: 1}`
  (optionally `pool: [...]`; `offer` must equal `dealt`).
- **Validation of objectives** (every scenario with objectives on, every seat count 2..`max_players` of its map, every
  admitted faction): the pool after the `maps`/`players`/`requires_system` filters holds at least max(3, `dealt`)
  objectives; none is met at any start (after start effects); each can be met (enough provinces of the type, techs of
  the branch researchable by every faction, a great work every faction can build on the map, ...).
- **Stats** (only while their source is on): `score.territory.province|capital`, `score.science.tier1..4`,
  `score.science.branch`, `score.war.per_point|cap_per_victim|cap|defense_pct`, `score.betrayal.per_break` (0..10,
  points lost per pact the seat broke; works under any non-default scoring). `rules(topic="victory")` shows a
  match's numbers.

## Stage-5 content: Abandoned lands and finds

The authoritative description is sections 2–5, 8, 9 and 11 of the [stage-5 specification](../ABANDONED_LANDS.md)
(in Russian), as amended by decision 25 of the [concept](../CONCEPT.md) and its «Реализация: уточнения» section.
Everything is optional: with the system off (no `systems: {discoveries: true}` in the scenario and no
`--discoveries on`), the folder `discoveries/`, the scenario's `discoveries` knobs and the maps' `rotation` are left
out of the ruleset and its hash.

- **`discoveries/*.yaml`** — finds:

```yaml
- id: sleeping_golem
  name: {ru: Спящий голем, en: Sleeping Golem}
  kind: mixed                 # boon | hindrance | mixed
  rarity: common              # boon/mixed: common|uncommon|rare; hindrance: minor|major
  weight: 3                   # optional, 1..20; default: the rarity's weight
  triggers: [army]            # `shade` gives the warning «ignored in this stage»
  players: {min: 2}           # optional, like objectives
  requires_system: conquest   # optional
  description: {ru: "…", en: "…"}
  choices:                    # mixed only; boon/hindrance have `effects: [...]` instead
    - id: wake
      label: {ru: Разбудить, en: Wake}
      outcomes:               # p sum to 1
        - {p: 0.7, effects: [{effect: grant_units, unit: walker, count: 2, where: here}]}
        - {p: 0.3, effects: [{effect: lose_units, frac: 0.3, min: 2}]}
    - {id: leave, label: {ru: Не трогать, en: Leave}, effects: []}
  default_choice: leave
```

- **Effects of finds.** `add_resource {resource, amount}` (a boon is shared by army price; a negative amount is a
  hindrance every entering seat pays in full, never below 0), `grant_units {unit, count, where: here}` (they arrive
  after the battles), `grant_building {building, where: here}`, `grant_artifact {artifact}` (only an ordinary
  artifact whose effects are all passive; the most expensive army's seat gets it; a seat that already has it gets
  15 × its power in the build resource), `lose_units {frac, min?}` (each army: cheapest units first until `frac` of
  its price and at least `min` units; 0..20), `spawn_neutral {units, grow? {every, add, cap}, reward? [...]}` (a
  guard or a lair; not inside a choice), `set_status {status: ravaged|ruined, turns}`. `where: here` is refused
  outside finds. A reward (`spawn_neutral.reward`, the scenario's `garrison.reward`) takes only `add_resource > 0`
  and `grant_units`.
- **Power.** The author does not write it: the validator computes `power` against a reference army (5 units of
  price 12; 1 P = 15 resources = a unit price of 20) and `power_small` against 1 and 2 of the pack's cheapest common
  unit; a mixed find gets the EV of each choice (its `power` is the best EV) and `risk` (best minus worst outcome).
  `describe`, `rules(topic="discoveries")` and `dah pack validate` show them. **Errors:** power outside its rarity's
  range (boon common 1.0–2.5, uncommon 2.5–4.0, rare 4.0–6.0; hindrance minor −1.5…−0.5, major −3.0…−1.5; mixed as
  boons, common from 0.5), a boon ≤ 0 or a hindrance ≥ 0, p not summing to 1, a `default_choice` not among the
  choices, an effect outside its context, a non-passive or faction-unique artifact. **Warnings:** the `shade`
  trigger; the risk lever (some choice must have a higher EV than the default, and the default must be the least
  risky); **scout sweep pays** — the weight-averaged `power_small` of the scenario's pool, or of one of its rings,
  is ≥ 0 for a 1- or 2-unit army. Any army sets a find off, so hindrances must be serious enough (a high `min` of
  `lose_units`, a resource loss) that sweeping with single cheap units loses on average.
- **The scenario's `discoveries` knobs** (all optional): `souls` (`ravage` — ravaged 3 turns, `halve` — income ×0.5
  for good, `ashes` — ruined 2 turns; `ravaged_turns`, `ruined_turns`, `income_mul` next to it override the preset),
  `garrison {units, reward}` (the ruins' guard and its reward), `density` (0..1, share of the Abandoned candidates
  that get a find; 0.67), `extra` (0..20: candidates from ordinary neutral provinces too), `rings` (`{ring: [rarity
  tokens]}`, ring ≥ 2 — ring 1 next to a start stays empty; tokens `common`, `uncommon`, `rare`, `boon`, `minor`,
  `major`, `hindrance`, `mixed`, `mixed_common`, …), `w` (`{ring: weight}` of the fairness budget), `max_ring` (3),
  `fair_tolerance` (1.0), `tries` (64), `observe_mirrors` (0..5, C of an Observation of Abandoned land; 2),
  `fair_mirror` (copy finds over the map's rotation), `ruins_find` (false), `pool` (a subset of find ids). An unknown
  knob is an error.
- **Maps** may set `rotation: {province: province}`: a permutation that keeps the edges and steps start *i* to start
  *i+1*; `fair_mirror` uses it. It is hashed only with the system on.

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
