"""HTTP server for authoritative judge mode."""

from __future__ import annotations

import json
import os
import socketserver
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..environment import forced_stop_code
from ..models import AgentDecision, ExperimentConfig
from .core import BenchmarkAssets, build_environment, build_initial_response, load_benchmark_assets, process_decision


@dataclass
class JudgeSession:
    session_id: str
    session_token: str
    run_name: str
    env: Any
    seed_manifest: list[dict[str, Any]]
    audit_path: Path
    created_at: str


class JudgeService:
    def __init__(self, config: ExperimentConfig, *, audit_root: Path, admin_token: str) -> None:
        self.config = config
        self.audit_root = audit_root
        self.admin_token = admin_token
        self.audit_root.mkdir(parents=True, exist_ok=True)
        self.assets: BenchmarkAssets = load_benchmark_assets(config)
        self.sessions: dict[str, JudgeSession] = {}

    def _timestamp(self) -> str:
        return datetime.now(UTC).isoformat()

    def _append_audit(
        self,
        session: JudgeSession,
        *,
        event: str,
        request: dict[str, Any],
        response: dict[str, Any],
        request_received_at: str | None = None,
        response_sent_at: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        entry = {
            "timestamp": self._timestamp(),
            "event": event,
            "session_id": session.session_id,
            "run_name": session.run_name,
            "request": request,
            "response": response,
            "request_received_at": request_received_at,
            "response_sent_at": response_sent_at,
            "duration_seconds": duration_seconds,
        }
        with session.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _seed_manifest_payload(self, env: Any) -> list[dict[str, Any]]:
        payload = json.loads(env.dumps_seed_manifest())
        if not isinstance(payload, list):
            raise RuntimeError("Judge seed manifest payload must be a list")
        return payload

    def create_session(self, *, run_name: str) -> dict[str, Any]:
        request_received_at = self._timestamp()
        started = time.perf_counter()
        session_id = uuid.uuid4().hex
        audit_path = self.audit_root / f"{session_id}.jsonl"
        env = build_environment(self.config, self.assets)
        session = JudgeSession(
            session_id=session_id,
            session_token=uuid.uuid4().hex,
            run_name=run_name,
            env=env,
            seed_manifest=self._seed_manifest_payload(env),
            audit_path=audit_path,
            created_at=self._timestamp(),
        )
        self.sessions[session_id] = session
        response = {
            "session_id": session_id,
            "session_token": session.session_token,
            "created_at": session.created_at,
            "seed_manifest": session.seed_manifest,
            **build_initial_response(session.env),
        }
        self._append_audit(
            session,
            event="session.created",
            request={"run_name": run_name},
            response={
                "terminal": response["terminal"],
                "request_count": session.env.request_count,
                "action_count": len(session.env.action_history),
            },
            request_received_at=request_received_at,
            response_sent_at=self._timestamp(),
            duration_seconds=max(time.perf_counter() - started, 0.0),
        )
        return response

    def _session(self, session_id: str) -> JudgeSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def require_admin_token(self, candidate: str | None) -> None:
        if not candidate or candidate != self.admin_token:
            raise PermissionError("missing or invalid admin token")

    def require_session_token(self, session_id: str, candidate: str | None) -> None:
        session = self._session(session_id)
        if not candidate or candidate != session.session_token:
            raise PermissionError("missing or invalid session token")

    def get_observation(self, session_id: str) -> dict[str, Any]:
        request_received_at = self._timestamp()
        started = time.perf_counter()
        session = self._session(session_id)
        response = build_initial_response(session.env)
        self._append_audit(
            session,
            event="observation.fetched",
            request={},
            response={
                "terminal": response["terminal"],
                "request_count": session.env.request_count,
                "action_count": len(session.env.action_history),
                "rejected_action_count": len(session.env.rejection_history),
            },
            request_received_at=request_received_at,
            response_sent_at=self._timestamp(),
            duration_seconds=max(time.perf_counter() - started, 0.0),
        )
        return response

    def submit_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_received_at = self._timestamp()
        started = time.perf_counter()
        session = self._session(session_id)
        decision = AgentDecision(
            action=str(payload.get("action") or ""),
            seed_id=str(payload["seed_id"]) if payload.get("seed_id") is not None else None,
            library_id=str(payload["library_id"]) if payload.get("library_id") is not None else None,
            batch_size=int(payload["batch_size"]) if payload.get("batch_size") is not None else None,
            screen_record_id=int(payload["screen_record_id"]) if payload.get("screen_record_id") is not None else None,
            skill_id=str(payload["skill_id"]) if payload.get("skill_id") is not None else None,
            reasoning=str(payload.get("reasoning") or ""),
        )
        response = process_decision(session.env, decision)
        self._append_audit(
            session,
            event="action.accepted" if response["accepted"] else "action.rejected",
            request=decision.__dict__,
            response={
                "step_index": response["step_index"],
                "accepted": response["accepted"],
                "rejection_code": response["rejection_code"],
                "rejection_reason": response["rejection_reason"],
                "action_record": response["action_record"],
                "terminal": response["terminal"],
                "stop_code": response["stop_code"],
                "stop_reason": response["stop_reason"],
                "request_count": response["request_count"],
                "action_count": response["action_count"],
                "rejected_action_count": response["rejected_action_count"],
            },
            request_received_at=request_received_at,
            response_sent_at=self._timestamp(),
            duration_seconds=max(time.perf_counter() - started, 0.0),
        )
        return response

    def fetch_owned_skill_bundle(self, session_id: str, skill_id: str) -> dict[str, Any]:
        request_received_at = self._timestamp()
        started = time.perf_counter()
        session = self._session(session_id)
        if skill_id not in session.env.owned_skill_ids:
            raise PermissionError(f"skill {skill_id!r} is not owned in this session")
        response = self.assets.skill_market.owned_bundle_payload(skill_id)
        self._append_audit(
            session,
            event="skill.bundle.fetched",
            request={"skill_id": skill_id},
            response={
                "skill_id": response["skill_id"],
                "bundle_sha256": response["bundle_sha256"],
                "bundle_size_bytes": response["bundle_size_bytes"],
            },
            request_received_at=request_received_at,
            response_sent_at=self._timestamp(),
            duration_seconds=max(time.perf_counter() - started, 0.0),
        )
        return response

    def force_stop(self, session_id: str, *, reason: str) -> dict[str, Any]:
        request_received_at = self._timestamp()
        started = time.perf_counter()
        session = self._session(session_id)
        if session.env.is_terminal():
            session.env.stopped = True
            session.env.stop_reason = reason
            session.env.stop_code = forced_stop_code(reason)
        else:
            session.env.apply(AgentDecision(action="stop", reasoning=reason))
            session.env.stop_code = forced_stop_code(reason)
            if session.env.action_history and session.env.action_history[-1].get("action") == "stop":
                session.env.action_history[-1]["stop_code"] = session.env.stop_code
        response = build_initial_response(session.env)
        self._append_audit(
            session,
            event="session.force_stopped",
            request={"reason": reason},
            response={
                "terminal": response["terminal"],
                "stop_code": session.env.stop_code,
                "stop_reason": session.env.stop_reason,
                "action_count": len(session.env.action_history),
                "request_count": session.env.request_count,
            },
            request_received_at=request_received_at,
            response_sent_at=self._timestamp(),
            duration_seconds=max(time.perf_counter() - started, 0.0),
        )
        return response

    def export_session(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        return {
            "session_id": session.session_id,
            "run_name": session.run_name,
            "seed_manifest": session.seed_manifest,
            "summary": session.env.summary(),
            "action_history": session.env.action_history,
            "screen_record_history": session.env.public_screen_record_history(),
            "rejection_history": session.env.rejection_history,
            "request_count": session.env.request_count,
        }

    def fetch_audit(self, session_id: str) -> str:
        session = self._session(session_id)
        if not session.audit_path.exists():
            return ""
        return session.audit_path.read_text(encoding="utf-8")


class _JudgeHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], service: JudgeService) -> None:
        super().__init__(server_address, _JudgeHandler)
        self.judge_service = service

    def server_bind(self) -> None:
        # Skip http.server.HTTPServer.server_bind's socket.getfqdn() reverse-DNS
        # lookup, which can block for ~30s on machines with slow/flaky DNS.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class _JudgeHandler(BaseHTTPRequestHandler):
    server: _JudgeHTTPServer

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _admin_token(self) -> str | None:
        return self.headers.get("X-TadACampaign-Admin-Token")

    def _session_token(self) -> str | None:
        return self.headers.get("X-TadACampaign-Session-Token")

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return

            parts = [part for part in self.path.strip("/").split("/") if part]
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "observation":
                self.server.judge_service.require_session_token(parts[1], self._session_token())
                self._send_json(200, self.server.judge_service.get_observation(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "export":
                self.server.judge_service.require_admin_token(self._admin_token())
                self._send_json(200, self.server.judge_service.export_session(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "audit":
                self.server.judge_service.require_admin_token(self._admin_token())
                self._send_json(200, {"session_id": parts[1], "audit": self.server.judge_service.fetch_audit(parts[1])})
                return
            if len(parts) == 5 and parts[0] == "sessions" and parts[2] == "skills" and parts[4] == "bundle":
                self.server.judge_service.require_session_token(parts[1], self._session_token())
                self._send_json(200, self.server.judge_service.fetch_owned_skill_bundle(parts[1], parts[3]))
                return
        except KeyError:
            self._send_json(404, {"error": "unknown session"})
            return
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        try:
            if self.path == "/sessions":
                self.server.judge_service.require_admin_token(self._admin_token())
                run_name = str(payload.get("run_name") or "").strip()
                if not run_name:
                    self._send_json(400, {"error": "run_name is required"})
                    return
                self._send_json(200, self.server.judge_service.create_session(run_name=run_name))
                return

            parts = [part for part in self.path.strip("/").split("/") if part]
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "actions":
                self.server.judge_service.require_session_token(parts[1], self._session_token())
                self._send_json(200, self.server.judge_service.submit_action(parts[1], payload))
                return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "force-stop":
                self.server.judge_service.require_admin_token(self._admin_token())
                reason = str(payload.get("reason") or "").strip()
                if not reason:
                    self._send_json(400, {"error": "reason is required"})
                    return
                self._send_json(200, self.server.judge_service.force_stop(parts[1], reason=reason))
                return
        except KeyError:
            self._send_json(404, {"error": "unknown session"})
            return
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return

        self._send_json(404, {"error": "not found"})


def create_judge_http_server(
    config: ExperimentConfig,
    *,
    host: str,
    advertise_host: str | None = None,
    port: int,
    audit_root: Path,
    admin_token: str,
    ready_file: Path | None = None,
) -> _JudgeHTTPServer:
    service = JudgeService(config, audit_root=audit_root, admin_token=admin_token)
    server = _JudgeHTTPServer((host, port), service)
    metadata_host = advertise_host or host
    if ready_file is not None:
        ready_file.parent.mkdir(parents=True, exist_ok=True)
        ready_file.write_text(
            json.dumps(
                {
                    "host": host,
                    "bind_host": host,
                    "advertise_host": metadata_host,
                    "port": server.server_port,
                    "base_url": f"http://{metadata_host}:{server.server_port}",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return server


def run_judge_server(
    config: ExperimentConfig,
    *,
    host: str,
    advertise_host: str | None = None,
    port: int,
    audit_root: Path,
    admin_token: str | None = None,
    ready_file: Path | None = None,
) -> None:
    resolved_admin_token = admin_token or os.environ.get("TADACAMPAIGN_JUDGE_ADMIN_TOKEN")
    if not resolved_admin_token:
        raise RuntimeError(
            "Authoritative judge server requires an admin token. "
            "Pass --admin-token or set TADACAMPAIGN_JUDGE_ADMIN_TOKEN."
        )
    server = create_judge_http_server(
        config,
        host=host,
        advertise_host=advertise_host,
        port=port,
        audit_root=audit_root,
        admin_token=resolved_admin_token,
        ready_file=ready_file,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
