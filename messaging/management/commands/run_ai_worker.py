import time
import signal
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from messaging.models import AIJob
from messaging.ai.pipeline import process_ai_job

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the background AI Assistant worker to process pending AIJobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process all currently due pending jobs and exit immediately (useful for testing or cron).",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help="Polling interval in seconds (default: 1.0).",
        )

    def handle(self, *args, **options):
        once = options["once"]
        interval = options["interval"]

        self.running = True

        def _handle_exit(sig, frame):
            self.stdout.write(self.style.WARNING("\nShutting down AI worker gracefully..."))
            self.running = False

        try:
            signal.signal(signal.SIGINT, _handle_exit)
            signal.signal(signal.SIGTERM, _handle_exit)
        except Exception:
            pass  # On Windows, some signals might differ

        self.stdout.write(self.style.SUCCESS(f"AI Assistant worker started. Polling every {interval}s (once={once})."))

        jobs_processed = 0

        while self.running:
            job_found = False

            # Claim a single due pending job safely
            try:
                with transaction.atomic():
                    # select_for_update with skip_locked handles concurrency across multiple worker processes
                    now = timezone.now()
                    job = (
                        AIJob.objects.select_for_update(skip_locked=True)
                        .filter(status=AIJob.Status.PENDING, run_after__lte=now)
                        .order_by("run_after", "created_at")
                        .first()
                    )

                    if job:
                        job_found = True
                        job.status = AIJob.Status.RUNNING
                        job.started_at = timezone.now()
                        job.attempts += 1
                        job.save(update_fields=["status", "started_at", "attempts"])

                if job_found and job:
                    self.stdout.write(f"Processing AIJob #{job.id} for Conversation #{job.conversation_id}...")
                    try:
                        import importlib
                        import messaging.ai.validator
                        import messaging.ai.prompt
                        import messaging.ai.pipeline
                        importlib.reload(messaging.ai.validator)
                        importlib.reload(messaging.ai.prompt)
                        importlib.reload(messaging.ai.pipeline)
                        reply = messaging.ai.pipeline.process_ai_job(job)
                        status_str = reply.status if reply else "done"
                        self.stdout.write(
                            self.style.SUCCESS(f"AIJob #{job.id} finished: status={status_str}")
                        )
                        jobs_processed += 1
                    except Exception as exc:
                        logger.exception("Unexpected error processing AIJob #%d: %s", job.id, exc)
                        job.status = AIJob.Status.FAILED
                        job.finished_at = timezone.now()
                        job.save(update_fields=["status", "finished_at"])
                        self.stdout.write(self.style.ERROR(f"AIJob #{job.id} failed: {exc}"))

            except Exception as exc:
                logger.exception("Error in worker polling loop: %s", exc)
                time.sleep(interval)

            if once:
                # In once mode, if no more jobs are currently due, stop
                if not job_found:
                    break
            else:
                if not job_found:
                    time.sleep(interval)

        self.stdout.write(self.style.SUCCESS(f"AI Assistant worker stopped. Total jobs processed: {jobs_processed}"))

