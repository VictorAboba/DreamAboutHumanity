"""Thin HTTP client of the game API (SLICE.md section 7) used by bots."""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ApiClient:
    def __init__(self, server_url: str, key: str, timeout: float = 70.0) -> None:
        self._http = httpx.Client(
            base_url=server_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._http.request(method, path, **kwargs)
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise ApiError("BAD_RESPONSE", f"{method} {path}: not JSON")
        if not data.get("ok", False):
            error = data.get("error") or {}
            raise ApiError(str(error.get("code", response.status_code)), str(error.get("message", "")))
        return data

    def whoami(self) -> dict[str, Any]:
        return self._call("GET", "/v1/whoami")

    def phase(self) -> dict[str, Any]:
        return self._call("GET", "/v1/phase")

    def wait_phase(self, after_seq: int, max_seconds: int) -> dict[str, Any]:
        return self._call("GET", "/v1/wait_phase", params={"after_seq": after_seq, "max_seconds": max_seconds})

    def state(self) -> dict[str, Any]:
        return self._call("GET", "/v1/state", params={"format": "json"})

    def orders(self) -> dict[str, Any]:
        return self._call("GET", "/v1/orders")

    def submit_orders(self, orders: list[dict[str, Any]], mode: str) -> dict[str, Any]:
        return self._call("POST", "/v1/orders", json={"orders": orders, "mode": mode})

    def estimate(
        self, attacker: dict[str, int], defender: dict[str, int], province_type: str, fortified: bool, hasty: bool
    ) -> float:
        data = self._call(
            "POST",
            "/v1/estimate",
            json={
                "attacker": attacker,
                "defender": defender,
                "province_type": province_type,
                "fortified": fortified,
                "hasty": hasty,
            },
        )
        return float(data.get("win_prob", data.get("estimate", {}).get("win_prob", 0.0)))

    def send_message(self, to: list[str] | str, text: str) -> dict[str, Any]:
        return self._call("POST", "/v1/messages", json={"to": to, "text": text})

    def send_gift(self, to: str, resources: dict[str, int]) -> dict[str, Any]:
        return self._call("POST", "/v1/gifts", json={"to": to, "resources": resources})

    def propose_pact(self, to: str, turns: int | None) -> dict[str, Any]:
        return self._call("POST", "/v1/pacts", json={"to": to, "turns": turns})

    def respond_pact(self, pact_id: str, decision: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/pacts/{pact_id}/respond", json={"decision": decision})

    def leave_pact(self, pact_id: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/pacts/{pact_id}/leave", json={})

    def ready(self, confirm_hash: str) -> dict[str, Any]:
        return self._call("POST", "/v1/ready", json={"confirm_hash": confirm_hash})
