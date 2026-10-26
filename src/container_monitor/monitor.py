"""One check: measure the host and containers, judge them, store, alert."""

from __future__ import annotations

import fnmatch
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.utils import timezone
from django.utils.module_loading import import_string

from container_monitor import conf, host
from container_monitor.docker import (
    DockerClient,
    DockerUnavailable,
    cpu_percent,
    memory_usage,
)
from container_monitor.models import Alert, Check, Status
from container_monitor.signals import status_changed

logger = logging.getLogger("container_monitor")


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------
def _describe(client, summary) -> dict:
    name = (summary.get("Names") or ["/?"])[0].lstrip("/")
    detail = client.inspect(summary["Id"])
    state = detail.get("State") or {}
    host_config = detail.get("HostConfig") or {}
    entry = {
        "name": name,
        "image": summary.get("Image", ""),
        "state": summary.get("State", ""),
        "status_text": summary.get("Status", ""),
        "health": (state.get("Health") or {}).get("Status", ""),
        "restart_count": detail.get("RestartCount", 0),
        "oom_killed": bool(state.get("OOMKilled")),
        "exit_code": state.get("ExitCode"),
        "memory_limit": host_config.get("Memory") or None,
        "cpu_limit": (host_config.get("NanoCpus") or 0) / 1e9 or None,
    }
    if entry["state"] == "running":
        stats = client.stats(summary["Id"])
        entry["cpu_percent"] = cpu_percent(stats)
        entry["memory_usage"] = usage = memory_usage(stats)
        if usage is not None and entry["memory_limit"]:
            entry["memory_percent"] = round(usage / entry["memory_limit"] * 100, 1)
    return entry


def containers(client) -> tuple[list[dict], str]:
    """Every container on the host, or ([], why not)."""
    if client is None:
        return [], ""
    try:
        summaries = client.containers()
        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(
                pool.map(lambda summary: _describe(client, summary), summaries)
            )
    except DockerUnavailable as exc:
        return [], str(exc)
    return sorted(entries, key=lambda entry: entry["name"]), ""


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------
def _level(value, name):
    warning, danger = conf.threshold(name)
    if value is None or value < warning:
        return Status.OK
    return Status.DANGER if value >= danger else Status.WARNING


def _finding(findings, value, name, text):
    level = _level(value, name)
    if level != Status.OK:
        limit = conf.threshold(name)[1 if level == Status.DANGER else 0]
        findings.append((level, f"{text} ({level} at {limit:g})"))


def judge_host(metrics) -> list:
    findings = []
    if metrics.get("memory_percent") is not None:
        _finding(
            findings,
            metrics["memory_percent"],
            "memory",
            f"host memory {metrics['memory_percent']:g}% used",
        )
    if metrics.get("swap_total"):
        _finding(
            findings,
            metrics["swap_percent"],
            "swap",
            f"swap {metrics['swap_percent']:g}% used",
        )
    for disk in metrics.get("disks", []):
        _finding(
            findings,
            disk["percent"],
            "disk",
            f"disk {disk['path']} {disk['percent']:g}% used",
        )
    if metrics.get("cpu_percent") is not None:
        _finding(
            findings, metrics["cpu_percent"], "cpu", f"CPU {metrics['cpu_percent']:g}%"
        )
    if metrics.get("load_5") is not None and (cores := metrics.get("cpu_count")):
        per_core = round(metrics["load_5"] / cores, 2)
        _finding(
            findings,
            per_core,
            "load_per_core",
            f"5-min load {metrics['load_5']:g} on {cores} cores",
        )
    return findings


def judge_container(entry, previous) -> list:
    name, findings = entry["name"], []
    if entry["state"] != "running":
        return [
            (
                Status.DANGER,
                f"{name} is {entry['state']} (exit code {entry.get('exit_code')})",
            )
        ]
    if entry.get("health") == "unhealthy":
        findings.append((Status.DANGER, f"{name} is unhealthy"))
    elif entry.get("health") == "starting":
        findings.append((Status.WARNING, f"{name} is still starting"))
    if entry.get("oom_killed"):
        findings.append((Status.DANGER, f"{name} was killed for memory"))
    if (
        previous
        and (restarts := entry["restart_count"] - previous.get("restart_count", 0)) > 0
    ):
        findings.append(
            (Status.WARNING, f"{name} restarted {restarts}x since the last check")
        )
    if entry.get("memory_percent") is not None:
        _finding(
            findings,
            entry["memory_percent"],
            "container_memory",
            f"{name} memory {entry['memory_percent']:g}% of its limit",
        )
    return findings


def _judged(name) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in conf.get("CONTAINERS"))


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def run_check(*, client=..., proc_root=None, sample_seconds: float = 1.0) -> Check:
    """Measure, judge, store, and alert on a change. Return the stored Check."""
    started = time.monotonic()
    if client is ...:
        client = DockerClient() if conf.get("DOCKER_URL") else None
    previous = Check.objects.order_by("-checked_at").first()
    before = {
        entry["name"]: entry for entry in (previous.containers if previous else [])
    }

    metrics = host.measure(
        proc_root or conf.get("PROC_ROOT"), conf.get("DISK_PATHS"), sample_seconds
    )
    found, docker_error = containers(client)
    findings = judge_host(metrics)
    for entry in found:
        entry["judged"] = _judged(entry["name"])
        own = (
            judge_container(entry, before.get(entry["name"])) if entry["judged"] else []
        )
        entry["status"] = Status.worst(level for level, _ in own)
        entry["reasons"] = [text for _, text in own]
        findings.extend(own)
    if docker_error:
        findings.append(
            (Status.WARNING, f"container figures unavailable: {docker_error}")
        )
    findings.sort(key=lambda finding: -Status.rank(finding[0]))

    check = Check.objects.create(
        checked_at=timezone.now(),
        status=Status.worst(level for level, _ in findings),
        reasons=[text for _, text in findings],
        host=metrics,
        containers=found,
        docker_error=docker_error,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    _alert(check, previous)
    return check


def _alert(check, previous) -> None:
    previous_status = previous.status if previous else Status.OK
    changed = check.status != previous_status
    if changed:
        status_changed.send(
            Check, check=check, previous_status=previous_status, status=check.status
        )
    elif check.status == Status.OK or not _repeat_due(check):
        return
    subject = (
        f"[{check.status.upper()}] server health"
        if check.status != Status.OK
        else "[OK] server health recovered"
    )
    message = (
        "\n".join(f"- {reason}" for reason in check.reasons)
        or "Everything is within limits."
    )
    for channel in conf.get("ALERTS"):
        minimum = channel.get("MIN_STATUS", Status.WARNING)
        involved = max(Status.rank(check.status), Status.rank(previous_status))
        if involved < Status.rank(minimum):
            continue
        alert = Alert(
            check_run=check,
            channel=channel["BACKEND"],
            previous_status=previous_status,
            status=check.status,
            message=f"{subject}\n{message}",
        )
        try:
            import_string(channel["BACKEND"])(**channel.get("OPTIONS", {})).send(
                subject, message, check
            )
            alert.delivered = True
        except Exception as exc:
            logger.exception("Alert through %s failed", channel["BACKEND"])
            alert.error = f"{type(exc).__name__}: {exc}"
        alert.save()


def _repeat_due(check) -> bool:
    minutes = conf.get("REPEAT_ALERT_MINUTES")
    if not minutes:
        return False
    last = Alert.objects.filter(status=check.status).order_by("-sent_at").first()
    return last is None or last.sent_at <= timezone.now() - timedelta(minutes=minutes)


def prune() -> int:
    cutoff = timezone.now() - timedelta(days=conf.get("RETENTION_DAYS"))
    return Check.objects.filter(checked_at__lt=cutoff).delete()[0]
