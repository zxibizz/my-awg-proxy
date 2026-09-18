FROM golang:1.25-alpine AS go-build
RUN apk add --no-cache git make
WORKDIR /src
RUN git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-go . \
    && CGO_ENABLED=0 make

# Built on the runtime base so the musl ABI matches exactly.
FROM alpine:3.21 AS awg-tools-build
RUN apk add --no-cache git make gcc musl-dev linux-headers
WORKDIR /awg
# Pinned: 3.1 is the release that added the awg3 parameters the configs rely on.
RUN git clone --depth 1 --branch v3.1.20260812 https://github.com/amnezia-vpn/amneziawg-tools . \
    && make -C src \
    && make -C src install PREFIX=/usr DESTDIR=/out

FROM alpine:3.21 AS microsocks-build
RUN apk add --no-cache git make gcc musl-dev
WORKDIR /microsocks
RUN git clone --depth 1 https://github.com/rofl0r/microsocks . \
    && make \
    && install -Dm755 microsocks /out/usr/bin/microsocks

FROM alpine:3.21
RUN apk add --no-cache iproute2 curl
COPY --from=go-build /src/amneziawg-go /usr/bin/amneziawg-go
COPY --from=awg-tools-build /out/ /
COPY --from=microsocks-build /out/ /
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 1080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
