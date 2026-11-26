# Game rules

**English** · [Русский](../ru/rules.md) · [All docs](README.md)

These are the rules as the engine implements them: the vertical slice plus stage 2 (techs, the setup phase,
factions and artifacts). The exact contracts are in the [slice specification](../SLICE.md) and the
[stage-2 specification](../TECH_AND_SETUP.md) (both in Russian); the [full concept](../CONCEPT.md) (in Russian)
describes the game the slice grows towards. Numbers below are the defaults of the `core` pack, version 0.7.1 (espionage and conquest of stage 3a, the scoring of stage 4 with the decision-27 changes: great works paid in parts, intel points, war per province, army fog; 0.7.1: no war points for taking back one's own land, decision 28; both on by default in `slice3`, `slice2` and `quad4`; the Abandoned lands of stage 5 are described [below](#abandoned-lands-and-finds-stage-5)); a
scenario or a pack can change them ([Content packs](modding.md)). The tables of units, buildings, techs, factions and
artifacts are a snapshot: the match's own numbers are always in `rules(topic)` and `describe(id)` (see the end of
this page).

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
| setup phase | фаза старта, СТАРТ | `setup` |

## The map and the seats

The map is a graph of provinces (`P01`, `P02`, ...). A move goes to a neighbouring province; through the seat's
own land a stack may go further, up to its move range (see Units). Each seat gets a start: a capital and a few
provinces around it. Starts are dealt to seats by a seeded shuffle. If there are fewer seats than starts, a free
start stays neutral land with no armies.

What a seat has at the start depends on its faction (see Factions and artifacts). The neutral `covenant` starts
with 30 alloy, 15 provisions, 4 assault units and barracks in the capital; the other factions swap a unit or two for
their unique one and may get extra stock.

## A turn

| Phase | What players do | Messages | Orders |
|---|---|---|---|
| N1 | negotiate, give gifts, make pacts | yes | check only: `validate_orders` |
| A | submit orders | no | free |
| intel slice | (empty in this version) | — | — |
| N2 | negotiate, give gifts, make pacts, adjust orders | yes | each change costs CP |
| resolution | the engine resolves the turn at once | — | — |

- Each phase has a time limit (in `slice3`, `slice2` and `quad4`: 12 min, 5 min, 5 min; in `blitz_test`: 5 s each). A phase
  closes on its deadline or as soon as every living seat has pressed ready.
- Orders are given only in A. In N1 `submit_orders`, `cancel_order` and `amend_order` answer `PHASE_LOCKED` with the
  hint «в П1 приказы можно только проверить через validate_orders; подавать — в фазе Д»; `validate_orders` works in
  every open phase (in N1 by the rules of A).
- The intel slice (recon) runs between A and N2. With espionage on it plays the Shades' rounds on the actions books (see «Espionage» below); with espionage off it is an empty step: only a `recon` event in the journal.
- After the last turn, or when one seat is left, the match moves to `game_over`.
- `slice3`, `slice2` and `quad4` open with a setup phase before turn 1 (see below); `blitz_test` has none.

### Ready

- Ready is pressed in every phase, negotiation phases included, as soon as there is nothing more to say or do,
  without waiting for the timer: the phase closes when everyone is ready.
- N1 (and the setup phase): `ready()` without `confirm_hash`. No order can be given or changed in N1, so no hash is
  needed or checked (one that is passed, even a stale one from the last turn, is ignored).
- A and N2: `ready(confirm_hash)` takes the `orders_hash` of the seat's current orders (from `phase()`,
  `list_orders` or the answer of `submit_orders`; every edit makes a new one). Without it, or with a stale one, the
  server answers `STALE_HASH` and names the exact call with the current hash.
- Any change of orders takes ready back (`ready_cleared` in the journal). `unready` takes it back by hand.
- Opponents see only "ready N of M".
- A seat that never pressed ready keeps the orders it submitted. A seat that submitted nothing plays on autopilot:
  its armies stand still.

## The setup phase

Before turn 1 a scenario with `setup` opens the phase `setup` (СТАРТ, 5 min in the core scenarios). Each seat:

1. `get_setup()` shows the factions with their bonuses and drawbacks, the artifacts offered to the seat and its
   current choice.
2. `choose_faction(["ash_choir", "growth", ...])`: factions in order of preference. Factions are unique by default:
   seats that chose are served first, in a seeded order, each taking the first free faction of its list; then the
   seats that did not choose take the first free faction of the scenario's list.
3. `choose_artifacts(["trail_maps"])`: in the draft mode (`draft`, the default) each seat is offered 3 artifacts
   and takes one; in the points mode (`points`) it takes any artifacts of the pool whose costs fit the budget (3
   points by default), without the pairs marked as exclusive. A seat that does not choose gets the first artifact
   offered (draft) or pool artifacts by id while the budget lasts (points).
4. `ready()`: in this phase no `confirm_hash` is needed (in every later phase it is required).

The phase closes when every seat is ready or on its deadline; then turn 1 is built from the results and N1 opens.
Choices are private while the phase is open. After it the factions of all seats are public; the artifacts stay
hidden until `game_over`. An artifact the final faction may not hold (marked «[только …]») is dropped: in the draft
it is replaced by the default; in the points mode only its cost is refilled by default. `choose_artifacts` and
`choose_faction` warn about such a choice at once. The host can change the mode with `dah game create --artifacts
draft|points|none`, `--artifact-offer`, `--artifact-points` and `--setup-limit`.

## Resources

Three resources, each with an engine role:

- **Alloy** (`core:alloy`, role `build`): pays for units and buildings.
- **Provisions** (`core:provisions`, role `upkeep_food`): feeds the army.
- **Creed** (`core:creed`, role `research`): spent only on techs. Every resolution the whole stock of Creed goes
  into the active tech (up to what it still needs); the rest stays in stock.

Income comes at the end of the turn from provinces and buildings, then the army's upkeep is paid in provisions. If
provisions drop below zero, the army starves: it loses 10% of its units (at least 1, the cheapest first), and the
stock is set to 0. No resource can go below 0.

## Provinces

| Type | Russian | Income per turn | Defense |
|---|---|---|---|
| `forge` | Кузнечный мир | 4 alloy | ×1 |
| `agri` | Агромир | 4 provisions | ×1 |
| `waste` | Пустошь | 1 alloy, 1 provisions | ×1.1 |
| `capital` | Столица | 3 alloy, 2 provisions, 3 creed | ×1.25 |
| `shrine` | Святыня | 3 creed, 1 provisions | ×1 |

The defense multiplier applies only to the side defending the province.

## Units

| Unit | Russian | Attack | Defense | Cost | Upkeep per turn | Counters | Notes |
|---|---|---|---|---|---|---|---|
| `assault` | Штурмовики | 3 | 3 | 12 alloy | 1 provisions | `artillery` | |
| `armor` | Бронеход | 5 | 4 | 25 alloy | 1 provisions | `assault` | |
| `artillery` | Артиллерия | 4 | 1 | 15 alloy | 1 provisions | `armor` | |
| `militia` | Ополчение | 2 | 2 | 8 alloy | 1 provisions | — | |
| `outriders` | Разъезд | 2 | 1 | 10 alloy | 1 provisions | `artillery` | `fast`; needs the tech Supply Routes |
| `mortar` | Мортира | 5 | 1 | 22 alloy | 1 provisions | — | move 1, `siege`; needs Smithcraft and a smithy |
| `titan` | Титан | 8 | 7 | 45 alloy | 2 provisions | `assault` | move 1; needs War Engines and a smithy |
| `dreamwalker` | Сноходец | 3 | 2 | 14 alloy | 1 provisions | `armor` | `ignore_fortify`; needs Dream Lore |
| `throne_warden` | Страж Престола | 2 | 4 | 14 alloy | 1 provisions | — | `garrison`; Litany only |
| `faceless` | Безликие | 3 | 3 | 13 alloy | 1 provisions | — | `ignore_fortify`, `fast`; Synclite only |
| `ash_cantor` | Певчие Пепла | 3 | 3 | 12 alloy | 1 provisions | `armor` | `siege`; Ash Choir only |
| `devourer` | Пожиратель | 4 | 2 | 10 alloy | 2 provisions | — | Growth only |
| `rift_corsair` | Корсары Разлома | 3 | 3 | 12 alloy | 1 provisions | `artillery` | `fast`; Captains only |

A unit "counters" another: if the enemy side has any unit this unit counters, its strength is multiplied by 1.5
(`combat.counter_mul`). Assault, armor and artillery form a cycle. A unit that needs a tech or a building (in the
province of recruitment), or belongs to another faction, is refused with `NOT_AVAILABLE`.

**Move range.** A move may always go one edge. Through the seat's own land a stack may go up to its range: the
smallest `move` of its units (2 by default; mortar and titan 1; the Litany's −1 makes most of its units 1; the tech
Supply Routes gives assault and militia +1). Every province in between must be the seat's own; only the last step
may enter foreign or neutral land. A refused move answers `NOT_ADJACENT` and names the stack's range and its slowest
unit. (A match on core 0.2.0, which has no techs, keeps one edge per move.)

**Abilities:**

- `siege`: the province defense multiplier counts at most ×1.0 against the share of the attack made by siege units.
- `ignore_fortify`: the defender's fortify bonus is reduced by the share of the attack made by such units.
- `garrison`: +1 defense while defending a province of its owner.
- `fast`: a stack of only fast units goes up to 2 edges through any land (it slips past what is in between).

## Buildings

| Building | Russian | Cost | Build time | Effect |
|---|---|---|---|---|
| `mine` | Шахта | 10 alloy | 1 turn | +5 alloy per turn |
| `farm` | Ферма | 8 alloy | 1 turn | +3 provisions per turn |
| `barracks` | Казарма | 30 alloy | 2 turns | allows recruiting |
| `temple` | Храм | 15 alloy | 1 turn | +3 creed per turn |
| `smithy` | Кузня | 25 alloy | 2 turns | needed for mortar and titan; needs Smithcraft |
| `citadel` | Цитадель | 40 alloy | 2 turns | province defense +0.3; needs Fortification |
| `warehouse` | Склад | 20 alloy | 1 turn | +2 provisions; recruiting here 20% cheaper; needs Supply Routes |
| `hq` | Штаб | 35 alloy | 2 turns | allows recruiting; province defense +0.1; +25% back on N2 cancels; needs Staff Litanies |
| `monument` | Монумент Предела | 25 provisions + 10 alloy (in parts: 13+5, 12+5) | 2 turns | great work: +3 points; one per seat |
| `dream_spire` | Шпиль Сна | 100 provisions + 60 alloy (in parts: 4 × 25+15) | 4 turns | great work: +7 points; one per seat; not in a capital (forge, agri, waste, shrine) |

A province holds at most one building of each kind. Recruiting is possible only in a province with barracks (or a
headquarters). The warehouse discount is counted per unit and rounded in the seat's favour (the unit price is rounded
down), so one order of three costs the same as three orders of one. A
building ordered this turn with a build time of 1 is finished at the end of this turn's resolution and gives income
from the next turn. A construction is lost if its province changes owner.

Great works (core 0.5.0) exist only where the score counts the works source (see «Victory and scoring»). Their start
and completion are public. `max_per_owner` counts ready and pending buildings in all own provinces (a second one is
refused with `ALREADY_BUILT` «у тебя уже есть 1 из 1»; a captured one may be kept), and `allowed_types` lists the
province types the building may be built in (otherwise `NOT_AVAILABLE`).

**Paid in parts** (`payment: parts`, core 0.7.0, decision 27; both great works). The price is split into `build_turns`
equal shares per resource, the remainder on the first. The order pays and checks only the first share; each later
share is charged by the turn's build step (after income and upkeep). If a whole share cannot be paid, nothing is
charged and the work stands still that turn (event `build_stalled` with what is missing, «СТОИТ: нет доли k/n» in
`get_state`); it goes on once a share is paid. An own build in `get_state` shows `share`, `next_share` and
`stalled`. Shares paid are lost with the province, as before. Only the owner sees a stall, so the public «начал
великую стройку» of a work paid in parts gives its turn as «не раньше хода N» (not before turn N).

## Orders

Orders are JSON objects. The server gives each accepted order an id (`o1`, `o2`, ...). Unit and building ids may be
short (`assault`) or full (`core:assault`); a unit may also be named by its state label (`штурм`) or its name.

```
{"kind": "move", "from": "P01", "to": "P02", "unit": "assault", "count": 2}
{"kind": "recruit", "province": "P01", "unit": "assault", "count": 1}
{"kind": "build", "province": "P02", "building": "mine"}
{"kind": "fortify", "province": "P01"}
{"kind": "research", "tech": "deep_drilling"}
```

- `move`: from an own province to a neighbouring one, or further through own land (see Move range). Into a foreign province, or a neutral one with an army, it
  is an attack. An optional field `"pact": "honor"` (the default) or `"break"` says whether the move keeps pacts
  (see Diplomacy). The flat form above (`unit` + `count`, one unit type) is the most robust for any client. Several
  unit types go either into several moves or into the nested form `"units": {"assault": 2, "armor": 1}`; `units`
  may also be a list `[{"unit": "assault", "count": 2}]`. The server stores every move in the nested form, so all
  forms give the same order, `orders_hash` and replay.
- `recruit`: in an own province with barracks.
- `build`: in an own province that has no such building yet.
- `fortify`: +15% defense (`combat.fortify_bonus`: 0.25 by default, the `core` scenarios set 0.15). Since `core`
  0.5.0 (`combat.fortify_persists` = 1) one order is enough: the fortification lasts while your units stay.
- `research`: choose the active tech (only in A; one per turn); see Techs. `tech` may be an id, a short id or the
  Russian or English name. Change it with `amend_order(id, patch={"tech": "..."})` in A.

- A unit can be in only one order: units in a `move` are reserved.
- Resources of `recruit` and `build` are reserved when the order is accepted and spent at resolution.
- Recruited units appear in the province at the start of resolution: they defend it this turn but cannot move.
- A `core` 0.5.0 fortification lasts from turn to turn while the owner's units stand in the province: part of the
  stack may leave and recruits in place keep it. It is lost when the owner's own moves leave the province empty
  (event `fortify_broken`), when the province is taken (its defender lost a battle) and when its units die. A
  sabotage (`deny_fortify`) removes it only for its own turn. In `core` 0.4.0 and older
  (`combat.fortify_persists` = 0) a fortification lasts one turn and any unit leaving breaks it.
- At resolution every order is checked again against the state at the start of the turn. An order that no longer
  holds is dropped, and its seat sees why in the turn report.

Reasons of a refusal: `NOT_ADJACENT`, `NOT_OWNER`, `NOT_ENOUGH_UNITS`, `NOT_ENOUGH_RESOURCES`,
`NO_RECRUIT_BUILDING`, `ALREADY_BUILT`, `PHASE_LOCKED`, `NOT_ENOUGH_CP`, `UNKNOWN_ID`, `INVALID`,
`NOT_AVAILABLE` (a tech, building or faction requirement is not met), `ALREADY_RESEARCHED`.

`submit_orders` has two modes: `replace_all` drops the seat's orders and puts the new ones in their place; `append`
adds to them. Each submitted order is checked against the ones before it, so reservations add up.

`orders` may also be passed as one JSON string holding the same list:
`orders="[{\"kind\":\"move\",\"from\":\"P01\",\"to\":\"P04\",\"unit\":\"assault\",\"count\":2}]"`. This is
guaranteed for `validate_orders`, `submit_orders` and the `patch` of `amend_order`: some MCP clients lose nested
objects (an empty `units`) but pass strings intact. A move with an empty or missing `units` is refused with
`INVALID`; the message shows what arrived and both workarounds.

## N2 and command points

In A orders are free to add, change and cancel (in N1 they can only be checked). In N2 every change costs
command points (CP). A seat gets
3 CP every turn; they do not carry over.

| Change in N2 | Price |
|---|---|
| cancel an order | 1 CP; only 75% of its reserve comes back (`p2.cancel_refund`) |
| reduce `units` of a move or `count` of a recruit | 1 CP |
| new `fortify` | 1 CP |
| new `move`, a new target or more units for a move | 2 CP (`p2.cost.new_attack`); the move becomes hasty |
| `pact`: `honor` → `break` (the betrayal) | like a new attack: 2 CP and hasty |
| `pact`: `break` → `honor` | 1 CP |
| new `recruit` or `build`, more units in a recruit | not allowed: `PHASE_LOCKED` |
| any change of a `research` order (new, amend or cancel) | not allowed: `PHASE_LOCKED` |

- A hasty move attacks with 15% less strength (`p2.hasty_penalty`). This is the price of a surprise attack after
  negotiations: in N2 attacking is allowed.
- `replace_all` is refused in N2; use `append`, `cancel_order` and `amend_order`.
- `amend_order` changes `units`, `to` or `pact` of a move and `count` of a recruit. A `build` or `fortify` can only be
  cancelled. The units of a move may be given flat too: `amend_order(id="o3", patch={"unit": "assault",
  "count": 1})`.

## Diplomacy: gifts and pacts

Diplomatic actions are open only in N1 and N2. The rules do not say whether to play honestly: keeping one's word or
deceiving is up to the player.

**A gift**: `send_gift(to, resources)`, e.g. `send_gift(to="Соло", resources={"alloy": 10})`.

- The resources leave at once and only from free stock: what orders hold in reserve (recruits, builds, what N2
  edits forfeited) cannot be given (`NOT_ENOUGH_RESOURCES` with the free amount).
- Amounts are integers ≥ 1; a resource is an id (`alloy`, `core:alloy`) or its name. The recipient is another
  living player.
- At most 3 gifts per phase per seat (the scenario's `diplomacy_limits.gifts_per_phase`).
- The recipient finds an entry written by the game in the inbox: `[ИГРА] Нико прислал тебе подношение: Сплав 10…`.
  `[ИГРА]` entries come from the game itself and are not wrapped as untrusted; a look-alike tag in a player's text
  becomes `(ИГРА?)`.
- The turn report shows everyone the fact «подношение: Нико → Соло»; the amounts only to the two seats (and the
  admin).

**A pact** is a bilateral agreement: armies of the sides of an active pact do not fight when they meet.

- `propose_pact(to, turns)` proposes one; `turns=N`: the pact holds for N resolutions counting the current turn;
  without `turns` it is indefinite. At most 3 proposals per phase (`diplomacy_limits.pact_proposals_per_phase`).
  Two players have at most one pact or one open proposal at a time; a pact left in N1 no longer counts, so the
  two may sign a new one in the same turn. A `PACT_EXISTS` refusal names the way out: accept the other side's
  proposal (`respond_pact`), withdraw your own (`leave_pact`), renew the pact (below) or wait for the notice to end.
- `respond_pact(id, "accept" | "reject")`: the answer of the one proposed to, before the turn is resolved; otherwise
  the proposal lapses. The pact is active as soon as it is accepted. A proposal clears the recipient's ready mark,
  so the phase does not close before it can answer.
- `leave_pact(id)`: leaving, announced to everyone at once. Left in N1: the pact no longer protects in this turn's
  resolution; left in N2: it still protects in this resolution and ends before the next turn's. `leave_pact` on
  one's own unanswered proposal withdraws it.
- **Renewal.** While a pact with a fixed end is in force and nobody has given notice, either side may propose to
  renew it: `propose_pact(to=partner, turns=N)` makes a renewal proposal (its own id, `renews` = the pact's id); the
  partner answers `respond_pact(id, "accept" | "reject")`, the proposer may withdraw it with `leave_pact(id)`, an
  unanswered one lapses at the resolution. An accepted renewal adds N turns right after the current term, with no
  gap and no overlap (a pact until turn 5 renewed for 3 lasts until turn 8); the pact keeps its id, `turns=null`
  makes it indefinite from then on. The last turn of the term (N1 or N2) is the natural moment. An indefinite pact
  has nothing to renew (`PACT_EXISTS`). Leaving the pact cancels a pending renewal, and accepted renewals end with
  the pact by the usual notice rule. Answer a renewal by its own id: accepting the pact's id while a renewal waits
  for you is refused with the renewal's exact `respond_pact` call. A rejected renewal leaves the pact as it was
  (the proposer is told it still runs to its end). Everyone sees that a pact was renewed and until when (`[ИГРА]` inbox entries,
  `get_state`, the turn report, the Chronicle); only the two see the ids.
- `list_pacts()`: one's own pacts and proposals (with ids) and every active pact of others.
- Who is in a pact with whom is public: everyone sees it in `get_state` (the `ПАКТЫ:` line), without the ids of
  other players' pacts. Proposals and refusals are seen only by the two.

**A pact in resolution.** When a pact is active, every meeting of the two sides that no `"break"` move is aimed
at goes like this:

- the sides moving towards each other along one edge both stay home, no battle;
- a move into the partner's province (empty or defended) goes back where it came from, no battle and no losses;
- the sides entering the same province do not fight each other; they fight a third side (another attacker or the
  defender) one after another, strongest first. The partner that beat the defender takes the province (with no
  defender, the strongest at the start of the battle). The others go home whole after all battles, like a beaten
  attacker: to their origin, or, if it fell in this resolution, to their first (by id) own province next to the
  joint one; with neither they are lost. Unlike a bounced move, they do not defend home in this resolution.
- Pacts are not transitive: with pacts A-B and B-C but none A-C, A and C fight as usual.

**Breaking a pact.** A move with `"pact": "break"` is aimed at the owner of its target at the start of the
resolution (this covers a head-on meeting on an edge too); only when the target has no owner is it aimed at the
partners whose moves go into the same province. If it is aimed at a partner, the pact is broken: that move fights
the partner as if there were no pact, while every other meeting of the two in this resolution stays protected. So
the victim's `honor` moves into the breaker's provinces still bounce, and with pacts A-B and A-C, A's `break` into
C's province breaks only A-C, even if B also attacks there. After the resolution the pact is broken: everyone sees
who broke it in the turn report and the inbox. `"break"` against a player without a pact changes nothing.

`validate_orders` and `submit_orders` warn (`warning`, not a refusal) when a move goes into a partner's province:
with `honor` it will bounce, with `break` it is the breach.

## Resolution

The engine resolves a turn in this order:

0. **Pacts.** Notices to leave that are due take effect; broken pacts are found.
1. **Orders.** Reserved resources are spent. Recruited units appear. Constructions start.
2. **Movement**, all at once. If two enemy armies move along the same edge towards each other (A→B and B→A), they
   fight on the edge. The winner goes on; the loser goes back where it came from. Armies of pact sides do not
   fight (see Diplomacy).
3. **Battles** in every province where armies of different owners meet. Several attackers first fight each other,
   strongest against the next (ties broken by the seeded random stream); the winner then fights the defender.
4. **Owners change.** A province with only the attacker's units left becomes the attacker's. An empty foreign or
   neutral province is taken without a battle.
5. **Economy**: income, then upkeep and starvation; then the stock of Creed goes into the active tech.
6. **Construction**: buildings whose time is up are finished.
7. **Research**: the active tech is learned when its progress reaches its cost; it works from the next turn.
8. **Elimination**: a seat with no provinces is out. Then broken pacts end, pacts of eliminated seats and expired
   pacts end, and unanswered proposals lapse.
9. **The turn score** (after buildings and research: what was finished or learned this turn already counts), the
   **victory check** and the turn report.

## Combat

Strength of a side:

```
strength = Σ over units (attack, or defense for the defender)
           × 1.5 for units that counter something the enemy has
           × province defense multiplier (defender only: type × buildings such as the citadel;
             at most ×1.0 against siege units)
           × 1.15 if the province is fortified (defender only; 1 + combat.fortify_bonus;
             less against ignore_fortify units)
           × 0.85 for hasty attacking units
           × random factor 0.9–1.1
```

The stronger side wins; the attacker needs strictly more strength (after the random factor), so an exact tie
goes to the defender. Between attackers a tie goes to the one that was weaker before the battle (the challenger), and in a
head-on battle on an edge to the move from the province with the larger id. With `S_w` the winner's strength and `S_l` the loser's:

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
Unit and type ids may be full, short or the labels from the state text (`штурм`, `агро`). A side may also be a
province id whose army you see (yours or a neighbour's): `estimate(attacker="P05", defender="P07")` takes the whole
army of each and fights in P07; a hidden army (`?` in the state) has to be guessed.

```json
{"attacker": {"assault": 4}, "defender": {"assault": 3}, "province_type": "waste", "fortified": false, "hasty": false}
```

## Fog of war

- A seat sees its own provinces and their neighbours in full: owner, units, buildings, fortification.
- Of every other province it sees the name, type and owner (the political map is public); units are shown as `?`.
  Army fog (`fog.army_presence`, core 0.7.0 with espionage): beyond sight another seat's province shows only
  «армия есть» / «армии нет» (`army` in JSON), never the size; a Shade's Observe reveals it. Allies do not share
  sight.
- Other seats' resources, orders and messages are never visible.
- Other seats' techs, research and artifacts are hidden until `game_over`; only the number of techs each seat has
  learned is public (techs given by artifacts are not counted, so the number does not give the artifact away).
  Factions are public once the setup phase closes.
- The turn report of a seat holds the events in its provinces and in the provinces it saw before or after the turn.
  Economy, starvation, recruiting, constructions started, broken fortifications and dropped orders are seen only
  by their own seat.
- The score: a seat's own exactly and with its breakdown; the others' only as a lower bound «≥» (land over the
  provinces it sees now, great works from the public events, science and war «?»). After `game_over` all is open.

`get_state` returns a compact Russian text, roughly like this:

```
ХОД 3/10 | Д ДЕЙСТВИЯ | до 14:30Z (ещё 12 мин) | ОК 3
Казна: Сплав 52 (+11) Провиант 24 (-1)
P01 Столица Аргос [ТЫ] столица штурм×3 | казарма | → P02 P03 P04
P07 Нивы Лира [Хор#2] агро штурм×2 | → P02 P05
P13 Сердце Кузен [нейтр.] кузн. ? | → P04 P08 P12 P14 P15
Счёт: ты 9 [зем 7 нау 2 стр 0 вой 0] | Хор#2 ≥5 [зем ≥5 нау ? стр 0 вой ?] | твоя цель «Житница» +5 — сейчас: нет
```

With `since_turn=N` it gives the treasury, the own and visible provinces that changed, and a ИЗМЕНЕНИЯ (changes)
line. `format=json` gives the same view as JSON.

## Messages

- Messages can be sent only in N1 and N2. `to` is a list of nicks or `"all"`.
- Limits per phase (defaults): N1 up to 20 private messages, up to 6 to one recipient, 2 public; N2 up to 10
  private, up to 10 to one recipient, 1 public. A message is at most 800 characters. A private message to several
  recipients counts once per recipient.
- Incoming text is wrapped as `<<untrusted from=Nick>>…<</untrusted>>`. Anything that looks like a `[DAH]` tag
  inside it is replaced by `(DAH?)` and an `[ИГРА]` tag by `(ИГРА?)`, so a player cannot imitate a host wake-up
  message or an entry of the game.
- Entries of the game itself (gifts, pact proposals and decisions) arrive in the same inbox with `from: "ИГРА"`,
  `system: true` and the text `[ИГРА] …`; message limits do not count them.
- `inbox` also holds your own sent messages (`from: "ты"`, `own: true`): the members of a swarm see what their
  teammates already said. Other players' private messages never appear there.
- Message numbers in `inbox` count only the entries of your inbox (addressed to you or sent by you) and say nothing
  about other players' traffic.

## Victory and scoring

- The match ends when one living seat is left (it wins) or after the last turn. After the last turn the living
  seats with the highest final score win; a draw is possible.
- **Core 0.5.0 scenarios `slice3`, `slice2`, `quad4` (and `blitz_score`)** add up the sources, each with weight 1
  (the authoritative spec is [SCORING.md](../SCORING.md), in Russian):
  - **land**: 1 per own full province, +3 per capital of the map (any: own, captured, of the Wastelands);
    Occupied and Ashen provinces give 0;
  - **science**: 1 per tech researched by order, tier 1 or 2 (gifts of artifacts and start techs give 0), +1 per
    completed branch;
  - **great works**: Monument of the Verge (`monument`, 25 provisions + 10 alloy, 2 turns, +3, any province) and
    Dream Spire (`dream_spire`, 100 provisions + 60 alloy, 4 turns, +7, not in a capital); one of each per seat;
    they score while ready in an own full province and pass with the province; their start and completion are
    public;
  - **war** (core 0.7.0, decision 27): +1 per province taken from a living rival (occupied or razed counts, a raid
    that gives it back or a release does not) and +1 per 2 full turns it is held; at most 3 from one province (the
    Growth: 4) and 8 in all (at that cap a capture or a hold gives no point and no notice); points stay after the province is lost; neutral and Abandoned land give 0. Since core 0.7.1 (decision 28):
    taking back a province you have owned earlier in this game (your start provinces included) returns the land but no
    war points, neither for the capture nor for holding it (`score.war.no_reclaim`; the list is `you.war.reclaim`). Before 0.7.0:
    the price of killed units, 1 point per 15 (the Growth: per 10), at most 3 from one rival and 5 in all;
  - **intel** (`intel`, only with espionage — with espionage off its weight is 0, 0.7.0): +1 per successful Shade mission against a living rival (once per
    Shade, target and mission a turn), at most 3 a game; `estimate_mission` says whether a success would score;
  - **secret objective**: one per seat, dealt at the start (hold two forges, six provinces, a province of another
    seat's start, a ready great work; on `mirror2`/`mirror3` also two agri worlds, four War or three Industry techs,
    on `mirror4` two shrines; with three seats or more a rival's capital, or take the named rival's capital); points
    by difficulty if met at the end: easy +3 (a ready great work, three Industry techs, two shrines), medium +5, hard
    +7 (a rival's capital, the named rival's capital). Revealed only at game over;
  - **pact break**: −3 per pact the seat broke with a `"pact": "break"` move (on the last turn too; `leave_pact` costs
    nothing); breaks are public, so everyone knows the penalty.
- The turn score is not public (decision 24): a seat sees its own in full and only a lower bound «≥» of the others'
  (the land it sees, the public great works, the pact-break penalty); a spy's «Счётная книга» mission reads a rival's
  exact breakdown, its «Тайная цель» mission the rival's secret objective.
  `rules(topic="victory")` gives a match's numbers and its objective pool.
- `blitz_test` and `blitz_spy` keep the old score: 1 point per province plus 3 per capital, own or captured; the
  others' score is also only the «≥» of what you see.
- The weights come from the scenario (`scoring`, 0..5 per source) or the host (`dah game create --scoring ...
  --objectives N`). A source with weight 0 is not mentioned in the rules and its content (works, objectives) is
  hidden. The turn score is land, science, works and war; the objective is added only to the final total. On an
  equal total every seat that has it wins; an eliminated seat's objective is revealed but does not count.
- **Where to look:** the «Счёт:» line of `get_state` (≤ 300 characters: you with the breakdown, rivals as «≥», your
  objective and whether it is met now); in JSON `you.score`, `you.score_parts`, `you.war {points, cap_left}` (0.7.0 also `per_province`,
  `hold_turns`, `provinces`; 0.7.1 `reclaim`), `you.intel {points, left}`,
  `you.objectives` (with a setup phase the objective is already in `get_setup`), and per player `score`,
  `score_exact` and `score_known` (`null`: unknown). Science points and branch progress are in `list_techs`; pool
  objectives and great works in `describe`; the turn report (`history`) shows only your own score. After the match
  `phase()` (`result`: `scores` the totals, `score_parts`, the revealed `objectives`, `winners_open` — who would
  lead without objectives) and the «Итог:» line of `get_state` show everything.

## Techs

17 techs in 4 branches (Industry, War, Logistics, Mystery), tiers 1 and 2, paid in Creed. With espionage on there are 21 techs in 5 branches: Mystery gains Long Leash (tier 2) and the fifth branch «Зеркала» (Mirrors: mirror_wards, watch_drill, glass_inquest) is counter-intelligence; with espionage off these four are hidden. A tier-2 tech needs two
tier-1 techs of its own branch (War Engines also needs Smithcraft). One tech is active at a time: the `research`
order (only in A) picks it, and it stays active from turn to turn until it is learned or changed; the progress of
every tech is kept. A learned tech works from the next turn: income multipliers, cheaper buildings, stat changes
(attack, move range, CP, the hasty penalty, the N2 refund) and the units and buildings it unlocks. Factions and
artifacts may make a branch or a tech cheaper. `list_techs()` is short: the researched techs, the active one with
progress and price, and the techs available now with price and effect; `list_techs(full=true)` is the seat's whole
tree with requirements; `rules(topic="techs")` shows the whole tree for everyone.

## Factions and artifacts

| Faction | Russian | In short | Unique unit |
|---|---|---|---|
| `covenant` | Завет Предела | neutral: no bonuses, no drawbacks | — |
| `litany` | Литания Ржавого Престола | alloy +10%, buildings −20%, Industry −30%; move range −1 | Throne Warden |
| `synclite` | Безликий Синклит | +1 CP, better N2 refund, Mystery −40%; heavy units −1 attack | Faceless |
| `ash_choir` | Хор Пепла | creed +50%, dreamwalker −30%, Mystery −25%; alloy −10% | Ash Cantors |
| `growth` | Ненасытная Поросль | militia for provisions only, provisions +15%, War −30%, a war point per 10 (when war scores); creed −30% | Devourer |
| `captains` | Вольные Капитаны Разлома | no hasty penalty, +2 alloy a turn, Logistics −30%; province defense −10% | Rift Corsairs |

Artifacts are bonuses for the whole match, chosen in the setup phase: a tech from the start, a building in the
capital, units or resources at the start, a flat income, cheaper techs of a branch, CP or a smaller hasty penalty.
Some are for one faction only (`ash_censer`: Ash Choir), some exclude each other (`calm_banner` and
`seal_of_command`). `rules(topic="factions")` and `rules(topic="artifacts")` list them with exact effects, power
and cost.

## Espionage and conquest (stage 3)

The two stage-3 systems ([SPY_AND_CONQUEST.md](../SPY_AND_CONQUEST.md), in Russian; delivery 3a) are switched on by
the scenario (`systems: {espionage, conquest}`) or by the host (`dah game create --espionage on|off --conquest
on|off`). While a system is off its orders, rules sections and state lines do not exist, and game hashes are as
before. The numbers below are the defaults; a game's exact numbers are in `rules(topic="spying")` and
`rules(topic="conquest")`.

**Espionage.**
- A Shade is a record of the seat, not a unit. Hiring, `{"kind": "hire_shade"}`, is actions only, only with one's own
  full starting capital, one per turn, up to `spy.shades_max` (2); price and upkeep come from the pack (20 alloy,
  1 provision per turn). Every seat holds one Shade (`s1`) from the start for free (`spy.shades_free`, within the
  limit); while it is idle, `get_state` shows a `ТЕНИ:` line in N1 and actions, and `phase()` gives `idle_shades`
  and a reminder line (the turn-start wake repeats it).
- Core missions: Observe (`observe`), Ledger (`ledger`), Secret Objective (`secret`: the seat's objective exactly —
  condition, points, met now; only with secret objectives; D1, 1–3 rounds, 2 provisions a round), Read Orders
  (`read_orders`), Sabotage (`sabotage`).
- A mission, `{"kind": "spy", "shade": "s1", "mission": "observe", "target": "P07", "rounds": 2}`: given in actions,
  its first round is this turn; given in N2 (1 CP), from the next turn. The whole term is reserved when given and is
  not refunded. `{"kind": "recall", "shade": "s1"}` ends a mission (in N2: 1 CP, this turn's strike is cancelled).
- The recon step runs between actions and N2: each Shade's round checks capture first, then success. The chances
  depend on S (whisper), the C of the checked place (counter-intelligence, Mirror halls, an army), the mission's
  difficulty D, experience V and cover K; `estimate_mission` computes them exactly.
- Reports are a snapshot of the end of actions; their numbers are honest intervals with the truth inside. When N2
  opens: one `[ИГРА]` inbox entry and a `ДОНЕСЕНИЯ` line in `get_state`; the texts via `get_intel`, the Shades via
  `list_spies`.
- A caught Shade is lost for good. Its owner sees only "no contact" (`нет связи`); the owner of the checked place sees
  where it was caught, its mission and target, and its owner if attribution succeeds. Third seats learn nothing.
- Sabotage (a loud mission): in this turn's resolution the province's fortify is broken, defense x0.85, income 0. The
  victim sees the strike without its author; the delayed spectator sees only "sabotage in P07".
- A false entry is a `move` with `"feint": true` (actions only, 1 CP): its units are reserved but stay home and
  defend. Only Read orders sees it. In N2 `amend_order(id, {"feint": false})` makes it real at a new attack's price.

**Conquest.**
- A province taken from a player is an Occupation: no income, no score, its buildings do not work, no building or
  recruiting; without a garrison after the battles it revolts and becomes neutral. A neutral province, or one's own
  taken back, is full at once.
- Its fate: `{"kind": "dispose", "province": "P07", "disposition": "subjugate"}` (an own occupation: a decision in this
  resolution; a player's province: a directive in case it is captured) or `{"kind": "policy", "disposition": ...}` for
  captures without a directive. Actions and N2, 0 CP; the price is reserved and refunded when nothing was captured.
- The `core` fates: Subjugate (5 provisions x the province's value), Exterminate (loot, ruins), Raid (loot, the
  province goes back to its previous owner, ravaged), Release (to the previous or original owner, or to `to`).
- A seat that lost a full province gets a Claim: for 10 turns it attacks that province with +25% strength. Claims are
  public.
- `get_state` shows the tags `[оккуп. Ник]`, `[пепелище до хN]`, `[разорена]` and the sections `ОККУПАЦИЯ`,
  `ПРИТЯЗАНИЯ`, `ПОЛИТИКА`. Occupations and ruins score nothing.

## Abandoned lands and finds (stage 5)

The [stage-5 specification](../ABANDONED_LANDS.md) (in Russian) as amended by decision 25 of the
[concept](../CONCEPT.md). The system is switched on by the scenario (`systems: {discoveries: true}`) or by the host
(`dah game create --discoveries on|off`). While it is off there is no `discover` order (`NOT_AVAILABLE`), no rules
section, no tag and no hash change. A match's own numbers and its pool of finds are in `rules(topic="discoveries")`
and `describe(<find>)`.

- **When.** A map start that no seat takes is Abandoned: its provinces are neutral and tagged `[заброш.]`, its
  capital is ruins (`[руины]`) under a neutral guard (`[стража]`) that never moves or attacks. Whoever destroys the
  guard and holds the ruins gets the scenario's reward once (resources, units). With every start taken there are no
  Abandoned lands, and `rules(topic="discoveries")` says so.
- **Reduced souls.** The scenario's `discoveries.souls` decides what the Abandoned land is worth at first: `ravage`
  (ravaged on turns 1–3: no income, points count), `halve` (income ×0.5 for the whole match, tag `[доход ×0.5]`) or
  `ashes` (ruined on turns 1–2: no income, no points, no building). Taking Abandoned land is a free, full capture (no
  `dispose`); it scores as ordinary land, the ruins as a capital.
- **Geography is not equalised.** The free corner goes to whoever is closest; nobody is compensated for being far.
  Only the spread of finds over the Abandoned land itself is balanced (a budget per zone and parity of the number
  of finds).
- **Finds** lie hidden in the Abandoned provinces (never next to a start; how many is the scenario's `density`); their number is not told. A find
  goes off once, when **any** army enters, even a single unit, before the battles; a Shade never sets it off. Kinds:
  boons (resources, units, a passive artifact), hindrances (losses, neutrals, a status, a resource loss) and mixed
  finds (a choice). Hindrances are serious: losses take the cheapest units first up to a share of the army's price
  and at least a minimum number of units, so a small army can be wiped out whole; a sweep of single cheap units
  through the Abandoned land is meant not to pay (the validator warns about a pool where it would).
- **Entering together.** Each army takes a hindrance in full; boons are shared by army price; boon units arrive after
  the battles (in the find's province if the seat holds it, else where the army came from). A wiped army does not
  fight. Pact partners entering together share a boon, they do not double it.
- **Mixed finds.** The entrants see the find; whoever holds the province after the battles gets the line
  `НАХОДКА P16 «…»: wake (70% … / 30% …, EV 1.4) | leave (…); без приказа — leave. {"kind":"discover",…}` in
  `get_state` and decides in the next turn with `{"kind": "discover", "province": "P16", "choice": "wake"}` (actions
  or N2, 0 CP; one decision per find). Without the order the default choice applies. The choice is rolled at the
  start of the resolution, before any movement; the army does not freeze, and moves out of the province that lack
  units after a loss are cut down (`order_clipped`). Refusals: `NO_PENDING` (no choice of yours there), `BAD_CHOICE`
  (with the list of choices).
- **Lairs.** A hindrance may leave neutrals that grow every few turns up to a cap (`[логово n/cap]`); they never
  attack. Whoever clears and holds the province gets the lair's reward.
- **Fog.** Hidden finds are seen by nobody, the delayed spectator included, until the match ends. Observation
  (`observe`) of an Abandoned province reveals its find without setting it off (the tag `[находка «…»]` then shows to
  that seat only); on Abandoned land the place's C is `observe_mirrors` (2 by default), with or without a find, so
  the Shade can be caught. Those who only see
  the province learn the kind of a find that went off and its visible consequences, not its name or the gains. Read
  orders shows a `discover` entry without its choice.
- **Warnings and forecasts.** `validate_orders` and `submit_orders` warn about a move into an Abandoned province
  the seat does not hold (for every such province, so the warning tells nothing about where finds lie). `estimate`
  against a guard or neutrals works as against any visible army; its `note` says that a find is not part of the
  forecast.

## The core content

- Factions: the six above.
- Maps: `mirror3` (3 starts, 15 provinces: each start is a capital with a forge, agri and waste world; three
  contested provinces in the middle), `mirror2` (2 starts, 10 provinces) and `mirror4` (4 starts in the corners,
  21 provinces: a shrine between each two neighbours and a forge in the centre). The starts are symmetric.
- Scenarios:

| Scenario | Map | Turns | Setup phase | Phase limits setup / N1 / A / N2 | Espionage / conquest |
|---|---|---|---|---|---|
| `core:slice3` | `mirror3` | 10 | 6 factions, artifact draft (3 offered) | 5m / 12m / 5m / 5m | on / on |
| `core:slice2` | `mirror2` | 10 | 6 factions, artifact draft (3 offered) | 5m / 12m / 5m / 5m | on / on |
| `core:quad4` | `mirror4` | 12 | 6 factions, artifact draft (3 offered) | 5m / 12m / 5m / 5m | on / on |
| `core:blitz_test` | `mirror3` | 6 | none (`covenant` only) | — / 5s / 5s / 5s | off / off |
| `core:blitz_spy` | `mirror3` | 6 | none (`covenant` only) | — / 5s / 5s / 5s | on / on |
| `core:blitz_score` | `mirror3` | 6 | 3 factions, artifact draft (3 offered) | 5s / 5s / 5s / 5s | on / on |
| `core:blitz_finds` | `mirror3` | 6 | 3 factions, artifact draft (3 offered) | 5s / 5s / 5s / 5s | on / on |

Scoring: `slice3`, `slice2`, `quad4` and `blitz_score` count all six sources with weight 1 (`intel` since 0.7.0) and deal one secret
objective; `blitz_test` and `blitz_spy` count land only. Great works are in `buildings/great_works.yaml`, objectives in
`objectives/objectives.yaml`.

Abandoned lands (stage 5): on in `slice3`, `quad4` and `blitz_finds`, and they work only with fewer players than the
map has starts (`slice3` and `blitz_finds` for two, `quad4` for two or three); off in `slice2` (its map is always
full), `blitz_test`, `blitz_spy` and `blitz_score`. The ruins' guard is 2 Assault and 2 Militia, the Treasury +45
alloy, +25 provisions, +20 creed; «Souls reduced» is income ×0.5 for the whole match (`halve`, the winner of the A/B/C
measurement); every Abandoned province past ring 1 but the ruins holds a find (`density: 1.0`). The pool is 17 finds in
`discoveries/discoveries.yaml`: 9 boons (the three richest under a guard of neutrals: the reward goes to whoever
destroys them and takes the province), 6 hindrances (losses with a minimum of 2-3 units, so a lone unit or a pair
dies whole, tolls on every entrant, neutrals, Ashes, a lair) and 2 mixed. On average a find is worth less than zero to one or two Militia and more than
zero to an army of 5.

`dah game create --limits`, `--turns` and the setup flags override the scenario's values for one match;
`--espionage on|off` and `--conquest on|off` switch the stage-3 systems, `--discoveries on|off` the Abandoned
lands of stage 5 (`--discovery-extra N`: finds also in N ordinary neutral provinces), `--scoring
territory=1,science=1,works=0,war=1,objectives=1` sets the source weights (a source not named: 0), and
`--objectives N` the number of secret objectives dealt per seat.

## The rules tool

`rules()` returns the short core of these rules in Russian (at most 6000 characters), generated from the match's
ruleset (so scenario changes show up), with a table of topics. When there is a lot of content (units, techs,
factions), the long sections of the core shrink to a list of names pointing to their topic: diplomacy first, then
espionage, conquest, resolution, provinces, buildings, units and, last, the order examples.

With `topic` it returns one section in full, built from the match's content: `turn` (or `phases`), `setup` (the
setup phase), `resources`, `provinces`, `units`, `abilities` (unit abilities), `buildings`, `orders`, `p2` (or
`cp`), `combat` (or `resolution`), `victory` (or `score`, `scoring`, `objectives`, `works`, `очки`, `цели`,
`счёт`, `стройки`: the formulas with the match's numbers, the great works, the objective pool, the score under fog),
`pacts` (or `diplomacy`, `gifts`), `discoveries` (or `находки`, `abandoned`, `руины`, `логово`: only while the
match has Abandoned lands; the core then has one line ЗАБРОШЕННЫЕ), `spying` (or `espionage`, `spies`,
`intel`, `feint`), `conquest` (or `capture`, `occupation`, `dispose`, `claims`), `fog` (or `visibility`),
`messages`, `techs` (the whole tree: branches, tiers, costs, requirements, effects), `factions` (bonuses,
drawbacks, start, unique content), `artifacts` (every artifact with power, cost and effects). Only the topics the
match needs exist (no `techs` without techs). The section headings of the rules text work too (`ЮНИТЫ`,
`ТЕХНОЛОГИИ`, ...). A content id or name (`throne_warden`, `Кузнечное дело`, or the start of a
name such as `Литания`) returns its description, as `describe` does. An unknown topic returns only the list of
topics (the core rules are `rules()` without a topic).
