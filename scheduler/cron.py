"""Cron job: polls the queue and spawns one engine agent per pending job.

Responsibilities (matching the project spec):
  - fetch the list of jobs to process from the DB,
  - spawn one engine agent per job (bounded concurrency),
  - retry a failed job up to a configurable maximum (default 3).

It does no vendor matching or calling itself — that's the engine agent's job.

Run one sweep:        python -m scheduler.cron --once
Run forever:          python -m scheduler.cron --interval 30
Config via env:       CRON_INTERVAL, CRON_MAX_CONCURRENCY, CRON_BATCH,
                      CRON_MAX_RETRIES, CRON_RETRY_BACKOFF
"""

import argparse
import asyncio
import logging
import os

from engine_agent import db
from .runner import process_request

log = logging.getLogger("cron")


class CronConfig:
    """Runtime configuration, overridable by env var or CLI flag."""

    def __init__(self) -> None:
        self.interval = int(os.environ.get("CRON_INTERVAL", "30"))          # seconds between sweeps
        self.max_concurrency = int(os.environ.get("CRON_MAX_CONCURRENCY", "5"))
        self.batch = int(os.environ.get("CRON_BATCH", "10"))                # jobs claimed per sweep
        self.max_retries = int(os.environ.get("CRON_MAX_RETRIES", str(db.DEFAULT_MAX_RETRIES)))
        self.retry_backoff = int(os.environ.get("CRON_RETRY_BACKOFF", "60"))  # seconds


async def _run_job(job: dict, cfg: CronConfig, sem: asyncio.Semaphore) -> None:
    """Processes a single claimed job and records success/retry/failure."""
    request_id = job["request_id"]
    attempt = job["attempt_count"]
    async with sem:
        log.info("processing request %s (attempt %d/%d)",
                 request_id, attempt, job["max_retries"])
        try:
            result = await process_request(request_id)
        except Exception as exc:  # defensive: never let one job kill the sweep
            result = {"ok": False, "error": str(exc)}

        if result.get("ok"):
            db.complete_job(request_id)
            log.info("request %s completed", request_id)
        else:
            new_status = db.fail_or_retry_job(
                request_id, result.get("error", "unknown error"), cfg.retry_backoff
            )
            log.warning("request %s failed (%s) -> %s",
                        request_id, result.get("error"), new_status)


async def run_sweep(cfg: CronConfig) -> int:
    """Runs a single poll sweep: enqueue, claim, and process due jobs.

    Returns the number of jobs processed this sweep.
    """
    db.init_db()
    created = db.sync_jobs_from_requests(cfg.max_retries)
    if created:
        log.info("enqueued %d new job(s) from approved requests", created)

    jobs = db.claim_due_jobs(cfg.batch)
    if not jobs:
        return 0

    log.info("claimed %d job(s)", len(jobs))
    sem = asyncio.Semaphore(cfg.max_concurrency)
    await asyncio.gather(*[_run_job(job, cfg, sem) for job in jobs])
    return len(jobs)


async def run_forever(cfg: CronConfig) -> None:
    """Polls on a fixed interval until interrupted."""
    log.info("cron started: interval=%ds concurrency=%d max_retries=%d",
             cfg.interval, cfg.max_concurrency, cfg.max_retries)
    while True:
        try:
            await run_sweep(cfg)
        except Exception:  # keep the loop alive across sweep-level errors
            log.exception("sweep failed")
        await asyncio.sleep(cfg.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Vendor-management cron job")
    parser.add_argument("--once", action="store_true",
                        help="run a single sweep and exit")
    parser.add_argument("--interval", type=int, help="seconds between sweeps")
    parser.add_argument("--max-retries", type=int, help="max attempts per job")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = CronConfig()
    if args.interval is not None:
        cfg.interval = args.interval
    if args.max_retries is not None:
        cfg.max_retries = args.max_retries

    if args.once:
        processed = asyncio.run(run_sweep(cfg))
        log.info("sweep done: %d job(s) processed", processed)
    else:
        try:
            asyncio.run(run_forever(cfg))
        except KeyboardInterrupt:
            log.info("cron stopped")


if __name__ == "__main__":
    main()
