"""Helpers for container-side egress allowlisting."""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .codex_home import load_codex_api_settings

_PROXY_ENV_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class NetworkEndpoint:
    host: str
    port: int


def _rewrite_local_host(host: str) -> str:
    normalized = host.strip()
    if normalized in _LOCALHOST_HOSTS:
        return "host.docker.internal"
    return normalized


def _parse_endpoint(raw_url: str, *, default_scheme: str) -> NetworkEndpoint:
    normalized = raw_url.strip()
    if "://" not in normalized:
        normalized = f"{default_scheme}://{normalized}"
    parsed = urllib.parse.urlparse(normalized)
    if not parsed.hostname:
        raise ValueError(f"Unable to parse host from URL {raw_url!r}")
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme == "http":
        port = 80
    elif parsed.scheme == "https":
        port = 443
    else:
        raise ValueError(f"Unable to infer port from URL {raw_url!r}")
    return NetworkEndpoint(host=_rewrite_local_host(parsed.hostname), port=port)


def _dedupe_endpoints(endpoints: list[NetworkEndpoint]) -> tuple[NetworkEndpoint, ...]:
    seen: set[tuple[str, int]] = set()
    ordered: list[NetworkEndpoint] = []
    for endpoint in endpoints:
        key = (endpoint.host, endpoint.port)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(endpoint)
    return tuple(ordered)


def resolve_proxy_endpoints(env: Mapping[str, str] | None = None) -> tuple[NetworkEndpoint, ...]:
    source = os.environ if env is None else env
    endpoints: list[NetworkEndpoint] = []
    for name in _PROXY_ENV_NAMES:
        raw_value = source.get(name)
        if raw_value:
            endpoints.append(_parse_endpoint(raw_value, default_scheme="http"))
    return _dedupe_endpoints(endpoints)


def resolve_codex_provider_endpoint(codex_home: Path) -> NetworkEndpoint:
    base_url, _ = load_codex_api_settings(codex_home)
    return _parse_endpoint(base_url, default_scheme="https")


def _host_should_bypass_proxy(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in _LOCALHOST_HOSTS or normalized == "host.docker.internal":
        return True
    try:
        return not ipaddress.ip_address(normalized).is_global
    except ValueError:
        return False


def resolve_backend_proxy_bypass_hosts(
    backend: str,
    *,
    codex_home: Path | None = None,
) -> tuple[str, ...]:
    hosts: list[str] = []
    if backend == "codex":
        if codex_home is None:
            raise ValueError("codex_home is required to resolve codex proxy bypass hosts")
        provider_host = resolve_codex_provider_endpoint(codex_home).host
        if _host_should_bypass_proxy(provider_host):
            hosts.append(provider_host)
    seen: set[str] = set()
    ordered: list[str] = []
    for host in hosts:
        if host in seen:
            continue
        seen.add(host)
        ordered.append(host)
    return tuple(ordered)


def resolve_backend_egress_allowlist(
    backend: str,
    *,
    codex_home: Path | None = None,
    env: Mapping[str, str] | None = None,
    extra_urls: Sequence[str] | None = None,
) -> tuple[NetworkEndpoint, ...]:
    endpoints: list[NetworkEndpoint] = list(resolve_proxy_endpoints(env))
    if backend == "codex":
        if codex_home is None:
            raise ValueError("codex_home is required to resolve codex egress targets")
        endpoints.append(resolve_codex_provider_endpoint(codex_home))
    if extra_urls:
        for raw_url in extra_urls:
            if not raw_url:
                continue
            endpoints.append(_parse_endpoint(raw_url, default_scheme="http"))
    if endpoints:
        return _dedupe_endpoints(endpoints)
    return ()


def merge_no_proxy(existing_value: str | None, *hosts: str) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_item in (existing_value or "").split(","):
        normalized = raw_item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    for raw_item in hosts:
        normalized = raw_item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ",".join(ordered)
