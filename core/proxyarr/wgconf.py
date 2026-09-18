"""Parse and sanitise AmneziaWG/WireGuard peer configs.

Tunnels are brought up with `awg setconf` rather than `awg-quick`, so the
PreUp/PostUp/PreDown/PostDown hooks -- which awg-quick would execute as root --
are never run. Configs arrive from the web UI, so those keys are rejected
loudly instead of being silently dropped.

Everything that is not a hook and not consumed here is passed through verbatim:
AmneziaWG keeps adding obfuscation parameters, and `awg setconf` is a better
authority on which ones it understands than a list that has to be maintained.
"""

from __future__ import annotations

import base64
import ipaddress
import re
from dataclasses import dataclass, field

# Only used to normalise casing; unknown keys are still passed through.
_KNOWN_KEYS = [
    "PrivateKey", "ListenPort", "FwMark",
    "PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive",
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4",
    "I1", "I2", "I3", "I4", "I5", "J1", "J2", "J3", "Itime",
    "HeaderProtectionKey", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
    "KeepaliveTimeout", "MaxHandshakeAttempts", "ContentPaddingAddition",
]
_CANON = {key.lower(): key for key in _KNOWN_KEYS}

# awg-quick directives this code interprets itself instead of passing through.
_LOCAL_KEYS = {"address", "dns", "mtu", "table", "saveconfig"}
_HOOK_KEYS = {"preup", "postup", "predown", "postdown"}
# Base64 32-byte values, worth failing fast on.
_KEY_VALUED = {"privatekey", "publickey", "presharedkey"}

_SECTION_RE = re.compile(r"^\[(\w+)\]$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_ENDPOINT_RE = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:]+)\]|(?P<host>[^:]+)):(?P<port>\d{1,5})$")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

DEFAULT_MTU = 1420


class ConfigError(ValueError):
    """Raised when a submitted peer config is unusable."""


@dataclass
class PeerConfig:
    addresses: list[str] = field(default_factory=list)
    dns: list[str] = field(default_factory=list)
    mtu: int = DEFAULT_MTU
    endpoint_host: str = ""
    endpoint_port: int = 0
    wg_text: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.endpoint_host}:{self.endpoint_port}"

    @property
    def has_ipv6(self) -> bool:
        return any(":" in addr for addr in self.addresses)


def _valid_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except Exception:
        return False


def _split_list(value: str) -> list[str]:
    return [item for item in re.split(r"[,\s]+", value) if item]


def _parse_addresses(value: str) -> list[str]:
    out = []
    for item in _split_list(value):
        try:
            ipaddress.ip_interface(item)
        except ValueError as exc:
            raise ConfigError(f"invalid Address {item!r}: {exc}") from exc
        out.append(item)
    return out


def _parse_dns(value: str) -> list[str]:
    out = []
    for item in _split_list(value):
        try:
            ipaddress.ip_address(item)
        except ValueError:
            # awg-quick also accepts search domains here; they are not useful
            # in a resolv.conf built from scratch for the namespace.
            continue
        out.append(item)
    return out


def _parse_endpoint(value: str) -> tuple[str, int]:
    match = _ENDPOINT_RE.match(value.strip())
    if not match:
        raise ConfigError(f"invalid Endpoint {value!r}, expected host:port")
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        raise ConfigError(f"invalid Endpoint port {port}")
    return match.group("v6") or match.group("host"), port


def parse(text: str) -> PeerConfig:
    """Validate a peer config and render the subset `awg setconf` accepts."""
    cfg = PeerConfig()
    section = ""
    interface_lines: list[str] = []
    peer_blocks: list[list[str]] = []
    seen_private_key = False
    peers_with_public_key = 0

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1).lower()
            if section == "peer":
                peer_blocks.append([])
            elif section != "interface":
                raise ConfigError(f"line {lineno}: unknown section [{match.group(1)}]")
            continue

        if "=" not in line:
            raise ConfigError(f"line {lineno}: expected 'Key = value'")
        key, value = (part.strip() for part in line.split("=", 1))
        lkey = key.lower()

        if not section:
            raise ConfigError(f"line {lineno}: {key} appears before any section")
        if not _KEY_RE.match(key):
            raise ConfigError(f"line {lineno}: {key!r} is not a valid setting name")
        if lkey in _HOOK_KEYS:
            raise ConfigError(
                f"line {lineno}: {key} is not supported -- tunnels are configured "
                "directly and never run commands from a config file"
            )

        if section == "interface" and lkey in _LOCAL_KEYS:
            if lkey == "address":
                cfg.addresses = _parse_addresses(value)
            elif lkey == "dns":
                cfg.dns = _parse_dns(value)
            elif lkey == "mtu":
                try:
                    cfg.mtu = int(value)
                except ValueError as exc:
                    raise ConfigError(f"line {lineno}: invalid MTU {value!r}") from exc
                if not 1280 <= cfg.mtu <= 9000:
                    raise ConfigError(f"line {lineno}: MTU {cfg.mtu} out of range")
            continue

        if lkey in _KEY_VALUED and not _valid_key(value):
            raise ConfigError(f"line {lineno}: {key} is not a valid base64 key")

        canon = _CANON.get(lkey, key)
        if section == "interface":
            if lkey == "privatekey":
                seen_private_key = True
            interface_lines.append(f"{canon} = {value}")
        else:
            if lkey == "publickey":
                peers_with_public_key += 1
            if lkey == "endpoint" and not cfg.endpoint_host:
                cfg.endpoint_host, cfg.endpoint_port = _parse_endpoint(value)
            peer_blocks[-1].append(f"{canon} = {value}")

    if not seen_private_key:
        raise ConfigError("[Interface] is missing PrivateKey")
    if not peer_blocks:
        raise ConfigError("config has no [Peer] section")
    if peers_with_public_key != len(peer_blocks):
        raise ConfigError("every [Peer] needs a PublicKey")
    if not cfg.addresses:
        raise ConfigError("[Interface] is missing Address")
    if not cfg.endpoint_host:
        raise ConfigError("[Peer] is missing Endpoint")

    chunks = ["[Interface]", *interface_lines]
    for block in peer_blocks:
        chunks += ["", "[Peer]", *block]
    cfg.wg_text = "\n".join(chunks) + "\n"
    return cfg
