"""Container-local egress firewall bootstrap helpers."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path


def _nameservers(resolv_conf: Path = Path("/etc/resolv.conf")) -> list[str]:
    servers: list[str] = []
    if not resolv_conf.is_file():
        return servers
    for line in resolv_conf.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("nameserver "):
            continue
        _, address = stripped.split(None, 1)
        servers.append(address.strip())
    return servers


def _resolve_ipv4(host: str) -> list[str]:
    addresses = {
        info[4][0]
        for info in socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        if info and info[4]
    }
    return sorted(addresses)


def _iptables(*args: str) -> None:
    subprocess.run(["iptables", *args], check=True)


def apply_egress_policy(policy: dict) -> None:
    endpoints = policy.get("endpoints", [])
    if not policy.get("enforce") or not isinstance(endpoints, list):
        return
    if not endpoints:
        raise ValueError("Refusing to enable firewall with an empty allowlist")

    _iptables("-F", "OUTPUT")
    _iptables("-P", "OUTPUT", "DROP")
    _iptables("-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    _iptables("-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")

    for nameserver in _nameservers():
        _iptables("-A", "OUTPUT", "-d", nameserver, "-p", "udp", "--dport", "53", "-j", "ACCEPT")
        _iptables("-A", "OUTPUT", "-d", nameserver, "-p", "tcp", "--dport", "53", "-j", "ACCEPT")

    for item in endpoints:
        if not isinstance(item, dict):
            continue
        host = item.get("host")
        port = item.get("port")
        if not isinstance(host, str) or not host:
            continue
        if not isinstance(port, int):
            continue
        try:
            addresses = _resolve_ipv4(host)
        except socket.gaierror as exc:
            # Skip endpoints that cannot be resolved from inside the container
            # (e.g. a provider reached through an HTTP proxy rather than directly).
            print(f"WARNING: skipping unresolvable egress endpoint {host}:{port} ({exc})", file=sys.stderr)
            continue
        for address in addresses:
            _iptables("-A", "OUTPUT", "-d", address, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap container egress firewall")
    parser.add_argument("--policy-json", required=True, help="Serialized network policy JSON")
    args = parser.parse_args(argv)

    payload = json.loads(args.policy_json)
    if not isinstance(payload, dict):
        raise SystemExit("policy-json must decode to an object")
    apply_egress_policy(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
