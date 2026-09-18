FROM golang:1.25-alpine AS go-build
RUN apk add --no-cache git make
WORKDIR /src
RUN git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-go . \
    && CGO_ENABLED=0 make

# Built on the runtime base so the musl ABI matches exactly.
FROM python:3.12-alpine AS c-build
RUN apk add --no-cache git make gcc musl-dev linux-headers
WORKDIR /awg
# Pinned: 3.1 is the release that added the awg3 parameters the configs rely on.
RUN git clone --depth 1 --branch v3.1.20260812 https://github.com/amnezia-vpn/amneziawg-tools . \
    && make -C src \
    && make -C src install PREFIX=/usr DESTDIR=/out
WORKDIR /microsocks
RUN git clone --depth 1 https://github.com/rofl0r/microsocks . \
    && make \
    && install -Dm755 microsocks /out/usr/bin/microsocks

FROM python:3.12-alpine
RUN apk add --no-cache iproute2 iptables ip6tables haproxy curl
COPY --from=go-build /src/amneziawg-go /usr/bin/amneziawg-go
COPY --from=c-build /out/ /

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY probe.py ./
COPY proxyarr ./proxyarr

ENV PYTHONUNBUFFERED=1
EXPOSE 1080 8080
ENTRYPOINT ["python3", "-m", "proxyarr.main"]
