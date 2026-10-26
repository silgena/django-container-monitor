"""Settings, read from ``settings.CONTAINER_MONITOR`` with these defaults."""

from django.conf import settings

DEFAULTS = {
    # The Docker Engine API: a Unix socket or a TCP address. Prefer a
    # read-only proxy (tecnativa/docker-socket-proxy with CONTAINERS=1) to
    # mounting the socket itself, which is root on the host. None skips
    # containers and watches the host only.
    "DOCKER_URL": "unix:///var/run/docker.sock",
    "DOCKER_TIMEOUT_SECONDS": 10,
    # Containers whose name matches (fnmatch) are judged; others are listed.
    "CONTAINERS": ["*"],
    # Where host figures come from. Inside a container /proc is still the
    # host's, so the worker running the check sees the whole machine.
    "PROC_ROOT": "/proc",
    "DISK_PATHS": ["/"],
    # (warning, danger) in percent; load is the 5-minute average per core.
    "THRESHOLDS": {
        "memory": (85, 93),
        "swap": (25, 60),
        "disk": (80, 90),
        "cpu": (85, 97),
        "load_per_core": (1.0, 2.0),
        "container_memory": (85, 95),
    },
    # Where alerts go. Each: {"BACKEND": dotted path, "OPTIONS": {...},
    # "MIN_STATUS": "warning" or "danger"}.
    "ALERTS": [{"BACKEND": "container_monitor.alerts.LogAlert"}],
    # While a problem lasts, alert again this often (None: only on change).
    "REPEAT_ALERT_MINUTES": 60,
    "RETENTION_DAYS": 30,
    # A token for the status endpoint (Authorization: Bearer <token>). Without
    # one the endpoint answers staff users only.
    "TOKEN": None,
    "LOG_LINES": 200,
}


def get(name):
    return getattr(settings, "CONTAINER_MONITOR", {}).get(name, DEFAULTS[name])


def threshold(name):
    return {**DEFAULTS["THRESHOLDS"], **get("THRESHOLDS")}[name]
