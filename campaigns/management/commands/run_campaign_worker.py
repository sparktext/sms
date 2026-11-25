import logging
import time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from campaigns.models import Campaign
from campaigns.services import process_campaign_batch

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Processes scheduled and active SMS broadcast campaigns with carrier rate limiting."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process due campaigns once and exit immediately (ideal for tests and cron).",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=2.0,
            help="Polling interval in seconds between passes (default: 2.0).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=25,
            help="Number of messages to dispatch per campaign batch (default: 25).",
        )

    def handle(self, *args, **options):
        once = options["once"]
        interval = options["interval"]
        batch_size = options["batch_size"]

        self.stdout.write(self.style.SUCCESS(f"Campaign worker started. Polling every {interval}s (once={once})."))

        total_processed_overall = 0

        while True:
            now = timezone.now()

            # Find campaigns ready to send:
            # 1. Scheduled and scheduled_at has arrived (or scheduled_at is null)
            # 2. Currently sending
            due_campaigns = (
                Campaign.objects.filter(
                    Q(status=Campaign.Status.SCHEDULED, scheduled_at__lte=now)
                    | Q(status=Campaign.Status.SCHEDULED, scheduled_at__isnull=True)
                    | Q(status=Campaign.Status.SENDING)
                )
                .order_by("created_at")
            )

            processed_in_pass = 0
            # "I'm running": lets the campaign page say so when nothing is sending (worker stopped)
            try:
                from campaigns.services import record_worker_heartbeat
                record_worker_heartbeat()
            except Exception as exc:
                logger.warning("Campaign worker heartbeat failed: %s", exc)

            for camp in due_campaigns:
                try:
                    num_sent = process_campaign_batch(camp, limit=batch_size)
                    if num_sent > 0:
                        processed_in_pass += num_sent
                        self.stdout.write(
                            f"Campaign #{camp.id} ('{camp.name}'): Dispatched {num_sent} messages "
                            f"(Progress: {camp.progress_percent}% - Status: {camp.status})"
                        )
                except Exception as exc:
                    logger.exception("Error processing campaign #%s: %s", camp.id, exc)
                    self.stderr.write(self.style.ERROR(f"Error processing campaign #{camp.id}: {exc}"))

            total_processed_overall += processed_in_pass

            if once:
                break

            time.sleep(interval)

        self.stdout.write(self.style.SUCCESS(f"Campaign worker stopped. Total messages dispatched: {total_processed_overall}."))

