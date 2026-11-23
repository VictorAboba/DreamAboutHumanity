"""Client of the DreamTeam HTTP API (/api/v1): the session summary, `continue` and `stop`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class DtResponse:
    """status 0 means the request never got an answer (a transport failure)."""

    status: int
    code: str | None = None
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status == 429 or self.status >= 500

    @property
    def session(self) -> dict[str, Any]:
        session = self.body.get("session")
        return session if isinstance(session, dict) else {}


class DreamTeamClient:
    """Thin wrapper over httpx.Client; the API token stays in the Authorization header."""

    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0):
        self._http = httpx.Client(base_url=url.rstrip("/") + "/api/v1", transport=transport, timeout=timeout,
                                  headers={"Authorization": f"Bearer {token}"})

    def close(self) -> None:
        self._http.close()

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> DtResponse:
        try:
            resp = self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            return DtResponse(0, "network", {"error": type(exc).__name__})
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        code = None if 200 <= resp.status_code < 300 else str(payload.get("code") or f"http_{resp.status_code}")
        return DtResponse(resp.status_code, code, payload)

    def get_session(self, session_id: str) -> DtResponse:
        return self._send("GET", f"/sessions/{session_id}")

    def continue_session(self, session_id: str, text: str) -> DtResponse:
        return self._send("POST", f"/sessions/{session_id}/continue", {"text": text})


    def stop_session(self, session_id: str) -> DtResponse:
        """Stop the session and its background jobs (reason `user`). The body is `{}`: DreamTeam wants
        `Content-Type: application/json` on every POST, the body-less `stop` too."""
        return self._send("POST", f"/sessions/{session_id}/stop", {})
