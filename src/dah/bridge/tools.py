"""MCP tools of dah-bridge: their schemas and the mapping of each call onto the game server's HTTP API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from dah.bridge.client import GameApiError, GameClient

DEFAULT_WAIT_SECONDS = 45

_ID = {"type": ["string", "integer"], "description": "ID приказа"}
_UNITS = {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0},
          "description": "Юниты: {unit_id: число}"}
_EST_UNITS = {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0},
              "description": "Юниты стороны: {юнит: число}, например {\"core:assault\": 4, \"core:armor\": 1}; "
                             "юнит — полный ID, короткий (assault) или метка из get_state (штурм)"}
EST_EXAMPLE = ('estimate(attacker={"core:assault": 4}, defender={"core:assault": 2}, province_type="core:agri", '
               'fortified=false)')
_ORDERS = {"type": "array", "items": {"type": "object"},
           "description": "Список приказов, например [{\"kind\": \"move\", \"from\": \"P01\", \"to\": \"P02\", "
                          "\"units\": {\"core:assault\": 2}}]"}


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
    ToolSpec("rules", "Правила игры кратко; topic — раздел правил.",
             _schema({"topic": {"type": "string", "description": "Раздел правил"}})),
    ToolSpec("phase", "Текущая фаза: ход, дедлайн, готовность, разрешённые действия, game_over.", _schema()),
    ToolSpec("wait_phase", "Ждать смены фазы (до max_seconds, по умолчанию 45).",
             _schema({"max_seconds": {"type": "integer", "minimum": 1, "maximum": 55, "default": 45}})),
    ToolSpec("get_state", "Состояние твоей державы и видимой карты. Без аргументов — всё; since_turn=N — только "
                          "изменения с хода N (обычно прошлый ход; 0 — всё). Пример: get_state(since_turn=2).",
             _schema({"since_turn": {"type": "integer", "minimum": 0,
                                     "description": "Ход 1..текущий: показать только изменения с него; 0 или без "
                                                    "него — всё состояние"},
                      "format": {"type": "string", "enum": ["text", "json"], "default": "text",
                                 "description": "text (коротко, по умолчанию) или json"}})),
    ToolSpec("list_orders", "Твои поданные приказы, их хэш и остаток ОК.", _schema()),
    ToolSpec("validate_orders", "Проверить приказы без подачи: причина отказа и цена в ОК.",
             _schema({"orders": _ORDERS}, ["orders"])),
    ToolSpec("submit_orders", "Подать приказы; mode: replace_all заменяет все, append добавляет.",
             _schema({"orders": _ORDERS,
                      "mode": {"type": "string", "enum": ["replace_all", "append"], "default": "replace_all"}},
                     ["orders"])),
    ToolSpec("cancel_order", "Отменить приказ по ID (в П2 стоит ОК).", _schema({"id": _ID}, ["id"])),
    ToolSpec("amend_order", "Изменить приказ: units, count или to (в П2 стоит ОК).",
             _schema({"id": _ID, "patch": {"type": "object", "description": "{units?, count?, to?}",
                                           "properties": {"units": _UNITS, "count": {"type": "integer"},
                                                          "to": {"type": "string"}}}},
                     ["id", "patch"])),
    ToolSpec("estimate", "Прогноз боя: вероятность победы атакующего и средние потери сторон (200 прогонов). "
                         f"Пример: {EST_EXAMPLE}.",
             _schema({"attacker": _EST_UNITS, "defender": _EST_UNITS,
                      "province_type": {"type": "string",
                                        "description": "Где бой: тип провинции (core:forge, core:agri, core:waste, "
                                                       "core:capital или метка из get_state: кузн., агро, пуст., "
                                                       "столица) либо ID провинции (P07)"},
                      "fortified": {"type": "boolean", "default": False,
                                    "description": "Защитник укрепился (fortify)"},
                      "hasty": {"type": "boolean", "default": False,
                                "description": "Атака подана в П2 (наспех, штраф)"}},
                     ["attacker", "defender", "province_type"])),
    ToolSpec("send_message", "Отправить сообщение (только в П1 и П2): to — список ников или \"all\". "
                             "Пример: send_message(to=[\"Ник\"], text=\"Предлагаю мир\").",
             _schema({"to": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                                       {"type": "string", "enum": ["all"]}]},
                      "text": {"type": "string", "maxLength": 800}}, ["to", "text"])),
    ToolSpec("inbox", "Входящие сообщения; since — только новее этой отметки.",
             _schema({"since": {"type": ["integer", "string"]}})),
    ToolSpec("ready", "Нажать «готово»; confirm_hash — orders_hash твоих текущих приказов.",
             _schema({"confirm_hash": {"type": "string"}}, ["confirm_hash"])),
    ToolSpec("unready", "Снять «готово».", _schema()),
    ToolSpec("history", "Отчёт рассчитанного хода; без turn — последний. До расчёта хода 1 отчётов нет. "
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
            raise ToolInputError(f"{name}: ожидался JSON") from None
    if not isinstance(value, kind):
        raise ToolInputError(f"{name}: ожидался {'список' if kind is list else 'объект'}")
    return value


EXAMPLES = {
    "send_message": 'send_message(to=["Ник"], text="Предлагаю мир") или send_message(to="all", text="...")',
    "validate_orders": 'validate_orders(orders=[{"kind": "move", "from": "P01", "to": "P02", '
                       '"units": {"core:assault": 2}}])',
    "submit_orders": 'submit_orders(orders=[{"kind": "move", "from": "P01", "to": "P02", '
                     '"units": {"core:assault": 2}}], mode="replace_all")',
    "cancel_order": 'cancel_order(id="o3")',
    "amend_order": 'amend_order(id="o3", patch={"to": "P03"})',
    "ready": 'ready(confirm_hash="<orders_hash из list_orders>")',
    "estimate": EST_EXAMPLE,
}


def _required(args: dict[str, Any], name: str) -> Any:
    if args.get(name) is None:
        raise ToolInputError(f"не указан аргумент {name}")
    return args[name]


def _lenient_json(value: Any) -> Any:
    """A JSON string becomes its value; anything else (an object, a list, a malformed string) goes to the server
    as is, which accepts the forms it documents and explains the rest with a correct example."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class Bridge:
    """Runs one tool call against the game server and turns the answer into text for the model."""

    def __init__(self, client: GameClient):
        self._client = client
        self.last_phase_seq: int | None = None
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
                                                  a.get("province_type"), a.get("fortified", False),
                                                  a.get("hasty", False)),
            "send_message": lambda a: client.send_message(_required(a, "to"), str(_required(a, "text"))),
            "inbox": lambda a: client.inbox(a.get("since")),
            "ready": lambda a: client.ready(str(_required(a, "confirm_hash"))),
            "unready": lambda a: client.unready(),
            "history": lambda a: client.history(a.get("turn")),
        }

    def _remember(self, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data.get("phase_seq"), int):
            self.last_phase_seq = data["phase_seq"]
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
