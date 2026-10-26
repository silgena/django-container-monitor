from django.core.management import BaseCommand

from container_monitor import monitor


class Command(BaseCommand):
    help = (
        "Measure the host and containers, store the verdict, and alert on a "
        "change. Schedule it every few minutes."
    )

    def handle(self, *args, **options):
        check = monitor.run_check()
        self.stdout.write(
            f"{check.status}: " + ("; ".join(check.reasons) or "within limits")
        )
