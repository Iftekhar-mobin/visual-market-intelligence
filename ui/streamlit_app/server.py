"""Starting and stopping the API from inside the console.

The console is useless without the API behind it, and the first thing anyone
sees on a fresh clone is "API unreachable" next to an instruction to open a
second terminal. This makes that a button instead.

It supervises a child process and nothing more. The console still speaks HTTP to
the API exactly as any other client would; a server started from a terminal, a
container or another machine works identically. Two things it is careful about:

* **It only manages what it started.** A server already answering on the port is
  reported as running and left alone — offering to stop someone else's process
  would be a lie at best.
* **It does not orphan the child.** The process is terminated when the console
  exits, so closing the tab does not leave a server holding the port until a
  reboot puzzles someone.
"""

from __future__ import annotations

import atexit
import contextlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})
"""Hosts we could plausibly start a server on. Anything else belongs to someone
else's machine, and the button is hidden rather than made to fail."""

STOP_TIMEOUT = 10.0


@dataclass(frozen=True)
class Target:
    host: str
    port: int

    @property
    def is_local(self) -> bool:
        return self.host in LOCAL_HOSTS


def parse_target(base_url: str) -> Target:
    """The host and port a base URL points at, with forgiving fallbacks."""
    base_url = (base_url or "").strip()
    try:
        parsed = urlparse(base_url if "//" in base_url else f"//{base_url}", scheme="http")
        host, port = parsed.hostname, parsed.port
    except ValueError:  # a non-numeric port
        host, port = None, None
    return Target(host=(host or "").strip() or DEFAULT_HOST, port=port or DEFAULT_PORT)


def is_answering(base_url: str, timeout: float = 2.0) -> bool:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/health", timeout=timeout)
    except httpx.RequestError:
        return False
    return response.status_code < 500


class ApiServer:
    """A supervised `uvicorn` child process.

    One instance is shared by every browser session (`st.cache_resource`), so two
    open tabs see one server rather than two.
    """

    def __init__(self, project_root: Path, log_path: Path) -> None:
        self._root = project_root
        self._log_path = log_path
        self._process: subprocess.Popen | None = None
        atexit.register(self.stop)

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def managed(self) -> bool:
        """Whether *this* console started the server that is running."""
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self.managed and self._process else None

    @property
    def exit_code(self) -> int | None:
        return self._process.poll() if self._process else None

    def start(self, target: Target) -> None:
        if self.managed:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = self._log_path.open("ab")
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "vmi.interfaces.api.app:app",
                "--host",
                target.host,
                "--port",
                str(target.port),
            ],
            cwd=str(self._root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
        if self._process is None:
            return
        with contextlib.suppress(Exception):
            self._process.terminate()
            self._process.wait(timeout=STOP_TIMEOUT)
        if self._process.poll() is None:
            with contextlib.suppress(Exception):
                self._process.kill()
        self._process = None

    def tail(self, lines: int = 40) -> str:
        if not self._log_path.exists():
            return "(no log yet)"
        content = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
