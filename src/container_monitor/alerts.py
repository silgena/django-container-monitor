"""Where alerts go. A channel is a class with ``send(subject, message, check)``.

Configure any number in ``CONTAINER_MONITOR["ALERTS"]``; write your own by
subclassing :class:`AlertChannel`.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from django.core.mail import mail_admins, send_mail

logger = logging.getLogger("container_monitor")


class AlertChannel:
    def __init__(self, **options):
        self.options = options

    def send(self, subject: str, message: str, check) -> None:
        raise NotImplementedError


class LogAlert(AlertChannel):
    """Write to the ``container_monitor`` logger: WARNING, or ERROR for danger."""

    def send(self, subject, message, check):
        level = logging.ERROR if check.status == "danger" else logging.WARNING
        logger.log(level, "%s\n%s", subject, message)


class EmailAlert(AlertChannel):
    """E-mail ``recipients``, or the ADMINS setting when none are given."""

    def send(self, subject, message, check):
        recipients = self.options.get("recipients")
        if recipients:
            send_mail(subject, message, None, recipients, fail_silently=False)
        else:
            mail_admins(subject, message, fail_silently=False)


class WebhookAlert(AlertChannel):
    """POST JSON to ``url``.

    The body carries ``text`` (what Slack, Mattermost and Discord-compatible
    endpoints read), ``content`` (Discord), and the status and reasons for
    anything else.
    """

    def send(self, subject, message, check):
        text = f"{subject}\n{message}"
        body = json.dumps(
            {
                "text": text,
                "content": text[:2000],
                "status": check.status,
                "reasons": check.reasons,
                "checked_at": check.checked_at.isoformat(),
            }
        ).encode()
        request = urllib.request.Request(
            self.options["url"],
            data=body,
            headers={
                "Content-Type": "application/json",
                **self.options.get("headers", {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.options.get("timeout", 10)):
            pass
