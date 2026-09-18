#!/usr/bin/env python3
"""Health endpoint reporting whether the probe targets are usable from this netns.

One instance runs inside each tunnel's network namespace, so it probes that
tunnel's own exit IP. Serves 200 while the target answers normally, 503 once it
starts throttling -- which is what haproxy fails over on.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROBE_URLS = [
    url
    for url in re.split(
        r"[,\s]+",
        os.environ.get("PROBE_URLS")
        or os.environ.get("PROBE_URL", "https://rutracker.org/forum/index.php"),
    )
    if url
]
# "all" trips failover as soon as one URL misbehaves, "any" only once every URL does.
PROBE_MODE = os.environ.get("PROBE_MODE", "all").strip().lower()
PROBE_INTERVAL = float(os.environ.get("PROBE_INTERVAL", "60"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "15"))
# DNS lives inside the tunnel, so a re-handshake briefly breaks resolution. Without
# retries a blip benches an otherwise healthy tunnel for a whole interval.
PROBE_RETRIES = int(os.environ.get("PROBE_RETRIES", "2"))
PROBE_RETRY_DELAY = float(os.environ.get("PROBE_RETRY_DELAY", "3"))
# Tunnel DNS gets slow under load, so a single bad cycle is usually a blip rather
# than throttling. Only a repeat takes the tunnel out of the pool.
PROBE_FAILURE_THRESHOLD = max(1, int(os.environ.get("PROBE_FAILURE_THRESHOLD", "2")))
# After a failure, re-check soon instead of benching the tunnel for a full interval.
PROBE_RECHECK_INTERVAL = float(os.environ.get("PROBE_RECHECK_INTERVAL", "10"))
PROBE_BAD_STATUS = {
    int(code)
    for code in os.environ.get("PROBE_BAD_STATUS", "403,429,503").split(",")
    if code.strip()
}
PROBE_BAD_TEXT = os.environ.get("PROBE_BAD_TEXT", "").strip()
PROBE_COOKIE = os.environ.get("PROBE_COOKIE", "").strip()
PROBE_USER_AGENT = os.environ.get("PROBE_USER_AGENT", "Mozilla/5.0")
START_DELAY = float(os.environ.get("START_DELAY", "0"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9998"))

_bad_text = re.compile(PROBE_BAD_TEXT, re.IGNORECASE) if PROBE_BAD_TEXT else None
_lock = threading.Lock()
# None, not False, so the first verdict counts as a change and always gets logged.
_state = {"healthy": None, "reason": "starting up", "checked_at": 0.0}

if PROBE_MODE not in {"all", "any"}:
    raise SystemExit(f"PROBE_MODE must be 'all' or 'any', got {PROBE_MODE!r}")


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {message}", flush=True)


def probe_url(url: str) -> tuple[bool, str]:
    headers = {"User-Agent": PROBE_USER_AGENT}
    if PROBE_COOKIE:
        headers["Cookie"] = PROBE_COOKIE

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            status = response.status
            body = response.read(131072).decode("utf-8", "replace") if _bad_text else ""
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(131072).decode("utf-8", "replace") if _bad_text else ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if status in PROBE_BAD_STATUS:
        return False, f"HTTP {status}"
    if _bad_text and _bad_text.search(body):
        return False, f"throttle marker matched (HTTP {status})"
    return True, f"HTTP {status}"


def probe_url_retrying(url: str) -> tuple[bool, str]:
    for attempt in range(PROBE_RETRIES + 1):
        healthy, reason = probe_url(url)
        if healthy:
            return True, reason
        if attempt < PROBE_RETRIES:
            time.sleep(PROBE_RETRY_DELAY)
    return False, reason


def probe() -> tuple[bool, str]:
    results = [(url, *probe_url_retrying(url)) for url in PROBE_URLS]
    failed = [(url, reason) for url, ok, reason in results if not ok]

    healthy = len(failed) < len(results) if PROBE_MODE == "any" else not failed
    summary = f"{len(results) - len(failed)}/{len(results)} ok"
    if failed:
        summary += " | " + "; ".join(f"{url}: {reason}" for url, reason in failed)
    return healthy, summary


def probe_loop() -> None:
    first = True
    failures = 0
    while True:
        passed, reason = probe()
        if passed:
            failures = 0
            healthy = True
        else:
            failures += 1
            healthy = _state["healthy"] is True and failures < PROBE_FAILURE_THRESHOLD
            if healthy:
                reason = f"degraded {failures}/{PROBE_FAILURE_THRESHOLD}: {reason}"
        with _lock:
            changed = healthy != _state["healthy"]
            _state.update(healthy=healthy, reason=reason, checked_at=time.time())
        if changed:
            log(f"{'healthy' if healthy else 'UNHEALTHY'}: {reason}")
        # The first verdict should be available immediately; stagger later cycles.
        if first:
            delay = START_DELAY
        else:
            delay = PROBE_INTERVAL if passed else PROBE_RECHECK_INTERVAL
        time.sleep(delay)
        first = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        with _lock:
            healthy, reason = _state["healthy"], _state["reason"]
        body = f"{'ok' if healthy else 'down'}: {reason}\n".encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *args: object) -> None:
        """Silences a line per health check every few seconds."""


if __name__ == "__main__":
    log(
        f"probing {len(PROBE_URLS)} url(s) every {PROBE_INTERVAL:.0f}s "
        f"(mode={PROBE_MODE}), serving :{LISTEN_PORT}"
    )
    threading.Thread(target=probe_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
