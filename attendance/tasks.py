"""
attendance/tasks.py

Celery tasks for what's deferred off the clock-in/clock-out hot path
(Part 3 of the Attendance performance plan): late-come/early-out
flagging. (The monthly overtime account update is a later, separate
pass -- see the plan's sequencing note on Attendance.save().)

Deliberately doesn't use Celery's own `self.retry()`: under
CELERY_TASK_ALWAYS_EAGER (the default whenever REDIS_URL isn't set --
see horilla/settings/base.py -- which includes every test run), a raised
Retry propagates straight out of `.delay()` as a real exception instead
of being scheduled, confirmed directly against this Celery version.
Since the whole point of deferring this work is that it can never affect
the punch response, this task instead always completes normally --
recording success or failure on BackgroundAttendanceTask itself, never
raising past its own boundary. Every retry, whether the task started and
failed or never started at all (worker down, broker hiccup), is instead
picked up uniformly by sweep_stuck_background_tasks() on its periodic
schedule (CELERY_BEAT_SCHEDULE).
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from attendance.methods.utils import shift_schedule_today
from attendance.models import (
    BackgroundAttendanceTask,
    BackgroundAttendanceTaskRetryLog,
)

logger = logging.getLogger(__name__)


def _task_lock_key(task_id):
    return f"background_attendance_task_lock:{task_id}"


def _run_late_come_early_out(task):
    from attendance.views.clock_in_out import (
        early_out,
        flexible_shortfall,
        late_come,
    )

    attendance = task.attendance
    shift = attendance.shift_id
    day = attendance.attendance_day
    # shift_schedule_today() is itself cached (Part 3 Sec 3.3) -- cheap
    # to re-derive here rather than carry start/end time in the payload.
    _minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
        day=day, shift=shift
    )

    if attendance.attendance_clock_out is None:
        # Still open -- e.g. this task is a stale duplicate from an
        # earlier clock-in the same day, superseded by a later one.
        # Nothing to flag yet; the eventual clock-out enqueues its own
        # task once there's something to check.
        if not attendance.is_flexible_mode():
            late_come(
                attendance=attendance, start_time=start_time_sec,
                end_time=end_time_sec, shift=shift,
            )
        return

    if attendance.is_flexible_mode():
        flexible_shortfall(attendance)
    else:
        early_out(
            attendance, start_time=start_time_sec, end_time=end_time_sec,
            shift=shift,
        )


_TASK_HANDLERS = {
    BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT: _run_late_come_early_out,
}


@shared_task
def process_background_attendance_task(task_id):
    """
    Locked per task_id so the same task can never run twice concurrently
    -- Celery/Redis brokers are at-least-once delivery (a message can be
    redelivered), and the periodic sweep can re-enqueue a task a worker
    is still partway through processing. The status field alone can't
    close that race: two workers could both read status=PENDING before
    either has written PROCESSING. cache.add() is atomic (SET NX under
    django-redis, so this is a real cross-process/cross-worker lock
    whenever REDIS_URL is configured -- with the LocMem cache used
    without it, atomicity only holds within one process, which is what
    dev/test runs actually need).
    """
    lock_key = _task_lock_key(task_id)
    if not cache.add(lock_key, "1", settings.BACKGROUND_TASK_LOCK_TTL_SECONDS):
        logger.info(
            "BackgroundAttendanceTask %s is already being processed -- skipping"
            " this run.",
            task_id,
        )
        return

    try:
        try:
            task = BackgroundAttendanceTask.objects.get(pk=task_id)
        except BackgroundAttendanceTask.DoesNotExist:
            logger.warning("BackgroundAttendanceTask %s no longer exists", task_id)
            return

        if task.status == BackgroundAttendanceTask.STATUS_DONE:
            return  # already processed by a competing retry path

        task.status = BackgroundAttendanceTask.STATUS_PROCESSING
        task.save(update_fields=["status"])

        handler = _TASK_HANDLERS.get(task.kind)
        if handler is None:
            logger.error("No handler for BackgroundAttendanceTask kind=%s", task.kind)
            task.status = BackgroundAttendanceTask.STATUS_FAILED
            task.last_error = f"No handler registered for kind={task.kind!r}"
            task.save(update_fields=["status", "last_error"])
            return

        try:
            handler(task)
        except Exception as exc:  # noqa: BLE001 -- must never escape this task
            task.attempts += 1
            task.status = BackgroundAttendanceTask.STATUS_FAILED
            task.last_error = str(exc)
            task.save(update_fields=["attempts", "status", "last_error"])
            logger.exception(
                "BackgroundAttendanceTask %s (kind=%s) failed on attempt %s",
                task.pk, task.kind, task.attempts,
            )
        else:
            task.status = BackgroundAttendanceTask.STATUS_DONE
            task.processed_at = timezone.now()
            task.save(update_fields=["status", "processed_at"])
    finally:
        # Released as soon as this run finishes, success or failure --
        # the TTL above is only a safety net for a worker that died
        # mid-task and never reached this line at all.
        cache.delete(lock_key)


@shared_task
def sweep_stuck_background_tasks():
    """
    The sole retry mechanism (see module docstring): re-enqueues every
    task that hasn't reached DONE and hasn't used up MAX_ATTEMPTS yet,
    whether it never started or started and failed -- both look the same
    from here (PENDING or FAILED, attempts < MAX_ATTEMPTS).
    """
    stuck_ids = list(
        BackgroundAttendanceTask.objects.filter(
            status__in=[
                BackgroundAttendanceTask.STATUS_PENDING,
                BackgroundAttendanceTask.STATUS_FAILED,
            ],
            attempts__lt=BackgroundAttendanceTask.MAX_ATTEMPTS,
        ).values_list("pk", flat=True)
    )
    for task_id in stuck_ids:
        process_background_attendance_task.delay(task_id)
    return len(stuck_ids)


def enqueue_background_attendance_task(attendance, kind):
    """
    Creates the durable "this still needs processing" record, then
    enqueues it -- the record exists before the enqueue is even
    attempted, so a broker hiccup right here still leaves something for
    the next sweep to find. Call sites wrap this in a defensive
    try/except (never let a broker being briefly unavailable affect the
    punch response); the row itself is what's actually durable.
    """
    task = BackgroundAttendanceTask.objects.create(attendance=attendance, kind=kind)
    process_background_attendance_task.delay(task.pk)
    return task


def retry_background_task(task, reset_by, note=""):
    """
    Manual retry, for a task that used up MAX_ATTEMPTS -- resets it to a
    fresh attempt cycle. The reset itself is recorded in the append-only
    BackgroundAttendanceTaskRetryLog first, capturing exactly the state
    being reset away from, before anything on the task itself changes.
    """
    BackgroundAttendanceTaskRetryLog.objects.create(
        task=task, reset_by=reset_by,
        previous_status=task.status, previous_attempts=task.attempts,
        note=note,
    )
    task.status = BackgroundAttendanceTask.STATUS_PENDING
    task.attempts = 0
    task.last_error = None
    task.save(update_fields=["status", "attempts", "last_error"])
    process_background_attendance_task.delay(task.pk)
    return task
