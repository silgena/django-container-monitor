from django.db import models


class Status(models.TextChoices):
    OK = "ok", "OK"
    WARNING = "warning", "Warning"
    DANGER = "danger", "Danger"

    @classmethod
    def rank(cls, status) -> int:
        return [cls.OK, cls.WARNING, cls.DANGER].index(status)

    @classmethod
    def worst(cls, statuses) -> str:
        return max(statuses, key=cls.rank, default=cls.OK)


class Check(models.Model):
    """The host and its containers at one moment, and the verdict on them."""

    checked_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=8, choices=Status.choices, db_index=True)
    #: Every threshold crossed, worst first.
    reasons = models.JSONField(default=list, blank=True)
    host = models.JSONField(default=dict, blank=True)
    #: One entry per container: name, state, health, restarts, CPU, memory
    #: against its limit, whether it is judged, and its own verdict.
    containers = models.JSONField(default=list, blank=True)
    docker_error = models.TextField(blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-checked_at"]

    def __str__(self):
        return f"{self.get_status_display()} at {self.checked_at:%Y-%m-%d %H:%M}"


class Alert(models.Model):
    """One notification sent - or attempted - about a change of status."""

    check_run = models.ForeignKey(Check, models.CASCADE, related_name="alerts")
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    channel = models.CharField(max_length=200)
    previous_status = models.CharField(max_length=8, choices=Status.choices, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices)
    message = models.TextField()
    delivered = models.BooleanField(default=False)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-sent_at"]
