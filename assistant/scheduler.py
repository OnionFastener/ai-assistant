"""APScheduler integration: nightly runs (if enabled) — reschedule on config change."""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .runner import create_run, process_run

log = logging.getLogger("assistant.scheduler")

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def start_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler()
        _reschedule()
        _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


def reschedule() -> None:
    """Re-read settings.schedule_enabled/time and re-register the job."""
    with _lock:
        if _scheduler is None:
            return
        _scheduler.remove_all_jobs()
        _reschedule()


def _reschedule() -> None:
    if _scheduler is None:
        return
    if not settings.schedule_enabled:
        log.info("nightly schedule disabled")
        return
    trigger = CronTrigger(hour=settings.schedule_hour, minute=settings.schedule_minute)
    _scheduler.add_job(_nightly, trigger, id="nightly", name="AI assistant nightly triage",
                       misfire_grace_time=3600, replace_existing=True)
    log.info("nightly schedule: %02d:%02d", settings.schedule_hour, settings.schedule_minute)


def _nightly() -> None:
    run_id = create_run(trigger="scheduled")
    threading.Thread(target=process_run, args=(run_id,), daemon=True).start()