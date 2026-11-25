# Паки контента

[English](../en/modding.md) · **Русский** · [Вся документация](README.md)

Весь контент игры лежит в паках: ресурсы, типы провинций, юниты, здания, фракции, карты, сценарии и, с этапа 2,
ветки технологий, технологии и артефакты. Движок ничего
не знает о конкретных юнитах и ресурсах — только о **ролях** ресурсов и адресуемых **характеристиках**. Базовый пак —
`content/packs/core/`; источник этих правил — раздел 2 [спецификации среза](../SLICE.md).

## Устройство пака

Пак — это каталог с `pack.yaml` и YAML-файлами, разложенными по типам сущностей:

```
mymod/
  pack.yaml
  resources/  province_types/  units/  buildings/  factions/  maps/  scenarios/
  branches/  techs/  artifacts/  lore/      # этап 2 (необязательно)
  objectives/                             # этап 4 (необязательно)
  discoveries/                            # этап 5 (необязательно)
```

- Читаются все файлы `*.yaml` в каталоге типа. В файле лежит одна сущность или список сущностей.
- Сущности обрабатываются в порядке полных ID, а не файлов.
- Остальные каталоги и файлы не читаются. README упоминает необязательный `locale/ru.yaml`, но загрузчик этой
  версии его не читает.

`pack.yaml`:

```yaml
id: mymod
version: 0.1.0          # semver: X.Y.Z
title: Мой модуль       # до 48 символов
author: я               # необязательно
requires: [core]        # паки, которые должны быть загружены вместе с этим
engine_api: "^1"        # версия API движка — 1
```

## ID

- ID внутри пака короткие: строчные латинские буквы, цифры и `_`, до 32 символов (`sniper`).
- После загрузки у сущности полный ID `пак:id` (`mymod:sniper`).
- На сущности своего пака можно ссылаться коротким ID, на сущности чужого — только полным (`core:alloy`).
- У провинций на карте свои ID: заглавная латинская буква и до 7 заглавных букв или цифр после неё (`P01`).

## Поля сущностей

Поля со звёздочкой обязательны. `name` — до 48 символов, `short` (сокращение для текста состояния) — до 12.

| Тип | Поля |
|---|---|
| ресурс | `id`\*, `name`\*, `short`, `role`\* (`build`, `upkeep_food` или `research`) |
| тип провинции | `id`\*, `name`\*, `short`, `income`\* (`{ресурс: n}`), `defense_mul` (больше 0, до 5) |
| юнит | `id`\*, `name`\*, `short`, `attack`\*, `defense`\*, `cost`\*, `upkeep`\*, `counters` (список юнитов) |
| здание | `id`\*, `name`\*, `short`, `cost`\*, `build_turns`\* (1..10), `effects`, `recruit`, `max_per_province` |
| фракция | `id`\*, `name`\*, `lore` (до 1000 символов), `bonuses`, `penalties`, `starting`\* |
| карта | `id`\*, `name`\*, `max_players`\* (1..16), `provinces`\*, `edges`\*, `starts`\* |
| сценарий | `id`\*, `name`\*, `map`\*, `factions`\*, `turn_limit`\*, `phase_limits`\*, `message_limits`, `diplomacy_limits`, `overrides` |

Подробнее:

- `turn_limit` сценария — от 1 до 200.
- `defense_mul` типа провинции по умолчанию 1.
- `recruit: true` разрешает найм в здании. `max_per_province` может быть только 1.
- `starting` фракции: `resources` (`{ресурс: n}`), `units` (`{юнит: n}`, ставятся в столицу), `buildings` (список,
  стоят в столице), `effects` (стартовые эффекты, см. ниже).
- `provinces` карты — `{id, name, type}`; `edges` — пары `[P01, P02]`; `starts` — `{capital, provinces}`. Число
  стартов должно равняться `max_players`.
- `phase_limits` сценария — `negotiation_1`, `actions` и `negotiation_2`, каждый в секундах или как `20m`, `5s`,
  `1h`.
- В `message_limits` можно задать `negotiation_1` и `negotiation_2` (в каждом `per_recipient`, `private`,
  `public`) и `max_chars` (1..4000). Всё, что не задано, берётся по умолчанию: в П1 6 одному адресату, 20 личных,
  2 публичных; в П2 10 одному адресату, 10 личных, 1 публичное; 800 символов.
- В `diplomacy_limits` можно задать `gifts_per_phase` и `pact_proposals_per_phase` (0..50 на место и фазу
  переговоров; по умолчанию 3 и 3).
- Лимиты фаз и дипломатии — настройки партии: их записывает конфиг каждой партии, и в хэш набора правил они не входят
  (новое значение по умолчанию не заставит `dah replay` старой партии сообщать об изменённом контенте).
- Без фазы старта (`setup: none`) место получает фракцию из своего конфига, иначе первую из `factions`
  сценария. С фазой старта места выбирают сами (ниже, «Контент этапа 2»).

## Эффекты

Типов эффектов двенадцать (шесть ниже, `unlock` и `grant_tech` из «Контента этапа 2» и `grant_artifact`,
`lose_units`, `spawn_neutral`, `set_status` находок из «Контента этапа 5»), и каждый разрешён
только в определённых местах.

| Эффект | Параметры | Где |
|---|---|---|
| `modify` | `target`, `op`, `value` | `bonuses`/`penalties` фракции, `overrides` сценария |
| `income` | `resource`, `amount` | `effects` здания, `bonuses`/`penalties` фракции |
| `add_resource` | `resource`, `amount` | `starting.effects` фракции |
| `grant_units` | `unit`, `count`, `where: capital` | `starting.effects` фракции |
| `grant_building` | `building`, `where: capital` | `starting.effects` фракции |
| `set_flag` | `flag` | `starting.effects` фракции |

- У `modify` в `target` — путь характеристики (см. [Характеристики](#характеристики)), `op` — `add`, `mul`
  или `set`.
- `income` — постоянная прибавка к доходу: с каждого здания этого вида или от фракции целиком.
- Бонусы и штрафы фракции действуют всегда (хук `passive`), стартовые эффекты — один раз в начале партии
  (`on_game_start`).
- `set_flag` только запоминает флаг у игрока; ни одно правило этой версии его не читает.

## Характеристики

`modify` меняет характеристику. В `target` ID юнитов, зданий и ресурсов можно писать коротко:
`unit.assault.attack`.

| Путь | Тип, границы | По умолчанию | Смысл |
|---|---|---|---|
| `resource.<id>.income_mul` | float 0.5..2.0 | 1.0 | множитель дохода ресурса |
| `unit.<id>.attack`, `unit.<id>.defense` | int 0..20 | из контента | сила юнита |
| `unit.<id>.cost.<res>` | int 0..200 | из контента | цена найма |
| `unit.<id>.upkeep.<res>` | int 0..20 | из контента | содержание за ход |
| `building.<id>.cost.<res>` | int 0..300 | из контента | цена здания |
| `p2.hasty_penalty` | float 0..0.5 | 0.15 | штраф силы движений, начатых в П2 |
| `p2.cost.new_attack` | int 1..5 | 2 | цена нового движения или смены цели в П2, в ОК |
| `p2.cancel_refund` | float 0..1 | 0.75 | доля резерва, которая возвращается при отмене в П2 |
| `command_points` | int 0..10 | 3 | ОК за ход |
| `combat.fortify_bonus` | float 0..1 | 0.25 | бонус обороны приказа `fortify` (сценарии `core` ставят 0.15) |
| `combat.counter_mul` | float 1..3 | 1.5 | множитель контрюнита |
| `combat.fortify_persists` | int 0..1 | 0 | 1 — укрепление держится, пока в провинции войска владельца (сценарии `core` 0.5.0 ставят 1) |
| `starvation.loss_frac` | float 0..1 | 0.10 | доля армии, теряемая при голоде |

### Порядок модификаторов

Модификаторы одной характеристики применяются так:

1. базовое значение (из контента или по умолчанию движка);
2. все `add`;
3. все `mul`;
4. `set` — побеждает последний в порядке сборки (сначала правки сценария, затем эффекты фракции);
5. clamp по границам характеристики.

Значение вне границ прямо в файле (например, `attack: 99`) — ошибка. Если значение вышло за границы только после
модификаторов, оно обрезается по границам, а валидатор выдаёт предупреждение.

## Контент этапа 2: технологии, артефакты, способности, фаза старта

Точное описание — разделы 1–2 и 6 [спецификации этапа 2](../TECH_AND_SETUP.md); `content/packs/core` использует
все поля ниже. Набор паков без технологий, веток и артефактов сохраняет правила среза: хэш как до этапа 2, а
`unit.<id>.move` в нём по умолчанию 1 (иначе 2).

Название — строка или `{ru, en}` (игрокам показывается ru). `lore` — текст или путь к Markdown-файлу пака
(`lore/litany.md`); `description` (до 1000 символов) — короткий текст для `describe`. Тексты (`lore`, `description`,
`name_en`, `color` ветки) в хэш правил не входят. Вид эффекта пишется `type:` или `effect:`.

| Тип | Каталог | Поля |
|---|---|---|
| ветка | `branches/` | `id`\*, `name`\*, `order`, `color`, `description` |
| технология | `techs/` | `id`\*, `name`\*, `branch`\*, `tier`\* (1..10), `cost`\* (в ресурсе роли `research`), `requires` (все), `requires_any` (хоть одна), `requires_tier {tier, count, same_branch=true}`, `factions`, `effects`, `unlocks` (справочно), `description`, `lore` |
| артефакт | `artifacts/` | `id`\*, `name`\*, `power`\* (вес для честного драфта), `cost` (режим очков, 0..10), `factions`, `exclusive_with`, `effects`\*, `description`, `lore` |

Новые поля старых типов:

- ресурс: `role` может быть и `research` (не больше одного ресурса; тратится только на технологии, валидатор
  предупреждает о юните или здании с ценой в нём). Без него технологий в партии нет.
- юнит: `move` (1..5, рёбер по своей земле), `requires {tech: [...], building: [...]}` (здание — в провинции найма),
  `factions` (уникальный юнит), `abilities` (`[siege]` или `[{id: siege, cap: 1.0}]`). Способности: `siege {cap}`,
  `ignore_fortify`, `garrison {defense}`, `fast {range}`.
- здание: `requires {tech: [...]}`, `factions`; в `effects` можно `modify` со `scope: province` (характеристики самой
  провинции) или `scope: owner` (характеристики владельца, пока здание стоит).
- фракция: `description`; `bonuses`/`penalties` могут менять дальность хода, способности и цены технологий.
- сценарий: `setup: none` или `{unique_factions: true, artifacts: draft|points|none, artifact_offer: 3,
  artifact_points: 3, artifact_pool: [...]}` (показаны умолчания) и `phase_limits.setup` (по умолчанию 5m). С фазой
  старта места выбирают фракции по предпочтению; невыбравшее получает фракцию из своего конфига, иначе первую
  свободную из списка сценария ([Правила игры](rules.md#фаза-старта)). Хост меняет режим флагом
  `dah game create --artifacts ...`.

Эффекты этапа 2:

| Эффект | Параметры | Где |
|---|---|---|
| `unlock` | `kind` (`unit`, `building`, `order`), `id` | `effects` технологии, `effects` артефакта |
| `grant_tech` | `tech` | `starting.effects` фракции, `effects` артефакта |

Контексты: эффекты технологии действуют как пассивные эффекты владельца (`modify`, `income`, `unlock`, `set_flag`);
артефакта — на всю партию (`modify`, `income`, `unlock`) плюс разовые на старте (`add_resource`, `grant_units`,
`grant_building`, `set_flag`, `grant_tech`); у здания — `income` и `modify` со `scope`.

Характеристики этапа 2:

| Путь | Тип, границы | По умолчанию | Смысл |
|---|---|---|---|
| `unit.<id>.move` | int 1..5 | 2 (1 без контента этапа 2) | рёбер по своей земле |
| `unit.<id>.ability.<имя>.on` | int 0..1 | 0 (1, если у юнита есть) | флаг способности |
| `unit.<id>.ability.siege.cap` | float 0.5..3 | 1.0 | потолок множителя обороны провинции против осады |
| `unit.<id>.ability.garrison.defense` | int 0..20 | 1 | прибавка к обороне дома |
| `unit.<id>.ability.fast.range` | int 1..5 | 2 | рёбер по любой земле |
| `tech.<id>.cost` | int 0..1000 | из контента | цена технологии |
| `tech.branch.<id>.cost_mul` | float 0.1..5 | 1.0 | множитель цены ветки |
| `province.defense_mul` | float 0.1..5 | 1.0 | доп. множитель обороны провинции (здания со scope province) |
| `province.income_mul.<рес>` | float 0..5 | 1.0 | множитель дохода провинции |
| `province.recruit_discount` | float 0..0.9 | 0 | скидка найма, на единицу, округление в пользу игрока |

## Контент этапа 3: шпионаж и завоевание

Точное описание — разделы 1, 2.2, 3.3 и 5 [спецификации этапа 3](../SPY_AND_CONQUEST.md); `content/packs/core` 0.4.0
использует все поля ниже. Всё необязательно: пак без них или сценарий с выключенными системами хэшируется и
играется как до этапа 3.

- **Выключатели.** У сценария `systems: {espionage: bool, conquest: bool}` (по умолчанию оба выключены); хост может
  переключить их для партии (`dah game create --espionage/--conquest`). Пока система выключена, сущности с
  `requires_system: espionage|conquest` (ветки, технологии, здания, артефакты) убираются из набора правил (скрытая
  ветка скрывает свои технологии), а строки `modify` по её характеристикам — из технологий, артефактов, зданий и
  фракций.
- **`espionage/*.yaml`** (нужен при включённом шпионаже): необязательный `config` — `shade {name, cost, upkeep}` и
  `ward {cost}` (3b), `shade.free` (0..3: свободные Тени каждого места с начала партии, в пределах лимита; `core`
  0.6.0 задаёт 1) — и `missions: [...]`. У миссии: `id`, `name`, `behavior` (`observe_province`, `read_ledger`,
  `read_objective`, `read_orders`, `sabotage`), `target` (`province`, `owned_province`, `seat`), `difficulty`,
  `rounds: [min, max]`, `requires: {whisper, tech?}`, `cost` (разово), `cost_per_round`, `params` (у саботажа:
  `deny_fortify`, `defense_mul`, `income_mul`), `requires_system` (например, `scoring.objectives` у
  `read_objective`: без системы миссии нет), `description`.
- **`dispositions/*.yaml`** (нужен при включённом завоевании): судьбы захваченной провинции — `id`, `name`,
  `behavior` (`annex`, `raze` с `params.ruin_turns`, `raid` с `params.ravage_turns`, `release`), `cost`, `loot`,
  `description`.
- **`conquest_value`** (1..5, по умолчанию 1) у типа провинции умножает цену и добычу судеб (у столицы в `core` — 3).
- **Характеристики** (только при включённой системе): `spy.*` (`spy.whisper`, `spy.mirrors`, `spy.shades_max`,
  `spy.rounds_bonus`, `spy.detect.army`, `spy.attribution.base`, `spy.mission.<id>.difficulty|max_rounds`,
  `spy.shade.cost|upkeep.<res>`, `spy.shades_free` и др.), `province.mirrors`, `p2.cost.spy`, `p2.cost.recall`,
  `feint.cp_cost`,
  `disposition.<id>.cost_mul|loot_mul`, `conquest.claim_turns`, `conquest.claim_bonus`, `conquest.loot_cooldown`.
  Полная таблица с диапазонами — раздел 1.2 спецификации; числа партии — `rules(topic="spying")` и
  `rules(topic="conquest")`.

## Контент этапа 4: источники очков, великие стройки, тайные цели

Точное описание — разделы 1–3 и 11 [спецификации счёта](../SCORING.md); `content/packs/core` 0.5.0 использует все
поля ниже. Всё необязательно: сценарий без `scoring` считает только землю (1 за провинцию, +3 за столицу) и
хэшируется как раньше.

- **Веса.** `scoring: {territory, science, works, war, objectives}` в сценарии (каждый 0..5; не названный — 0; нужен
  хотя бы один источник кроме `objectives` с весом > 0). Хост переопределяет для партии: `dah game create --scoring
  territory=1,science=1,works=0,war=1,objectives=1 --objectives N`. Источник с весом 0 — выключенная система
  `scoring.<src>`: сущности с `requires_system: scoring.<src>` скрыты, строки `modify` по его характеристикам
  `score.*` отбрасываются.
- **Здания** получают `score` (0..10: очки, пока готово в своей полноценной провинции; характеристика
  `building.<id>.score`), `max_per_owner` (1..10: приказ `build` отвергается с `ALREADY_BUILT`, когда у места уже
  столько таких, готовых и строящихся, во всех его провинциях) и `allowed_types` (типы провинций, где можно строить).
  Здание со `score > 0` — великая стройка: её начало и готовность — публичные события; валидатор предупреждает, если
  у неё `build_turns` < 2.
- **`objectives/*.yaml`**: `id`, `name`, `kind` (виды движка: `hold_type {type, count}`, `hold_count {count}`,
  `hold_foreign {count}`, `capital_other {count}`, `techs_branch {branch, count}`, `great_work {count}`,
  `rival_capital_taken` — против соперника), `params`, `points` (1..8), `difficulty` (необязательно: `easy`,
  `medium`, `hard`; очки должны расти с уровнем, пропуск уровня рядом с заданными — предупреждение), `maps` (пусто —
  любая), `players {min, max}`, `requires_system`, `description`, `lore`. Сценарий с целями задаёт `objectives: {dealt: 1}` (по желанию
  `pool: [...]`; `offer` должен равняться `dealt`).
- **Проверка целей** (каждый сценарий с целями, каждое число мест 2..`max_players` его карты, каждая допустимая
  фракция): пул после фильтров `maps`/`players`/`requires_system` — не меньше max(3, `dealt`); ни одна цель не
  выполнена ни на одном старте (после стартовых эффектов); каждая выполнима (хватает провинций типа, технологий ветки,
  изучаемых каждой фракцией, есть великая стройка, которую каждая фракция может строить на карте, и т. д.).
- **Характеристики** (только пока источник включён): `score.territory.province|capital`, `score.science.tier1..4`,
  `score.science.branch`, `score.war.per_point|cap_per_victim|cap|defense_pct`, `score.betrayal.per_break` (0..10,
  очков минус за каждый разорванный местом пакт; действует при любом счёте не по умолчанию). Числа партии — в
  `rules(topic="victory")`.

## Контент этапа 5: Заброшенные земли и находки

Точное описание — разделы 2–5, 8, 9 и 11 [спецификации этапа 5](../ABANDONED_LANDS.md) с поправками решения 25
[концепции](../CONCEPT.md) и разделом «Реализация: уточнения». Всё необязательно: при выключенной системе (нет
`systems: {discoveries: true}` в сценарии и нет `--discoveries on`) папка `discoveries/`, ручки сценария
`discoveries` и `rotation` карт в набор правил и его хэш не попадают.

- **`discoveries/*.yaml`** — находки:

```yaml
- id: sleeping_golem
  name: {ru: Спящий голем, en: Sleeping Golem}
  kind: mixed                 # boon | hindrance | mixed
  rarity: common              # boon/mixed: common|uncommon|rare; hindrance: minor|major
  weight: 3                   # необязательно, 1..20; по умолчанию — вес редкости
  triggers: [army]            # shade — предупреждение «ignored in this stage»
  players: {min: 2}           # необязательно, как у целей
  requires_system: conquest   # необязательно
  description: {ru: "…", en: "…"}
  choices:                    # только mixed; у boon/hindrance вместо них effects: [...]
    - id: wake
      label: {ru: Разбудить, en: Wake}
      outcomes:               # сумма p — 1
        - {p: 0.7, effects: [{effect: grant_units, unit: walker, count: 2, where: here}]}
        - {p: 0.3, effects: [{effect: lose_units, frac: 0.3, min: 2}]}
    - {id: leave, label: {ru: Не трогать, en: Leave}, effects: []}
  default_choice: leave
```

- **Эффекты находок.** `add_resource {resource, amount}` (плюшка делится по цене армий; отрицательная сумма — помеха,
  которую каждое вошедшее место платит полностью, не ниже 0), `grant_units {unit, count, where: here}` (приходят после
  боёв), `grant_building {building, where: here}`, `grant_artifact {artifact}` (только обычный артефакт, все эффекты
  которого пассивны; достаётся месту с самой дорогой армией; у кого он уже есть — 15 × его силу ресурсом стройки),
  `lose_units {frac, min?}` (каждой армии: самые дешёвые юниты, пока не снята доля `frac` её цены и не меньше `min`
  юнитов; 0..20), `spawn_neutral {units, grow? {every, add, cap}, reward? [...]}` (стража или логово; не внутри
  выбора), `set_status {status: ravaged|ruined, turns}`. `where: here` вне находок отвергается. Награда
  (`spawn_neutral.reward`, `garrison.reward` сценария) принимает только `add_resource > 0` и `grant_units`.
- **Сила.** Автор её не пишет: валидатор считает `power` против эталонной армии (5 юнитов ценой 12; 1 P = 15
  ресурсов = цена юнита 20) и `power_small` против 1 и 2 самых дешёвых общих юнитов пака; у смешанной — EV каждого
  варианта (`power` — лучший EV) и `risk` (лучший исход минус худший). Их показывают `describe`,
  `rules(topic="discoveries")` и `dah pack validate`. **Ошибки:** сила вне диапазона редкости (boon common 1.0–2.5,
  uncommon 2.5–4.0, rare 4.0–6.0; hindrance minor −1.5…−0.5, major −3.0…−1.5; mixed — как boon, common от 0.5),
  boon ≤ 0 или hindrance ≥ 0, сумма p ≠ 1, `default_choice` не среди вариантов, эффект вне своего контекста,
  непассивный или фракционный артефакт. **Предупреждения:** триггер `shade`; рычаг риска (у какого-то варианта EV
  выше, чем у варианта по умолчанию, а вариант по умолчанию — наименее рискованный); **scout sweep pays** —
  средневзвешенная `power_small` пула сценария или одного из его колец ≥ 0 для армии из 1 или 2 юнитов. Находку
  запускает любая армия, поэтому помехи должны быть достаточно серьёзными (высокий `min` у `lose_units`, потеря
  ресурса), чтобы разведка одиночками в среднем была убыточной.
- **Ручки сценария `discoveries`** (все необязательны): `souls` (`ravage` — Разорение 3 хода, `halve` — доход ×0.5
  навсегда, `ashes` — Пепелище 2 хода; `ravaged_turns`, `ruined_turns`, `income_mul` рядом перекрывают пресет),
  `garrison {units, reward}` (стража руин и её награда), `density` (0..1, доля Заброшенных кандидатов с находкой;
  0.67), `extra` (0..20: кандидаты и из обычных нейтральных провинций), `rings` (`{кольцо: [токены редкости]}`,
  кольцо ≥ 2 — кольцо 1 у старта остаётся пустым; токены `common`, `uncommon`, `rare`, `boon`, `minor`, `major`,
  `hindrance`, `mixed`, `mixed_common`, …), `w` (`{кольцо: вес}` бюджета честности), `max_ring` (3),
  `fair_tolerance` (1.0), `tries` (64), `observe_mirrors` (0..5, C Наблюдения Заброшенной земли; 2), `fair_mirror`
  (копировать находки по повороту карты), `ruins_find` (false), `pool` (подмножество ID находок). Неизвестная ручка —
  ошибка.
- **Карты** могут задать `rotation: {провинция: провинция}` — перестановку, сохраняющую рёбра и переводящую старт *i*
  в старт *i+1*; ею пользуется `fair_mirror`. Хэшируется только при включённой системе.

## Проверка

```sh
uv run dah pack validate content/packs/core path/to/mymod   # зависимости надо передать вместе с модом
```

Валидатор проверяет:

1. JSON Schema каждого файла (схемы лежат в `src/dah/engine/schemas/`);
2. ссылки: каждый упомянутый ID есть среди загруженных паков, и каждый пак из `requires` загружен;
3. эффекты: известный тип, разрешён в этом месте, верные параметры, известный путь характеристики;
4. роли ресурсов: ровно один ресурс на `build` и на `upkeep_food`, не больше одного на `research`;
5. графы карт: граф связен, у каждого старта одинаковое число провинций каждого типа в радиусе двух рёбер от
   столицы, ни одна провинция не входит в два старта;
6. границы чисел;
7. тексты: `name` или `lore`, похожие на инструкцию агентам (`ignore`, `игнорируй`, `system`, `токен`, URL), дают
   предупреждение, а не ошибку.

Собирается и проверяется каждый сценарий, который определяют паки. Ошибки печатаются как
`ERROR файл: путь: сообщение`, предупреждения — как `WARN`. У собранного набора правил есть хэш (`ruleset_hash`,
sha256 канонического JSON); журнал его записывает, а `dah replay` сверяет.

## Загрузка паков

- По умолчанию партия загружает все паки из `content/packs/`. Положите свой пак туда как `content/packs/<id>/`.
- Или перечислите паки явно: `dah game create --packs content/packs/core --packs path/to/mymod ...`. Флаг
  повторяется для каждого пака, и загружаются только перечисленные, поэтому `core` тоже надо указать.
- Сценарий задаётся полным ID: `--scenario mymod:snipers3`.
- Список каталогов паков сохраняется в конфиге партии, поэтому `dah replay` пересобирает тот же набор правил. Если
  паки изменились после партии, повтор сообщит о расхождении правил.

## Пример

```yaml
# units/units.yaml
- id: sniper
  name: Снайперы
  attack: 6                      # границы задаёт реестр: 0..20
  defense: 1
  cost: {core:alloy: 18}
  upkeep: {core:provisions: 1}
  counters: [core:artillery]     # против артиллерии действует combat.counter_mul

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
  overrides:                     # в сценарии разрешён только modify
    - {type: modify, target: unit.core:assault.attack, op: add, value: 1}
    - {type: modify, target: p2.hasty_penalty, op: set, value: 0.2}
```

Фракция с бонусом, штрафом и стартовым эффектом:

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
