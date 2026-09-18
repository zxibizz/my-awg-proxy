#!/bin/sh
# Brings up one AmneziaWG peer in this container's own network namespace and
# serves SOCKS5 over it. One peer per container, so there is no netns, veth or
# NAT plumbing -- the container boundary already provides the isolation.
set -eu

CONF=${WG_CONFIG:-/config/wg.conf}
IFACE=awg0
PORT=${SOCKS_PORT:-1080}

[ -r "$CONF" ] || { echo "no readable config at $CONF" >&2; exit 1; }
umask 077

# First value of a wg-quick key, with surrounding whitespace stripped. Values may
# themselves contain '=' (base64 keys), so only the first one separates.
value_of() {
    awk -F= -v key="$1" '
        {
            k = $1
            gsub(/[ \t]/, "", k)
            if (tolower(k) == key) {
                sub(/^[^=]*=/, "")
                gsub(/^[ \t]+|[ \t]+$/, "")
                print
                exit
            }
        }' "$CONF"
}

ADDRESSES=$(value_of address | tr ',' ' ')
NAMESERVERS=$(value_of dns | tr ',' ' ')
ENDPOINT=$(value_of endpoint)
MTU=$(value_of mtu)
MTU=${MTU:-1420}

[ -n "$ADDRESSES" ] || { echo "config has no Address" >&2; exit 1; }
[ -n "$ENDPOINT" ] || { echo "config has no Endpoint" >&2; exit 1; }

EP_HOST=$(printf '%s' "${ENDPOINT%:*}" | tr -d '[]')

# awg setconf only understands protocol keys; the hooks would additionally run
# as root, so they are dropped rather than honoured.
STRIPPED=/tmp/$IFACE.conf
grep -viE '^[[:space:]]*(address|dns|mtu|table|saveconfig|preup|postup|predown|postdown)[[:space:]]*=' \
    "$CONF" > "$STRIPPED"

# Resolve while the container's own resolver and default route still apply.
GW=$(ip -4 route show default | awk '{print $3; exit}')
DEV=$(ip -4 route show default | awk '{print $5; exit}')
[ -n "$GW" ] || { echo "no default route to reach the peer endpoint" >&2; exit 1; }

EP_IPS=$(getent ahostsv4 "$EP_HOST" | awk '{print $1}' | sort -u)
[ -n "$EP_IPS" ] || { echo "cannot resolve endpoint $EP_HOST" >&2; exit 1; }

amneziawg-go -f "$IFACE" &
AWG_PID=$!

i=0
while [ "$i" -lt 75 ]; do
    ip link show "$IFACE" >/dev/null 2>&1 && break
    kill -0 "$AWG_PID" 2>/dev/null || { echo "amneziawg-go exited during startup" >&2; exit 1; }
    sleep 0.2
    i=$((i + 1))
done
ip link show "$IFACE" >/dev/null 2>&1 || { echo "$IFACE did not appear" >&2; exit 1; }

HAS_V6=no
for addr in $ADDRESSES; do
    case $addr in
        *:*) ip -6 addr add "$addr" dev "$IFACE"; HAS_V6=yes ;;
        *) ip -4 addr add "$addr" dev "$IFACE" ;;
    esac
done
ip link set dev "$IFACE" mtu "$MTU" up

# Pin the endpoint to the original gateway before the tunnel takes the default
# route, otherwise the handshake would route into itself.
for ep in $EP_IPS; do
    ip route add "$ep/32" via "$GW" dev "$DEV"
done
ip route replace default dev "$IFACE"
if [ "$HAS_V6" = yes ]; then
    ip -6 route replace default dev "$IFACE" || true
fi

if [ -n "$NAMESERVERS" ]; then
    # Truncated in place: /etc/resolv.conf is a bind mount and cannot be replaced.
    : > /etc/resolv.conf
    for ns in $NAMESERVERS; do
        echo "nameserver $ns" >> /etc/resolv.conf
    done
fi

# Loading the peer is last: amneziawg-go handshakes as soon as it has one, and a
# handshake sent before the endpoint route exists costs a back-off window.
awg setconf "$IFACE" "$STRIPPED"
rm -f "$STRIPPED"

microsocks -i 0.0.0.0 -p "$PORT" -q &
SOCKS_PID=$!

trap 'kill "$AWG_PID" "$SOCKS_PID" 2>/dev/null || true' INT TERM

echo "up: $ENDPOINT -> socks5 on :$PORT (mtu $MTU)"

# Exit if either half dies so the restart policy rebuilds a working container,
# rather than leaving a socks server attached to a dead tunnel.
while kill -0 "$AWG_PID" 2>/dev/null && kill -0 "$SOCKS_PID" 2>/dev/null; do
    sleep 5
done

echo "amneziawg-go or microsocks exited; stopping" >&2
kill "$AWG_PID" "$SOCKS_PID" 2>/dev/null || true
exit 1
