# Game rules

**English** · [Русский](../ru/rules.md) · [All docs](README.md)

These are the rules of the vertical slice as the engine implements them. The exact contracts are in the
[slice specification](../SLICE.md) (in Russian); the [full concept](../CONCEPT.md) (in Russian) describes the game
the slice grows towards. Numbers below are the defaults of the `core` pack; a scenario or a pack can change them
([Content packs](modding.md)).

Players see Russian texts and Russian phase names. This guide uses English names and gives the Russian ones once:

| English | Russian | Id in the API |
|---|---|---|
| N1, Negotiation 1 | П1, переговоры | `negotiation_1` |
| A, Actions | Д, действия | `actions` |
| intel slice | разведсрез | journal event `recon` |
| N2, Negotiation 2 | П2, переговоры-2 | `negotiation_2` |
| resolution | расчёт | `resolution` |
| command points, CP | очки командования, ОК | `cp_left`, `command_points` |
| hasty (penalty) | наспех | `hasty`, `p2.hasty_penalty` |

## The map and the seats

The map is a graph of provinces (`P01`, `P02`, ...). An order moves units along one edge. Each seat gets a start:
a capital and a few provinces around it. Starts are dealt to seats by a seeded shuffle. If there are fewer seats
than starts, a free start stays neutral land with no armies.

At the start each seat of the `covenant` faction has 40 alloy, 30 provisions, 3 assault units and barracks in the
capital.

## A turn

| Phase | What players do | Messages | Orders |
|---|---|---|---|
| N1 | negotiate | yes | free; orders are drafts and stay in force |
| A | submit orders | no | free |
| intel slice | (empty in this version) | — | — |
| N2 | negotiate, adjust orders | yes | each change costs CP |
| resolution | the engine resolves the turn at once | — | — |

- Each phase has a time limit (in `slice3`: 20 min, 20 min, 10 min; in `blitz_test`: 5 s each). A phase closes on
  its deadline or as soon as every living seat has pressed ready.
- The intel slice is an empty step between A and N2: only a `recon` event in the journal.
- After the last turn, or when one seat is left, the match moves to `game_over`.

### Ready

- `ready(confirm_hash)` takes the `orders_hash` of the seat's current orders (from `list_orders` or the answer of
  `submit_orders`). If the orders have changed since, the server answers `STALE_HASH`. Ready is pressed in every
  phase, negotiation phases included.
- Any change of orders takes ready back (`ready_cleared` in the journal). `unready` takes it back by hand.
- Opponents see only "ready N of M".
- A seat that never pressed ready keeps the orders it submitted. A seat that submitted nothing plays on autopilot:
  its armies stand still.

## Resources

Two resources, each with an engine role:

- **Alloy** (`core:alloy`, role `build`): pays for units and buildings.
- **Provisions** (`core:provisions`, role `upkeep_food`): feeds the army.

Income comes at the end of the turn from provinces and buildings, then the army's upkeep is paid in provisions. If
provisions drop below zero, the army starves: it loses 10% of its units (at least 1, the cheapest first), and the
stock is set to 0. No resource can go below 0.

## Provinces

| Type | Russian | Income per turn | Defense |
|---|---|---|---|
| `forge` | Кузнечный мир | 4 alloy | ×1 |
| `agri` | Агромир | 5 provisions | ×1 |
| `waste` | Пустошь | 1 alloy, 1 provisions | ×1.25 |
| `capital` | Столица | 3 alloy, 3 provisions | ×1.5 |

The defense multiplier applies only to the side defending the province.

## Units

| Unit | Russian | Attack | Defense | Cost | Upkeep per turn | Counters |
|---|---|---|---|---|---|---|
| `assault` | Штурмовики | 3 | 3 | 12 alloy | 1 provisions | `artillery` |
| `armor` | Бронеход | 5 | 4 | 25 alloy | 2 provisions | `assault` |
| `artillery` | Артиллерия | 4 | 1 | 20 alloy | 1 provisions | `armor` |

A unit "counters" another: if the enemy side has any unit this unit counters, its strength is multiplied by 1.5
(`combat.counter_mul`). The three units form a cycle.

## Buildings

| Building | Russian | Cost | Build time | Effect |
|---|---|---|---|---|
| `mine` | Шахта | 20 alloy | 1 turn | +3 alloy per turn |
| `farm` | Ферма | 15 alloy | 1 turn | +3 provisions per turn |
| `barracks` | Казарма | 30 alloy | 2 turns | allows recruiting |

A province holds at most one building of each kind. Recruiting is possible only in a province with barracks. A
building ordered this turn with a build time of 1 is finished at the end of this turn's resolution and gives income
from the next turn. A construction is lost if its province changes owner.

## Orders

Orders are JSON objects. The server gives each accepted order an id (`o1`, `o2`, ...). Unit and building ids may be
short (`assault`) or full (`core:assault`); a unit may also be named by its state label (`штурм`) or its name.

```
{"kind": "move", "from": "P01", "to": "P02", "unit": "assault", "count": 2}
{"kind": "recruit", "province": "P01", "unit": "assault", "count": 1}
{"kind": "build", "province": "P02", "building": "mine"}
{"kind": "fortify", "province": "P01"}
```

- `move`: from an own province to a neighbouring one. Into a foreign province, or a neutral one with an army, it
  is an attack. The flat form above (`unit` + `count`, one unit type) is the most robust for any client. Several
  unit types go either into several moves or into the nested form `"units": {"assault": 2, "armor": 1}`; `units`
  may also be a list `[{"unit": "assault", "count": 2}]`. The server stores every move in the nested form, so all
  forms give the same order, `orders_hash` and replay.
- `recruit`: in an own province with barracks.
- `build`: in an own province that has no such building yet.
- `fortify`: +25% defense (`combat.fortify_bonus`) if no units leave the province.

- A unit can be in only one order: units in a `move` are reserved.
- Resources of `recruit` and `build` are reserved when the order is accepted and spent at resolution.
- Recruited units appear in the province at the start of resolution: they defend it this turn but cannot move.
- If any unit leaves a fortified province, the fortification is broken.
- At resolution every order is checked again against the state at the start of the turn. An order that no longer
  holds is dropped, and its seat sees why in the turn report.

Reasons of a refusal: `NOT_ADJACENT`, `NOT_OWNER`, `NOT_ENOUGH_UNITS`, `NOT_ENOUGH_RESOURCES`,
`NO_RECRUIT_BUILDING`, `ALREADY_BUILT`, `PHASE_LOCKED`, `NOT_ENOUGH_CP`, `UNKNOWN_ID`, `INVALID`.

`submit_orders` has two modes: `replace_all` drops the seat's orders and puts the new ones in their place; `append`
adds to them. Each submitted order is checked against the ones before it, so reservations add up.

`orders` may also be passed as one JSON string holding the same list:
`orders="[{\"kind\":\"move\",\"from\":\"P01\",\"to\":\"P04\",\"unit\":\"assault\",\"count\":2}]"`. This is
guaranteed for `validate_orders`, `submit_orders` and the `patch` of `amend_order`: some MCP clients lose nested
objects (an empty `units`) but pass strings intact. A move with an empty or missing `units` is refused with
`INVALID`; the message shows what arrived and both workarounds.

## N2 and command points

In N1 and A orders are free to add, change and cancel. In N2 every change costs command points (CP). A seat gets
3 CP every turn; they do not carry over.

| Change in N2 | Price |
|---|---|
| cancel an order | 1 CP; only 75% of its reserve comes back (`p2.cancel_refund`) |
| reduce `units` of a move or `count` of a recruit | 1 CP |
| new `fortify` | 1 CP |
| new `move`, a new target or more units for a move | 2 CP (`p2.cost.new_attack`); the move becomes hasty |
| new `recruit` or `build`, more units in a recruit | not allowed: `PHASE_LOCKED` |

- A hasty move attacks with 15% less strength (`p2.hasty_penalty`). This is the price of a surprise attack after
  negotiations: in N2 attacking is allowed.
- `replace_all` is refused in N2; use `append`, `cancel_order` and `amend_order`.
- `amend_order` changes `units` or `to` of a move and `count` of a recruit. A `build` or `fortify` can only be
  cancelled. The units of a move may be given flat too: `amend_order(id="o3", patch={"unit": "assault",
  "count": 1})`.

## Resolution

The engine resolves a turn in this order:

1. **Orders.** Reserved resources are spent. Recruited units appear. Constructions start.
2. **Movement**, all at once. If two enemy armies move along the same edge towards each other (A→B and B→A), they
   fight on the edge. The winner goes on; the loser goes back where it came from.
3. **Battles** in every province where armies of different owners meet. Several attackers first fight each other,
   strongest against the next (ties broken by the seeded random stream); the winner then fights the defender.
4. **Owners change.** A province with only the attacker's units left becomes the attacker's. An empty foreign or
   neutral province is taken without a battle.
5. **Economy**: income, then upkeep and starvation.
6. **Construction**: buildings whose time is up are finished.
7. **Elimination**: a seat with no provinces is out.
8. **Victory check** and the turn report.

## Combat

Strength of a side:

```
strength = Σ over units (attack, or defense for the defender)
           × 1.5 for units that counter something the enemy has
           × province defense multiplier (defender only)
           × 1.25 if the province is fortified (defender only)
           × 0.85 for hasty attacking units
           × random factor 0.9–1.1
```

The stronger side wins. With `S_w` the winner's strength and `S_l` the loser's:

- the loser loses `min(1, 0.5 · S_w / S_l)` of its units;
- the winner loses `0.3 · S_l / S_w` of its units;
- counts are rounded half up, and losses come from the cheapest units first.

After a battle:

- a beaten attacker's survivors go back to the provinces they came from;
- a beaten defender's survivors retreat to a neighbouring province of the same owner; if there is none, they are
  lost;
- neutral defenders are destroyed.

### Estimate

`estimate(attacker, defender, province_type, fortified, hasty)` forecasts one attack. It runs the same battle
formula 200 times with a fixed seed and returns `win_prob`, `attacker_losses_avg` and `defender_losses_avg`. The
same inputs always give the same answer. It knows nothing about the real map: you pass the armies yourself. The
only map lookup is `province_type`, which may also be a province id (`P07`): it stands for that province's type.
Unit and type ids may be full, short or the labels from the state text (`штурм`, `агро`).

```json
{"attacker": {"assault": 4}, "defender": {"assault": 3}, "province_type": "waste", "fortified": false, "hasty": false}
```

## Fog of war

- A seat sees its own provinces and their neighbours in full: owner, units, buildings, fortification.
- Of every other province it sees the name, type and owner (the political map is public); units are shown as `?`.
- Other seats' resources, orders and messages are never visible.
- The turn report of a seat holds the events in its provinces and in the provinces it saw before or after the turn.
  Economy, starvation, recruiting, constructions started, broken fortifications and dropped orders are seen only
  by their own seat.

`get_state` returns a compact Russian text, roughly like this:

```
ХОД 3/10 | Д ДЕЙСТВИЯ | до 14:30Z (ещё 12 мин) | ОК 3
Казна: Сплав 52 (+11) Провиант 24 (-1)
P01 Столица Аргос [ТЫ] столица штурм×3 | казарма | → P02 P03 P04
P07 Нивы Лира [Хор#2] агро штурм×2 | → P02 P05
P13 Сердце Кузен [нейтр.] кузн. ? | → P04 P08 P12 P14 P15
```

With `since_turn=N` it gives the treasury, the own and visible provinces that changed, and a ИЗМЕНЕНИЯ (changes)
line. `format=json` gives the same view as JSON.

## Messages

- Messages can be sent only in N1 and N2. `to` is a list of nicks or `"all"`.
- Limits per phase (defaults): N1 up to 20 private messages, up to 6 to one recipient, 2 public; N2 up to 10
  private, up to 10 to one recipient, 1 public. A message is at most 800 characters. A private message to several
  recipients counts once per recipient.
- Incoming text is wrapped as `<<untrusted from=Nick>>…<</untrusted>>`. Anything that looks like a `[DAH]` tag
  inside it is replaced by `(DAH?)`, so a player cannot imitate a host wake-up message.
- Message numbers in `inbox` count only the messages addressed to you and say nothing about other players' traffic.

## Victory and scoring

- The match ends when one living seat is left (it wins) or after the last turn.
- After the last turn the seats with the most points win: 1 point per province, plus 3 per capital, own or
  captured. A draw is possible.
- Scores are public and shown in `get_state` and in each turn report.

## The core content

- Faction: `covenant` «Завет Предела», no bonuses.
- Maps: `mirror3` (3 starts, 15 provinces: each start is a capital with a forge, agri and waste world; three
  contested provinces in the middle) and `mirror2` (2 starts, 10 provinces). The starts are symmetric.
- Scenarios:

| Scenario | Map | Turns | Phase limits N1 / A / N2 |
|---|---|---|---|
| `core:slice3` | `mirror3` | 10 | 20m / 20m / 10m |
| `core:slice2` | `mirror2` | 10 | 20m / 20m / 10m |
| `core:blitz_test` | `mirror3` | 6 | 5s / 5s / 5s |

`dah game create --limits` and `--turns` override the scenario's limits for one match.

## The rules tool

`rules()` returns these rules in Russian, generated from the match's ruleset (so scenario changes show up). With
`topic` it returns one section: `turn` or `phases`, `resources`, `provinces`, `units`, `buildings`, `orders`, `p2`
or `cp`, `combat` or `resolution`, `victory`, `fog` or `visibility`, `messages`. The section headings of the
rules text work too (`ЮНИТЫ`, `П2`, ...). An unknown topic returns the list of topics and the whole rules.
