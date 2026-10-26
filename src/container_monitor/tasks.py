"""A task for schedulers that enqueue django.tasks (Django 6.0+ or django-tasks)."""

try:
    from django.tasks import task
except ImportError:  # pragma: no cover
    try:
        from django_tasks import task
    except ImportError:
        task = None

if task is not None:

    @task()
    def run_check() -> str:
        from container_monitor import monitor

        return monitor.run_check().status
