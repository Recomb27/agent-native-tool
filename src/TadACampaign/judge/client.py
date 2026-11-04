"""HTTP client for authoritative judge mode."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..models import AgentDecision


class JudgeHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int,
        session_token: str | None = None,
        admin_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session_token = session_token
        self.admin_token = admin_token

    def _open(self, request: urllib.request.Request):
        hostname = urllib.parse.urlparse(request.full_url).hostname
        if hostname in {"127.0.0.1", "localhost", "::1", "host.docker.internal"}:
            return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request,
                timeout=self.timeout_seconds,
            )
        return urllib.request.urlopen(request, timeout=self.timeout_seconds)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth_kind: str = "none",
    ) -> dict[str, Any]:
        url = urllib.parse.urljoin(f"{self.base_url}/", path.lstrip("/"))
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth_kind == "session":
            if not self.session_token:
                raise RuntimeError("Judge session token is required for this request")
            headers["X-TadACampaign-Session-Token"] = self.session_token
        elif auth_kind == "admin":
            if not self.admin_token:
                raise RuntimeError("Judge admin token is required for this request")
            headers["X-TadACampaign-Admin-Token"] = self.admin_token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._open(request) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Judge request failed: {method} {url} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Judge request failed: {method} {url}: {exc.reason}") from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Judge returned invalid JSON for {method} {url}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"Judge returned non-object payload for {method} {url}")
        return decoded

    def healthcheck(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def create_session(self, *, run_name: str) -> dict[str, Any]:
        return self._request("POST", "/sessions", {"run_name": run_name}, auth_kind="admin")

    def get_observation(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{session_id}/observation", auth_kind="session")

    def submit_action(self, session_id: str, decision: AgentDecision) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/actions", decision.__dict__, auth_kind="session")

    def fetch_owned_skill_bundle(self, session_id: str, skill_id: str) -> dict[str, Any]:
        quoted_skill_id = urllib.parse.quote(skill_id, safe="")
        return self._request("GET", f"/sessions/{session_id}/skills/{quoted_skill_id}/bundle", auth_kind="session")

    def force_stop(self, session_id: str, *, reason: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/force-stop", {"reason": reason}, auth_kind="admin")

    def export_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{session_id}/export", auth_kind="admin")

    def fetch_audit(self, session_id: str) -> str:
        payload = self._request("GET", f"/sessions/{session_id}/audit", auth_kind="admin")
        audit = payload.get("audit")
        if not isinstance(audit, str):
            raise RuntimeError(f"Judge audit response for session {session_id} is missing string audit payload")
        return audit
