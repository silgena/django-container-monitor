"""A read-only Docker Engine API client using only the standard library.

It speaks HTTP over a Unix socket (``unix:///var/run/docker.sock``) or TCP
(``http://docker-proxy:2375``), and only ever sends GET requests: list,
inspect, one stats sample, and a log tail.
"""

from __future__ import annotations

import http.client
import json
import socket
import struct
from urllib.parse import urlencode, urlsplit

from container_monitor import conf


class DockerUnavailable(Exception):
    """The Docker API could not be reached or answered with an error."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerClient:
    def __init__(self, url: str | None = None, timeout: float | None = None):
        self.url = url or conf.get("DOCKER_URL")
        self.timeout = timeout or conf.get("DOCKER_TIMEOUT_SECONDS")

    def _connection(self) -> http.client.HTTPConnection:
        parts = urlsplit(self.url)
        if parts.scheme == "unix":
            return _UnixConnection(parts.path, self.timeout)
        if parts.scheme == "https":
            return http.client.HTTPSConnection(parts.netloc, timeout=self.timeout)
        return http.client.HTTPConnection(parts.netloc, timeout=self.timeout)

    def _get(self, path: str, **params) -> bytes:
        connection = self._connection()
        try:
            connection.request("GET", f"{path}?{urlencode(params)}" if params else path)
            response = connection.getresponse()
            body = response.read()
        except OSError as exc:
            raise DockerUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            connection.close()
        if response.status >= 400:
            raise DockerUnavailable(f"HTTP {response.status}: {body[:200]!r}")
        return body

    def containers(self) -> list[dict]:
        return json.loads(self._get("/containers/json", all="true"))

    def inspect(self, container: str) -> dict:
        return json.loads(self._get(f"/containers/{container}/json"))

    def stats(self, container: str) -> dict:
        return json.loads(self._get(f"/containers/{container}/stats", stream="false"))

    def logs(self, container: str, lines: int) -> str:
        return demux(
            self._get(
                f"/containers/{container}/logs", stdout="1", stderr="1", tail=str(lines)
            )
        )


def demux(payload: bytes) -> str:
    """Docker's multiplexed log stream (8-byte frame headers) to text."""
    if len(payload) < 8 or payload[0] not in (0, 1, 2):
        return payload.decode("utf-8", errors="replace")
    chunks, offset = [], 0
    while offset + 8 <= len(payload):
        size = struct.unpack(">I", payload[offset + 4 : offset + 8])[0]
        chunks.append(payload[offset + 8 : offset + 8 + size])
        offset += 8 + size
    return b"".join(chunks).decode("utf-8", errors="replace")


def cpu_percent(stats: dict) -> float | None:
    """CPU as `docker stats` shows it: 100% is one full core."""
    try:
        cpu, previous = stats["cpu_stats"], stats["precpu_stats"]
        used = cpu["cpu_usage"]["total_usage"] - previous["cpu_usage"]["total_usage"]
        system = cpu["system_cpu_usage"] - previous.get("system_cpu_usage", 0)
    except (KeyError, TypeError):
        return None
    cores = (
        cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1
    )
    return round(used / system * cores * 100, 1) if system > 0 and used >= 0 else 0.0


def memory_usage(stats: dict) -> int | None:
    """Memory as `docker stats` shows it: usage minus reclaimable page cache."""
    memory = stats.get("memory_stats") or {}
    if memory.get("usage") is None:
        return None
    detail = memory.get("stats") or {}
    return max(
        memory["usage"]
        - detail.get("inactive_file", detail.get("total_inactive_file", 0)),
        0,
    )
