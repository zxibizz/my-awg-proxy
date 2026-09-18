"""Lifecycle of a single tunnel: its netns, the AmneziaWG link, socks and probe.

Each tunnel gets a private network namespace joined to the container's main
namespace by a veth pair. Encrypted traffic to the peer endpoint leaves over
that veth (pinned with a host route), while everything else defaults into the
tunnel -- so the socks proxy and the probe inside the namespace can only ever
reach the internet through the VPN.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from . import sh
from .store import Peer

log = logging.getLogger("proxyarr.tunnel")

RUN_DIR = Path("/run/proxyarr")
SOCKS_PORT = 1080
PROBE_PORT = 9998
_IFACE_TIMEOUT = 15.0


class TunnelError(RuntimeError):
    pass


class Tunnel:
    def __init__(self, peer: Peer, slot: int, settings, probe_env: dict[str, str]) -> None:
        self.peer = peer
        self.slot = slot
        self.settings = settings
        self.probe_env = probe_env
        self.name = peer.name
        self.netns = f"pa{slot}"
        self.host_if = f"pa{slot}h"
        self.ns_if = f"pa{slot}n"
        self.wg_if = f"awg{slot}"
        self.host_ip = f"169.254.{slot}.1"
        self.ns_ip = f"169.254.{slot}.2"
        self.started_at = 0.0
        self.error: str | None = None
        self.procs: dict[str, subprocess.Popen] = {}

    # ---------------------------------------------------------------- helpers

    @property
    def wg_conf_path(self) -> Path:
        return RUN_DIR / f"{self.netns}.conf"

    @property
    def netns_etc(self) -> Path:
        return Path("/etc/netns") / self.netns

    def _resolve_endpoint(self) -> list[str]:
        """Resolve the peer endpoint in the main namespace, where DNS works."""
        host = self.peer.config.endpoint_host
        try:
            infos = socket.getaddrinfo(
                host, self.peer.config.endpoint_port, proto=socket.IPPROTO_UDP
            )
        except socket.gaierror as exc:
            raise TunnelError(f"cannot resolve endpoint {host}: {exc}") from exc
        return sorted({info[4][0] for info in infos})

    def _wait_for_link(self) -> None:
        deadline = time.monotonic() + _IFACE_TIMEOUT
        while time.monotonic() < deadline:
            if sh.ip("-n", self.netns, "link", "show", self.wg_if, check=False).returncode == 0:
                return
            if (proc := self.procs.get("awg")) is not None and proc.poll() is not None:
                raise TunnelError(f"amneziawg-go exited with {proc.returncode}")
            time.sleep(0.2)
        raise TunnelError(f"{self.wg_if} did not appear within {_IFACE_TIMEOUT:.0f}s")

    def _spawn(self, key: str, argv: list[str], env: dict[str, str] | None = None) -> None:
        full_env = {**os.environ, **(env or {})}
        self.procs[key] = subprocess.Popen(
            ["ip", "netns", "exec", self.netns, *argv],
            env=full_env,
            stdin=subprocess.DEVNULL,
        )

    def _add_netns(self) -> None:
        Path("/run/netns").mkdir(parents=True, exist_ok=True)
        last_error = None
        for attempt in range(5):
            try:
                sh.run("mount", "--bind", "/run/netns", "/run/netns", check=False)
                sh.run("mount", "--make-shared", "/run/netns")
                sh.run("ip", "netns", "add", self.netns)
                return
            except sh.CommandError as exc:
                last_error = exc
                sh.quiet("ip", "netns", "del", self.netns)
                if attempt < 4:
                    time.sleep(1)
            assert last_error is not None
        raise last_error

    def _probe_env(self, immediate: bool = False) -> dict[str, str]:
        env = dict(self.probe_env)
        env["LISTEN_PORT"] = str(PROBE_PORT)
        # Staggered so the pool does not hammer the target in lockstep.
        interval = max(1.0, float(env.get("PROBE_INTERVAL", "60")))
        env["START_DELAY"] = "0" if immediate else str(round((self.slot * 7) % interval, 1))
        return env

    # ------------------------------------------------------------------ up

    def up(self) -> None:
        log.info("[%s] starting on slot %d", self.name, self.slot)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        endpoints = self._resolve_endpoint()

        self.wg_conf_path.write_text(self.peer.config.wg_text)
        self.wg_conf_path.chmod(0o600)

        self.netns_etc.mkdir(parents=True, exist_ok=True)
        nameservers = self.peer.config.dns or self.settings.fallback_dns
        (self.netns_etc / "resolv.conf").write_text(
            "".join(f"nameserver {ns}\n" for ns in nameservers)
        )

        self._add_netns()
        sh.ip("-n", self.netns, "link", "set", "lo", "up")

        sh.ip("link", "add", self.host_if, "type", "veth", "peer", "name", self.ns_if)
        sh.ip("link", "set", self.ns_if, "netns", self.netns)
        sh.ip("addr", "add", f"{self.host_ip}/30", "dev", self.host_if)
        sh.ip("link", "set", self.host_if, "up")
        sh.ip("-n", self.netns, "addr", "add", f"{self.ns_ip}/30", "dev", self.ns_if)
        sh.ip("-n", self.netns, "link", "set", self.ns_if, "up")

        self._spawn("awg", ["amneziawg-go", "-f", self.wg_if])
        self._wait_for_link()

        for address in self.peer.config.addresses:
            family = "-6" if ":" in address else "-4"
            sh.ip("-n", self.netns, family, "addr", "add", address, "dev", self.wg_if)
        sh.ip("-n", self.netns, "link", "set", "dev", self.wg_if,
              "mtu", str(self.peer.config.mtu), "up")

        # Pin the peer endpoint to the veth before the tunnel claims the
        # default route, otherwise the handshake would route into itself.
        for addr in endpoints:
            if ":" in addr:
                continue
            sh.ip("-n", self.netns, "route", "add", f"{addr}/32", "via", self.host_ip)
        sh.ip("-n", self.netns, "route", "replace", "default", "dev", self.wg_if)
        if self.peer.config.has_ipv6:
            sh.ip("-n", self.netns, "-6", "route", "replace", "default",
                  "dev", self.wg_if, check=False)

        # Loading the peer is deliberately last: amneziawg-go starts handshaking
        # the moment it has one, and a handshake sent before the endpoint route
        # exists fails as unreachable and costs a back-off window.
        sh.netns_exec(self.netns, "awg", "setconf", self.wg_if, str(self.wg_conf_path))

        self._spawn("socks", ["microsocks", "-i", "0.0.0.0", "-p", str(SOCKS_PORT), "-q"])
        self._spawn("probe", ["python3", str(self.settings.probe_script)], self._probe_env())

        self.started_at = time.time()
        self.error = None
        log.info("[%s] up via %s", self.name, self.peer.config.endpoint)

    # ---------------------------------------------------------------- down

    def down(self) -> None:
        log.info("[%s] stopping", self.name)
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 5
        for proc in self.procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.procs.clear()

        sh.quiet("ip", "netns", "del", self.netns)
        # ip-netns cannot unlink a handle whose bind mount was already invalidated.
        netns_path = Path("/run/netns") / self.netns
        if netns_path.exists() or netns_path.is_symlink():
            try:
                netns_path.unlink()
            except OSError as exc:
                log.warning("[%s] could not remove namespace handle: %s", self.name, exc)
        sh.quiet("ip", "link", "del", self.host_if)
        shutil.rmtree(self.netns_etc, ignore_errors=True)
        self.wg_conf_path.unlink(missing_ok=True)
        self.started_at = 0.0

    # -------------------------------------------------------------- status

    def dead_processes(self) -> list[str]:
        return [key for key, proc in self.procs.items() if proc.poll() is not None]

    def restart_process(self, key: str) -> None:
        log.warning("[%s] %s died, respawning", self.name, key)
        if key == "socks":
            self._spawn("socks", ["microsocks", "-i", "0.0.0.0", "-p", str(SOCKS_PORT), "-q"])
        elif key == "probe":
            self._spawn("probe", ["python3", str(self.settings.probe_script)], self._probe_env())
        else:
            raise TunnelError(f"{key} cannot be respawned in place")

    def restart_probe(self) -> None:
        """Reloads the probe in place so new settings apply without a rebuild."""
        proc = self.procs.get("probe")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        # Immediate: a staggered restart would leave haproxy checking a 503 probe.
        self._spawn("probe", ["python3", str(self.settings.probe_script)],
                    self._probe_env(immediate=True))

    def wg_stats(self) -> dict:
        """Latest handshake and byte counters from `awg show <iface> dump`."""
        proc = sh.netns_exec(self.netns, "awg", "show", self.wg_if, "dump",
                             check=False, timeout=10)
        stats = {"handshake_age": None, "rx_bytes": 0, "tx_bytes": 0, "endpoint": None}
        if proc.returncode != 0:
            return stats
        lines = proc.stdout.strip().splitlines()
        if len(lines) < 2:
            return stats
        fields = lines[1].split("\t")
        if len(fields) < 8:
            return stats
        handshake = int(fields[4] or 0)
        stats["endpoint"] = fields[2] if fields[2] != "(none)" else None
        stats["handshake_age"] = round(time.time() - handshake, 1) if handshake else None
        stats["rx_bytes"] = int(fields[5] or 0)
        stats["tx_bytes"] = int(fields[6] or 0)
        return stats

    def probe_status(self) -> str | None:
        try:
            with socket.create_connection((self.ns_ip, PROBE_PORT), timeout=2) as conn:
                conn.sendall(b"GET / HTTP/1.0\r\nHost: probe\r\n\r\n")
                raw = b""
                while len(raw) < 4096:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
        except OSError:
            return None
        _, _, body = raw.partition(b"\r\n\r\n")
        return body.decode("utf-8", "replace").strip() or None

    def exit_ip(self, url: str, timeout: float = 10) -> str:
        proc = sh.netns_exec(
            self.netns, "curl", "-sS", "--max-time", str(int(timeout)), url,
            check=False, timeout=timeout + 5,
        )
        if proc.returncode != 0:
            raise TunnelError(proc.stderr.strip() or "exit IP lookup failed")
        return proc.stdout.strip()[:128]
