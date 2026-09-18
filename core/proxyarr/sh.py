"""Thin wrappers around the iproute2/iptables commands the manager drives."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("proxyarr.sh")


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"{' '.join(argv)} exited {returncode}: {stderr.strip()}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


def run(*argv: str, check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess:
    log.debug("run %s", " ".join(argv))
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and proc.returncode != 0:
        raise CommandError(list(argv), proc.returncode, proc.stderr)
    return proc


def quiet(*argv: str) -> None:
    """Best-effort cleanup call; failures are expected and ignored."""
    try:
        run(*argv, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def ip(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("ip", *argv, check=check)


def netns_exec(netns: str, *argv: str, check: bool = True, timeout: float = 30):
    return run("ip", "netns", "exec", netns, *argv, check=check, timeout=timeout)


def netns_exec_pid(pid: int, *argv: str, check: bool = True, timeout: float = 30):
    return run("nsenter", "-t", str(pid), "-m", "-n", "--", *argv,
               check=check, timeout=timeout)


def ip_pid(pid: int, *argv: str, check: bool = True):
    return netns_exec_pid(pid, "ip", *argv, check=check)
