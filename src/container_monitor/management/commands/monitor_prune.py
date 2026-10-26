from django.core.management import BaseCommand

from container_monitor import monitor


class Command(BaseCommand):
    help = (
        "Delete checks older than CONTAINER_MONITOR['RETENTION_DAYS'] "
        "(their alerts go with them)."
    )

    def handle(self, *args, **options):
        self.stdout.write(f"Deleted {monitor.prune()} check(s).")
