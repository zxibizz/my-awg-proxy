"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_PROBE_DEFAULTS = {
    "PROBE_URLS": "https://rutracker.org/forum/index.php",
    "PROBE_MODE": "all",
    "PROBE_INTERVAL": "60",
    "PROBE_BAD_STATUS": "403,429,503",
}


@dataclass
class Settings:
    data_dir: Path = Path("/data")
    seed_dir: Path = Path("/seed")
    probe_script: Path = Path("/app/probe.py")
    run_dir: Path = Path("/run/proxyarr")
    token: str = ""
    api_port: int = 8080
    socks_port: int = 1080
    fallback_dns: list[str] = field(default_factory=lambda: ["1.1.1.1", "9.9.9.9"])
    exit_ip_url: str = "https://api.ipify.org"
    # A tunnel whose peer has gone quiet for this long is rebuilt from scratch.
    handshake_timeout: float = 180.0
    supervise_interval: float = 10.0
    probe_env: dict[str, str] = field(default_factory=dict)


def load() -> Settings:
    env = os.environ
    settings = Settings(
        data_dir=Path(env.get("PROXYARR_DATA_DIR", "/data")),
        seed_dir=Path(env.get("PROXYARR_SEED_DIR", "/seed")),
        token=env.get("PROXYARR_TOKEN", ""),
        api_port=int(env.get("PROXYARR_API_PORT", "8080")),
        socks_port=int(env.get("PROXYARR_SOCKS_PORT", "1080")),
        exit_ip_url=env.get("PROXYARR_EXIT_IP_URL", "https://api.ipify.org"),
        handshake_timeout=float(env.get("PROXYARR_HANDSHAKE_TIMEOUT", "180")),
    )
    if fallback := env.get("PROXYARR_FALLBACK_DNS", "").strip():
        settings.fallback_dns = [part for part in fallback.replace(",", " ").split() if part]
    settings.probe_env = {
        **_PROBE_DEFAULTS,
        **{key: value for key, value in env.items() if key.startswith("PROBE_")},
    }
    return settings
