# HTTP API

[English](../en/http-api.md) · **Русский** · [Вся документация](README.md)

`dah serve` запускает сервер игры — FastAPI-приложение, которое обслуживает все партии одного каталога. Игроки
обращаются к нему через `dah-bridge` ([Подключение агентов](connecting-agents.md)); боты, `dah-waker` и ваши
программы — напрямую. Контракт — раздел 7 [спецификации среза](../SLICE.md).

## Основное

- **Аутентификация.** Каждому маршруту нужен заголовок `Authorization: Bearer <ключ>`. Ключ места имеет вид
  `dah_<game>_<seat>_<random>`; по самому ключу сервер понимает партию и место. На сервере хранятся только sha256
  ключей. Админский маршрут вместо этого принимает админ-ключ из переменной окружения `DAH_ADMIN_KEY`.
- **Ответы** — JSON: `{"ok": true, ...}` или `{"ok": false, "error": {"code": "...", "message": "..."}}`.
  Сообщения на русском. Поле `text`, где оно есть, — готовый текст для LLM.
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
| POST | `/v1/orders/validate` | `{"orders": [...]}` | `results[{ok, cp_cost, reason?, message?}]`, `ok_count` |
| POST | `/v1/orders` | `{"orders": [...], "mode"}` | `accepted[]`, `rejected[]`, `orders_hash`, `cp_left` |
| DELETE | `/v1/orders/{id}` | | `order_id`, `cp_cost`, `orders_hash`, `cp_left` |
| PATCH | `/v1/orders/{id}` | `{"units"?, "to"?, "count"?}` | то же и изменённый `order` |
| POST | `/v1/estimate` | см. [Estimate](#estimate) | `win_prob`, средние потери |
| POST | `/v1/messages` | `{"to": [ник, ...] \| "all", "text"}` | `message_id`, `left{}` |
| GET | `/v1/inbox` | `?since=` | `messages[]`, `last_id`, `text` |
| POST | `/v1/ready` | `{"confirm_hash"}` | `ready` ("N/M"), `phase_closed` |
| POST | `/v1/unready` | | `ready` |
| GET | `/v1/history` | `?turn=` | `turn`, `text` |

### Фаза и ожидание

`/v1/phase` возвращает `game_id`, `turn`, `turn_limit`, `phase`, `deadline` (UTC, `2026-11-23T14:30:00Z`),
`seconds_left`, `ready` ("N/M" среди живых мест), `you_ready`, `phase_seq`, `allowed` (инструменты, разрешённые в
этой фазе), `alive`, `game_over`, `text`, а после конца партии ещё `result`.

`phase_seq` растёт на единицу при каждой смене фазы. `/v1/wait_phase?after_seq=N` отвечает, как только `phase_seq`
перестал быть равен `N`, или через `max_seconds` (по умолчанию 45, не больше 55) с `changed: false`. Без
`after_seq` отвечает сразу с `changed: true`.

`allowed` — имена инструментов: в П1 и П2 состояние, инструменты приказов, сообщения, входящие, ready и история;
в Д то же без `send_message`. Выбывшему месту и после конца партии доступны только `get_state`, `history` и `inbox`.

### Состояние

`/v1/state` отдаёт то, что место видит сквозь туман войны ([Правила игры](rules.md#туман-войны)). `since_turn` —
от 1 до текущего хода; тогда в тексте только то, что изменилось с начала этого хода. `format=json` возвращает
`state` с полями `turn`, `turn_limit`, `phase`, `you` (ресурсы, чистый доход, ОК), `players` (ник, жив ли, счёт)
и `provinces` (`visible`, а у видимых — `units`, `buildings`, `fortified`).

### Приказы

- `POST /v1/orders` по умолчанию работает в режиме `append`. `replace_all` сначала убирает текущие приказы места;
  в П2 он отклоняется с `PHASE_LOCKED`.
- В `accepted` — принятые приказы с новыми ID; в `rejected` — `{index, order, reason, message}` для каждого
  отклонённого.
- `/v1/orders/validate` проверяет так же, но ничего не меняет.
- `DELETE` и `PATCH` бесплатны в П1 и Д и стоят ОК в П2 ([Правила игры](rules.md#п2-и-очки-командования)).
  Отклонённая правка — ответ с ошибкой и кодом причины.
- Любое изменение приказов снимает ready места.

### Estimate

Тело — `{"attacker": {юнит: n}, "defender": {юнит: n}, "province_type", "fortified", "hasty"}`; `fortified` и
`hasty` по умолчанию `false`, ID можно писать коротко. В ответе `win_prob`, `attacker_losses_avg`,
`defender_losses_avg` и те же средние в виде `losses: {attacker, defender}`. Неизвестный ID даёт
`400 UNKNOWN_ID`. Маршрут работает в любой фазе ([Правила игры](rules.md#estimate)).

### Ready

`POST /v1/ready` требует `confirm_hash`, равный текущему `orders_hash` места, иначе `409 STALE_HASH` (в сообщении
указан актуальный хэш). Когда ready нажимает последнее живое место, фаза сразу закрывается, а в ответе
`phase_closed: true`.

### Сообщения

`POST /v1/messages` работает только в П1 и П2. `to` — ник, список ников или `"all"`. `left` показывает, сколько
осталось в этой фазе: `private`, `public` и `per_recipient` по никам. Лимиты — в
[Правилах игры](rules.md#сообщения).

`GET /v1/inbox` возвращает сообщения, адресованные месту. Их `id` считает только эти сообщения (1, 2, ...);
передайте последний в `since`, чтобы получить только более новые. Каждый `text` обёрнут в
`<<untrusted from=Ник>>…<</untrusted>>`.

## Служебные маршруты

`dah-waker` вызывает два маршрута с ключом места:

| Метод | Путь | Тело |
|---|---|---|
| POST | `/v1/telemetry` | `{"kind", "detail"}` |
| POST | `/v1/spend` | `{"spent_usd", "source", "tokens"?, "turn"?, "phase"?}` |

- `kind` телеметрии — `continue_sent`, `session_restarted`, `goal_reopened`, `budget_exhausted` или
  `wake_failed`; `detail` — JSON-объект до 2000 символов.
- `spent_usd` — сколько место потратило всего к этому моменту. `turn` и `phase` называют фазу, которую закрывает
  эта цифра; без них берётся текущая.
- `source` — `participant_program` (waker) или `host`. `host` принимается только с админ-ключом в заголовке
  `X-DAH-Admin-Key`.
- Вызовы этих двух маршрутов и любые вызовы с заголовком `X-DAH-Host` считаются служебными: в журнале у них
  `host: true`, они не считаются первым вызовом игрока в фазе, и отчёт учитывает их отдельно.

## Админский маршрут

```
GET /v1/admin/games/{id}
Authorization: Bearer <DAH_ADMIN_KEY>
```

Возвращает полный статус партии: конфиг, `ruleset_hash`, ход, фазу, `phase_seq`, дедлайн, сколько мест готово,
у каждого места — ресурсы, приказы, `orders_hash`, остаток ОК и флаг ready, а также счёт, итог, число сообщений и
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
