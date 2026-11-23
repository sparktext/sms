import importlib
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from messaging.models import AIJob

logger = logging.getLogger(__name__)

# A RUNNING job older than this no longer blocks its conversation (its worker probably crashed)
STALE_RUNNING_MINUTES = 10


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
        parser.add_argument(
            "--concurrency",
            type=int,
            default=int(getattr(settings, "AI_WORKER_CONCURRENCY", 1) or 1),
            help="How many conversations to reply to at the same time (default: 1, or AI_WORKER_CONCURRENCY). "
                 "Keep it at or below the model server's parallel slots (LM Studio: Max Concurrent Predictions).",
        )

    # ------------------------------------------------------------------ helpers

    def _reload_pipeline(self):
        """Pick up code edits without restarting (only called while no reply is in progress)."""
        import messaging.ai.pipeline
        import messaging.ai.prompt
        import messaging.ai.validator
        importlib.reload(messaging.ai.validator)
        importlib.reload(messaging.ai.prompt)
        importlib.reload(messaging.ai.pipeline)
        return messaging.ai.pipeline

    def _claim_job(self):
        """
        Claim the next due job, skipping conversations that already have a reply in progress,
        so one customer never gets two AI replies written at the same time. The check runs in
        the database, so it also holds across several worker processes.
        """
        now = timezone.now()
        reply_in_progress = AIJob.objects.filter(
            conversation=OuterRef("conversation"),
            status=AIJob.Status.RUNNING,
            started_at__gte=now - timedelta(minutes=STALE_RUNNING_MINUTES),
        )
        with transaction.atomic():
            # select_for_update with skip_locked handles concurrency across multiple worker processes
            job = (
                AIJob.objects.select_for_update(skip_locked=True)
                .filter(status=AIJob.Status.PENDING, run_after__lte=now)
                .filter(~Exists(reply_in_progress))
                .order_by("run_after", "created_at")
                .first()
            )
            if job:
                job.status = AIJob.Status.RUNNING
                job.started_at = timezone.now()
                job.attempts += 1
                job.save(update_fields=["status", "started_at", "attempts"])
        return job

    def _run_job(self, pipeline, job, in_thread: bool):
        self.stdout.write(f"Processing AIJob #{job.id} for Conversation #{job.conversation_id}...")
        try:
            reply = pipeline.process_ai_job(job)
            status_str = reply.status if reply else "done"
            self.stdout.write(self.style.SUCCESS(f"AIJob #{job.id} finished: status={status_str}"))
            return True
        except Exception as exc:
            logger.exception("Unexpected error processing AIJob #%d: %s", job.id, exc)
            job.status = AIJob.Status.FAILED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at"])
            self.stdout.write(self.style.ERROR(f"AIJob #{job.id} failed: {exc}"))
            return False
        finally:
            if in_thread:
                connections.close_all()   # each worker thread has its own DB connection

    # ------------------------------------------------------------------ main loop

    def handle(self, *args, **options):
        once = options["once"]
        interval = options["interval"]
        concurrency = max(1, int(options["concurrency"] or 1))

        self.running = True

        def _handle_exit(sig, frame):
            self.stdout.write(self.style.WARNING("\nShutting down AI worker gracefully (finishing replies in progress)..."))
            self.running = False

        try:
            signal.signal(signal.SIGINT, _handle_exit)
            signal.signal(signal.SIGTERM, _handle_exit)
        except Exception:
            pass  # On Windows, some signals might differ

        self.stdout.write(self.style.SUCCESS(
            f"AI Assistant worker started. Polling every {interval}s (once={once}, concurrency={concurrency})."
        ))

        jobs_processed = 0
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ai-job") if concurrency > 1 else None
        in_flight = set()
        pipeline = None

        while self.running:
            if in_flight:
                for fut in [f for f in in_flight if f.done()]:
                    in_flight.discard(fut)
                    if fut.result():
                        jobs_processed += 1

            job = None
            if len(in_flight) < concurrency:
                if not in_flight:
                    pipeline = self._reload_pipeline()
                try:
                    job = self._claim_job()
                except Exception as exc:
                    logger.exception("Error in worker polling loop: %s", exc)
                    time.sleep(interval)
                    continue

            if job:
                if pool is None:
                    # one at a time: run inline (same behaviour as before)
                    if self._run_job(pipeline, job, in_thread=False):
                        jobs_processed += 1
                else:
                    in_flight.add(pool.submit(self._run_job, pipeline, job, True))
                continue   # look for the next due job straight away

            if once and not in_flight:
                break      # in once mode, stop when nothing is due and nothing is running
            time.sleep(0.2 if in_flight else interval)

        if pool is not None:
            pool.shutdown(wait=True)
            jobs_processed += sum(1 for f in in_flight if f.done() and not f.exception() and f.result())

        self.stdout.write(self.style.SUCCESS(f"AI Assistant worker stopped. Total jobs processed: {jobs_processed}"))
