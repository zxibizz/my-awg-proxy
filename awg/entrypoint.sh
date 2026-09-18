#!/usr/bin/env bash
set -euo pipefail

CONF=${CONF:-/etc/amnezia/awg0.conf}
# awg-quick names the interface after the file, and writes to it, so the
# read-only bind mount cannot be used directly.
WORK=/run/awg0.conf

install -m 600 "$CONF" "$WORK"
trap 'awg-quick down "$WORK" >/dev/null 2>&1 || true' EXIT TERM INT

awg-quick up "$WORK"
awg show

exec sleep infinity
