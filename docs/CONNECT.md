# Подключение игрока к DreamAboutHumanity

Игрок — рой DreamTeam, одиночный агент или любой MCP-клиент — подключается к серверу игры через `dah-bridge`:
stdio MCP-сервер `dah`, который переводит вызовы инструментов в HTTP API сервера (SLICE.md, разделы 7–8).
Рою DreamTeam нужен ещё `dah-waker`: он будит сессию, когда наступает фаза, в которой место должно действовать.

Что понадобится:

- запущенный сервер игры: `uv run dah serve --games-dir games --port 8800`;
- ключ места `dah_<game>_<seat>_<...>` — его один раз печатает `dah game create` (храните его как пароль);
- текст цели для места: `uv run dah goal-text <GAME_ID> <SEAT>` (в нём нет ключа).

Ключей два вида, путать их нельзя:

| Ключ | Вид | Куда идёт |
|---|---|---|
| ключ места | `dah_<game>_<seat>_...` | `DAH_KEY` моста `dah-bridge`, `--key`/`DAH_KEY` у `dah-waker`, свои программы и боты: маршруты игрока `/v1/*` |
| ключ зрителя | `dahw_<game>_...` | только браузер: страница `http://127.0.0.1:8800/watch/<id>` (и `/v1/spectate/*`); агентам его не давать |

Ключ зрителя печатается вместе с ключами мест, новый выдаёт `dah game spectator-key <id>`. Ключ места на странице
наблюдения не принимается, ключ зрителя не работает в API игрока. Страница наблюдения принимает и ссылку `/watch/<id>#key=<ключ зрителя>`: ключ во фрагменте не уходит на сервер, страница сохраняет его в sessionStorage, сразу убирает из адресной строки и шлёт только заголовком `Authorization: Bearer`; без фрагмента ключ спрашивает форма.

## Инструменты `dah`

`whoami`, `rules(topic?)`, `phase`, `wait_phase(max_seconds=45)`, `get_state(since_turn?, format?)`, `list_orders`,
`validate_orders(orders)`, `submit_orders(orders, mode)`, `cancel_order(id)`, `amend_order(id, patch)`,
`estimate(attacker, defender, province_type, fortified?, hasty?)`, `send_message(to, text)`, `inbox(since?, all?)`,
`send_gift(to, resources)`, `propose_pact(to, turns?)`, `respond_pact(id, decision)`, `leave_pact(id)`, `list_pacts`,
`ready(confirm_hash?)`, `unready`, `history(turn?)`; этап 2 (фаза старта и технологии, [TECH_AND_SETUP.md](TECH_AND_SETUP.md)):
`get_setup`, `choose_faction(prefs)`, `choose_artifacts(ids)`, `list_techs(full?)`, `describe(id)`; этап 3 (только
в партиях со шпионажем, [SPY_AND_CONQUEST.md](SPY_AND_CONQUEST.md)): `list_spies`, `get_intel(turn?, shade?,
report_id?)`, `estimate_mission(mission, target, rounds?, shade?, assume_mirrors?)`.

- `ready` нажимай в каждой фазе, как только сказать и сделать больше нечего (фаза кончается, когда готовы все): в фазе
  старта и П1 — `ready()` без `confirm_hash` (в П1 приказов нет, хэш не проверяется); в Д и П2 —
  `ready(confirm_hash=orders_hash)`, где `orders_hash` — из `phase()`, `list_orders()` или ответа `submit_orders`
  (после каждой правки новый). Отказ `STALE_HASH` называет точный вызов с актуальным хэшем.

- `rules()` — краткое ядро правил (≤ 6000 символов) с оглавлением; `rules(topic="techs")` — раздел целиком, собранный
  из контента партии. Темы: `turn`, `setup`, `resources`, `provinces`, `units`, `abilities`, `buildings`, `orders`,
  `p2`, `combat`, `victory`, `pacts`, `spying`, `conquest`, `discoveries`, `fog`, `messages`, `techs`, `factions`, `artifacts` (есть
  только те, что нужны партии); ID или название контента в `topic` даёт его описание.
- Счёт (docs/SCORING.md): строка «Счёт:» в `get_state` — твой счёт хода с разбивкой (`зем`, `нау`, `стр`, `вой`), у
  соперников только нижняя граница «≥», твоя тайная цель и выполнена ли она сейчас (с фазой старта цель видна уже
  в `get_setup`); числа партии, великие стройки и пул целей — `rules(topic="victory")`; очки науки — `list_techs`;
  после `game_over` — итог всех в `phase()` (`result`) и строка «Итог:» в `get_state`.
- Шпионаж и завоевание — только если их включили сценарий или хост (`dah game create --espionage on --conquest on`).
  Приказы идут через те же `validate_orders`/`submit_orders`/`amend_order`/`cancel_order`: `{"kind": "hire_shade"}`
  (только Д), `{"kind": "spy", "shade": "s1", "mission": "observe", "target": "P07", "rounds": 2}`,
  `{"kind": "recall", "shade": "s1"}`, `move` с `"feint": true` (ложная запись, только Д),
  `{"kind": "dispose", "province": "P07", "disposition": "subjugate"}` и `{"kind": "policy", "disposition":
  "subjugate"}` (Д и П2, 0 ОК). Разведсрез идёт на закрытии Д; в начале П2 — одна запись `[ИГРА] Разведсрез хода N`
  во входящих, строка `ДОНЕСЕНИЯ` в `get_state`, хвост «донесений: N» в `phase()` и пробуждении waker'а; тексты —
  `get_intel`. Оккупации, притязания и политика — разделы `ОККУПАЦИЯ`, `ПРИТЯЗАНИЯ`, `ПОЛИТИКА` в `get_state`. В
  `core` у каждого места с начала есть свободная Тень `s1` (строка `ТЕНИ` в `get_state`, пока она свободна); миссия
  `secret` читает тайную цель соперника.
- Заброшенные земли и находки (этап 5, [ABANDONED_LANDS.md](ABANDONED_LANDS.md)) — только если их включили сценарий
  или хост (`dah game create --discoveries on`) и старт карты остался без игрока. Метки `[заброш.]`, `[руины]`,
  `[стража]`, `[логово n/предел]` в `get_state`; вход любой армии в Заброшенную провинцию запускает её скрытую
  находку, если она там есть (помеха может стереть мелкую армию; `validate_orders` предупреждает о таком ходе).
  Смешанную находку решает приказ `{"kind": "discover", "province": "P16", "choice": "wake"}` по строке `НАХОДКА`
  (Д или П2, 0 ОК; поправить — `amend_order(id, patch={"choice": "leave"})`), без него — вариант по умолчанию.
  Пул находок — `rules(topic="discoveries")` и `describe(<находка>)`; нового инструмента нет.
- Фаза старта `setup` (если она есть в партии) — до хода 1: `phase()` возвращает `phase: "setup"` и подсказку `hint`.
  `get_setup()` — все фракции с описаниями, твои предложенные артефакты (драфт) или пул и бюджет (очки), твой выбор;
  `choose_faction(prefs=["litany", "captains"])` — фракции по предпочтению; `choose_artifacts(ids=["ancient_blueprint"])`;
  `ready()` — без `confirm_hash`. Чужой выбор не виден; `get_state` во время фазы — предварительное состояние, фракции
  соперников в нём скрыты.
- Технологии: `list_techs()` — коротко (изученные, активная с прогрессом, доступные сейчас с ценой и эффектом);
  `list_techs(full=true)` — всё дерево с закрытыми и их требованиями;
  исследование — обычный приказ `{"kind": "research", "tech": "deep_drilling"}` в Д (в П1 — только `validate_orders`),
  сменить — `amend_order(id, patch={"tech": "agro_vats"})`. `describe(id="throne_warden")` — любой юнит, здание, технология,
  артефакт, фракция по ID или названию. Чужие технологии и артефакты не видны (только число изученных технологий).
- Приказы подаются только в фазе Д (`submit_orders`; в П2 — `append`, `amend_order`, `cancel_order` за ОК). В П1
  `submit_orders`, `cancel_order`, `amend_order` отвечают `PHASE_LOCKED`: там приказы можно только проверить
  (`validate_orders` работает в любой фазе).
- `inbox()` моста по умолчанию отдаёт только записи новее последней, которую этот процесс моста уже вернул;
  `inbox(all=true)` или `inbox(since=0)` — вся история. Записи `[ИГРА]` (`system: true`) пишет сама игра: подношения
  и пакты. Свои отправленные сообщения тоже там (`from: "ты"`, `own: true`): участники роя видят, что уже сказали
  напарники.
- Дипломатия — в П1 и П2: `send_gift` (свободные ресурсы сразу уходят игроку, ≤3 за фазу), пакты
  (`propose_pact` → `respond_pact`; `leave_pact`; продление действующего пакта со сроком — `propose_pact` тому же
  партнёру, срок добавится к концу нынешнего). У `move` есть поле `pact`: `honor` (по умолчанию: в провинцию
  союзника войска не войдут) или `break` (нарушение: бой, пакт публично разорван). Подробно —
  [Правила игры](ru/rules.md#дипломатия-подношения-и-пакты).

- `orders` — JSON-массив приказов. Надёжнее всего плоская форма `move` (один тип юнитов на приказ):
  `[{"kind": "move", "from": "P01", "to": "P04", "unit": "assault", "count": 2}]`. Вложенная тоже работает:
  `[{"kind": "move", "from": "P01", "to": "P04", "units": {"assault": 2, "armor": 1}}]`. Если клиент теряет
  вложенные объекты (приходит пустой `units`), передайте весь список одной JSON-строкой:
  `orders="[{\"kind\":\"move\",\"from\":\"P01\",\"to\":\"P04\",\"unit\":\"assault\",\"count\":2}]"` — это
  гарантировано для `validate_orders`, `submit_orders` и `patch` у `amend_order`
  (`amend_order(id="o3", patch={"unit": "assault", "count": 1})` или `patch` строкой). Сервер приводит любую форму
  к вложенной; отказ из-за пустого `units` показывает, что пришло, и оба обхода.
- `estimate` — прогноз боя, например
  `estimate(attacker={"core:assault": 4}, defender={"core:assault": 2}, province_type="P07")`. Юнит — полный ID,
  короткий (`assault`) или метка из текста состояния (`штурм`); `province_type` — ID типа (`core:agri`), его метка
  из текста состояния (`агро`, `кузн.`, `пуст.`, `столица`) или ID провинции (`P07`) — тогда берётся её тип.
  `side` (`attacker` или `defender`) — твоя сторона: на ней действуют твои технологии, артефакты и
  здания; без `side` её определяют ID провинций (своя провинция — твоя сторона; чужая армия у `attacker` — ты
  защищаешься), иначе `attacker`; ответ называет сторону в `side` и `note`. `opponent` — ник соперника, его фракция
  берётся на другую сторону (без него — фракция владельца чужой провинции, если она указана ID). Сторону можно задать и ID провинции с
  видимой армией: `estimate(attacker="P05", defender="P07")` — вся армия каждой, бой в P07.
- `get_state()` — всё состояние, `get_state(since_turn=N)` — изменения с хода N (`since_turn=0` — тоже всё);
  `history()` — отчёт последнего рассчитанного хода (до расчёта хода 1 и для идущего хода — подсказка вместо
  отчёта).
- На неверные аргументы `estimate`, `get_state`, `history` и `send_message` ответ называет ошибку и показывает верный
  вызов (`… Пример: estimate(attacker={"core:assault": 4}, …)`).
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

**Windows.** Запущенный `dah-bridge` держит открытым свой файл `.venv\Scripts\dah-bridge.exe`, а открытый exe
Windows перезаписать не даёт. Обычный `uv run` сначала синхронизирует окружение; если ему надо перезаписать этот
файл (второй мост для другого места, waker, `dah` после смены зависимостей), он падает с `os error 32` («процесс не
может получить доступ к файлу, так как этот файл занят другим процессом»). Поэтому в Windows запускайте мост (и
waker) без синхронизации:

```toml
command = ["uv", "run", "--no-sync", "--project", "C:/path/to/DreamAboutHumanity", "dah-bridge"]
# или сам файл:
command = ["C:/path/to/DreamAboutHumanity/.venv/Scripts/dah-bridge.exe"]
```

`uv sync` выполните один раз заранее, пока не запущены ни мост, ни waker.

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

**Доска, если у хаба есть токен.** С `DREAMTEAM_HUB_TOKEN` доска отвечает браузеру без токена 401. Откройте адрес
доски один раз с токеном хаба в запросе: `http://127.0.0.1:8765/?token=<DREAMTEAM_HUB_TOKEN>`. Хаб превращает его в
cookie (HttpOnly, SameSite=Strict) и открывает адрес уже без токена; дальше доска работает в этом браузере, в том
числе доска самой сессии (`board_url` в сводке `GET /api/v1/sessions/{id}`). Нужен именно токен хаба, а не токен
API; на `/api/v1` действует только заголовок `Authorization`, cookie и `?token=` там ничего не дают (документация
DreamTeam: `docs/ru/sandbox.md`, «Токен доступа»; `docs/en/http-api.md`).

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

Флаги пробуждения: `--wake-mode all|continue|both` (по умолчанию `all`) и `--ack-seconds N` (120).
`continue` в DreamTeam доходит только до ОДНОГО участника — координатора (держателя задачи планирования). Рой, который
поделил ходы по сменам, может не услышать его: так в замере m3 вся фаза Д закрылась по таймеру без единого вызова игры.
Поэтому по умолчанию waker шлёт пробуждение сообщением ВСЕМ участникам работающей сессии
(`POST /api/v1/sessions/{id}/messages {"to": ["all"], "text": "[DAH] ..."}`); если сессия остановлена (сводка
говорит `stopped`/`failed`/`created` или `final: true`, или сообщение получило 409 `not_running`) — шлёт `continue`,
который запускает её снова. Сессии в состоянии `done` (или с выполненной целью), `paused` или `waiting` тоже шлётся
`continue` (fallback `goal_done`, `paused`, `waiting`): сообщение такую сессию не разбудит, а `continue` откроет цель
заново, снимет паузу пользователя или ответит на вопрос. `continue` — только `continue`, как раньше; `both` —
сообщение всем и `continue`.

Что делает waker:

- ждёт смены фазы (`/v1/wait_phase`); когда открывается фаза, где место действует (П1, Д, П2), будит сессию (по
  умолчанию — сообщением всем участникам, см. `--wake-mode`) самодостаточным текстом вида
  `[DAH] Ход 3/10, фаза ДЕЙСТВИЯ, до 14:30Z (ещё 20 мин). Готово 1/3. Посмотри dah.get_state(since_turn=2), подай приказы (dah.submit_orders), затем dah.ready(confirm_hash=orders_hash из ответа dah.submit_orders).` (в П1 — «сразу dah.ready() (в П1 без confirm_hash)», в П2 — точный вызов с текущим `orders_hash`; при скрытом конце, core 0.8.2, quad4 — «Ход 7 (конец — один из ходов 11–15, скрыт)», с хода 11 «Ход 11 (может быть последним; …)»: вытянутый ход waker не знает и не называет);
- за 60 секунд до дедлайна, если место ещё не нажало ready, шлёт напоминание (тем же способом);
- (core 0.8.1) если чужое предложение обмена или пакта сняло «готов» места, будит его один раз (`reason:
  ready_cleared`: что сняло, ответь, снова нажми ready), если только `dah.wait_phase` самого места не вернул эту
  новость сам (он возвращается сразу, `reason: "ready_cleared"`); если игрок обращался к игре последние 30 секунд
  (сам ready() тоже считается), пробуждение откладывается до того, как этому обращению исполнится 30 секунд;
- после каждого пробуждения следит, ответил ли рой: если в той же фазе за `--ack-seconds` (120) от места пришёл
  собственный вызов игрока (не waker'а), пишет `wake_answered` {seconds — через сколько секунд после пробуждения},
  иначе `wake_unanswered` {turn, phase, seconds; `phase_closed`, если фаза закрылась раньше; `superseded`, если
  раньше пришло напоминание}. Ответом считается первый вызов после того, как пробуждение ушло (после повторов), кроме чтения статуса фазы (`dah.phase`, `dah.wait_phase`): участник, который в ответ на [DAH] только снова ждёт, не сыграл — такое пробуждение `wake_unanswered` с `passive_only: true`. Пока ответа нет, опрашивает игру не реже раза в 15 с. `dah report` показывает по
  каждому месту: пробуждений отправлено / с ответом / без ответа и медиану секунд до первого вызова игры;
- не шлёт этот `continue` при открытии фазы, если игрок уже работает: сервер сообщает waker'у (только ему, по
  заголовку `X-DAH-Host`), ждёт ли сейчас long-poll `dah.wait_phase` самого места или ждал в момент открытия фазы
  (`player_waiting`; такой вызов сам принесёт игроку новую фазу), и сколько секунд назад игрок обращался к игре в
  этой фазе (`player_active_s_ago`; до 30 секунд — значит, работает; вызов, начатый ещё в прошлой фазе, например
  `ready`, который сам закрыл фазу, не считается). Вместо `continue` пишет `continue_skipped` с причиной
  (`player_waiting` или `player_active`) и состоянием сессии. Если сессия DreamTeam остановлена (`stopped`,
  `failed`, `done`, `budget`, `created`), `continue` шлётся всё равно: long-poll может пережить агента, который его
  начал. Напоминание перед дедлайном не пропускается никогда;
- при старте записывает свой `--dt-session` в партию (`/v1/seat/dreamteam_session`: `config.json` и событие
  `seat_info` в журнале), так что связь места с сессией DreamTeam сохраняется без ручных шагов; если сервер игры
  ещё не поднят, повторяет запись после первого удачного опроса;
- после каждой смены фазы читает сводку сессии DreamTeam и отправляет в игру её расход (`/v1/spend`,
  `source: participant_program`) как цифру только что закрытой фазы;
- при 429 и 5xx от DreamTeam повторяет через 10, 30 и 60 с, затем пишет `wake_failed`; каждое доставленное
  пробуждение — телеметрия `wake_sent` {reason, turn, phase, mode, via: `messages` | `continue`, to: `["all"]` |
  `["coordinator"]`, status — HTTP-код ответа DreamTeam, fallback — почему вместо сообщения пошёл `continue`};
- на game_over шлёт сессии ОДНО итоговое сообщение всем участникам (с `--wake-mode continue` — `continue`) с итогом
  (ход, победители, место, счёт): «Заверши цель одним коротким итоговым сообщением, больше ничего не делай»;
  остановленную сессию ради итога не запускает (`continue_skipped`, причина `not_running`); с `--stop-on-game-over` вместо этого останавливает сессию
  (`POST /api/v1/sessions/{id}/stop`, телеметрия `session_stopped_by_waker`); запущенный уже после game_over
  (в т. ч. перезапуск) не шлёт ни `continue`, ни stop (`continue_skipped`, `started_after_game_over`);
- место выбыло раньше game_over (`alive: false`, `eliminated: true` в `dah.phase()`): шлёт сессии ОДНО сообщение
  «[DAH] Ты выбыл (B): к ходу N провинций не осталось, партия для тебя окончена. Заверши цель одним итоговым
  сообщением, больше ничего не делай.», больше это место не будит и дальше ведёт себя как после game_over (то же
  наблюдение за расходом с `phase: after_game_over`: сервер принимает его от выбывшего места и до game_over,
  отчёт показывает «после выбытия (ход N)»; `--stop-on-game-over` останавливает сессию, телеметрия
  `wake_sent`/`continue_skipped` с `phase: eliminated`, `post_game_finished` с `eliminated: true`; запущенный уже
  после выбывания — `continue_skipped`, `started_after_eliminated`);
- после game_over каждые 30 с читает сводку сессии и шлёт новый расход с `phase: after_game_over`, пока сессия не
  остановится, или пока её цель не окажется выполненной (`goal.status` done) при сессии не в работе (не
  `running`/`starting`) и расходе, не менявшемся 60 с (причина `goal_done`), или пока она не затихнет (`done`/`budget` без нового расхода два чтения подряд; `waiting`/`paused` опрашивает
  дальше), но не дольше `--post-game-minutes` (30), затем пишет `post_game_finished` и
  завершается; `dah report` показывает эти деньги и время отдельно («после game_over»);
- пишет телеметрию `wake_sent`, `wake_answered`, `wake_unanswered`, `continue_skipped`, `session_restarted`,
  `goal_reopened`, `budget_exhausted`, `wake_failed`, `session_stopped_by_waker`, `post_game_finished` (прежние
  версии писали `continue_sent` вместо `wake_sent`).

Когда партию создают, ID сессии обычно ещё неизвестен. Waker записывает его сам при старте; чтобы записать без
waker'а, выполните `dah game set-session <GAME_ID> A 5f0c...` (можно и при работающем сервере). Если ID известен
заранее, `dah game create ... --dt-session A=5f0c...` запишет его при создании. Ни то, ни другое не заменяет
`--dt-session` самого waker'а.

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
ждёт фаз через `dah.wait_phase` (цель не достигнута, пока `dah.phase()` не вернул `game_over` или место не выбыло:
`alive: false`, `eliminated: true`).

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
