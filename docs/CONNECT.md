# Подключение игрока к DreamAboutHumanity

Игрок — рой DreamTeam, одиночный агент или любой MCP-клиент — подключается к серверу игры через `dah-bridge`:
stdio MCP-сервер `dah`, который переводит вызовы инструментов в HTTP API сервера (SLICE.md, разделы 7–8).
Рою DreamTeam нужен ещё `dah-waker`: он будит сессию, когда наступает фаза, в которой место должно действовать.

Что понадобится:

- запущенный сервер игры: `uv run dah serve --games-dir games --port 8800`;
- ключ места `dah_<game>_<seat>_<...>` — его один раз печатает `dah game create` (храните его как пароль);
- текст цели для места: `uv run dah goal-text <GAME_ID> <SEAT>` (в нём нет ключа).

## Инструменты `dah`

`whoami`, `rules(topic?)`, `phase`, `wait_phase(max_seconds=45)`, `get_state(since_turn?, format?)`, `list_orders`,
`validate_orders(orders)`, `submit_orders(orders, mode)`, `cancel_order(id)`, `amend_order(id, patch)`,
`estimate(attacker, defender, province_type, fortified?, hasty?)`, `send_message(to, text)`, `inbox(since?)`,
`ready(confirm_hash)`, `unready`, `history(turn?)`.

- `orders` — JSON-массив приказов, например
  `[{"kind": "move", "from": "P01", "to": "P02", "units": {"core:assault": 2}}]`.
- `wait_phase` сам помнит последний виденный `phase_seq`: первый вызов сразу возвращает текущую фазу, следующие ждут
  её смены (до 55 с).
- Мост берёт адрес и ключ из переменных `DAH_SERVER` и `DAH_KEY`. Ключ уходит только в заголовок
  `Authorization` и никогда не попадает в ответы инструментов и в логи.

## Рой DreamTeam

Нужен DreamTeam **0.3.0 или новее** (HTTP API `/api/v1` с `continue`).

### 1. Мост в настройках DreamTeam

В `~/DreamTeam/dreamteam.toml` (или в настройках конкретной сессии — так удобнее, если на одном хабе играют
несколько мест):

```toml
[mcp.external.dah]
command = ["uv", "run", "--project", "C:/path/to/DreamAboutHumanity", "dah-bridge"]
env = { DAH_SERVER = "http://127.0.0.1:8800", DAH_KEY = "dah_..." }
description = "Игра DreamAboutHumanity"
```

Ключ передавайте только через таблицу `env`: переменные с секретными именами из окружения хаба DreamTeam не отдаёт
ни агентам, ни внешним MCP-серверам, а `env` внешнего сервера получает только он сам.

### 2. Хаб DreamTeam с HTTP API

Для первых замеров запускайте обычный `dreamteam serve`, **без песочницы** (`dreamteam sandbox` добавляет свои
накладные расходы и меняет время ответа):

```bash
export DREAMTEAM_API_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export DREAMTEAM_HUB_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
dreamteam serve --no-browser
```

PowerShell:

```powershell
$env:DREAMTEAM_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:DREAMTEAM_HUB_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
dreamteam serve --no-browser
```

### 3. Сессия с целью из `dah goal-text`

Сессию нужно создать **через `/api/v1`**: токен API видит только такие сессии, и `dah-waker` не сможет будить
сессию, начатую на доске или в терминале.

```bash
uv run dah goal-text <GAME_ID> A > goal.txt
python3 -c "import json; print(json.dumps({'goal': open('goal.txt', encoding='utf-8').read(),
  'language': 'ru', 'max_cost_usd': 5, 'answers': 'auto', 'stop_when_done': False}))" > body.json
curl -s -H "Authorization: Bearer $DREAMTEAM_API_TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8765/api/v1/sessions -d @body.json
# 202 {"session": {"id": "5f0c...", ...}} — id понадобится waker'у
```

- **Одиночный агент вместо роя** — та же сессия с `"start_agents": 1, "max_agents": 1` в теле запроса.
- `max_cost_usd` — бюджет места. Когда он кончится, DreamTeam ответит на `continue` кодом `budget_exhausted`,
  waker запишет это в журнал партии и больше будить не будет; ход, где кончились деньги, виден в `dah report`.
- `stop_when_done: false` оставляет сессию работать между фазами. Если сессия всё же остановится или рой сочтёт
  цель выполненной, `continue` запустит её снова — waker отметит это как `session_restarted` / `goal_reopened`.

### 4. Waker

```bash
export DREAMTEAM_API_TOKEN=...   # тот же токен API; waker берёт его только из окружения
uv run --project C:/path/to/DreamAboutHumanity dah-waker \
  --server http://127.0.0.1:8800 --key dah_... \
  --dt-url http://127.0.0.1:8765 --dt-session 5f0c...
```

Ключ можно не писать в командной строке, а задать в `DAH_KEY` (и адрес — в `DAH_SERVER`).

Что делает waker:

- ждёт смены фазы (`/v1/wait_phase`); когда открывается фаза, где место действует (П1, Д, П2), отправляет сессии
  `continue` с самодостаточным текстом вида
  `[DAH] Ход 3/10, фаза ДЕЙСТВИЯ, до 14:30Z (ещё 20 мин). Готово 1/3. Посмотри dah.get_state(since_turn=2), подай приказы, нажми dah.ready.`;
- за 60 секунд до дедлайна, если место ещё не нажало ready, шлёт напоминание;
- после каждой смены фазы читает сводку сессии DreamTeam и отправляет в игру расход (`/v1/spend`,
  `source: participant_program`);
- при 429 и 5xx от DreamTeam повторяет через 10, 30 и 60 с, затем пишет `wake_failed`;
- пишет телеметрию `continue_sent`, `session_restarted`, `goal_reopened`, `budget_exhausted`, `wake_failed`;
- завершается, когда партия окончена.

## Любой другой MCP-клиент

`dah-bridge` — обычный stdio MCP-сервер: клиенту нужны команда запуска и две переменные окружения.

### Claude Code

```bash
claude mcp add dah \
  --env DAH_SERVER=http://127.0.0.1:8800 \
  --env DAH_KEY=dah_... \
  -- uv run --project C:/path/to/DreamAboutHumanity dah-bridge
```

Затем дайте агенту текст из `uv run dah goal-text <GAME_ID> <SEAT>`. Waker одиночному клиенту не нужен: агент сам
ждёт фаз через `dah.wait_phase` (цель не достигнута, пока `dah.phase()` не вернул `game_over`).

### Клиенты с JSON-конфигом

```json
{
  "mcpServers": {
    "dah": {
      "command": "uv",
      "args": ["run", "--project", "C:/path/to/DreamAboutHumanity", "dah-bridge"],
      "env": {"DAH_SERVER": "http://127.0.0.1:8800", "DAH_KEY": "dah_..."}
    }
  }
}
```

## Проверка

- `uv run dah-bridge` без переменных окружения сразу завершится с подсказкой — значит, команда найдена.
- Первый вызов агента — `dah.whoami()`: он покажет партию, место и ник. Ошибка `unauthorized` — неверный ключ.
