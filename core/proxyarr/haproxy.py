"""Generates haproxy's config from the live tunnel set and reloads it in place.

haproxy runs in master-worker mode, so a rewritten config plus SIGUSR2 swaps the
worker without dropping the listening socket.
"""

from __future__ import annotations

import logging
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import sh

log = logging.getLogger("proxyarr.haproxy")

_TEMPLATE = """\
global
    log stdout format raw local0 info
    user haproxy
    group haproxy
    chroot /var/empty
    stats socket {sock} mode 660 level admin
    stats timeout 30s

defaults
    log     global
    mode    tcp
    option  tcplog
    timeout connect 5s
    timeout client  5m
    timeout server  5m

frontend socks_in
    bind *:{port}
    default_backend vpn_pool

backend vpn_pool
    balance {balance}
    # The socks daemon accepts connections happily over a dead tunnel, so health
    # is judged by the per-tunnel probe rather than by the socks port itself.
    option httpchk
    http-check send meth GET uri / ver HTTP/1.1 hdr Host localhost
    http-check expect status 200
    default-server check port {probe_port} inter 10s fall 2 rise 3
{servers}
"""


@dataclass
class ServerSpec:
    name: str
    address: str
    port: int
    backup: bool = False


class Haproxy:
    def __init__(self, cfg_path: Path, sock_path: Path, listen_port: int, probe_port: int) -> None:
        self.cfg_path = cfg_path
        self.sock_path = sock_path
        self.listen_port = listen_port
        self.probe_port = probe_port
        self.proc: subprocess.Popen | None = None

    def render(self, servers: list[ServerSpec], balance: str) -> str:
        if balance == "failover":
            lines = [
                f"    server {s.name} {s.address}:{s.port}" + (" backup" if s.backup else "")
                for s in servers
            ]
            balance_directive = "roundrobin"
        else:
            lines = [f"    server {s.name} {s.address}:{s.port}" for s in servers]
            balance_directive = balance
        body = "\n".join(lines) if lines else "    # no enabled tunnels"
        return _TEMPLATE.format(
            sock=self.sock_path,
            port=self.listen_port,
            probe_port=self.probe_port,
            balance=balance_directive,
            servers=body,
        )

    def apply(self, servers: list[ServerSpec], balance: str) -> None:
        rendered = self.render(servers, balance)
        if self.cfg_path.exists() and self.cfg_path.read_text() == rendered and self.running:
            return

        candidate = self.cfg_path.with_suffix(".next")
        candidate.write_text(rendered)
        check = sh.run("haproxy", "-c", "-f", str(candidate), check=False)
        if check.returncode != 0:
            candidate.unlink(missing_ok=True)
            raise RuntimeError(f"generated haproxy config rejected: {check.stderr.strip()}")
        candidate.replace(self.cfg_path)

        if not self.running:
            self._start()
        else:
            log.info("reloading haproxy with %d server(s)", len(servers))
            self.proc.send_signal(signal.SIGUSR2)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self) -> None:
        log.info("starting haproxy on :%d", self.listen_port)
        self.proc = subprocess.Popen(
            ["haproxy", "-W", "-db", "-f", str(self.cfg_path)],
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.sock_path.exists():
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"haproxy exited with {self.proc.returncode}")
            time.sleep(0.2)

    def stop(self) -> None:
        if not self.running:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def server_states(self) -> dict[str, dict[str, str]]:
        """`show stat` for the pool backend, keyed by server name."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(3)
                conn.connect(str(self.sock_path))
                conn.sendall(b"show stat\n")
                chunks = []
                while chunk := conn.recv(65536):
                    chunks.append(chunk)
        except OSError:
            return {}

        rows = b"".join(chunks).decode("utf-8", "replace").splitlines()
        if not rows or not rows[0].startswith("# "):
            return {}
        header = rows[0][2:].split(",")
        states: dict[str, dict[str, str]] = {}
        for row in rows[1:]:
            if not row.strip():
                continue
            values = dict(zip(header, row.split(",")))
            if values.get("pxname") != "vpn_pool":
                continue
            svname = values.get("svname", "")
            if svname in ("FRONTEND", "BACKEND"):
                continue
            states[svname] = {
                "status": values.get("status", ""),
                "check_status": values.get("check_status", ""),
                "sessions": values.get("scur", "0"),
                "total": values.get("stot", "0"),
                "backup": values.get("bck", "0") == "1",
            }
        return states
