# Паки контента

[English](../en/modding.md) · **Русский** · [Вся документация](README.md)

Весь контент игры лежит в паках: ресурсы, типы провинций, юниты, здания, фракции, карты и сценарии. Движок ничего
не знает о конкретных юнитах и ресурсах — только о **ролях** ресурсов и адресуемых **характеристиках**. Базовый пак —
`content/packs/core/`; источник этих правил — раздел 2 [спецификации среза](../SLICE.md).

## Устройство пака

Пак — это каталог с `pack.yaml` и YAML-файлами, разложенными по типам сущностей:

```
mymod/
  pack.yaml
  resources/  province_types/  units/  buildings/  factions/  maps/  scenarios/
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
| ресурс | `id`\*, `name`\*, `short`, `role`\* (`build` или `upkeep_food`) |
| тип провинции | `id`\*, `name`\*, `short`, `income`\* (`{ресурс: n}`), `defense_mul` (больше 0, до 5) |
| юнит | `id`\*, `name`\*, `short`, `attack`\*, `defense`\*, `cost`\*, `upkeep`\*, `counters` (список юнитов) |
| здание | `id`\*, `name`\*, `short`, `cost`\*, `build_turns`\* (1..10), `effects`, `recruit`, `max_per_province` |
| фракция | `id`\*, `name`\*, `lore` (до 1000 символов), `bonuses`, `penalties`, `starting`\* |
| карта | `id`\*, `name`\*, `max_players`\* (1..16), `provinces`\*, `edges`\*, `starts`\* |
| сценарий | `id`\*, `name`\*, `map`\*, `factions`\*, `turn_limit`\*, `phase_limits`\*, `message_limits`, `overrides` |

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
- Места получают первую фракцию из списка `factions` сценария.

## Эффекты

Типов эффектов шесть, и каждый разрешён только в определённых местах.

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
| `combat.fortify_bonus` | float 0..1 | 0.25 | бонус обороны приказа `fortify` |
| `combat.counter_mul` | float 1..3 | 1.5 | множитель контрюнита |
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

## Проверка

```sh
uv run dah pack validate content/packs/core path/to/mymod   # зависимости надо передать вместе с модом
```

Валидатор проверяет:

1. JSON Schema каждого файла (схемы лежат в `src/dah/engine/schemas/`);
2. ссылки: каждый упомянутый ID есть среди загруженных паков, и каждый пак из `requires` загружен;
3. эффекты: известный тип, разрешён в этом месте, верные параметры, известный путь характеристики;
4. роли ресурсов: ровно один ресурс на каждую роль (`build`, `upkeep_food`);
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
