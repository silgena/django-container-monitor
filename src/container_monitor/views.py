import hmac

from django.http import JsonResponse

from container_monitor import conf
from container_monitor.models import Check, Status


def status(request):
    """The latest check as JSON, for an uptime monitor or a dashboard.

    HTTP 200 while the verdict is OK or a warning, 503 when it is danger, so a
    plain uptime check alerts on what matters. Answers a request carrying
    ``Authorization: Bearer <TOKEN>``, or a staff user.
    """
    token = conf.get("TOKEN")
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    allowed = (token and hmac.compare_digest(supplied, token)) or (
        request.user.is_authenticated and request.user.is_staff
    )
    if not allowed:
        return JsonResponse({"detail": "Forbidden"}, status=403)
    check = Check.objects.order_by("-checked_at").first()
    if check is None:
        return JsonResponse(
            {"status": None, "detail": "No check has run yet."}, status=503
        )
    return JsonResponse(
        {
            "status": check.status,
            "checked_at": check.checked_at.isoformat(),
            "reasons": check.reasons,
            "host": check.host,
            "containers": [
                {
                    key: entry.get(key)
                    for key in (
                        "name",
                        "state",
                        "health",
                        "status",
                        "memory_percent",
                        "cpu_percent",
                        "restart_count",
                    )
                }
                for entry in check.containers
            ],
        },
        status=503 if check.status == Status.DANGER else 200,
    )
