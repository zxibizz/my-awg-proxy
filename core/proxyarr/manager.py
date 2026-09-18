"""Reconciles the desired peer set with the tunnels actually running."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time

from . import sh
from .config import Settings
from .haproxy import Haproxy, ServerSpec
from .store import Store
from .tunnel import PROBE_PORT, SOCKS_PORT, Tunnel, TunnelError

log = logging.getLogger("proxyarr.manager")

MAX_TUNNELS = 250
_NETNS_RE = re.compile(r"^pa\d+$")
# Grace period after bring-up before the handshake watchdog may fire.
_HANDSHAKE_GRACE = 90.0


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Manager:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.tunnels: dict[str, Tunnel] = {}
        self.failures: dict[str, str] = {}
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._next_retry = 0.0
        self.haproxy = Haproxy(
            cfg_path=settings.run_dir / "haproxy.cfg",
            sock_path=settings.run_dir / "haproxy.sock",
            listen_port=settings.socks_port,
            probe_port=PROBE_PORT,
        )

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.settings.run_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_host()
        self.reconcile(wait_for_probes=True)
        self._supervisor = threading.Thread(target=self._supervise, daemon=True)
        self._supervisor.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            self.haproxy.stop()
            for tunnel in list(self.tunnels.values()):
                tunnel.down()
            self.tunnels.clear()

    def _prepare_host(self) -> None:
        # Left over from a previous run if the container was restarted in place.
        listed = sh.run("ip", "-o", "netns", "list", check=False)
        for line in listed.stdout.splitlines():
            name = line.split()[0] if line.split() else ""
            if _NETNS_RE.match(name):
                log.warning("removing stale netns %s", name)
                sh.quiet("ip", "netns", "del", name)

        route = sh.run("ip", "-4", "route", "show", "default", check=False)
        parts = route.stdout.split()
        uplink = parts[parts.index("dev") + 1] if "dev" in parts else "eth0"
        # Tunnel handshakes originate inside a netns and leave via the veth, so
        # they need NAT onto the container's own address.
        rule = ["POSTROUTING", "-s", "169.254.0.0/16", "-o", uplink, "-j", "MASQUERADE"]
        if sh.run("iptables", "-t", "nat", "-C", *rule, check=False).returncode != 0:
            sh.run("iptables", "-t", "nat", "-A", *rule, check=False)

    # ------------------------------------------------------------ reconcile

    def _alloc_slot(self) -> int:
        used = {tunnel.slot for tunnel in self.tunnels.values()}
        for slot in range(1, MAX_TUNNELS + 1):
            if slot not in used:
                return slot
        raise TunnelError(f"tunnel limit of {MAX_TUNNELS} reached")

    def effective_probe_env(self) -> dict[str, str]:
        env = dict(self.settings.probe_env)
        if urls := self.store.probe_urls:
            env["PROBE_URLS"] = urls
        return env

    def apply_probe_urls(self, value: str) -> None:
        self.store.set_probe_urls(value)
        with self._lock:
            env = self.effective_probe_env()
            for tunnel in self.tunnels.values():
                tunnel.probe_env = env
                tunnel.restart_probe()
        log.info("probe urls set to %s", env["PROBE_URLS"])

    def _wait_for_probe_verdicts(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = []
            for name, tunnel in self.tunnels.items():
                verdict = tunnel.probe_status()
                if verdict is None or verdict.startswith("down: starting up"):
                    pending.append(name)
            if not pending:
                return
            time.sleep(0.25)
        log.warning("probe verdict timeout; continuing with pending probes: %s", ", ".join(pending))

    def reconcile(self, wait_for_probes: bool = False) -> None:
        with self._lock:
            peers = self.store.list()
            desired = {peer.name: peer for peer in peers if peer.enabled}

            for name in list(self.tunnels):
                peer = desired.get(name)
                if peer is None or self._fingerprints.get(name) != _fingerprint(peer.text):
                    self._teardown(name)

            for name, peer in desired.items():
                if name in self.tunnels:
                    continue
                tunnel = Tunnel(peer, self._alloc_slot(), self.settings,
                                self.effective_probe_env())
                try:
                    tunnel.up()
                except Exception as exc:
                    log.error("[%s] failed to start: %s", name, exc)
                    tunnel.down()
                    self.failures[name] = str(exc)
                    continue
                self.tunnels[name] = tunnel
                self._fingerprints[name] = _fingerprint(peer.text)
                self.failures.pop(name, None)

            if wait_for_probes:
                self._wait_for_probe_verdicts()
            self._apply_haproxy(peers)

    def _teardown(self, name: str) -> None:
        tunnel = self.tunnels.pop(name, None)
        self._fingerprints.pop(name, None)
        if tunnel is not None:
            tunnel.down()

    def _apply_haproxy(self, peers) -> None:
        specs = []
        for peer in peers:
            tunnel = self.tunnels.get(peer.name)
            if tunnel is None:
                continue
            # Highest-priority tunnel that actually came up is the primary.
            specs.append(
                ServerSpec(peer.name, tunnel.ns_ip, SOCKS_PORT, backup=bool(specs))
            )
        try:
            self.haproxy.apply(specs, self.store.balance)
        except Exception as exc:
            log.error("haproxy reload failed: %s", exc)

    def restart(self, name: str) -> None:
        with self._lock:
            tunnel = self.tunnels.get(name)
            if tunnel is None:
                self.reconcile()
                return
            peer = self.store.get(name)
            if peer is None:
                self._teardown(name)
                return
            tunnel.down()
            tunnel.peer = peer
            tunnel.probe_env = self.effective_probe_env()
            try:
                tunnel.up()
                self.failures.pop(name, None)
            except Exception as exc:
                log.error("[%s] restart failed: %s", name, exc)
                tunnel.down()
                self.tunnels.pop(name, None)
                self._fingerprints.pop(name, None)
                self.failures[name] = str(exc)
                self._apply_haproxy(self.store.list())

    # ----------------------------------------------------------- supervision

    def _supervise(self) -> None:
        while not self._stop.wait(self.settings.supervise_interval):
            try:
                self._supervise_once()
            except Exception:
                log.exception("supervisor pass failed")

    def _supervise_once(self) -> None:
        with self._lock:
            for name, tunnel in list(self.tunnels.items()):
                dead = tunnel.dead_processes()
                if "awg" in dead:
                    log.warning("[%s] amneziawg-go died, rebuilding tunnel", name)
                    self.restart(name)
                    continue
                for key in dead:
                    try:
                        tunnel.restart_process(key)
                    except Exception as exc:
                        log.error("[%s] could not respawn %s: %s", name, key, exc)

                if time.time() - tunnel.started_at < _HANDSHAKE_GRACE:
                    continue
                age = tunnel.wg_stats()["handshake_age"]
                if age is None or age > self.settings.handshake_timeout:
                    log.warning("[%s] no handshake for %ss, rebuilding tunnel", name, age)
                    self.restart(name)

            if self.failures and time.monotonic() >= self._next_retry:
                self._next_retry = time.monotonic() + 60
                self.reconcile()

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        with self._lock:
            peers = self.store.list()
            states = self.haproxy.server_states()
            tunnels = []
            for peer in peers:
                tunnel = self.tunnels.get(peer.name)
                entry = {
                    "name": peer.name,
                    "enabled": peer.enabled,
                    "priority": peer.priority,
                    "endpoint": peer.config.endpoint,
                    "addresses": peer.config.addresses,
                    "dns": peer.config.dns,
                    "mtu": peer.config.mtu,
                    "state": "stopped",
                    "error": self.failures.get(peer.name),
                    "probe": None,
                    "haproxy": states.get(peer.name),
                    "handshake_age": None,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "uptime": None,
                }
                if not peer.enabled:
                    entry["state"] = "disabled"
                elif tunnel is None:
                    entry["state"] = "failed" if entry["error"] else "starting"
                else:
                    entry["state"] = "up"
                    entry["probe"] = tunnel.probe_status()
                    entry["uptime"] = round(time.time() - tunnel.started_at, 1)
                    stats = tunnel.wg_stats()
                    entry["peer_endpoint"] = stats.pop("endpoint")
                    entry.update(stats)
                tunnels.append(entry)

            return {
                "balance": self.store.balance,
                "probe_urls": self.effective_probe_env()["PROBE_URLS"],
                "socks_port": self.settings.socks_port,
                "haproxy_running": self.haproxy.running,
                "tunnels": tunnels,
            }

    def exit_ip(self, name: str) -> str:
        with self._lock:
            tunnel = self.tunnels.get(name)
            if tunnel is None:
                raise TunnelError(f"{name} is not running")
        return tunnel.exit_ip(self.settings.exit_ip_url)
