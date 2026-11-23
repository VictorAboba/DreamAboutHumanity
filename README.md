# DreamAboutHumanity

Пошаговая стратегия, в которую играют ИИ-агенты: рои DreamTeam, одиночные агенты, люди и встроенные боты.
Партия идёт через HTTP-сервер, агенты подключаются к нему MCP-мостом `dah-bridge`. Сервер записывает в журнал
каждое действие, поэтому после партии видно, сколько денег и минут ушло на каждый ход.

Сейчас в репозитории вертикальный срез игры. Его точная спецификация лежит в [docs/SLICE.md](docs/SLICE.md),
полная концепция в [docs/CONCEPT.md](docs/CONCEPT.md).

Ход состоит из четырёх фаз:

1. П1, переговоры.
2. Д, действия. После неё идёт разведсрез, в этой версии он пустой.
3. П2, правка приказов за очки командования (ОК).
4. Расчёт.

В срезе есть:

- модульный контент в YAML с валидатором;
- туман войны;
- сообщения между игроками;
- журнал партии, отчёт по нему и повтор партии со сверкой хэшей.

## Быстрый старт

Нужны Python 3.11 или новее и [uv](https://docs.astral.sh/uv/).

```sh
uv sync                     # поставить зависимости
uv run pytest -q            # тесты (должны быть зелёными)
```

### Партия из трёх ботов

```sh
# 1. Создать партию. Сценарий blitz_test даёт по 5 секунд на фазу и 6 ходов.
#    Ключи мест печатаются ОДИН раз, на диске хранятся только их sha256.
uv run dah game create --scenario core:blitz_test --seed 7 --id demo \
    --seat A:bot-rush:Раш --seat B:bot-builder:Строитель --seat C:bot-passive:Пасс

# 2. Запустить сервер. Он обслуживает каталог games/, держите его в отдельном терминале.
uv run dah serve --games-dir games --port 8800

# 3. Запустить ботов за все места kind=bot. Команда завершится на game_over.
uv run dah bots run --game demo --server http://127.0.0.1:8800

# 4. Посмотреть отчёт и повтор.
uv run dah report demo            # текстовый отчёт; с --json — машиночитаемый
uv run dah replay demo            # пересчёт всех ходов и сверка state_hash с журналом
```

Есть три бота. `passive` ничего не делает. `builder` строит шахты и фермы и держит армию дома. `rush`
нанимает максимум войск, идёт к ближайшей чужой провинции и атакует, когда `estimate` даёт ≥ 0.6.

### Партия с ИИ-агентами

```sh
uv run dah game create --scenario core:slice3 --seed 7 --limits 20m,20m,10m --turns 10 \
    --seat A:swarm:Нико --seat B:agent:Соло --seat C:bot-rush:Бот
uv run dah goal-text <id> A        # текст цели для роя или агента (ключа в нём нет)
```

Как подключить агента, описано в [docs/CONNECT.md](docs/CONNECT.md):

- MCP-сервер `dah-bridge` настраивается переменными `DAH_SERVER` и `DAH_KEY`;
- `dah-waker` будит сессию DreamTeam, когда открывается новая фаза;
- сессию DreamTeam надо создать с текстом цели из `dah goal-text`.

### Команды `dah`

| Команда | Что делает |
|---|---|
| `dah pack validate [PATH...]` | проверить паки контента; ошибки печатаются с файлом и путём к полю |
| `dah game create --scenario ... --seat SEAT:KIND:NICK ...` | создать партию в `games/<id>/`; KIND — `swarm`, `agent`, `human` или `bot-<passive\|builder\|rush>` |
| `dah serve [--games-dir games] [--port 8800]` | HTTP API (раздел 7 SLICE.md); админ-ключ берётся из `DAH_ADMIN_KEY` |
| `dah bots run --game ID [--server URL]` | сыграть ботами |
| `dah report ID [--json]` | отчёт: время до первого вызова, подачи и ready по местам и фазам, вызовы, символы, простой, счёт, деньги |
| `dah replay ID` | повтор партии из начального состояния и сохранённых приказов |
| `dah goal-text ID SEAT` | текст цели участника |

Ещё два console script: `dah-bridge` (stdio MCP → HTTP) и `dah-waker` (пробуждение сессий DreamTeam).

## Устройство проекта

```
content/packs/core/        базовый модуль: ресурсы, типы провинций, юниты, здания, фракция, карты, сценарии
src/dah/
  engine/                  чистая библиотека правил, без ввода-вывода
    registry.py            характеристики (stats) с границами и 6 типов эффектов
    content.py             загрузка паков, валидация по JSON Schema, сборка Ruleset
    schemas/               JSON Schema для каждого типа контента
    model.py               состояние партии (dataclasses), state_hash
    orders.py              проверка приказов, цены в ОК для П2
    combat.py, resolve.py  бой и estimate; расчёт хода (детерминированная чистая функция)
    view.py                туман войны, текст состояния и отчёта, rules_text
    rng.py                 потоки случайности от seed|turn|system|entity
  game.py                  партия: фазы, дедлайны, ready, приказы, сообщения, replay
  journal.py, store.py     журнал JSONL и каталог партии на диске
  server/                  FastAPI-приложение (HTTP API)
  bridge/                  dah-bridge: MCP-инструменты поверх HTTP
  waker/                   dah-waker: будильник для сессий DreamTeam
  bots/                    боты passive, builder, rush (обычные HTTP-клиенты, без поблажек)
  cli.py, report.py        команды dah, отчёт партии
tests/                     pytest; test_e2e_bots.py — сквозной тест: CLI → сервер → боты → отчёт/повтор → мост
docs/                      SLICE.md (спецификация), CONCEPT.md (концепция), CONNECT.md (подключение агентов)
```

Каталог партии `games/<id>/`:

- `config.json`;
- `keys.json` (хэши ключей);
- `journal.jsonl`;
- `private/` со снапшотами состояний, приказами и отчётами по ходам, сообщениями и ключами ботов. Игрокам
  сервер его никогда не отдаёт.

## Как написать пак контента

Пак — это каталог с `pack.yaml` и YAML-файлами по типам сущностей. В одном файле лежит одна сущность или
список сущностей:

```
mymod/
  pack.yaml
  resources/  province_types/  units/  buildings/  factions/  maps/  scenarios/
  locale/ru.yaml               # необязательно
```

`pack.yaml`:

```yaml
id: mymod
version: 0.1.0          # semver
title: Мой модуль
author: я
requires: [core]
engine_api: "^1"
```

Внутри пака ID пишутся коротко (`sniper`). После загрузки у сущности полный ID `пак:id` (`mymod:sniper`).
На сущности чужого пака ссылаются по полному ID:

```yaml
# units/units.yaml
- id: sniper
  name: Снайперы
  attack: 6                      # границы задаёт реестр: 0..20
  defense: 1
  cost: {core:alloy: 18}
  upkeep: {core:provisions: 1}
  counters: [core:artillery]     # против кого действует множитель combat.counter_mul

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

Эффектов шесть: `modify`, `add_resource`, `grant_units`, `grant_building`, `set_flag` и `income`. Их параметры
и список характеристик, которые можно менять через `modify`, описаны в разделе 2 SLICE.md.

Модификаторы применяются в таком порядке:

1. base;
2. все `add`;
3. все `mul`;
4. `set`;
5. clamp по границам.

Значение вне границ прямо в файле считается ошибкой. Если значение вышло за границы только после модификаторов,
валидатор выдаст предупреждение.

Проверка:

```sh
uv run dah pack validate content/packs/core path/to/mymod   # зависимости надо передать вместе с модом
```

Валидатор проверяет:

- схему;
- ссылки;
- что у каждой роли ресурса ровно один ресурс;
- что граф карты связен и старты симметричны;
- границы чисел;
- длину текстов и подозрительные инструкции агентам в них (это предупреждение, а не ошибка).

Чтобы игра увидела пак, положите его в `content/packs/<id>/`: по умолчанию загружаются все паки оттуда.
Можно и явно перечислить паки: `dah game create --packs content/packs/core --packs path/to/mymod`. Флаг
повторяется для каждого пака, и с ним загружаются только перечисленные паки, поэтому `core` тоже надо указать. Сценарий
задаётся полным ID: `--scenario mymod:snipers3`.
