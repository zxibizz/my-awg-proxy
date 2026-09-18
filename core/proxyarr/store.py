"""On-disk peer configs plus the pool settings that order them."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .wgconf import NAME_RE, ConfigError, PeerConfig, parse

BALANCE_MODES = ("failover", "roundrobin", "leastconn")
MAX_PROBE_URLS = 10


def parse_probe_urls(value: str) -> list[str]:
    urls = [url for url in re.split(r"[,\s]+", value.strip()) if url]
    if not urls:
        raise ConfigError("at least one probe URL is required")
    if len(urls) > MAX_PROBE_URLS:
        raise ConfigError(f"at most {MAX_PROBE_URLS} probe URLs")
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(f"{url!r} is not an http(s) URL")
    return urls


@dataclass
class Peer:
    name: str
    enabled: bool
    priority: int
    text: str
    config: PeerConfig


class Store:
    def __init__(self, data_dir: Path, seed_dir: Path | None = None) -> None:
        self.peers_dir = data_dir / "peers"
        self.state_path = data_dir / "pool.json"
        self.peers_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if seed_dir is not None:
            self._seed(seed_dir)

    def _seed(self, seed_dir: Path) -> None:
        if not seed_dir.is_dir() or any(self.peers_dir.glob("*.conf")):
            return
        for src in sorted(seed_dir.glob("*.conf")):
            if NAME_RE.match(src.stem):
                shutil.copyfile(src, self.peers_dir / src.name)

    def _read_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        if state.get("balance") not in BALANCE_MODES:
            state["balance"] = "failover"
        if not isinstance(state.get("tunnels"), dict):
            state["tunnels"] = {}
        # Empty means "fall back to the PROBE_URLS baked into the environment".
        if not isinstance(state.get("probe_urls"), str):
            state["probe_urls"] = ""
        return state

    def _write_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, self.state_path)

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise ConfigError(
                "name must be 1-32 chars of letters, digits, dot, dash or underscore"
            )
        return self.peers_dir / f"{name}.conf"

    @property
    def balance(self) -> str:
        with self._lock:
            return self._read_state()["balance"]

    def set_balance(self, mode: str) -> None:
        if mode not in BALANCE_MODES:
            raise ConfigError(f"balance must be one of {', '.join(BALANCE_MODES)}")
        with self._lock:
            state = self._read_state()
            state["balance"] = mode
            self._write_state(state)

    @property
    def probe_urls(self) -> str:
        with self._lock:
            return self._read_state()["probe_urls"]

    def set_probe_urls(self, value: str) -> None:
        urls = parse_probe_urls(value)
        with self._lock:
            state = self._read_state()
            state["probe_urls"] = " ".join(urls)
            self._write_state(state)

    def list(self) -> list[Peer]:
        """Peers in pool order. Unparseable files are skipped, not fatal."""
        with self._lock:
            state = self._read_state()
            peers = []
            for path in sorted(self.peers_dir.glob("*.conf")):
                name = path.stem
                if not NAME_RE.match(name):
                    continue
                try:
                    text = path.read_text()
                    config = parse(text)
                except (OSError, ConfigError):
                    continue
                meta = state["tunnels"].get(name, {})
                peers.append(
                    Peer(
                        name=name,
                        enabled=bool(meta.get("enabled", True)),
                        priority=int(meta.get("priority", 100)),
                        text=text,
                        config=config,
                    )
                )
        peers.sort(key=lambda p: (p.priority, p.name))
        return peers

    def get(self, name: str) -> Peer | None:
        return next((p for p in self.list() if p.name == name), None)

    def save(self, name: str, text: str, enabled: bool = True, priority: int | None = None) -> Peer:
        path = self._path(name)
        config = parse(text)
        with self._lock:
            state = self._read_state()
            meta = state["tunnels"].setdefault(name, {})
            if priority is None:
                priority = meta.get("priority")
            if priority is None:
                used = {m.get("priority", 100) for m in state["tunnels"].values()}
                priority = max(used, default=0) + 10
            meta["enabled"] = enabled
            meta["priority"] = int(priority)
            path.write_text(text)
            self._write_state(state)
        return Peer(name=name, enabled=enabled, priority=int(priority), text=text, config=config)

    def update(self, name: str, *, enabled: bool | None = None, priority: int | None = None) -> None:
        with self._lock:
            if not self._path(name).exists():
                raise KeyError(name)
            state = self._read_state()
            meta = state["tunnels"].setdefault(name, {})
            if enabled is not None:
                meta["enabled"] = bool(enabled)
            if priority is not None:
                meta["priority"] = int(priority)
            self._write_state(state)

    def delete(self, name: str) -> None:
        with self._lock:
            path = self._path(name)
            if not path.exists():
                raise KeyError(name)
            path.unlink()
            state = self._read_state()
            state["tunnels"].pop(name, None)
            self._write_state(state)
