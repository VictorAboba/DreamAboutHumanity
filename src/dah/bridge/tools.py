"""MCP tools of dah-bridge: their schemas and the mapping of each call onto the game server's HTTP API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from dah.bridge.client import GameApiError, GameClient

DEFAULT_WAIT_SECONDS = 45

_ID = {"type": ["string", "integer"], "description": "ID приказа"}
_UNITS = {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0},
          "description": "Вложенная форма (если типов юнитов несколько): {юнит: число}"}
FLAT_MOVE = '{"kind": "move", "from": "P01", "to": "P04", "unit": "assault", "count": 2}'
NESTED_MOVE = '{"kind": "move", "from": "P01", "to": "P04", "units": {"assault": 2, "armor": 1}}'
STRING_ORDERS = 'orders="[{\\"kind\\":\\"move\\",\\"from\\":\\"P01\\",\\"to\\":\\"P04\\",\\"unit\\":\\"assault\\",\\"count\\":2}]"'
_ORDER_ITEM = {
    "type": "object",
    "description": "Один приказ. move лучше плоско: unit + count (один тип юнитов на приказ); несколько типов — "
                   "units {юнит: число} или несколько приказов",
    "properties": {
        "kind": {"type": "string", "enum": ["move", "recruit", "build", "fortify", "research", "dispose", "policy",
                                            "hire_shade", "spy", "recall", "ward", "discover"],
                 "description": "dispose/policy — только с завоеванием, hire_shade/spy/recall/ward и feint — только со "
                                "шпионажем, discover {province, choice} — выбор смешанной находки, только с Заброшенными "
                                "землями (rules покажет разделы "
                                "ЗАВОЕВАНИЕ, ШПИОНАЖ, ЗАБРОШЕННЫЕ)"},
        "from": {"type": "string", "description": "move: откуда (P01)"},
        "to": {"type": "string", "description": "move: куда, соседняя провинция (P04); dispose с освобождением: "
                                                "ник получателя"},
        "unit": {"type": "string", "description": "move/recruit: юнит — короткий ID (assault), полный "
                                                  "(core:assault) или метка из get_state (штурм)"},
        "count": {"type": "integer", "minimum": 1, "description": "move/recruit: сколько юнитов"},
        "units": _UNITS,
        "province": {"type": "string", "description": "recruit/build/fortify/ward (Застава): провинция"},
        "building": {"type": "string", "description": "build: здание (mine, barracks...)"},
        "tech": {"type": "string", "description": "research: технология — ID или название из list_techs "
                                                  "(только в Д, один приказ research на ход)"},
        "pact": {"type": "string", "enum": ["honor", "break"],
                 "description": "move: honor (по умолчанию) — соблюдать пакты: в провинцию союзника войска не "
                                "войдут, вернутся без боя; break — нарушить пакт с владельцем цели (с ничейной — с "
                                "союзником, идущим туда же): этот ход бьётся, пакт публично разорван"},
        "feint": {"type": "boolean", "description": "move, только в Д и со шпионажем: true — ложная запись (войска "
                                                    "остаются дома, стоит ОК)"},
        "disposition": {"type": ["string", "null"],
                        "description": "dispose/policy: судьба провинции — ID или название (subjugate, raze, raid, "
                                       "release; список — rules(topic=\"conquest\")); policy: null — без политики"},
        "shade": {"type": ["string", "integer"], "description": "spy/recall: Тень (\"s1\" или 1) из list_spies"},
        "mission": {"type": "string", "description": "spy: миссия — ID или название (observe — состав точно и кто "
                                                     "будет защищать провинцию; read_orders — удары по тебе и "
                                                     "нарушения пактов; ledger, secret, sabotage; список — "
                                                     "rules(topic=\"spying\"))"},
        "target": {"type": "string", "description": "spy: цель — провинция (P07) или ник игрока, по миссии"},
        "rounds": {"type": "integer", "minimum": 1, "description": "spy: срок задания в раундах (по одному за ход)"},
        "choice": {"type": "string", "description": "discover: вариант смешанной находки из строки НАХОДКА "
                                                    "(province — её провинция)"},
    },
    "required": ["kind"],
}
_EST_UNITS = {"anyOf": [{"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
                        {"type": "string"}],
              "description": "Юниты стороны: {юнит: число}, например {\"core:assault\": 4, \"core:armor\": 1}; "
                             "юнит — полный ID, короткий (assault) или метка из get_state (штурм). Или ID провинции "
                             "с видимыми войсками (своей или соседней, \"P05\") — берутся все войска в ней; "
                             "у defender ещё ID донесения Наблюдения (\"r3\") — гарнизон и найм по нему"}
EST_EXAMPLE = ('estimate(attacker={"core:assault": 4}, defender={"core:assault": 2}, province_type="core:agri", '
               'fortified=false)')
_ORDERS = {"type": "array", "items": _ORDER_ITEM,
           "description": f"Список приказов. Плоская форма (надёжнее всего): [{FLAT_MOVE}]. Вложенная тоже "
                          f"работает: [{NESTED_MOVE}]. Можно передать весь список одной JSON-строкой: "
                          f"{STRING_ORDERS}"}
_PATCH = {"type": "object",
          "description": "Что изменить: {\"unit\": \"assault\", \"count\": 1} (плоско), {\"units\": {...}}, "
                         "{\"to\": \"P03\"} или {\"count\": 1} у найма. Можно одной JSON-строкой: "
                         "patch=\"{\\\"unit\\\":\\\"assault\\\",\\\"count\\\":1}\"",
          "properties": {"unit": {"type": "string", "description": "move: юнит (плоская форма, вместе с count)"},
                         "count": {"type": "integer", "minimum": 1,
                                   "description": "move: сколько юнитов unit; recruit: новое число"},
                         "units": _UNITS,
                         "to": {"type": "string", "description": "move: новая цель; dispose: ник получателя"},
                         "disposition": {"type": ["string", "null"],
                                         "description": "dispose/policy: другая судьба (policy: null — снять)"},
                         "feint": {"type": "boolean", "description": "move: false — ложную запись сделать настоящей "
                                                                     "(в П2 — как новая атака)"},
                         "tech": {"type": "string", "description": "research: другая технология"},
                         "choice": {"type": "string", "description": "discover: другой вариант находки"},
                         "pact": {"type": "string", "enum": ["honor", "break"],
                                  "description": "move: honor или break (в П2 honor→break стоит как новая атака)"}}}
RESEARCH = '{"kind": "research", "tech": "deep_drilling"}'
_ORDERS_HINT = (f" Плоская форма move: {FLAT_MOVE}; исследование: {RESEARCH}; если вложенные объекты теряются — "
                "передай orders одной JSON-строкой.")
_LIST_OR_STRING = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]}
RULE_TOPICS = ("turn, setup, units, buildings, orders, p2, combat, pacts, trade, spying, conquest, discoveries, fog, "
               "messages, techs, factions, artifacts, abilities, resources, provinces, victory (очки, стройки, тайные "
               "цели)")
DISPOSE = '{"kind": "dispose", "province": "P07", "disposition": "subjugate"}'
SPY = '{"kind": "spy", "shade": "s1", "mission": "observe", "target": "P07", "rounds": 2}'
SPY_ONLY = " Только в партиях со шпионажем."
TRADE_ONLY = " Только в партиях с торговлей."
_GOODS = {"anyOf": [{"type": "object"}, {"type": "string"}],
          "description": "{ресурс: число}, например {\"alloy\": 10}; провинция {\"P07\": 1}, своё донесение "
                         "(только в give) {\"r4\": 1}; пусто или {} — ничего"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("whoami", "Кто ты в игре: партия, место, ник, фракция, ход и фаза.", _schema()),
    ToolSpec("rules", "Правила игры: без topic — краткое ядро с оглавлением; topic — раздел целиком, собранный из "
                      f"контента партии ({RULE_TOPICS}). Пример: rules(topic=\"techs\").",
             _schema({"topic": {"type": "string", "description": f"Раздел правил: {RULE_TOPICS}"}})),
    ToolSpec("phase", "Текущая фаза (setup — фаза старта перед ходом 1, затем П1/Д/П2): ход, дедлайн, готовность, "
                      "разрешённые действия, game_over, подсказка, как нажать ready; в Д и П2 — ещё orders_hash для "
                      "ready(confirm_hash=...). После game_over — result: победители, итог (scores), разбивка "
                      "(score_parts) и раскрытые тайные цели.", _schema()),
    ToolSpec("get_setup", "Фаза старта: все фракции с описаниями, предложенные тебе артефакты (драфт) или пул и "
                          "бюджет (очки), твой текущий выбор, твоя тайная цель (если она есть в партии; под неё можно "
                          "выбрать фракцию); после фазы — итог и фракции всех игроков. Чужой выбор и чужие цели не "
                          "видны.", _schema()),
    ToolSpec("choose_faction", "Фаза старта: фракции по предпочтению (лучшая первой; ID или название). При "
                               "совпадении с соперником спор решает жребий, проигравший получает следующую из списка. "
                               "Пример: choose_faction(prefs=[\"litany\", \"captains\"]).",
             _schema({"prefs": {**_LIST_OR_STRING, "description": "Список фракций по предпочтению (или одна)"}},
                     ["prefs"])),
    ToolSpec("choose_artifacts", "Фаза старта: артефакты — в драфте один из предложенных, по очкам — любые в пределах "
                                 "бюджета. Пример: choose_artifacts(ids=[\"ancient_blueprint\"]).",
             _schema({"ids": {**_LIST_OR_STRING, "description": "ID или названия артефактов из get_setup"}},
                     ["ids"])),
    ToolSpec("list_techs", "Твои технологии коротко: изученные, активная (прогресс/цена), доступные сейчас (цена, "
                           "эффект), очки и доход ресурса исследований, очки науки к счёту и прогресс веток (если наука "
                           "приносит очки). full=true — всё дерево с закрытыми и их "
                           f"требованиями (примерно вдвое длиннее). Изучать — приказ {RESEARCH} в фазе Д.",
             _schema({"full": {"type": "boolean", "default": False,
                               "description": "true — всё дерево с закрытыми технологиями и требованиями"}})),
    ToolSpec("describe", "Полное описание любого контента партии по ID или названию: юнит, здание, технология, "
                         "артефакт, фракция, ветка, ресурс, тип провинции, тайная цель пула (что нужно и сколько "
                         "очков), великая стройка (очки, где строить), находка (эффекты, шансы, сила, EV вариантов). Пример: "
                         "describe(id=\"throne_warden\").",
             _schema({"id": {"type": "string", "description": "ID (assault, core:assault) или название (можно начало)"}}, ["id"])),
    ToolSpec("wait_phase", "Ждать смены фазы (до max_seconds, по умолчанию 45). Вернётся и раньше, если чьё-то "
                          "предложение (обмен, пакт) сняло твою готовность: reason \"ready_cleared\" — ответь на "
                          "него и снова нажми ready.",
             _schema({"max_seconds": {"type": "integer", "minimum": 1, "maximum": 55, "default": 45}})),
    ToolSpec("get_state", "Состояние твоей державы и видимой карты. Без аргументов — всё, со строкой «Счёт:» (твой "
                          "счёт с разбивкой, у соперников «≥» — только видимая часть (наука, война, разведка, цель "
                          "скрыты), твоя тайная цель и "
                          "выполнена ли она сейчас; после партии — «Итог:» всех), строками НАХОДКА (смешанная "
                          "находка ждёт приказа discover) и ТЕНИ (свободная Тень); since_turn=N — только изменения "
                          "с хода N (обычно прошлый ход; 0 — всё). Пример: get_state(since_turn=2).",
             _schema({"since_turn": {"type": "integer", "minimum": 0,
                                     "description": "Ход 1..текущий: показать только изменения с него; 0 или без "
                                                    "него — всё состояние"},
                      "format": {"type": "string", "enum": ["text", "json"], "default": "text",
                                 "description": "text (коротко, по умолчанию) или json"}})),
    ToolSpec("list_orders", "Твои поданные приказы, их хэш и остаток ОК.", _schema()),
    ToolSpec("validate_orders", "Проверить приказы без подачи (в любой фазе, в П1 — как будто сейчас Д): причина "
                                "отказа, цена в ОК, предупреждения (ход в провинцию союзника по пакту вернётся; вход в "
                                "Заброшенную провинцию может запустить находку)."
                                + _ORDERS_HINT,
             _schema({"orders": _ORDERS}, ["orders"])),
    ToolSpec("submit_orders", "Подать приказы — только в фазе Д (в П2 — append за ОК; в П1 нельзя, только "
                              "validate_orders); mode: replace_all заменяет все, append добавляет." + _ORDERS_HINT,
             _schema({"orders": _ORDERS,
                      "mode": {"type": "string", "enum": ["replace_all", "append"], "default": "replace_all"}},
                     ["orders"])),
    ToolSpec("cancel_order", "Отменить приказ по ID (в Д бесплатно, в П2 стоит ОК; в П1 нельзя).",
             _schema({"id": _ID}, ["id"])),
    ToolSpec("amend_order", "Изменить приказ (в Д бесплатно, в П2 стоит ОК; в П1 нельзя): у move — юниты (плоско "
                            "unit+count или units), to, pact и/или feint, у recruit — count, у dispose — "
                            "disposition и/или to, у policy — disposition, у discover — choice. Пример: amend_order(id=\"o3\", "
                            "patch={\"unit\": \"assault\", \"count\": 1}); patch можно передать одной JSON-строкой.",
             _schema({"id": _ID, "patch": _PATCH}, ["id", "patch"])),
    ToolSpec("estimate", "Прогноз боя: вероятность победы атакующего и средние потери сторон (200 прогонов). "
                         f"Пример: {EST_EXAMPLE}.",
             _schema({"attacker": _EST_UNITS, "defender": _EST_UNITS,
                      "province_type": {"type": "string",
                                        "description": "Где бой: тип провинции (core:forge, core:agri, core:waste, "
                                                       "core:capital или метка из get_state: кузн., агро, пуст., "
                                                       "столица) либо ID провинции (P07); без него, если "
                                                       "defender — ID провинции, бой в ней"},
                      "fortified": {"type": "boolean",
                                    "description": "Защитник укрепился (fortify); без него — нет, а при "
                                                   "defender=\"r3\" — по донесению"},
                      "hasty": {"type": "boolean", "description": "Атака подана в П2 (наспех, штраф); без него — нет"},
                      "side": {"type": "string", "enum": ["attacker", "defender"],
                               "description": "Твоя сторона: на ней действуют твои технологии, артефакты и здания. "
                                              "Без него — по ID провинций (своя — твоя сторона, чужая армия у "
                                              "attacker — ты защищаешься), иначе attacker"},
                      "opponent": {"type": "string",
                                   "description": "Ник соперника: его фракция (публична) на другой стороне"}},
                     ["attacker", "defender"])),
    ToolSpec("list_spies", "Твои Тени коротко: S и C, Тени и их задания (раунд, прикрытие K, опыт V, последнее "
                           f"донесение), миссии (сложность D, срок, нужное S), в П2 — пойманные у тебя.{SPY_ONLY} "
                           f"Задание — приказ {SPY}.", _schema()),
    ToolSpec("get_intel", "Донесения Теней (снимок конца Д, числа — интервалы с правдой внутри): без аргументов — "
                          "свёрнутые донесения последнего разведсреза; report_id — одно донесение целиком "
                          "(купленная копия — \"c1\"); turn — "
                          f"прошлый ход; shade — одна Тень.{SPY_ONLY} Пример: get_intel(report_id=\"r4\").",
             _schema({"turn": {"type": ["integer", "string"], "description": "Ход разведсреза (по умолчанию последний)"},
                      "shade": {"type": ["string", "integer"], "description": "Тень: \"s1\" или 1"},
                      "report_id": {"type": "string", "description": "ID донесения (r4 или #r4) — полный текст"}})),
    ToolSpec("estimate_mission", "Шансы задания Тени по точной формуле: по раундам успех/поимка, дожить до конца, "
                                 "хотя бы одно донесение (удар). Чужое C скрыто: принимается база, если не задать "
                                 f"assume_mirrors.{SPY_ONLY} Пример: estimate_mission(mission=\"read_orders\", "
                                 "target=\"Ник\", rounds=3).",
             _schema({"mission": {"type": "string", "description": "Миссия: ID или название"},
                      "target": {"type": "string", "description": "Провинция (P07) или ник игрока"},
                      "rounds": {"type": ["integer", "string"], "description": "Срок в раундах"},
                      "shade": {"type": ["string", "integer"], "description": "Твоя Тень (её опыт V)"},
                      "assume_mirrors": {"type": ["integer", "string"],
                                         "description": "Предполагаемое C владельца цели"}},
                     ["mission", "target"])),
    ToolSpec("send_message", "Отправить сообщение (только в П1 и П2): to — список ников или \"all\". В ответе "
                             "inbox_id — номер этого сообщения в твоих входящих (message_id — счёт твоих "
                             "отправленных, не номер для inbox(since=)). "
                             "Пример: send_message(to=[\"Ник\"], text=\"Предлагаю мир\").",
             _schema({"to": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                                       {"type": "string", "enum": ["all"]}]},
                      "text": {"type": "string", "maxLength": 800}}, ["to", "text"])),
    ToolSpec("inbox", "Входящие: сообщения игроков (обёрнуты untrusted), записи [ИГРА] от самой игры "
                      "(подношения, пакты) и твои отправленные (from: \"ты\", own: true — их видят все участники "
                      "роя). По умолчанию — только новые с прошлого вызова inbox; all=true или "
                      "since=0 — вся история; since=N — новее номера N.",
             _schema({"since": {"type": ["integer", "string"], "description": "Номер последнего прочитанного; 0 — всё"},
                      "all": {"type": "boolean", "default": False, "description": "Вся история входящих"}})),
    ToolSpec("send_gift", "Подношение (только П1 и П2): сразу отдать игроку свои свободные (не в резерве приказов) "
                          "ресурсы. Пример: send_gift(to=\"Ник\", resources={\"alloy\": 10}).",
             _schema({"to": {"type": "string", "description": "Ник получателя"},
                      "resources": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1},
                                    "description": "{ресурс: число}, например {\"alloy\": 10, \"provisions\": 5}"}},
                     ["to", "resources"])),
    ToolSpec("propose_pact", "Предложить пакт (только П1 и П2): армии сторон при встрече не сражаются. turns — на "
                             "сколько расчётов, считая текущий ход; без turns — бессрочно. Партнёру по действующему "
                             "пакту со сроком это предложение продлить: turns добавятся к концу нынешнего срока (без "
                             "разрыва), без turns — пакт станет бессрочным; бессрочный пакт продлевать нечего. "
                             "Пример: propose_pact(to=\"Ник\", turns=3).",
             _schema({"to": {"type": "string", "description": "Ник"},
                      "turns": {"type": ["integer", "null"], "minimum": 1,
                                "description": "Число ходов; null или без него — бессрочно"}}, ["to"])),
    ToolSpec("respond_pact", "Ответить на предложение пакта или его продления (только П1 и П2) до конца хода: "
                             "decision accept или reject. У продления свой ID (не ID самого пакта) — бери его из "
                             "входящих или list_pacts. Пример: respond_pact(id=\"pact-A-B-1\", decision=\"accept\").",
             _schema({"id": {"type": "string", "description": "ID пакта из входящих или list_pacts"},
                      "decision": {"type": "string", "enum": ["accept", "reject"]}}, ["id", "decision"])),
    ToolSpec("leave_pact", "Выйти из пакта (объявляется всем; из П1 — пакт не действует уже в расчёте этого хода, "
                           "из П2 — со следующего; ждущее продление при этом отменяется; выход до конца срока "
                           "может стоить очков — rules(topic=\"pacts\")) или отозвать своё предложение, в том "
                           "числе продления (по его ID). Пример: "
                           "leave_pact(id=\"pact-A-B-1\").",
             _schema({"id": {"type": "string", "description": "ID пакта"}}, ["id"])),
    ToolSpec("list_pacts", "Твои пакты и предложения (с ID) и все действующие пакты других (публично).", _schema()),
    ToolSpec("propose_trade", "Предложить обмен (только П1 и П2): give — что отдаёшь, want — что просишь; одна "
                              "сторона может быть пустой; провинция переходит в расчёте этого хода (ресурсы — в "
                              "залоге до него), донесение — заверенной копией. Тому же игроку — одно открытое предложение: заменить — "
                              "replaces=id, встречное на чужое — reply_to=id. Ответ — до конца хода. later — обещание "
                              "на будущее {\"give\": {...}, \"want\": {...}, \"by_turn\": N}: игра его не "
                              "исполняет, только помнит; fulfils=id — пометка «это исполнение обещания обмена id»."
                              f"{TRADE_ONLY} Пример: propose_trade(to=\"Ник\", give={{\"alloy\": 10}}, "
                              "want={\"provisions\": 15}).",
             _schema({"to": {"type": "string", "description": "Ник"}, "give": _GOODS, "want": _GOODS,
                      "later": {"anyOf": [{"type": "object"}, {"type": "string"}],
                                "description": "Обещание: {\"give\": {...}, \"want\": {...}, \"by_turn\": N}"},
                      "reply_to": {"type": "string", "description": "ID предложения тебе, на которое это встречное"},
                      "replaces": {"type": "string", "description": "ID твоего открытого предложения, которое "
                                                                    "это заменяет"},
                      "fulfils": {"type": "string", "description": "ID обмена, обещание которого это исполняет"}},
                     ["to"])),
    ToolSpec("respond_trade", "Ответить на предложение обмена (только П1 и П2) до конца хода: accept — обмен "
                              "исполняется сразу, если у обоих хватает свободного (с провинцией — в расчёте хода); reject — отказ, а для своего "
                              f"предложения — отзыв.{TRADE_ONLY} Пример: respond_trade(id=\"trade-A-B-1\", "
                              "decision=\"accept\").",
             _schema({"id": {"type": "string", "description": "ID обмена из входящих или list_trades"},
                      "decision": {"type": "string", "enum": ["accept", "reject"]}}, ["id", "decision"])),
    ToolSpec("list_trades", f"Твои предложения обмена, сделки последних ходов и обещания (в любой фазе).{TRADE_ONLY}",
             _schema()),
    ToolSpec("ready", "Нажать «готово», как только в фазе сказать и сделать больше нечего (фаза кончается, когда "
                      "готовы все). Старт и П1 — ready() без confirm_hash. Д и П2 — ready(confirm_hash=orders_hash "
                      "твоих текущих приказов): он есть в ответе submit_orders, в phase() и list_orders(); после "
                      "правки приказов — новый.",
             _schema({"confirm_hash": {"type": "string", "description": "Д и П2: orders_hash из phase(), "
                                                                         "list_orders() или submit_orders; в фазе "
                                                                         "старта и П1 не нужен"}})),
    ToolSpec("unready", "Снять «готово».", _schema()),
    ToolSpec("history", "Отчёт рассчитанного хода (события и твой счёт после хода); без turn — последний. До "
                        "расчёта хода 1 отчётов нет. "
                        "Пример: history(turn=1).",
             _schema({"turn": {"type": "integer", "minimum": 1,
                               "description": "Номер уже рассчитанного хода (1..текущий-1)"}})),
)


class ToolInputError(Exception):
    pass


def _json_arg(value: Any, kind: type, name: str) -> Any:
    """Arguments documented as JSON arrays/objects; some clients send them as JSON strings."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ToolInputError(f"{name}: ожидался JSON{_json_arg_hint(name)}") from None
    if kind is list and isinstance(value, dict) and "kind" in value:
        value = [value]     # one order without the surrounding list
    if not isinstance(value, kind):
        raise ToolInputError(f"{name}: ожидался {'список' if kind is list else 'объект'}{_json_arg_hint(name)}")
    return value


def _json_arg_hint(name: str) -> str:
    if name == "orders":
        return f". Пример: orders=[{FLAT_MOVE}] или {STRING_ORDERS}"
    if name == "patch":
        return '. Пример: patch={"unit": "assault", "count": 1} или patch={"to": "P03"}'
    return ""


EXAMPLES = {
    "send_message": 'send_message(to=["Ник"], text="Предлагаю мир") или send_message(to="all", text="...")',
    "validate_orders": f"validate_orders(orders=[{FLAT_MOVE}]) или validate_orders({STRING_ORDERS})",
    "submit_orders": f'submit_orders(orders=[{FLAT_MOVE}], mode="replace_all") или '
                     f"submit_orders({STRING_ORDERS})",
    "cancel_order": 'cancel_order(id="o3")',
    "amend_order": 'amend_order(id="o3", patch={"unit": "assault", "count": 1}) или '
                   'amend_order(id="o3", patch={"to": "P03"})',
    "ready": 'в П1 и фазе старта — ready(); в Д и П2 — ready(confirm_hash="<orders_hash из phase() или list_orders>")',
    "choose_faction": 'choose_faction(prefs=["litany", "captains"])',
    "choose_artifacts": 'choose_artifacts(ids=["ancient_blueprint"])',
    "describe": 'describe(id="throne_warden")',
    "estimate": EST_EXAMPLE,
    "get_intel": 'get_intel() или get_intel(report_id="r4")',
    "estimate_mission": 'estimate_mission(mission="observe", target="P07", rounds=2)',
    "send_gift": 'send_gift(to="Ник", resources={"alloy": 10})',
    "propose_pact": 'propose_pact(to="Ник", turns=3) или propose_pact(to="Ник")',
    "respond_pact": 'respond_pact(id="pact-A-B-1", decision="accept")',
    "leave_pact": 'leave_pact(id="pact-A-B-1")',
    "propose_trade": 'propose_trade(to="Ник", give={"alloy": 10}, want={"provisions": 15})',
    "respond_trade": 'respond_trade(id="trade-A-B-1", decision="accept")',
}


def _required(args: dict[str, Any], name: str) -> Any:
    if args.get(name) is None:
        raise ToolInputError(f"не указан аргумент {name}")
    return args[name]


def _lenient_json(value: Any, *, lists_only: bool = False) -> Any:
    """A JSON string becomes its value; anything else (an object, a list, a malformed string) goes to the server
    as is, which accepts the forms it documents and explains the rest with a correct example. `lists_only`: only a
    string that looks like a JSON list is decoded (a plain id stays a string)."""
    if isinstance(value, str) and (not lists_only or value.strip().startswith("[")):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# get_setup and list_techs answer with a full text plus JSON that repeats it (every faction, artifact and tech with
# descriptions, ~9k characters): the model gets the text and only these fields of the JSON.
SETUP_KEEP = ("phase", "open", "seconds_left", "you_ready", "artifacts_mode", "your_choice", "confirm_hash", "result",
              "players", "warning")
TECHS_KEEP = ("enabled", "points", "income", "active", "locked")


def _compact(data: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(data.get("text"), str):
        return data
    return {"text": data["text"], **{k: data[k] for k in keep if data.get(k) not in (None, [], {})}}


def _flag(value: Any) -> bool:
    """A boolean argument; some clients send "true"/"1" as strings."""
    return value is True or str(value).strip().lower() in ("true", "1", "yes", "да")


def _first(args: dict[str, Any], *names: str) -> Any:
    """The documented argument or one of its likely spellings; refuses when none is given."""
    for name in names:
        if args.get(name) not in (None, ""):
            return args[name]
    raise ToolInputError(f"не указан аргумент {names[0]}")


def _any(args: dict[str, Any], *names: str) -> Any:
    """The documented optional argument or one of its likely spellings; None when none is given."""
    return next((args[n] for n in names if args.get(n) not in (None, "")), None)


class Bridge:
    """Runs one tool call against the game server and turns the answer into text for the model."""

    def __init__(self, client: GameClient):
        self._client = client
        self.last_phase_seq: int | None = None
        self.last_inbox_id = 0     # the newest inbox entry this bridge process has returned
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "whoami": lambda a: client.whoami(),
            "rules": lambda a: client.rules(a.get("topic")),
            "phase": lambda a: self._remember(client.phase()),
            "wait_phase": self._wait_phase,
            "get_state": lambda a: client.state(a.get("since_turn"), a.get("format")),
            "list_orders": lambda a: client.list_orders(),
            "validate_orders": lambda a: client.validate_orders(_json_arg(_required(a, "orders"), list, "orders")),
            "submit_orders": lambda a: client.submit_orders(_json_arg(_required(a, "orders"), list, "orders"),
                                                            a.get("mode") or "replace_all"),
            "cancel_order": lambda a: client.cancel_order(str(_required(a, "id"))),
            "amend_order": lambda a: client.amend_order(str(_required(a, "id")),
                                                        _json_arg(_required(a, "patch"), dict, "patch")),
            # estimate: the server checks the arguments and answers with what was wrong and a correct call.
            "estimate": lambda a: client.estimate(_lenient_json(a.get("attacker")), _lenient_json(a.get("defender")),
                                                  a.get("province_type"), a.get("fortified"),
                                                  a.get("hasty"), a.get("side"), a.get("opponent")),
            "get_setup": lambda a: _compact(client.get_setup(), SETUP_KEEP),
            "choose_faction": lambda a: client.choose_faction(
                _lenient_json(_first(a, "prefs", "factions", "faction"), lists_only=True)),
            "choose_artifacts": lambda a: client.choose_artifacts(
                _lenient_json(_first(a, "ids", "artifacts", "artifact"), lists_only=True)),
            "list_techs": lambda a: _compact(client.list_techs(full=True) if _flag(a.get("full"))
                                             else client.list_techs(), TECHS_KEEP),
            "describe": lambda a: client.describe(str(_first(a, "id", "name", "ref"))),
            "send_message": lambda a: client.send_message(_required(a, "to"), str(_required(a, "text"))),
            "inbox": self._inbox,
            "send_gift": lambda a: client.send_gift(_required(a, "to"), _lenient_json(_required(a, "resources"))),
            "list_pacts": lambda a: client.list_pacts(),
            "propose_pact": lambda a: client.propose_pact(_required(a, "to"), a.get("turns")),
            "respond_pact": lambda a: client.respond_pact(str(_required(a, "id")), _required(a, "decision")),
            "leave_pact": lambda a: client.leave_pact(str(_required(a, "id"))),
            # trade: the server reads the forms it documents (flat or nested goods, a JSON string)
            "list_trades": lambda a: _compact(client.list_trades(), ()),
            "propose_trade": lambda a: client.propose_trade(
                _required(a, "to"), _lenient_json(a.get("give")), _lenient_json(a.get("want")),
                _lenient_json(a.get("later")), _any(a, "reply_to"), _any(a, "replaces"), _any(a, "fulfils")),
            "respond_trade": lambda a: client.respond_trade(str(_first(a, "id", "trade_id")), _required(a, "decision")),
            "ready": lambda a: client.ready(None if a.get("confirm_hash") in (None, "") else str(a["confirm_hash"])),
            "unready": lambda a: client.unready(),
            "history": lambda a: client.history(a.get("turn")),
            # espionage reads: the server's text is the whole answer (its JSON repeats it)
            "list_spies": lambda a: _compact(client.list_spies(), ()),
            "get_intel": lambda a: _compact(client.get_intel(_any(a, "turn"), _any(a, "shade", "shade_id"),
                                                             _any(a, "report_id", "report", "id")), ()),
            "estimate_mission": lambda a: _compact(client.estimate_mission(
                _first(a, "mission", "type"), _first(a, "target", "province", "seat", "player"),
                _any(a, "rounds", "turns"), _any(a, "shade"), _any(a, "assume_mirrors", "mirrors")), ()),
        }

    def _remember(self, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data.get("phase_seq"), int):
            self.last_phase_seq = data["phase_seq"]
        return data

    def _inbox(self, args: dict[str, Any]) -> dict[str, Any]:
        """Only entries newer than the last one this process returned (like wait_phase's phase_seq); `all` or
        `since=0` gives the whole history, `since=N` the entries after N."""
        since = args.get("since")
        if isinstance(since, str) and since.strip().isascii() and since.strip().isdigit() and len(since.strip()) < 10:
            since = int(since.strip())
        everything = args.get("all") is True or str(args.get("all")).strip().lower() == "true"
        if everything or since == 0:
            data = self._client.inbox(None)
        elif since is not None:
            data = self._client.inbox(since)
        else:
            data = self._client.inbox(self.last_inbox_id or None)
        # the cursor moves only past entries actually returned: a guessed `since` beyond the newest entry must not
        # make later default calls skip messages that arrive afterwards
        ids = [m["id"] for m in data.get("messages") or [] if isinstance(m, dict) and isinstance(m.get("id"), int)]
        if ids:
            self.last_inbox_id = max(self.last_inbox_id, max(ids))
        return data

    def _wait_phase(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.last_phase_seq is None:
            # Nothing seen yet: the current phase is already news to the caller.
            return self._remember({**self._client.phase(), "changed": True})
        seconds = args.get("max_seconds") or DEFAULT_WAIT_SECONDS
        return self._remember(self._client.wait_phase(self.last_phase_seq, seconds))

    def call(self, name: str, args: dict[str, Any] | None) -> tuple[str, bool]:
        """(text, is_error) of one tool call."""
        handler = self._handlers.get(name)
        if handler is None:
            return f"Неизвестный инструмент: {name}", True
        try:
            data = handler(dict(args or {}))
        except ToolInputError as exc:
            example = f". Пример: {EXAMPLES[name]}" if name in EXAMPLES else ""
            return f"Ошибка аргументов: {exc}{example}", True
        except GameApiError as exc:
            return self._client.scrub(f"Ошибка {exc.code}: {exc.message}".rstrip(": ")), True
        return self._client.scrub(render(data)), False


def render(data: dict[str, Any]) -> str:
    """The server's ready-made `text` when there is one, else compact JSON."""
    if isinstance(data.get("text"), str) and len(data) == 1:
        return data["text"]
    if isinstance(data.get("text"), str):
        rest = {k: v for k, v in data.items() if k != "text"}
        return f"{data['text']}\n{json.dumps(rest, ensure_ascii=False, separators=(',', ':'))}"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
