FROM golang:1.25-alpine AS go-build
RUN apk add --no-cache git make
WORKDIR /src
RUN git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-go . && make

FROM alpine:3.21 AS tools-build
RUN apk add --no-cache git make gcc musl-dev linux-headers bash
WORKDIR /src
# Pinned: 3.1 is the release that added the awg3 parameters the configs rely on.
RUN git clone --depth 1 --branch v3.1.20260812 https://github.com/amnezia-vpn/amneziawg-tools . \
    && make -C src WITH_WGQUICK=yes \
    && make -C src install WITH_WGQUICK=yes PREFIX=/usr DESTDIR=/out

FROM alpine:3.21
RUN apk add --no-cache bash iproute2 iptables ip6tables openresolv
COPY --from=go-build /src/amneziawg-go /usr/bin/amneziawg-go
COPY --from=tools-build /out/ /
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    # awg-quick sets this sysctl unconditionally, but /proc/sys is read-only in a
    # container. Compose sets the value instead, so the write is redundant.
    && sed -i 's#cmd sysctl -q net.ipv4.conf.all.src_valid_mark=1#{ cmd sysctl -q net.ipv4.conf.all.src_valid_mark=1 || true; }#' /usr/bin/awg-quick

# awg-quick finds no kernel module in a container and needs the userspace name.
ENV WG_QUICK_USERSPACE_IMPLEMENTATION=amneziawg-go \
    AWG_QUICK_USERSPACE_IMPLEMENTATION=amneziawg-go

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
