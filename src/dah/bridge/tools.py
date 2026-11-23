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
    ToolSpec("get_state", "Состояние твоей державы и видимой карты; since_turn — только изменения с хода.",
             _schema({"since_turn": {"type": "integer", "minimum": 0},
                      "format": {"type": "string", "enum": ["text", "json"], "default": "text"}})),
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
    ToolSpec("estimate", "Оценить исход боя: вероятность победы и средние потери.",
             _schema({"attacker": _UNITS, "defender": _UNITS,
                      "province_type": {"type": "string", "description": "Тип провинции, например core:forge"},
                      "fortified": {"type": "boolean", "default": False},
                      "hasty": {"type": "boolean", "default": False, "description": "Атака начата в П2"}},
                     ["attacker", "defender", "province_type"])),
    ToolSpec("send_message", "Отправить сообщение: to — список ников или \"all\".",
             _schema({"to": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                                       {"type": "string", "enum": ["all"]}]},
                      "text": {"type": "string", "maxLength": 800}}, ["to", "text"])),
    ToolSpec("inbox", "Входящие сообщения; since — только новее этой отметки.",
             _schema({"since": {"type": ["integer", "string"]}})),
    ToolSpec("ready", "Нажать «готово»; confirm_hash — orders_hash твоих текущих приказов.",
             _schema({"confirm_hash": {"type": "string"}}, ["confirm_hash"])),
    ToolSpec("unready", "Снять «готово».", _schema()),
    ToolSpec("history", "Отчёт хода; без turn — последний.",
             _schema({"turn": {"type": "integer", "minimum": 1}})),
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


def _required(args: dict[str, Any], name: str) -> Any:
    if args.get(name) is None:
        raise ToolInputError(f"не указан аргумент {name}")
    return args[name]


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
            "estimate": lambda a: client.estimate(_json_arg(_required(a, "attacker"), dict, "attacker"),
                                                  _json_arg(_required(a, "defender"), dict, "defender"),
                                                  str(_required(a, "province_type")),
                                                  bool(a.get("fortified", False)), bool(a.get("hasty", False))),
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
            return f"Ошибка аргументов: {exc}", True
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
