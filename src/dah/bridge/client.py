"""HTTP client of the game server API (SLICE.md section 7), shared by dah-bridge and dah-waker."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

MAX_WAIT_SECONDS = 55
WAIT_SLACK_SECONDS = 15.0


class GameApiError(Exception):
    """A failed call: an `{ok: false}` answer, a non-JSON answer or a transport failure."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


HOST_HEADER = "X-DAH-Host"


class GameClient:
    """Thin wrapper over httpx.Client with the seat key as a Bearer token. The key never leaves the header.

    `host` names a host program (dah-waker): its calls are journaled apart from the player's.
    """

    def __init__(self, server: str, key: str, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0, host: str | None = None):
        self._key = key
        headers = {"Authorization": f"Bearer {key}"}
        if host:
            headers[HOST_HEADER] = host
        self._http = httpx.Client(base_url=server.rstrip("/"), transport=transport, timeout=timeout,
                                  headers=headers)

    def close(self) -> None:
        self._http.close()

    def scrub(self, text: str) -> str:
        """Remove the key from any text that might leave the process."""
        return text.replace(self._key, "***") if self._key else text

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        """The answer without its `ok` flag; raises GameApiError otherwise."""
        query = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._http.request(method, path, params=query or None, json=body,
                                      timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.HTTPError as exc:
            raise GameApiError("network", self.scrub(f"{type(exc).__name__}: {exc}")) from None
        try:
            payload = resp.json()
        except ValueError:
            raise GameApiError("bad_response", f"HTTP {resp.status_code}, ответ не JSON", resp.status_code) from None
        if not isinstance(payload, dict):
            raise GameApiError("bad_response", f"HTTP {resp.status_code}, ответ не объект", resp.status_code)
        if payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raise GameApiError(str(error.get("code", f"http_{resp.status_code}")),
                               self.scrub(str(error.get("message", ""))), resp.status_code)
        return {k: v for k, v in payload.items() if k != "ok"}

    # --- routes -------------------------------------------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        return self.request("GET", "/v1/whoami")

    def rules(self, topic: str | None = None) -> dict[str, Any]:
        return self.request("GET", "/v1/rules", params={"topic": topic})

    def phase(self) -> dict[str, Any]:
        return self.request("GET", "/v1/phase")

    def wait_phase(self, after_seq: int | None, max_seconds: float) -> dict[str, Any]:
        seconds = max(1, min(MAX_WAIT_SECONDS, int(max_seconds)))
        return self.request("GET", "/v1/wait_phase", params={"after_seq": after_seq, "max_seconds": seconds},
                            timeout=seconds + WAIT_SLACK_SECONDS)

    def state(self, since_turn: int | None = None, fmt: str | None = None) -> dict[str, Any]:
        return self.request("GET", "/v1/state", params={"format": fmt, "since_turn": since_turn})

    def list_orders(self) -> dict[str, Any]:
        return self.request("GET", "/v1/orders")

    def validate_orders(self, orders: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request("POST", "/v1/orders/validate", body={"orders": orders})

    def submit_orders(self, orders: list[dict[str, Any]], mode: str) -> dict[str, Any]:
        return self.request("POST", "/v1/orders", body={"orders": orders, "mode": mode})

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self.request("DELETE", f"/v1/orders/{order_id}")

    def amend_order(self, order_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/v1/orders/{order_id}", body=patch)

    def estimate(self, attacker: Any, defender: Any, province_type: Any, fortified: Any = False,
                 hasty: Any = False, side: Any = None, opponent: Any = None) -> dict[str, Any]:
        """Arguments go as given: the server checks them and explains a refusal with a correct example."""
        body = {"attacker": attacker, "defender": defender, "province_type": province_type,
                "fortified": fortified, "hasty": hasty}
        body.update({k: v for k, v in (("side", side), ("opponent", opponent)) if v is not None})
        return self.request("POST", "/v1/estimate", body=body)

    def get_setup(self) -> dict[str, Any]:
        return self.request("GET", "/v1/setup")

    def choose_faction(self, prefs: Any) -> dict[str, Any]:
        return self.request("POST", "/v1/setup/faction", body={"prefs": prefs})

    def choose_artifacts(self, ids: Any) -> dict[str, Any]:
        return self.request("POST", "/v1/setup/artifacts", body={"ids": ids})

    def list_techs(self, full: bool = False) -> dict[str, Any]:
        return self.request("GET", "/v1/techs", params={"full": "true"} if full else None)

    def describe(self, ref: Any) -> dict[str, Any]:
        return self.request("GET", "/v1/describe", params={"id": ref})

    def send_message(self, to: list[str] | str, text: str) -> dict[str, Any]:
        return self.request("POST", "/v1/messages", body={"to": to, "text": text})

    def inbox(self, since: int | str | None = None) -> dict[str, Any]:
        return self.request("GET", "/v1/inbox", params={"since": since})

    def send_gift(self, to: Any, resources: Any) -> dict[str, Any]:
        return self.request("POST", "/v1/gifts", body={"to": to, "resources": resources})

    def list_pacts(self) -> dict[str, Any]:
        return self.request("GET", "/v1/pacts")

    def propose_pact(self, to: Any, turns: Any = None) -> dict[str, Any]:
        return self.request("POST", "/v1/pacts", body={"to": to, "turns": turns})

    def respond_pact(self, pact_id: str, decision: Any) -> dict[str, Any]:
        return self.request("POST", f"/v1/pacts/{quote(str(pact_id), safe='')}/respond", body={"decision": decision})

    def leave_pact(self, pact_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/pacts/{quote(str(pact_id), safe='')}/leave", body={})

    def ready(self, confirm_hash: str | None) -> dict[str, Any]:
        """`confirm_hash` may be None in the setup phase and in negotiation_1 (no orders there)."""
        return self.request("POST", "/v1/ready", body={"confirm_hash": confirm_hash})

    def unready(self) -> dict[str, Any]:
        return self.request("POST", "/v1/unready", body={})

    def history(self, turn: int | None = None) -> dict[str, Any]:
        return self.request("GET", "/v1/history", params={"turn": turn})

    def telemetry(self, kind: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/telemetry", body={"kind": kind, "detail": detail})

    def report_session(self, session_id: str) -> dict[str, Any]:
        """Record the seat's DreamTeam session id in the game config and journal (host route)."""
        return self.request("POST", "/v1/seat/dreamteam_session", body={"dreamteam_session_id": session_id})

    def spend(self, spent_usd: float, source: str, tokens: int | None = None, *, turn: int | None = None,
              phase: str | None = None) -> dict[str, Any]:
        """`turn`/`phase`: the phase the figure is for, i.e. the one that just closed (omit both for a baseline
        taken before any phase closed)."""
        body: dict[str, Any] = {"spent_usd": spent_usd, "source": source}
        extra = {"tokens": tokens, "turn": turn, "phase": phase}
        body.update({k: v for k, v in extra.items() if v is not None})
        return self.request("POST", "/v1/spend", body=body)
