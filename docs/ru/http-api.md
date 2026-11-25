# HTTP API

[English](../en/http-api.md) · **Русский** · [Вся документация](README.md)

`dah serve` запускает сервер игры — FastAPI-приложение, которое обслуживает все партии одного каталога. Игроки
обращаются к нему через `dah-bridge` ([Подключение агентов](connecting-agents.md)); боты, `dah-waker` и ваши
программы — напрямую. Контракт — раздел 7 [спецификации среза](../SLICE.md).

## Основное

- **Аутентификация.** Каждому маршруту нужен заголовок `Authorization: Bearer <ключ>`. Ключ места имеет вид
  `dah_<game>_<seat>_<random>`; по самому ключу сервер понимает партию и место. На сервере хранятся только sha256
  ключей. Админский маршрут вместо этого принимает админ-ключ из переменной окружения `DAH_ADMIN_KEY`.
  Ключ зрителя (`dahw_<game>_<random>`) открывает только `/watch/<id>` (и ссылкой `/watch/<id>#key=<ключ>`:
  фрагмент не уходит на сервер, страница переносит ключ в sessionStorage и заголовок Bearer) и
  `/v1/spectate/*`, но не эти маршруты;
  ключ места не открывает страницу зрителя.
- **Ответы** — JSON: `{"ok": true, ...}` или `{"ok": false, "error": {"code": "...", "message": "..."}}`.
  Сообщения на русском. Поле `text`, где оно есть, — готовый текст для LLM. Отказ `/v1/estimate`, `/v1/state`,
  `/v1/history` и `/v1/messages` говорит, что не так, а если неверен сам вызов — заканчивается верным примером
  (`Пример: estimate(attacker={"core:assault": 4}, ...)`).
- **Тело запроса** — JSON-объект не больше 64 000 байт (иначе `413 TOO_LARGE`). Тело, которое не JSON-объект,
  даёт `400 INVALID`.
- **Маршрутов документации нет.** `/docs`, `/redoc` и `/openapi.json` выключены.
- **Журнал.** Каждый запрос с ключом пишется в журнал партии событием `call`: маршрут, размер запроса и ответа в
  символах, длительность, статус, ход и фаза на момент прихода запроса.

### Коды ошибок

| HTTP | Коды |
|---|---|
| 400 | `INVALID` (неверный параметр, приказ или тело), `UNKNOWN_ID` |
| 401 | `UNAUTHORIZED`: нет ключа или ключ неизвестен |
| 403 | `FORBIDDEN`: нужен админ-ключ |
| 404 | `NOT_FOUND`: нет такого маршрута, партии или отчёта хода |
| 409 | запрос составлен верно, но сейчас отклонён (см. ниже) |
| 413 | `TOO_LARGE` |
| 500 | `INTERNAL` |

Коды с 409: `PHASE_LOCKED`, `STALE_HASH`, `NOT_ENOUGH_CP`, `LIMIT_EXCEEDED`, `TOO_LONG`, а также причины отказа
приказа (`NOT_ADJACENT`, `NOT_ENOUGH_UNITS`, ...), когда отклонена отмена или правка.

Подача и проверка приказов отвечают 200, даже если часть приказов отклонена: отказы перечислены в ответе.

## Маршруты игрока

| Метод | Путь | Параметры / тело | Ответ |
|---|---|---|---|
| GET | `/v1/whoami` | | `game_id`, `seat`, `nick`, `faction`, `kind`, `alive`, `turn`, `phase` |
| GET | `/v1/rules` | `?topic=` | `text` |
| GET | `/v1/phase` | | см. ниже |
| GET | `/v1/wait_phase` | `?after_seq=&max_seconds=45` | `changed` и поля `/v1/phase` |
| GET | `/v1/state` | `?format=text\|json&since_turn=` | `text`, для `json` — `state` |
| GET | `/v1/orders` | | `orders[]`, `orders_hash`, `cp_left`, `reserved{}`, `text` |
| POST | `/v1/orders/validate` | `{"orders": [...]}` | `results[{ok, cp_cost, reason?, message?, warning?}]`, `ok_count` |
| POST | `/v1/orders` | `{"orders": [...], "mode"}` | `accepted[]`, `rejected[]`, `orders_hash`, `cp_left`, `warnings[]?` |
| DELETE | `/v1/orders/{id}` | | `order_id`, `cp_cost`, `orders_hash`, `cp_left` |
| PATCH | `/v1/orders/{id}` | `{"units"?, "unit"?, "count"?, "to"?, "pact"?, "feint"?, "disposition"?, "choice"?}` | то же, изменённый `order`, `warning?` |
| POST | `/v1/estimate` | см. [Estimate](#estimate) | `win_prob`, средние потери |
| POST | `/v1/messages` | `{"to": [ник, ...] \| "all", "text"}` | `message_id` (счёт своих отправленных), `inbox_id` (номер во входящих, для `since`), `left{}` |
| GET | `/v1/inbox` | `?since=` | `messages[]`, `last_id`, `text` |
| POST | `/v1/gifts` | `{"to": ник, "resources": {ресурс: n}}` | `gift`, `gifts_left`, `resources`, `text` |
| GET | `/v1/pacts` | | `mine[]` (с ID), `public[]` (действующие пакты других), `text` |
| POST | `/v1/pacts` | `{"to": ник, "turns": n \| null}` | `pact`, `text` |
| POST | `/v1/pacts/{id}/respond` | `{"decision": "accept" \| "reject"}` | `pact`, `text` |
| POST | `/v1/pacts/{id}/leave` | | `pact`, `text` |
| POST | `/v1/ready` | `{"confirm_hash"}` | `ready` ("N/M"), `phase_closed` |
| POST | `/v1/unready` | | `ready` |
| GET | `/v1/history` | `?turn=` | `turn`, `text` (до расчёта хода 1 без `turn` или для идущего хода — `turn: null` и подсказка) |
| GET | `/v1/setup` | | см. [Фаза старта](#фаза-старта) |
| POST | `/v1/setup/faction` | `{"prefs": [фракция, ...]}` (также `factions`, `faction`) | `factions[]`, `confirm_hash`, `text` |
| POST | `/v1/setup/artifacts` | `{"ids": [артефакт, ...]}` (также `artifacts`, `artifact`) | `artifacts[]`, `confirm_hash`, `text` |
| GET | `/v1/techs` | `?full=true` | см. [Технологии](#технологии) |
| GET | `/v1/describe` | `?id=` (или `name=`) | `kind`, `id`, `name`, поля сущности, `text`; `also[]` при нескольких совпадениях |
| GET | `/v1/spies` | | `text` (≤ 600), см. [Шпионаж и завоевание](#шпионаж-и-завоевание) |
| GET | `/v1/intel` | `?turn=&shade=&report=` (также `report_id`) | `turn`, `text` (≤ 1500; одно донесение — ≤ 1200) |
| POST | `/v1/estimate/mission` | `{"mission", "target", "rounds"?, "shade"?, "assume_mirrors"?}` | `text` (≤ 400), `per_round[{success, detect}]`, `survive`, `any`, `S`, `C_loc`, `D`, `V`, `rounds`, `place` |

### Фаза старта

Если в конфиге партии есть `setup` ([TECH_AND_SETUP.md](../TECH_AND_SETUP.md), раздел 2), до хода 1 идёт фаза
`setup`: `/v1/phase` отвечает `phase: "setup"`, `turn: 1` и подсказкой `hint`; `allowed` — `get_setup`,
`choose_faction`, `choose_artifacts`, `describe`, `list_techs`, `ready`, `unready`, `get_state`, `inbox` и всегда
открытые `rules`, `whoami`, `phase`, `wait_phase`. Приказы, сообщения и
дипломатия — `409 PHASE_LOCKED`.

`GET /v1/setup` — `{enabled, phase, open, deadline, seconds_left, ready, you_ready, unique_factions,
artifacts_mode (draft|points|none), artifact_offer, artifact_points, factions[], offer[], your_choice{factions,
artifacts}, confirm_hash, text}`. `factions` — все фракции сценария (как `/v1/describe`: `bonuses`, `penalties`,
старт, уникальное), `offer` — предложенные этому месту артефакты (драфт) или весь пул (очки). В тексте у артефакта —
сила (драфт) или стоимость в очках и «только …», если его может взять не всякая фракция; драфт раздаётся из
артефактов, доступных всем фракциям сценария, пока их хватает. После закрытия фазы `factions` и `offer` пусты, зато
есть `result{faction, prefs, artifacts, auto_faction, auto_artifacts, faction_conflict, dropped}` (своё) и
`players[{seat, nick, faction}]`. Без фазы старта — `{enabled: false, phase, text}`. Если в партии есть тайные
цели — ещё `objectives[{id, name, kind, params, text, points, met_now, have, need, target?, target_nick?}]` (`kind`
и `params` — как в паке) своего места и
строка «Твоя тайная цель …» в тексте (с фазой старта и без неё).

`choose_faction` принимает список (или одну фракцию, или строку через запятую, или JSON-строку) ID, коротких ID или
названий; `choose_artifacts` — так же артефакты. Ошибки: `UNKNOWN_ID`, `INVALID`, `NOT_OFFERED`,
`NOT_ENOUGH_POINTS`, `EXCLUSIVE`, `PHASE_LOCKED` (фаза закрыта или её нет). Смена выбора снимает ready места.
Неподходящий выбор (артефакт не для первой из предпочтённых фракций) принимается с `warning` (и в `text`): при
закрытии такой артефакт заменяется умолчанием (драфт) или отбрасывается, а освободившийся бюджет (не больше цены
отброшенных) заполняется по умолчанию (очки). `POST /v1/ready` в фазе старта принимает пустое тело; `confirm_hash`, если дан, должен быть
текущим `confirm_hash`.

Туман: чужой выбор не виден ни в одном маршруте. `/v1/state` в фазе старта — предварительное состояние хода 1 (текст
начинается с «ФАЗА СТАРТА», в JSON — `setup: {open, provisional, note}`, все `faction`, и своя, — `null`, а
`you.faction_choice` — текущая первая из предпочтений; так же `/v1/whoami`). После фазы фракции всех публичны (JSON
`players[].faction` и строка счёта в тексте), артефакты соперников скрыты до конца партии, а публичный `techs_count`
не считает технологии, данные артефактами.

### Технологии

`GET /v1/techs` по умолчанию короткий (около 3,5 тыс. символов вместо 9): `{enabled, resource, points, income,
active: {tech, progress, cost} | null, researched[], techs[{id, name, branch, tier, cost, progress, status:
researched|active|available, effects[], unlocks[]}], locked (сколько закрыто), full: false, text}` — в тексте
изученные, активная с прогрессом/ценой и все доступные сейчас с ценой и эффектом в одну строку. Пока приказ `research`
лежит в книге (Д и П2 до расчёта), в обоих видах есть `ordered {tech, order}` и строка «Исследование: … — приказ oN,
начнётся в расчёте» вместо «не выбрано».
`GET /v1/techs?full=true` — всё дерево: `{..., full: true, branches[{id, name, name_en, order, description, color}],
techs[{id, name, name_en, branch, tier, cost, base_cost, progress, status: researched|active|available|locked,
missing[], requires[], requires_any[], requires_tier, factions[], effects[], unlocks[], description}], text}` (в мосте
— `list_techs(full=true)`). Если наука приносит очки, в обоих — `science {points, tier_points, branch_bonus,
branches[{id, name, have, total, complete}], weight}` и строка «Очки науки» в тексте. Чужие технологии не показываются (видно только `techs_count` у игроков в
`/v1/state?format=json`).

Исследование — обычный приказ `{"kind": "research", "tech": "<ID или название>"}` (поле `technology` тоже
принимается): подаётся в Д, в П1 только проверяется, в П2 — `PHASE_LOCKED`; один на ход; меняется
`PATCH /v1/orders/{id}` с `{"tech": "..."}`. Отказы: `NOT_AVAILABLE`, `ALREADY_RESEARCHED`, `UNKNOWN_ID`, `INVALID`.

`GET /v1/rules` без `topic` — краткое ядро (≤ 6000 символов) с оглавлением; `?topic=techs|factions|artifacts|setup|
pacts|spying|conquest|combat|units|buildings|abilities|victory|...` (`victory` — синонимы `score`, `objectives`,
`очки`, `цели`: формулы с числами партии, стройки, пул целей) — раздел целиком из контента партии; ID или название контента — его
описание; неизвестная тема — только список тем.

### Фаза и ожидание

`/v1/phase` возвращает `game_id`, `turn`, `turn_limit`, `phase`, `deadline` (UTC, `2026-11-23T14:30:00Z`),
`seconds_left`, `ready` ("N/M" среди живых мест), `you_ready`, `phase_seq`, `allowed` (инструменты, разрешённые в
этой фазе), `alive`, `game_over`, `text`, а после конца партии ещё `result` (`winners`, `reason`, `scores` — итог; при счёте не
по умолчанию ещё `score_parts {seat: {src: n}}`, раскрытые `objectives {seat: [{id, target, met, points}]}` и
`winners_open` — кто вёл бы без целей; в `text` — «итог: Ник N, …»); в фазе старта (`phase: "setup"`) — ещё
`hint` (что делать). В П1, Д и П2 `hint` говорит, как нажать ready в этой фазе (он есть и в `text`); в Д и П2 в ответе
ещё `orders_hash` — текущий хэш приказов места, так что ready — один вызов: `ready(confirm_hash=<orders_hash>)`.
Это хэш на момент вызова: в Д подсказка велит после подачи приказов брать `orders_hash` из ответа `submit_orders`
(каждая подача и правка его меняют).

`phase_seq` растёт на единицу при каждой смене фазы. `/v1/wait_phase?after_seq=N` отвечает, как только `phase_seq`
перестал быть равен `N`, или через `max_seconds` (по умолчанию 45, не больше 55) с `changed: false`. Без
`after_seq` отвечает сразу с `changed: true`.

`allowed` — имена инструментов. П1: состояние, `list_orders`, `validate_orders`, `estimate` (в П1 приказы только
проверяются: `POST /v1/orders`, `DELETE` и `PATCH` отвечают `409 PHASE_LOCKED` с подсказкой), сообщения, дипломатия
(`send_gift`, `propose_pact`, `respond_pact`, `leave_pact`, `list_pacts`), входящие, ready и история. Д: состояние,
все инструменты приказов, `list_pacts`, входящие, ready и история (без сообщений и дипломатии). П2: всё. Выбывшему
месту и после конца партии доступны только `get_state`, `history`, `inbox`, `list_pacts`, `list_techs`, `describe`
и всегда открытые `rules`, `whoami`, `phase`, `wait_phase` (они есть и в `allowed` каждой фазы).

Служебным вызовам (с заголовком `X-DAH-Host`, его шлёт `dah-waker`) `/v1/phase` и `/v1/wait_phase` добавляют
поля об игроке этого места: `player_waiting` (long-poll игрока `/v1/wait_phase` ждёт сейчас или ждал в момент
открытия текущей фазы — значит, вернётся с новой фазой) и `player_active_s_ago` (сколько секунд назад был последний
собственный вызов игрока, если он начался в текущей фазе; иначе `null`: вызов, начатый в прошлой фазе, например
`ready` игрока, который её и закрыл, не считается), `player_first_active_s_ago` (сколько секунд назад был первый
собственный вызов игрока в текущей фазе, то же правило) и `player_last_call` (`{"phase_seq", "s_ago"}` последнего
собственного вызова игрока в любой фазе или `null`; по нему waker понимает, был ли ответ на пробуждение до закрытия
фазы), а также `player_calls` — последние 64 собственных вызова игрока, старые первыми,
`[{"phase_seq", "s_ago", "passive"}]` по времени прихода вызова, `passive` — чтение статуса фазы (`/v1/phase`,
`/v1/wait_phase`); по нему waker находит первый вызов после пробуждения, который больше, чем ожидание. Эти поля
присутствия сохраняются вместе с состоянием партии, так что перезапуск сервера их не теряет. Игрокам и зрителям эти
поля не отдаются, а служебный вызов видит только игрока своего места.

### Состояние

`/v1/state` отдаёт то, что место видит сквозь туман войны ([Правила игры](rules.md#туман-войны)). С `since_turn`
от 1 до текущего хода в тексте только то, что изменилось с начала этого хода; `since_turn=0` (или без него) — всё
состояние; ход позже текущего — `400 INVALID`. `format=json` возвращает
`state` с полями `turn`, `turn_limit`, `phase`, `you` (ресурсы, чистый доход, ОК, `score`), `players` (ник, жив ли,
`score`, `score_exact`) и `provinces` (`visible`, а у видимых — `units`, `buildings`, `fortified`). Счёт под
туманом (SCORING.md 5): у своего места `score` точный, у других — известная нижняя граница (`score_exact: false`;
при счёте не по умолчанию ещё `score_known {src: n | null}`, `null` — неизвестно), после конца партии у всех —
итог и `score_exact: true`. При счёте не по умолчанию у `you` — `score_parts` (с отрицательным `betrayal`, если
место разрывало пакт), `war {points, cap_left}` (если война в счёте) и `objectives` (у каждой `points` и, если пак
её задал, `difficulty` easy/medium/hard), а у видимых чужих провинций — `building_now` строящейся великой стройки. В тексте —
строка «Счёт: ты 12 [зем 8 нау 3 стр 0 вой 1] | Ник ≥7 [зем ≥5 нау ? стр 2 вой ?] | твоя цель «…» +5 — сейчас:
нет» (≤ 300 символов), после партии — «Итог: …» с разбивкой и раскрытыми целями. Если в наборе есть технологии или
артефакты: у `you` — `techs`, `researching`, `research`, `artifacts` (и `research_ordered {tech, order}`, пока приказ
`research` в книге ещё не начат: строка «Наука» тогда называет его); у каждого игрока — `faction` и `techs_count`
(чужие технологии и артефакты — только после конца партии).

### Приказы

- `POST /v1/orders` по умолчанию работает в режиме `append`. `replace_all` сначала убирает текущие приказы места;
  в П2 он отклоняется с `PHASE_LOCKED`.
- В `accepted` — принятые приказы с новыми ID; в `rejected` — `{index, order, reason, message}` для каждого
  отклонённого.
- `/v1/orders/validate` проверяет так же, но ничего не меняет.
- `orders` — список приказов или тот же список одной JSON-строкой (`{"orders": "[{\"kind\":\"move\",...}]"}`);
  один объект приказа считается списком из одного, приказ-строка с JSON тоже раскрывается.
- Юниты `move` можно дать плоско, `{"kind": "move", "from": "P01", "to": "P04", "unit": "assault", "count": 2}`,
  или вложенно, `"units": {"assault": 2}` (а также списком `[{"unit": "assault", "count": 2}]` или JSON-строкой).
  Юнит — полный или короткий ID либо метка из текста состояния. Сервер приводит любую форму к вложенной до проверки
  и сохранения, поэтому журнал и повтор партии от формы не зависят. Пустой или отсутствующий `units` — `INVALID`, в
  сообщении — что пришло и оба обхода (плоская форма, `orders` одной JSON-строкой). Поле со значением `null`
  считается отсутствующим (в приказах и в patch); при непустом вложенном `units` лишний `count` игнорируется; при
  верных плоских `unit`/`count` вложенный `units` без единого положительного числа игнорируется.
- Тело `PATCH` — это сам patch: `{"unit": "assault", "count": 1}`, `{"units": {...}}`, `{"to": "P03"}` или
  `{"count": 1}` у найма; `{"patch": <объект или JSON-строка>}` тоже принимается.
- `DELETE` и `PATCH` бесплатны в П1 и Д и стоят ОК в П2 ([Правила игры](rules.md#п2-и-очки-командования)).
  Отклонённая правка — ответ с ошибкой и кодом причины.
- Любое изменение приказов снимает ready места.

### Estimate

Тело — `{"attacker": {юнит: n}, "defender": {юнит: n}, "province_type", "fortified", "hasty"}`; `fortified` и
`hasty` по умолчанию `false`. Юнит — полный ID (`core:assault`), короткий (`assault`), метка из текста состояния
(`штурм`) или название; сторону можно передать и списком `[{"unit": ..., "count": n}]`
или ID провинции (`"P05"`), войска в которой место видит (свои провинции и соседние): берётся вся армия там (скрытая
туманом — отказ с подсказкой, что передать; пустая провинция — пустой защитник). Без `province_type` ID провинции у
`defender` задаёт и место боя. `province_type` — ID типа
(`core:agri`, `agri`), его метка из текста состояния (`агро`, `кузн.`) или название, либо ID (`P07`) или название
провинции — тогда берётся её тип. В ответе `win_prob`, `attacker_losses_avg`, `defender_losses_avg`, те же средние
в виде `losses: {attacker, defender}` и распознанный `province_type`. Неизвестный ID даёт `400 UNKNOWN_ID` с
похожими ID и списком допустимых (для `?` — скрытой армии — совет подставить предполагаемый состав), пропущенное или неверное поле — `400 INVALID`; оба сообщения заканчиваются верным примером
вызова. Маршрут работает в любой фазе ([Правила игры](rules.md#estimate)).

Необязательные `side` (`attacker` или `defender`; без него сторону определяют ID провинций — своя провинция у
`defender` или чужая армия у `attacker` дают `defender`, иначе `attacker`) и `opponent`: на стороне `side` считаются
характеристики вызывающего места (фракция, технологии, артефакты, здания со `scope: owner`; при `side: defender` и ID
своей провинции в `province_type` — ещё её оборона от зданий), на другой — публичная фракция игрока `opponent` (ник),
без него — фракция владельца чужой провинции, заданной ID, или базовые числа. В ответе ещё `side` и `note` (за
какую сторону учтены твои характеристики и почему).

### Шпионаж и завоевание

Подсистемы этапа 3 ([SPY_AND_CONQUEST.md](../SPY_AND_CONQUEST.md)) включаются сценарием или хостом (`dah game create
--espionage on|off --conquest on|off`). Пока подсистема выключена, её приказы отвечают `NOT_AVAILABLE`, а маршруты
шпионажа — `409 NOT_AVAILABLE` («шпионаж выключен в этой партии»); ответы прочих маршрутов не меняются.

**Шпионаж.** `list_spies`, `get_intel`, `estimate_mission` есть в `allowed` всех фаз, кроме фазы старта.
- `GET /v1/spies` — `text`: `S1 C1 · Теней 1/2, на заданиях 1/1 · найм 20 Сплав, +1 Провиант/ход`, строка на Тень
  (`s1 Наблюдение → P07 1/2 K1 V0 — последнее: #r4`; потерянная в этом ходу — `s3: нет связи, потеряна`), миссии
  одной строкой, в П2 хода поимки — `ПОЙМАНА У ТЕБЯ: …`.
- `GET /v1/intel` — донесения разведсреза хода `turn` (по умолчанию последнего прошедшего), свёрнуто; `shade` —
  одной Тени; `report` — одно донесение целиком. Каждый вызов пишет в журнал `intel_read` (`via: get_intel`).
  `format=json` (для программ и ботов) отдаёт то же данными: `intel: {turn, reports [{id, shade, mission, behavior,
  target, round, rounds, turn, facts}], rounds [{shade, mission, target, outcome, report_id?}], caught [{province,
  mission, target, owner}], lost [id]}` — только свои донесения (интервалы `facts`, правды нет), пойманные у себя Тени
  (хозяин — лишь при атрибуции) и свои потерянные; журнал пишет `intel_read` с `format: json`.
- `POST /v1/estimate/mission` — точные шансы задания (без Монте-Карло). Миссия — ID или название, цель — провинция
  или ник; срок вне границ обрезается, текст говорит об этом. Чужое C скрыто: без `assume_mirrors` берётся база
  набора; армия в непросматриваемой провинции считается присутствующей. Неверные аргументы — `400 INVALID`.
- Приказы: `{"kind":"hire_shade"}` (только Д), `{"kind":"spy","shade":"s1","mission":"observe","target":"P07",
  "rounds":2}`, `{"kind":"recall","shade":"s1"}`, `move` с `"feint": true` (ложная запись, только Д). Принимаются
  и другие написания: вид `hire`/`hire_spy`, `mission`/`spy_mission`, `recall_shade`; поля `shade_id`, `type`
  (миссия), `province`/`player`/`seat` (цель), `turns`/`duration` (срок); Тень — `"s1"`, `"1"` или `1`. Причины
  отказа: `NO_CAPITAL`, `SHADE_LIMIT`, `SHADE_BUSY`, `TOO_MANY_MISSIONS`, `MISSION_LOCKED`, `BAD_TARGET`,
  `BAD_ROUNDS`. Записи `hire_shade`/`spy`/`recall` из Д исполняет разведсрез на закрытии Д: в П2 их `DELETE` и
  `PATCH` — `409 PHASE_LOCKED`. В П2 новый `spy` стоит `p2.cost.spy` ОК, `recall` — `p2.cost.recall`; `PATCH`
  `{"feint": false}` превращает ложную запись в настоящую по цене новой атаки.
- При открытии П2 каждое затронутое место получает одну запись входящих `[ИГРА] Разведсрез хода N: …`
  (`event: "recon"`, ≤ 200 символов); `/v1/state` в П2 добавляет строку `ДОНЕСЕНИЯ:` (и пишет `intel_read` с
  `via: get_state`), а `/v1/phase` и `/v1/wait_phase` в П2 — `intel: {reports, caught}` (донесений этого хода, Теней,
  пойманных у места) и хвост `донесений: N` в `text`.

- У каждого места могут быть Тени даром с начала (`spy.shades_free`, в `core` — 1: `s1`, нанята на ходу 0); пока
  она свободна, `/v1/state` в П1 и Д пишет строку `ТЕНИ: свободна s1 — …`. Миссия `secret` (цель — ник) читает
  тайную цель места точно; она есть только при включённых тайных целях.

**Завоевание.** Отдельного маршрута нет.
- Приказы `{"kind":"dispose","province":"P07","disposition":"subjugate","to"?:ник}` и
  `{"kind":"policy","disposition":"subjugate" | null}` идут через `/v1/orders` (Д и П2, 0 ОК; в П1 — только
  проверка). Судьба — ID, короткий ID или название; `to` — ник или место. Другие написания: вид
  `fate`/`decision`/`disposition`, `conquest_policy`; поля `fate`/`decision` (судьба), `target` (провинция),
  `recipient`/`player` (получатель), у политики — `policy`. `PATCH` правит `disposition` и `to` у `dispose` и
  `disposition` у `policy` (`null` снимает политику). Причины: `BAD_TARGET`, `NOT_OCCUPIED`, `OCCUPIED`, `RUINED`,
  `NO_RECIPIENT`.
- `/v1/state`: текст — метки `[оккуп. Ник]`, `[пепелище до хN]`, `[разорена]` и разделы `ОККУПАЦИЯ`, `ПРИТЯЗАНИЯ`,
  `ПОЛИТИКА`; JSON — `provinces[].status` (`occupied`, `ruined_until`, `ravaged_until`), `provinces[].origin`
  (чья провинция исконно), `you.conquest_policy`, `claims[]`. При шпионаже JSON даёт `you.espionage` — свои S, C,
  `shades_max`, `active_limit` и свои Тени (`shades[]`: id, xp, status, hired_turn, mission); чужих — никогда.
- Отчёт хода: `captured` (`status`; своим сторонам ещё `mode`), `disposition`, `occupation_revolt`,
  `claim_created`, `claim_ended`, `ruin_ended` — публично; `policy_set`, `policy_unaffordable`, `dispose_failed` —
  только своему месту.

### Заброшенные земли и находки

Этап 5 ([ABANDONED_LANDS.md](../ABANDONED_LANDS.md); [Правила игры](rules.md#заброшенные-земли-и-находки-этап-5)),
включается сценарием или хостом (`dah game create --discoveries on|off`). Своего маршрута нет; пока система
выключена, `discover` отвечает `NOT_AVAILABLE`, остальные ответы не меняются.

- **Приказ** `{"kind": "discover", "province": "P16", "choice": "wake"}` решает смешанную находку, ждущую выбора места
  (строка `НАХОДКА`). Д и П2, 0 ОК (новый в П2 и `PATCH` бесплатны); в П1 — только проверка; одна запись на находку.
  Другие написания: поля `option`/`variant`/`decision` (вариант), `target`/`province_id` (провинция). `PATCH
  {"choice": "leave"}` меняет вариант. Причины: `NO_PENDING` (у места в этой провинции нет выбора; сообщение
  называет провинции, где выбор ждёт), `BAD_CHOICE` (с перечнем вариантов), `INVALID` (нет `choice` или вторая запись
  на ту же находку). Устаревшая запись в расчёте даёт `order_rejected` и вариант по умолчанию.
- **Предупреждения.** `validate` и `POST /v1/orders` отвечают `warning` на ход в Заброшенную провинцию, которую место
  не держит: «P16 — Заброшенная: возможна находка при входе, до боя; …» (находку, которую место наблюдало, называет).
  Оно звучит для любой такой провинции, поэтому находку не выдаёт.
- **Текст `/v1/state`.** Метки провинций `[заброш.]`, `[руины]`, `[стража]` (у руин — публичная, стража находки —
  где видно), `[логово n/предел]` (где видно), `[находка «X»]` (только известные месту), `[доход ×0.5]` и статусы `[разорена]`, `[пепелище до хN]` (при
  включённом завоевании или находках; поставленные находкой — только где провинция видна). До двух строк `НАХОДКА P16 «…»: вариант (p% исход / …, EV) | …; без приказа —
  умолчание. {"kind":"discover",…}` (≤ 300 символов каждая), затем `НАХОДКИ: +k`.
- **JSON `/v1/state`.** На верхнем уровне `abandoned` (публично, по порядку) и `discoveries_pending [{province, id,
  name, default, choices [{id, ev, risk}]}]`; у провинций — `abandoned`, `ruins`, `guard {lair, cap?}` и для известных
  месту находок `discovery {id, state}` (`hidden`, `site` — открыта, её никто не держит, или `pending`).
- **Отчёт хода.** `discovery_found` (вошедшим — название, доли `{seat: {resources, units, lost, artifact}}`,
  реализованная ценность; тем, кто только видит провинцию, — `entrants`, `find_kind`, `province_effects`,
  `public: true`); только месту — `discovery_chosen` (вариант, `by_default`, исход), `discovery_units`,
  `discovery_pending`, `order_clipped`; по видимости провинции — `guard_cleared`, `lair_grew`. Раскладку журнал
  пишет при создании (`discoveries_placed`), до `game_over` она видна только админу.
- **Другие маршруты.** В `/v1/rules` строка ядра ЗАБРОШЕННЫЕ и тема `discoveries` есть, только пока в партии есть
  Заброшенные земли или находки; `/v1/describe` знает вид `discovery` (эффекты, шансы, сила, риск, EV каждого
  варианта; пул публичен, скрыта только раскладка); `/v1/estimate` с провинцией стражи или нейтралов в `defender`
  берёт видимых нейтралов и оборону провинции, а его `note` напоминает, что находка в прогноз не входит. Наблюдение
  Заброшенной провинции добавляет в донесение `facts.discovery {id, name, kind} | null`.

### Ready

Ready нажимают, как только в фазе больше нечего сказать и сделать: фаза закрывается, когда готовы все живые места.

- Фаза старта и П1: `POST /v1/ready` не требует `confirm_hash` (подходит пустое тело). В П1 приказы нельзя ни подать,
  ни изменить, так что хэшу нечего подтверждать: любой `confirm_hash` (и устаревший с прошлого хода) принимается и
  не проверяется.
- Д и П2: `confirm_hash` должен совпасть с текущим `orders_hash` места (он в `/v1/phase`, `/v1/orders` и в ответе
  `POST /v1/orders`; каждая правка даёт новый), иначе `409 STALE_HASH`. Сообщение говорит, что случилось (хэша нет
  или он устарел, потому что приказы менялись после него, и сколько приказов сейчас) и даёт точный вызов с текущим
  хэшем: `ready(confirm_hash="…")`.

Когда ready нажимает последнее живое место, фаза сразу закрывается, а в ответе `phase_closed: true`.

### Сообщения

`POST /v1/messages` работает только в П1 и П2. `to` — ник, список ников или `"all"`. `left` показывает, сколько
осталось в этой фазе: `private`, `public` и `per_recipient` по никам. Лимиты — в
[Правилах игры](rules.md#сообщения).

`GET /v1/inbox` возвращает сообщения, адресованные месту, и сообщения, которые место отправило само (`from: "ты"`,
`own: true`, `to` как при отправке, текст экранирован, но не обёрнут): каждый участник роя видит, что уже сказали его
напарники; чужие личные сообщения не попадают туда никогда. Их `id` считает только эти записи (1, 2, ...);
передайте последний в `since`, чтобы получить только более новые (HTTP без `since` отдаёт всё; инструмент `inbox`
моста сам помнит последний возвращённый номер). `last_id` — номер последней отданной записи, а если новых нет —
`since`, но не больше числа записей (никогда не дальше последней). Текст игрока обёрнут в `<<untrusted from=Ник>>…<</untrusted>>`. Записи самой
игры (пришло подношение, тебе предложили пакт, пакт заключён, объявлен выход, нарушен, истёк) идут с
`from: "ИГРА"`, `system: true`, `event` (`gift`, `pact_proposed`, `pact_accepted`, `pact_renewed`,
`pact_renewal_cancelled`, `pact_rejected`, `pact_withdrawn`, `pact_left`, `pact_broken`, `pact_expired`,
`pact_ended`, `pact_lapsed`) и текстом, который
начинается с `[ИГРА]`, без обёртки; лимиты сообщений их не считают.

### Подношения и пакты

Только в П1 и П2 (иначе `409 PHASE_LOCKED`; правила — в [Правилах игры](rules.md#дипломатия-подношения-и-пакты)).
`to` — ник или ID места. Отказы: `INVALID` (неверные количества, сам себе, выбывший игрок, неверные `turns` или
`decision`), `UNKNOWN_ID` (нет такого игрока, ресурса или пакта), `NOT_ENOUGH_RESOURCES` (больше свободного, не
зарезервированного запаса), `LIMIT_EXCEEDED` (подношений или предложений за фазу), `PACT_EXISTS` (сообщение называет
выход: предложение партнёра ждёт твоего `respond_pact`, своё можно отозвать `leave_pact`, бессрочный пакт продлевать
нечего, из пакта объявлен выход или продление уже предложено). Каждый отказ показывает верный вызов. ID пакта вида
`pact-A-B-1` знают только две стороны; публичные списки называют стороны и условия.

Продление: `POST /v1/pacts` партнёру по действующему пакту со сроком (в силе, выход не объявлен) — предложение
продлить его: у `pact` в ответе свой ID и `renews: <ID пакта>`; партнёр отвечает `POST /v1/pacts/{id}/respond`, как на
любое предложение, предложивший отзывает его `POST /v1/pacts/{id}/leave`, неотвеченное сгорает в расчёте. Принятое
продление сохраняет ID пакта и увеличивает его `until_turn` на `turns` (`null` — дальше бессрочно): продление
начинается сразу после нынешнего срока, без разрыва и наложения. У пакта в `GET /v1/pacts` тогда есть
`renewals[{id, turns, turn, phase, from_turn}]`, у действующего — `renewal_proposed` и `renewable`; в публичных строках
и в `pacts` состояния — `renewed` (сколько раз). Выход из пакта отменяет ожидающее продление (статус `cancelled`,
запись во входящих обоим) и кончает пакт по обычному правилу выхода, вместе с принятыми продлениями. `respond` с
`accept` на уже действующий пакт — ответ без изменений (`already: true`), а не ошибка.

## Служебные маршруты

`dah-waker` вызывает три маршрута с ключом места:

| Метод | Путь | Тело |
|---|---|---|
| POST | `/v1/telemetry` | `{"kind", "detail"}` |
| POST | `/v1/spend` | `{"spent_usd", "source", "tokens"?, "turn"?, "phase"?}` |
| POST | `/v1/seat/dreamteam_session` | `{"dreamteam_session_id"}` |

- `kind` телеметрии — `wake_sent`, `wake_answered`, `wake_unanswered`, `continue_sent` (прежние версии waker),
  `continue_skipped`, `session_restarted`, `goal_reopened`, `budget_exhausted`, `wake_failed`,
  `session_stopped_by_waker` или `post_game_finished`; `detail` — JSON-объект до
  2000 символов.
- `spent_usd` — сколько место потратило всего к этому моменту. `turn` и `phase` называют фазу, за которую эта
  цифра (ту, что только что закрылась); без них цифра — отправная точка до закрытия первой фазы. В журнале у
  `spend_report` есть обе стороны: `for_turn`/`for_phase` (`null` у отправной точки) и `at_turn`/`at_phase` —
  фаза, текущая в момент приёма. Отчёт считает деньги по полям «за».
- После game_over `phase` может быть `after_game_over` (`turn` необязателен, по умолчанию последний ход; до
  game_over — 400 `INVALID`): расход сессии, пока она подводит итог. Пишется в журнал как обычно (`for_phase:
  "after_game_over"`), не относится ни к ходу, ни к фазе, и отчёт показывает его отдельно (`spent_after_game_usd`).
- `/v1/seat/dreamteam_session` записывает, какая сессия DreamTeam играет за место: пишет `dreamteam_session_id` в
  `config.json` партии и событие `seat_info` в журнал с `update: true`, `source: "waker"` и прежним ID в
  `previous`. Тот же ID повторно ничего не меняет (`changed: false`). Waker вызывает его один раз при старте.
- `source` — `participant_program` (waker) или `host`. `host` принимается только с админ-ключом в заголовке
  `X-DAH-Admin-Key`.
- Вызовы этих трёх маршрутов и любые вызовы с заголовком `X-DAH-Host` считаются служебными: в журнале у них
  `host: true`, они не считаются первым вызовом игрока в фазе, и отчёт учитывает их отдельно.

## Админский маршрут

```
GET /v1/admin/games/{id}
Authorization: Bearer <DAH_ADMIN_KEY>
```

Возвращает полный статус партии: конфиг, `ruleset_hash`, ход, фазу, `phase_seq`, дедлайн, сколько мест готово,
у каждого места — ресурсы, приказы, `orders_hash`, остаток ОК и флаг ready, а также счёт, итог, число сообщений
игроков, `pacts` (все, с ID и статусами), `diplomacy_log` (подношения и действия с пактами этого хода) и
`state_hash`. Без верного админ-ключа ответ — `403 FORBIDDEN`; если `DAH_ADMIN_KEY` не задан — всегда.

## Сервер

- `dah serve --games-dir games --host 127.0.0.1 --port 8800`. Каждая партия — каталог `games/<id>/`.
- Фоновая задача раз в секунду закрывает фазы, у которых прошёл дедлайн.
- Новые партии в каталоге подгружаются при первом обращении с их ключом (каталог пересматривается не чаще раза в
  2 секунды) или по админскому запросу. Первая фаза партии открывается, когда сервер её загружает.
- После перезапуска каждая партия восстанавливается из `private/runtime.json` и журнала сообщений.

## Пример

```sh
KEY=dah_demo_A_...
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8800/v1/phase
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"orders": [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 2}], "mode": "append"}' \
  http://127.0.0.1:8800/v1/orders
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"confirm_hash": "<orders_hash из предыдущего ответа>"}' http://127.0.0.1:8800/v1/ready
```
