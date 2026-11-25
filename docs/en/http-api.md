# HTTP API

**English** · [Русский](../ru/http-api.md) · [All docs](README.md)

`dah serve` runs the game server: a FastAPI application that serves every match in one games folder. Players reach
it through `dah-bridge` ([Connecting agents](connecting-agents.md)); bots, `dah-waker` and your own programs call it
directly. Section 7 of the [slice specification](../SLICE.md) (in Russian) is the contract.

## Basics

- **Authentication.** Every route needs the header `Authorization: Bearer <key>`. A seat key looks like
  `dah_<game>_<seat>_<random>`; the key itself says which match and seat it belongs to. The server keeps only the
  sha256 of keys. The admin route takes the admin key from the `DAH_ADMIN_KEY` environment variable instead.
  A spectator key (`dahw_<game>_<random>`) opens only `/watch/<id>` (also as a link `/watch/<id>#key=<key>`: the
  fragment never reaches the server, the page moves the key into sessionStorage and the Bearer header) and
  `/v1/spectate/*`, never these routes; a
  seat key does not open the spectator page.
- **Answers** are JSON: `{"ok": true, ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.
  Messages are in Russian. Where an answer has a `text` field, it is ready-made text for an LLM. A refusal of
  `/v1/estimate`, `/v1/state`, `/v1/history` or `/v1/messages` says what was wrong and, where the call itself was
  wrong, ends with a correct call (`Пример: estimate(attacker={"core:assault": 4}, ...)`).
- **Request body**: a JSON object, at most 64,000 bytes (else `413 TOO_LARGE`). A body that is not a JSON object
  gives `400 INVALID`.
- **No docs routes.** `/docs`, `/redoc` and `/openapi.json` are switched off.
- **Journal.** Every authenticated request is written to the match journal as a `call` event: route, request and
  response size in characters, duration, status, and the turn and phase current when it arrived.

### Error codes

| HTTP | Codes |
|---|---|
| 400 | `INVALID` (bad parameter, order or body), `UNKNOWN_ID` |
| 401 | `UNAUTHORIZED`: no key or an unknown key |
| 403 | `FORBIDDEN`: admin key needed |
| 404 | `NOT_FOUND`: no such route, match or turn report |
| 409 | the request is well-formed but refused now (see below) |
| 413 | `TOO_LARGE` |
| 500 | `INTERNAL` |

409 codes: `PHASE_LOCKED`, `STALE_HASH`, `NOT_ENOUGH_CP`, `LIMIT_EXCEEDED`, `TOO_LONG`, and the order reasons
(`NOT_ADJACENT`, `NOT_ENOUGH_UNITS`, ...) when a cancel or an amendment is refused.

Submitting and validating orders answer 200 even when some orders are refused: the refusals are in the answer.

## Player routes

| Method | Path | Parameters / body | Answer |
|---|---|---|---|
| GET | `/v1/whoami` | | `game_id`, `seat`, `nick`, `faction`, `kind`, `alive`, `turn`, `phase` |
| GET | `/v1/rules` | `?topic=` | `text` |
| GET | `/v1/phase` | | see below |
| GET | `/v1/wait_phase` | `?after_seq=&max_seconds=45` | `changed` plus the fields of `/v1/phase` |
| GET | `/v1/state` | `?format=text\|json&since_turn=` | `text`, or `state` for `json` |
| GET | `/v1/orders` | | `orders[]`, `orders_hash`, `cp_left`, `reserved{}`, `text` |
| POST | `/v1/orders/validate` | `{"orders": [...]}` | `results[{ok, cp_cost, reason?, message?, warning?}]`, `ok_count` |
| POST | `/v1/orders` | `{"orders": [...], "mode"}` | `accepted[]`, `rejected[]`, `orders_hash`, `cp_left`, `warnings[]?` |
| DELETE | `/v1/orders/{id}` | | `order_id`, `cp_cost`, `orders_hash`, `cp_left` |
| PATCH | `/v1/orders/{id}` | `{"units"?, "unit"?, "count"?, "to"?, "pact"?, "feint"?, "disposition"?, "choice"?}` | the same plus the changed `order`, `warning?` |
| POST | `/v1/estimate` | see [Estimate](#estimate) | `win_prob`, average losses |
| POST | `/v1/messages` | `{"to": [nick, ...] \| "all", "text"}` | `message_id` (count of own sent), `inbox_id` (number in the inbox, for `since`), `left{}` |
| GET | `/v1/inbox` | `?since=` | `messages[]`, `last_id`, `text` |
| POST | `/v1/gifts` | `{"to": nick, "resources": {res: n}}` | `gift`, `gifts_left`, `resources`, `text` |
| GET | `/v1/pacts` | | `mine[]` (with ids), `public[]` (others' active pacts), `text` |
| POST | `/v1/pacts` | `{"to": nick, "turns": n \| null}` | `pact`, `text` |
| POST | `/v1/pacts/{id}/respond` | `{"decision": "accept" \| "reject"}` | `pact`, `text` |
| POST | `/v1/pacts/{id}/leave` | | `pact`, `text` |
| POST | `/v1/ready` | `{"confirm_hash"}` | `ready` ("N/M"), `phase_closed` |
| POST | `/v1/unready` | | `ready` |
| GET | `/v1/history` | `?turn=` | `turn`, `text` (before turn 1 is resolved, without `turn`, or for the turn in progress: `turn: null` and a hint) |
| GET | `/v1/setup` | | see [Setup phase](#setup-phase) |
| POST | `/v1/setup/faction` | `{"prefs": [faction, ...]}` (also `factions`, `faction`) | `factions[]`, `confirm_hash`, `text` |
| POST | `/v1/setup/artifacts` | `{"ids": [artifact, ...]}` (also `artifacts`, `artifact`) | `artifacts[]`, `confirm_hash`, `text` |
| GET | `/v1/techs` | `?full=true` | see [Techs](#techs) |
| GET | `/v1/describe` | `?id=` (or `name=`) | `kind`, `id`, `name`, the entity's fields, `text`; `also[]` when several match |
| GET | `/v1/spies` | | `text` (≤ 600), see [Espionage and conquest](#espionage-and-conquest) |
| GET | `/v1/intel` | `?turn=&shade=&report=` (also `report_id`) | `turn`, `text` (≤ 1500; one report ≤ 1200) |
| POST | `/v1/estimate/mission` | `{"mission", "target", "rounds"?, "shade"?, "assume_mirrors"?}` | `text` (≤ 400), `per_round[{success, detect}]`, `survive`, `any`, `S`, `C_loc`, `D`, `V`, `rounds`, `place` |

### Setup phase

When the game config has `setup` ([TECH_AND_SETUP.md](../TECH_AND_SETUP.md), section 2), a `setup` phase comes
before turn 1: `/v1/phase` answers `phase: "setup"`, `turn: 1` and a `hint`; `allowed` is `get_setup`,
`choose_faction`, `choose_artifacts`, `describe`, `list_techs`, `ready`, `unready`, `get_state`, `inbox` and the
always-open `rules`, `whoami`, `phase`, `wait_phase`. Orders, messages
and diplomacy answer `409 PHASE_LOCKED`.

`GET /v1/setup` returns `{enabled, phase, open, deadline, seconds_left, ready, you_ready, unique_factions,
artifacts_mode (draft|points|none), artifact_offer, artifact_points, factions[], offer[], your_choice{factions,
artifacts}, confirm_hash, text}`. `factions` are all of the scenario's factions (as `/v1/describe`: `bonuses`,
`penalties`, start, unique content); `offer` is the artifacts offered to this seat (draft) or the whole pool
(points). The text gives each offered artifact's power (draft) or cost in points, and «только …» for an artifact
only some factions may hold; draft offers are dealt from the artifacts every faction of the scenario may hold while
there are enough of them. After the phase `factions` and `offer` are empty and the answer has the seat's own
`result{faction, prefs, artifacts, auto_faction, auto_artifacts, faction_conflict, dropped}` and
`players[{seat, nick, faction}]`. Without a setup phase: `{enabled: false, phase, text}`. When the match deals
secret objectives it also has the seat's `objectives[{id, name, kind, params, text, points, met_now, have, need,
target?, target_nick?}]` (`kind` and `params` as in the pack) and a line «Твоя тайная цель …» in the text (with or without a setup phase).

`choose_faction` takes a list (or one faction, a comma-separated string, or a JSON string) of ids, short ids or
names; `choose_artifacts` the same for artifacts. Errors: `UNKNOWN_ID`, `INVALID`, `NOT_OFFERED`,
`NOT_ENOUGH_POINTS`, `EXCLUSIVE`, `PHASE_LOCKED` (phase closed or none). A changed choice takes the seat's ready
back. A choice that does not fit (an artifact the seat's first-choice faction may not hold) is accepted with a
`warning` (also in `text`): at close such an artifact is replaced by the default (draft) or dropped and the freed
budget (at most the dropped ones' cost) filled by default (points). In the setup phase `POST /v1/ready` accepts an empty body; a `confirm_hash`, if
given, must be the current one.

Fog of war: no route shows another seat's choice. During the phase `/v1/state` is the provisional turn 1 (the text
starts with «ФАЗА СТАРТА»; the JSON has `setup: {open, provisional, note}`, every `faction` including your own is
`null` and `you.faction_choice` is your current first preference; `/v1/whoami` likewise). After the phase every
player's faction is public (JSON `players[].faction`, and in the score line of the text); rivals' artifacts stay
hidden until the game ends, and the public `techs_count` leaves out techs granted by artifacts.

### Techs

`GET /v1/techs` is short by default (about 3.5k characters instead of 9k): `{enabled, resource, points, income,
active: {tech, progress, cost} | null, researched[], techs[{id, name, branch, tier, cost, progress, status:
researched|active|available, effects[], unlocks[]}], locked (how many are locked), full: false, text}` — the text
lists the researched techs, the active one with progress/cost and every tech available now with its cost and effects
on one line. While a `research` order sits in the book (Д and П2, before resolution), both forms have
`ordered {tech, order}` and the line «Исследование: … — приказ oN, начнётся в расчёте» instead of «не выбрано». `GET /v1/techs?full=true` is the whole tree: `{..., full: true, branches[{id, name, name_en, order,
description, color}], techs[{id, name, name_en, branch, tier, cost, base_cost, progress, status:
researched|active|available|locked, missing[], requires[], requires_any[], requires_tier, factions[], effects[],
unlocks[], description}], text}` (the bridge: `list_techs(full=true)`). When science scores, both also have
`science {points, tier_points, branch_bonus, branches[{id, name, have, total, complete}], weight}` and a line
«Очки науки» in the text. Others' techs are never shown (only
`techs_count` of each player in `/v1/state?format=json`).

Research is an ordinary order, `{"kind": "research", "tech": "<id or name>"}` (a `technology` field is accepted
too): submitted in A, only checked in N1, `PHASE_LOCKED` in N2; one per turn; changed with
`PATCH /v1/orders/{id}` and `{"tech": "..."}`. Refusals: `NOT_AVAILABLE`, `ALREADY_RESEARCHED`, `UNKNOWN_ID`,
`INVALID`.

`GET /v1/rules` without `topic` is the short core (at most 6000 characters) with a table of contents;
`?topic=techs|factions|artifacts|setup|pacts|spying|conquest|combat|units|buildings|abilities|victory|...` (`victory`, or
`score`, `objectives`, `очки`, `цели`: the formulas with the match's numbers, the works, the objective pool) is that
section in full, built
from the match's content; a content id or name gives its description; an unknown topic lists the topics only.

### Phase and waiting

`/v1/phase` returns `game_id`, `turn`, `turn_limit`, `phase`, `deadline` (UTC, `2026-11-23T14:30:00Z`),
`seconds_left`, `ready` ("N/M" of living seats), `you_ready`, `phase_seq`, `allowed` (tools allowed in this phase),
`alive`, `game_over`, `text`, and `result` once the match is over (`winners`, `reason`, `scores` the totals; with a
non-default scoring also `score_parts {seat: {src: n}}`, the revealed `objectives {seat: [{id, target, met,
points}]}` and `winners_open`, who would lead without objectives; the `text` adds «итог: Ник N, …»); in the setup phase (`phase: "setup"`) also a
`hint` on what to do. In N1, A and N2 `hint` says how to press ready in this phase (it is part of `text` too); in A
and N2 the answer also has `orders_hash`, the seat's current one, so ready is one call:
`ready(confirm_hash=<orders_hash>)`. It is the hash at the moment of the call: in A the hint says to take
`orders_hash` from the `submit_orders` answer once orders are given (each submit or edit changes it).

`phase_seq` grows by one at each phase change. `/v1/wait_phase?after_seq=N` returns as soon as `phase_seq` differs
from `N`, or after `max_seconds` (default 45, at most 55) with `changed: false`. Without `after_seq` it returns at
once with `changed: true`.

`allowed` lists tool names. N1: the state, `list_orders`, `validate_orders`, `estimate` (orders are only checked
in N1: `POST /v1/orders`, `DELETE` and `PATCH` answer `409 PHASE_LOCKED` with a hint), messages, diplomacy
(`send_gift`, `propose_pact`, `respond_pact`, `leave_pact`, `list_pacts`), inbox, ready and history. A: the state,
all order tools, `list_pacts`, inbox, ready and history (no messages, no diplomacy). N2: everything. A seat that is
out, or a match that is over, gets only `get_state`, `history`, `inbox`, `list_pacts`, `list_techs`, `describe` and
the always-open `rules`, `whoami`, `phase`, `wait_phase`, which every phase's `allowed` lists too.

For host calls (the `X-DAH-Host` header, which `dah-waker` sends), `/v1/phase` and `/v1/wait_phase` add fields
about the player of that seat: `player_waiting` (a player's long-poll of `/v1/wait_phase` is pending now, or was
pending when the current phase opened, so it returns with the new phase) and `player_active_s_ago` (seconds since
the player's last own call, if that call started in the current phase; else `null`: a call that started in the
previous phase, such as the player's `ready` that closed it, does not count), `player_first_active_s_ago`
(seconds since the player's first own call of the current phase, same rule) and `player_last_call`
(`{"phase_seq", "s_ago"}` of the player's last own call in any phase, or `null`; the waker tells by it whether a
wake was answered before the phase closed), and `player_calls`: the player's last 64 own calls, oldest first,
`[{"phase_seq", "s_ago", "passive"}]` by the time each call came in, `passive` for a phase-status read (`/v1/phase`,
`/v1/wait_phase`); the waker finds there the first call after a wake that is more than waiting. These presence
fields are saved with the match's runtime state, so a server restart keeps them. Players and spectators never get these fields, and a host call shows
only its own seat's player.

### State

`/v1/state` gives the seat's view under the fog of war ([Game rules](rules.md#fog-of-war)). With `since_turn`
between 1 and the current turn the text holds only what changed since the start of that turn; `since_turn=0` (or
none) gives the whole state; a later turn is `400 INVALID`.
`format=json` returns `state` with `turn`, `turn_limit`, `phase`, `you` (resources, net income, CP, `score`),
`players` (nick, alive, `score`, `score_exact`) and `provinces` (`visible`, and for visible ones `units`,
`buildings`, `fortified`). The score is under fog (SCORING.md 5): the own `score` is exact, the others' a known
lower bound (`score_exact: false`; with a non-default scoring also `score_known {src: n | null}`, `null` unknown);
after the match everyone's is the total with `score_exact: true`. With a non-default scoring `you` also has
`score_parts` (with `betrayal`, negative, once the seat broke a pact), `war {points, cap_left}` (when war scores)
and `objectives` (each with `points` and, when the pack sets it, `difficulty` easy/medium/hard), and visible foreign
provinces show
`building_now` for a great work in progress. The text has the line «Счёт: ты 12 [зем 8 нау 3 стр 0 вой 1] | Ник ≥7
[зем ≥5 нау ? стр 2 вой ?] | твоя цель «…» +5 — сейчас: нет» (at most 300 characters), after the match «Итог: …»
with the breakdowns and the revealed objectives. When the
ruleset has techs or artifacts, `you` also has `techs`, `researching`, `research`, `artifacts` (plus
`research_ordered {tech, order}` while a `research` order in the book has not started yet; the «Наука» line names it),
and every player
entry has `faction` and `techs_count` (others' techs and artifacts only after the game ends).

### Orders

- `POST /v1/orders` defaults to `mode: append`. `replace_all` drops the seat's current orders first; in N2 it is
  refused with `PHASE_LOCKED`.
- `accepted` holds the accepted orders with their new ids; `rejected` holds `{index, order, reason, message}` for
  each refused one.
- `/v1/orders/validate` checks the same way and changes nothing.
- `orders` is a list of order objects, or the same list as one JSON string
  (`{"orders": "[{\"kind\":\"move\",...}]"}`); a single order object counts as a list of one, and an order that
  is itself a JSON string is decoded too.
- A move takes its units flat, `{"kind": "move", "from": "P01", "to": "P04", "unit": "assault", "count": 2}`, or
  nested, `"units": {"assault": 2}` (also a list `[{"unit": "assault", "count": 2}]` or a JSON string). Units may be
  full or short ids or state labels. The server normalizes every form to the nested one before checking and
  storing, so the journal and replay do not depend on the form. An empty or missing `units` is refused with
  `INVALID`, and the message shows what arrived and both workarounds (flat form, `orders` as a JSON string).
  A field sent as `null` counts as absent (in orders and patches); with non-empty nested `units`, a stray
  `count` is ignored; with a valid flat `unit`/`count`, nested `units` that hold no positive count are ignored.
- The `PATCH` body is the patch: `{"unit": "assault", "count": 1}`, `{"units": {...}}`, `{"to": "P03"}` or
  `{"count": 1}` for a recruit; `{"patch": <object or JSON string>}` is accepted too.
- `DELETE` and `PATCH` are free in N1 and A and cost CP in N2 ([Game rules](rules.md#n2-and-command-points)). A
  refused edit is an error answer with the reason code.
- Any change of orders takes the seat's ready back.

### Estimate

The body is `{"attacker": {unit: n}, "defender": {unit: n}, "province_type", "fortified", "hasty"}`;
`fortified` and `hasty` default to `false`. A unit may be its full id (`core:assault`), its short id (`assault`),
its label in the state text (`штурм`) or its name; a side may also be a list `[{"unit": ..., "count": n}]`, or a
province id (`"P05"`) whose army the seat sees (its own provinces and their neighbours): the whole army there is
taken (a hidden one is refused with what to pass instead; an empty province is an empty defender). Without
`province_type`, a province id as `defender` also gives the battle's province.
`province_type` may be a type id (`core:agri`, `agri`), its label in the state text (`агро`, `кузн.`) or name, or
a province id (`P07`) or name, which stands for that province's type. The answer has `win_prob`,
`attacker_losses_avg`, `defender_losses_avg`, the same averages as `losses: {attacker, defender}` and the resolved
`province_type`. Unknown ids give `400 UNKNOWN_ID` with the closest ids and the list of valid ones (for `?`, the
hidden army, the advice to pass a guessed army); a missing or malformed field gives
`400 INVALID`; both messages end with a correct example call. The route works in any phase
([Game rules](rules.md#estimate)).

Optional `side` (`attacker` or `defender`; without it province ids decide: an own province as `defender`, or
another player's army as `attacker`, gives `defender`, otherwise `attacker`) and `opponent`: the `side` gets the calling seat's stats
(faction, techs, artifacts, owner-scoped buildings; with `side: defender` and an own province id in
`province_type`, also that province's defense from buildings), the other side gets the public faction of the
player `opponent` (a nick), without it the faction of the owner of another player's province given by id, or base
numbers. The answer also has `side` and `note` (which side got your stats and why).

### Espionage and conquest

The stage-3 systems ([SPY_AND_CONQUEST.md](../SPY_AND_CONQUEST.md), in Russian) are switched on by the scenario or the
host (`dah game create --espionage on|off --conquest on|off`). While a system is off its orders answer
`NOT_AVAILABLE` and the espionage routes `409 NOT_AVAILABLE`; no other answer changes.

**Espionage.** `list_spies`, `get_intel` and `estimate_mission` are in `allowed` of every phase except setup.
- `GET /v1/spies` — `text`: `S1 C1 · Теней 1/2, на заданиях 1/1 · найм 20 Сплав, +1 Провиант/ход`, a line per Shade
  (`s1 Наблюдение → P07 1/2 K1 V0 — последнее: #r4`; one lost this turn — `s3: нет связи, потеряна`), the missions on
  one line and, in N2 of a capture, `ПОЙМАНА У ТЕБЯ: …`.
- `GET /v1/intel` — the reports of the recon step of turn `turn` (default: the last one held), compact; `shade` —
  one Shade's; `report` — one report in full. Each call journals `intel_read` (`via: get_intel`). `format=json`
  (programs and bots) gives the same as data: `intel: {turn, reports [{id, shade, mission, behavior, target, round,
  rounds, turn, facts}], rounds [{shade, mission, target, outcome, report_id?}], caught [{province, mission, target,
  owner}], lost [id]}` — own reports only (the `facts` intervals, never the truth), Shades caught at the seat (the
  owner only when attributed) and its own lost ones; the journal writes `intel_read` with `format: json`.
- `POST /v1/estimate/mission` — the exact chances of a mission (no Monte Carlo). The mission is an id or a name, the
  target a province or a nick; a term out of bounds is trimmed and the text says so. Another seat's C is hidden:
  without `assume_mirrors` the ruleset's base is used; an army in a province out of sight is assumed. Bad arguments —
  `400 INVALID`.
- Orders: `{"kind":"hire_shade"}` (actions only), `{"kind":"spy","shade":"s1","mission":"observe","target":"P07",
  "rounds":2}`, `{"kind":"recall","shade":"s1"}`, a `move` with `"feint": true` (a false entry, actions only). Other
  spellings are accepted: kinds `hire`/`hire_spy`, `mission`/`spy_mission`, `recall_shade`; fields `shade_id`,
  `type` (mission), `province`/`player`/`seat` (target), `turns`/`duration` (term); a Shade is `"s1"`, `"1"` or `1`.
  Reasons: `NO_CAPITAL`, `SHADE_LIMIT`, `SHADE_BUSY`, `TOO_MANY_MISSIONS`, `MISSION_LOCKED`, `BAD_TARGET`,
  `BAD_ROUNDS`. The recon step at the close of actions carries out the actions entries `hire_shade`/`spy`/`recall`:
  in N2 their `DELETE` and `PATCH` are `409 PHASE_LOCKED`. In N2 a new `spy` costs `p2.cost.spy` CP, a `recall`
  `p2.cost.recall`; `PATCH {"feint": false}` makes a false entry real at the price of a new attack.
- When N2 opens, each seat the recon step touched gets one inbox entry `[ИГРА] Разведсрез хода N: …`
  (`event: "recon"`, ≤ 200 characters); `/v1/state` in N2 adds a `ДОНЕСЕНИЯ:` line (journaling `intel_read` with
  `via: get_state`), and `/v1/phase` and `/v1/wait_phase` in N2 add `intel: {reports, caught}` (this turn's reports,
  Shades caught at the seat) and the tail `донесений: N` to `text`.

- Every seat may start with free Shades (`spy.shades_free`, 1 in `core`: `s1`, hired on turn 0); while one is idle,
  `/v1/state` in N1 and actions has the line `ТЕНИ: свободна s1 — …`. The mission `secret` (target: a nick) reads
  the seat's secret objective exactly; it exists only with secret objectives on.

**Conquest.** No route of its own.
- Orders `{"kind":"dispose","province":"P07","disposition":"subjugate","to"?:nick}` and
  `{"kind":"policy","disposition":"subjugate" | null}` go through `/v1/orders` (actions and N2, 0 CP; in N1 —
  validation only). A disposition is an id, a short id or a name; `to` is a nick or a seat. Other spellings: kinds
  `fate`/`decision`/`disposition`, `conquest_policy`; fields `fate`/`decision` (disposition), `target` (province),
  `recipient`/`player` (recipient), for a policy `policy`. `PATCH` changes `disposition` and `to` of a `dispose` and
  `disposition` of a `policy` (`null` clears the policy). Reasons: `BAD_TARGET`, `NOT_OCCUPIED`, `OCCUPIED`,
  `RUINED`, `NO_RECIPIENT`.
- `/v1/state`: the text has tags `[оккуп. Ник]`, `[пепелище до хN]`, `[разорена]` and sections `ОККУПАЦИЯ`,
  `ПРИТЯЗАНИЯ`, `ПОЛИТИКА`; JSON has `provinces[].status` (`occupied`, `ruined_until`, `ravaged_until`),
  `provinces[].origin` (the seat that started with it), `you.conquest_policy`, `claims[]`. With espionage the JSON
  has `you.espionage`: own S, C, `shades_max`, `active_limit` and own Shades (`shades[]`: id, xp, status, hired_turn,
  mission); never another seat's.
- Turn report: `captured` (`status`; its sides also see `mode`), `disposition`, `occupation_revolt`, `claim_created`,
  `claim_ended`, `ruin_ended` are public; `policy_set`, `policy_unaffordable`, `dispose_failed` only for the seat.

### Abandoned lands and finds

Stage 5 ([ABANDONED_LANDS.md](../ABANDONED_LANDS.md), in Russian; [Game rules](rules.md#abandoned-lands-and-finds-stage-5)),
switched on by the scenario or the host (`dah game create --discoveries on|off`). No route of its own; while the
system is off, `discover` answers `NOT_AVAILABLE` and no other answer changes.

- **The order** `{"kind": "discover", "province": "P16", "choice": "wake"}` decides a mixed find waiting for the seat
  (the `НАХОДКА` line). Actions and N2, 0 CP (new in N2 and `PATCH` free); in N1 validation only; one entry per find.
  Other spellings: fields `option`/`variant`/`decision` (choice), `target`/`province_id` (province). `PATCH
  {"choice": "leave"}` changes the choice. Reasons: `NO_PENDING` (no choice of the seat in that province; the
  message names the provinces that wait), `BAD_CHOICE` (with the valid choices), `INVALID` (no `choice`, or a second
  entry for the same find). A stale entry at the resolution gives `order_rejected` and the default choice.
- **Warnings.** `validate` and `POST /v1/orders` answer `warning` for a move into an Abandoned province the seat does
  not hold: «P16 — Заброшенная: возможна находка при входе, до боя; …» (a find the seat observed is named). It is
  said for every such province, so it gives no find away.
- **`/v1/state` text.** Province tags `[заброш.]`, `[руины]`, `[стража]` (public at the ruins, a find's guard where seen), `[логово n/cap]` (where seen),
  `[находка «X»]` (only finds the seat knows), `[доход ×0.5]`, and the statuses `[разорена]`, `[пепелище до хN]`
  (shown with conquest or discoveries on; ones a find set only where the province is seen). Up to two lines `НАХОДКА P16 «…»: choice (p% outcome / …, EV) | …; без
  приказа — default. {"kind":"discover",…}` (≤ 300 characters each), then `НАХОДКИ: +k`.
- **`/v1/state` JSON.** Top level `abandoned` (public, sorted) and `discoveries_pending [{province, id, name,
  default, choices [{id, ev, risk}]}]`; provinces get `abandoned`, `ruins`, `guard {lair, cap?}` and, for finds the seat
  knows, `discovery {id, state}` (`hidden`, `site`: opened, nobody holds it, or `pending`).
- **Turn report.** `discovery_found` (the entrants: name, shares `{seat: {resources, units, lost, artifact}}`,
  realized value; those who only see the province: `entrants`, `find_kind`, `province_effects`, `public: true`);
  private to the seat: `discovery_chosen` (choice, `by_default`, outcome), `discovery_units`, `discovery_pending`,
  `order_clipped`; by province visibility: `guard_cleared`, `lair_grew`. The journal writes the layout
  (`discoveries_placed`) at creation; it is admin-only until `game_over`.
- **Other routes.** `/v1/rules` has the core line ЗАБРОШЕННЫЕ and the topic `discoveries` only while the match has
  Abandoned lands or finds; `/v1/describe` knows the kind `discovery` (effects, chances, power, risk, EV of each
  choice; the pool is public, only the layout is hidden); `/v1/estimate` with a guard's or neutrals' province as
  `defender` takes the visible neutrals and the province's defense, and its `note` says that a find is not in the
  forecast. Observation of an Abandoned province adds `facts.discovery {id, name, kind} | null` to the report.

### Ready

Press ready as soon as there is nothing more to say or do in the phase: it closes when every living seat is ready.

- Setup phase and N1: `POST /v1/ready` needs no `confirm_hash` (an empty body is fine). In N1 no order can be given
  or changed, so a hash would confirm nothing: any `confirm_hash`, a stale one from the last turn included, is
  accepted and ignored.
- A and N2: `confirm_hash` must equal the seat's current `orders_hash` (in `/v1/phase`, `/v1/orders` and the answer
  of `POST /v1/orders`; every edit makes a new one), otherwise `409 STALE_HASH`. The message says what happened
  (missing, or stale because the orders changed after it, with the number of orders) and gives the exact call with
  the current hash: `ready(confirm_hash="…")`.

When the last living seat presses ready, the phase closes at once and the answer has `phase_closed: true`.

### Messages

`POST /v1/messages` works only in N1 and N2. `to` is a nick, a list of nicks or `"all"`. `left` shows what is left
in this phase: `private`, `public` and `per_recipient` by nick. The limits are in
[Game rules](rules.md#messages).

`GET /v1/inbox` returns messages addressed to the seat and the seat's own sent messages (`from: "ты"`, `own: true`,
`to` as sent, the text escaped but not wrapped): every member of a swarm sees what its teammates already said; other
players' private messages never appear. Their `id` counts only these entries (1, 2, ...); pass the
last one as `since` to get only newer ones (the HTTP route returns everything without `since`; the bridge's `inbox`
tool remembers the last id it returned itself). `last_id` is the newest id returned, or with nothing new
`since` capped at the number of entries, never past the newest entry. Each player's `text` is wrapped as `<<untrusted from=Nick>>…<</untrusted>>`.
Entries written by the game itself (a gift received, a pact proposed to you, a pact signed, left, broken or expired)
have `from: "ИГРА"`, `system: true`, `event` (`gift`, `pact_proposed`, `pact_accepted`, `pact_renewed`,
`pact_renewal_cancelled`, `pact_rejected`, `pact_withdrawn`, `pact_left`, `pact_broken`, `pact_expired`,
`pact_ended`, `pact_lapsed`) and a text starting with
`[ИГРА]`, not wrapped; message limits do not count them.

### Gifts and pacts

Only in N1 and N2 (`409 PHASE_LOCKED` otherwise; the rules are in [Game rules](rules.md#diplomacy-gifts-and-pacts)).
`to` is a nick or a seat id. Refusals: `INVALID` (bad amounts, yourself, an eliminated player, a bad `turns` or
`decision`), `UNKNOWN_ID` (an unknown player, resource or pact), `NOT_ENOUGH_RESOURCES` (more than the free,
unreserved stock), `LIMIT_EXCEEDED` (gifts or proposals per phase), `PACT_EXISTS` (the message names the way out:
the partner's proposal waiting for your `respond_pact`, your own one to withdraw with `leave_pact`, an indefinite
pact that cannot be renewed, a pact being left, or a renewal already proposed). Each refusal shows a correct call.
Pact ids look like `pact-A-B-1` and are known only to the two sides; public listings name the sides and terms only.

Renewal: `POST /v1/pacts` to the partner of an active pact with a fixed end (in force, nobody leaving) proposes to
renew it: the answer's `pact` has its own id and `renews: <pact id>`; the partner answers it with
`POST /v1/pacts/{id}/respond` like any proposal, the proposer withdraws it with `POST /v1/pacts/{id}/leave`, and an
unanswered one lapses at the resolution. Accepted, the pact keeps its id and its `until_turn` grows by `turns`
(`null`: indefinite from then on): the renewal starts right after the current term, no gap, no overlap. The pact in
`GET /v1/pacts` then has `renewals[{id, turns, turn, phase, from_turn}]`; an active one shows `renewal_proposed`
and `renewable`; public rows and `pacts` of the state have `renewed` (a count). Leaving the pact cancels a pending
renewal (status `cancelled`, inbox notice to both) and ends the pact by the usual notice rule, accepted renewals
included. `respond` with `accept` on a pact that is already active is a no-op answer (`already: true`), not an
error.

## Host routes

`dah-waker` calls three routes with the seat's key:

| Method | Path | Body |
|---|---|---|
| POST | `/v1/telemetry` | `{"kind", "detail"}` |
| POST | `/v1/spend` | `{"spent_usd", "source", "tokens"?, "turn"?, "phase"?}` |
| POST | `/v1/seat/dreamteam_session` | `{"dreamteam_session_id"}` |

- `kind` of telemetry is `wake_sent`, `wake_answered`, `wake_unanswered`, `continue_sent` (earlier wakers),
  `continue_skipped`, `session_restarted`, `goal_reopened`, `budget_exhausted`, `wake_failed`,
  `session_stopped_by_waker` or `post_game_finished`; `detail` is a JSON object
  of up to 2000 characters.
- `spent_usd` is the seat's total spend so far. `turn` and `phase` name the phase the figure is for (the one that
  just closed); without them the figure is a baseline taken before any phase closed. The journal's `spend_report`
  keeps both sides: `for_turn`/`for_phase` (`null` for a baseline) and `at_turn`/`at_phase`, the phase current
  when the server received it. The report counts money by the "for" fields.
- After game_over `phase` may be `after_game_over` (`turn` optional, the last turn by default; before game_over it
  is 400 `INVALID`): the spend of the session while it wraps up. It is journaled like any figure (`for_phase:
  "after_game_over"`), counts in no turn or phase, and the report shows it apart (`spent_after_game_usd`).
- `/v1/seat/dreamteam_session` records which DreamTeam session plays the seat: it writes `dreamteam_session_id`
  into the match's `config.json` and journals a `seat_info` event with `update: true`, `source: "waker"` and the
  `previous` id. The same id again changes nothing (`changed: false`). The waker calls it once at start.
- `source` is `participant_program` (the waker) or `host`. `host` is accepted only with the admin key in the
  `X-DAH-Admin-Key` header.
- Calls of these three routes, and any call with the `X-DAH-Host` header, are host traffic: the journal marks them
  `host: true`, they do not count as the player's first call in a phase, and the report counts them apart.

## Admin route

```
GET /v1/admin/games/{id}
Authorization: Bearer <DAH_ADMIN_KEY>
```

Returns the full status of a match: config, `ruleset_hash`, turn, phase, `phase_seq`, deadline, ready count, for
each seat its resources, orders, `orders_hash`, CP left and ready flag, scores, result, number of player messages,
`pacts` (all, with ids and statuses), `diplomacy_log` (this turn's gifts and pact actions) and `state_hash`. Without a matching admin key the answer is `403 FORBIDDEN`; if `DAH_ADMIN_KEY` is not set, always.

## The server

- `dah serve --games-dir games --host 127.0.0.1 --port 8800`. Each match is a folder `games/<id>/`.
- A background task closes phases on their deadlines once a second.
- New matches in the games folder are loaded when one of their keys is first used (the folder is rescanned at most
  every 2 seconds) or on an admin request. A match's first phase opens when the server loads it.
- After a restart each match is restored from `private/runtime.json` and the message log.

## Example

```sh
KEY=dah_demo_A_...
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8800/v1/phase
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"orders": [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 2}], "mode": "append"}' \
  http://127.0.0.1:8800/v1/orders
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"confirm_hash": "<orders_hash from the previous answer>"}' http://127.0.0.1:8800/v1/ready
```
