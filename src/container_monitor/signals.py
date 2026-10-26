from django.dispatch import Signal

#: Sent when a check's status differs from the previous check's, with
#: ``check``, ``previous_status`` and ``status``.
status_changed = Signal()
