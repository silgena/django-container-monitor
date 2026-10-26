# django-container-monitor

Watch the server and the Docker containers behind a Django project, from the
Django admin.

- **Host and containers.** CPU, load, memory, swap and disks of the host;
  state, health, restarts, CPU and memory against its own limit for every
  container.
- **A verdict.** Each check is OK, warning or danger, with the reasons, worst
  first. Thresholds are settings.
- **Alerts on change.** When the status changes, and on recovery; again every
  `REPEAT_ALERT_MINUTES` while a problem lasts. Log, e-mail, webhooks (Slack,
  Mattermost, Discord, Teams through a workflow, anything that takes JSON), a
  Django signal, or your own channel.
- **Container logs in the admin.** The last lines of any container, read on
  demand.
- **A status endpoint** for uptime monitors: JSON, HTTP 503 on danger.
- **No dependencies.** The Docker API is spoken over the standard library, via
  the socket or a read-only proxy.

## Install

```bash
pip install django-container-monitor
```

```python
INSTALLED_APPS = [..., "container_monitor"]

# urls.py - optional, for the status endpoint
path("monitor/", include("container_monitor.urls")),
```

```bash
python manage.py migrate container_monitor
```

Schedule a check every few minutes - cron, a systemd timer, Celery beat, or
anything that enqueues `container_monitor.tasks.run_check` with `django.tasks`:

```bash
*/5 * * * *  python manage.py monitor_check
0 4 * * *    python manage.py monitor_prune
```

**Container monitor > Checks** shows the history, **Check now**, and each
container's logs.

## Reaching Docker safely

The worker running the check needs the Docker Engine API. Mounting
`/var/run/docker.sock` gives that process root on the host, so put a
read-only proxy in between:

```yaml
# docker-compose.yml
docker-proxy:
  image: tecnativa/docker-socket-proxy:v0.4.2
  environment: {CONTAINERS: 1, INFO: 1, POST: 0}
  volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]
```

```python
CONTAINER_MONITOR = {"DOCKER_URL": "http://docker-proxy:2375"}
```

Inside a container, `/proc` still describes the whole machine, so host figures
are the server's. The disk checked is the one `DISK_PATHS` live on.

## Settings

```python
CONTAINER_MONITOR = {
    "DOCKER_URL": "unix:///var/run/docker.sock",   # or None: host only
    "CONTAINERS": ["shop-*"],     # judged (fnmatch); others are listed only
    "DISK_PATHS": ["/", "/var/lib/postgresql"],
    "THRESHOLDS": {                # (warning, danger), percent
        "memory": (85, 93),        # host memory, by MemAvailable
        "swap": (25, 60),
        "disk": (80, 90),
        "cpu": (85, 97),
        "load_per_core": (1.0, 2.0),
        "container_memory": (85, 95),
    },
    "ALERTS": [
        {"BACKEND": "container_monitor.alerts.LogAlert"},
        {"BACKEND": "container_monitor.alerts.EmailAlert",
         "OPTIONS": {"recipients": ["ops@example.com"]}, "MIN_STATUS": "danger"},
        {"BACKEND": "container_monitor.alerts.WebhookAlert",
         "OPTIONS": {"url": "https://hooks.slack.com/services/..."}},
    ],
    "REPEAT_ALERT_MINUTES": 60,   # None: only on change
    "RETENTION_DAYS": 30,
    "TOKEN": "a-long-random-string",   # for the status endpoint
    "LOG_LINES": 200,
}
```

A channel is a class with `send(subject, message, check)`; subclass
`container_monitor.alerts.AlertChannel` and point `BACKEND` at it. To react in
code instead:

```python
from container_monitor.signals import status_changed

@receiver(status_changed)
def page_someone(sender, check, previous_status, status, **kwargs):
    ...
```

## Status endpoint

```bash
curl -H "Authorization: Bearer $TOKEN" https://example.com/monitor/status/
```

HTTP 200 while OK or warning, 503 on danger, so a plain uptime check alerts on
what matters. Staff users can open it in the browser without the token.

## Alongside django-health-check

[django-health-check](https://github.com/codingjoe/django-health-check)
answers "can this instance serve requests?" for load balancers and probes;
this package keeps a history of the machine and its containers and tells you
when it changes. They do not overlap and can run side by side.

## Development

```bash
python runtests.py
```

## License

BSD-3-Clause.
