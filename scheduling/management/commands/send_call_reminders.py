import time

from django.core.management.base import BaseCommand
from django.db import connections

from scheduling.reminders import send_due_reminders


class Command(BaseCommand):
    help = ("Sends due call reminders (heads-up and 'time to call'). The AI worker already does this; "
            "use this when running reminders without the AI worker.")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running and check every --interval seconds.")
        parser.add_argument("--interval", type=float, default=15.0, help="Seconds between checks (default 15).")

    def handle(self, *args, **options):
        if not options["loop"]:
            count = send_due_reminders()
            self.stdout.write(self.style.SUCCESS(f"Sent {count} call reminder(s)."))
            return
        self.stdout.write(self.style.SUCCESS(f"Call reminders running (every {options['interval']}s). Ctrl+C to stop."))
        try:
            while True:
                count = send_due_reminders()
                if count:
                    self.stdout.write(f"Sent {count} call reminder(s).")
                connections.close_all()
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            self.stdout.write("Stopped.")
