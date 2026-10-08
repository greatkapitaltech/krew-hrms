import calendar
import contextlib
import datetime as dt
import logging
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

today = datetime.now()


def recruitment_close():
    """
    Auto-closes PUBLISHED job openings whose End Date has passed.

    Runs through the lifecycle service so an automatic close is validated,
    mirrored and audited (actor: Horilla Bot) exactly like a manual one,
    instead of writing `closed`/`is_published` behind the lifecycle's back.

    Two bugs in the previous implementation are fixed here:

    * "today" is now computed per run. The module-level `today` is evaluated
      at import, so a long-running process kept comparing against the date
      it booted on and stopped closing anything after the first midnight.
    * `end_date <= today`, not `== today`. An exact-day match silently
      skipped any opening whose end date fell during downtime or a missed
      run, leaving it open forever.

    Openings still in DRAFT/REVIEW are ignored -- a draft with a past end
    date must not be dragged into CLOSED.
    """
    from recruitment.models import Recruitment
    from recruitment.services.job_opening import close_expired

    today_date = dt.date.today()

    expired = Recruitment.default.filter(
        status=Recruitment.Status.PUBLISHED,
        end_date__isnull=False,
        end_date__lte=today_date,
    )

    for rec in expired:
        try:
            close_expired(rec)
        except Exception:
            # One bad row must not stop the rest of the batch.
            logger.exception("Failed to auto-close job opening %s", rec.pk)
            continue
        _notify_managers_of_auto_close(rec)


def _notify_managers_of_auto_close(recruitment):
    """
    Tell the drive's managers and HR that applications have stopped (PRD: a
    notification fires when applications auto-stop at End Date).

    Recipients are the opening's own managers plus the HR users who can change
    job openings in that company -- the two parties the PRD holds responsible
    for the drive. Scoped to the opening's company, so one tenant's auto-close
    never notifies another's HR.

    Best-effort: a notification failure must never undo a completed close,
    which is already committed and audited by this point.
    """
    from horilla.methods import horilla_users_with_perms
    from horilla_auth.models import HorillaUser
    from notifications.signals import notify

    with contextlib.suppress(Exception):
        managers = recruitment.recruitment_managers.select_related("employee_user_id")
        users = [
            employee.employee_user_id
            for employee in managers
            if employee.employee_user_id
        ]

        hr_users = horilla_users_with_perms("recruitment.change_recruitment")
        if recruitment.company_id_id:
            hr_users = hr_users.filter(
                employee_get__employee_work_info__company_id=recruitment.company_id_id
            )
        seen = {user.pk for user in users}
        for user in hr_users:
            if user.pk not in seen:
                seen.add(user.pk)
                users.append(user)

        if not users:
            return
        sender = HorillaUser.objects.filter(username="Horilla Bot").first() or users[0]
        notify.send(
            sender,
            recipient=users,
            verb=(
                f"{recruitment} has reached its end date and has stopped "
                "accepting new applications."
            ),
            icon="close-circle",
            redirect="",
        )


def candidate_convert():
    """
    Converts candidates to a "converted" state if they already exist as users.
    """
    from horilla_auth.models import HorillaUser
    from recruitment.models import Candidate

    mails = list(
        Candidate.objects.filter(is_active=True).values_list("email", flat=True)
    )
    existing_emails = list(
        HorillaUser.objects.filter(email__in=mails).values_list("email", flat=True)
    )
    Candidate.objects.filter(
        is_active=True,
        email__in=existing_emails,
        converted=False,
    ).update(converted=True)


# if not any(
#     cmd in sys.argv
#     for cmd in ["makemigrations", "migrate", "compilemessages", "flush", "shell"]
# ):
#     scheduler = BackgroundScheduler()
#     scheduler.add_job(recruitment_close, "interval", hours=1)
#     scheduler.start()
#
# Runs are recorded in django_apscheduler's tables: the job in
# django_apscheduler_djangojob, every run (start, success/error, duration) in
# django_apscheduler_djangojobexecution.
#
# Started only in a process that serves requests -- runserver's serving child
# (RUN_MAIN=true, not its file watcher) or a gunicorn/uwsgi/daphne worker --
# never in management commands (collectstatic in the Docker build has no
# tables; test/createhorillauser must not write jobs). Set
# RECRUITMENT_SCHEDULER=0 to switch it off, =1 to force it on.
import os as _os

_argv0 = _os.path.basename(sys.argv[0]) if sys.argv else ""
_is_server = (
    "runserver" in sys.argv and _os.environ.get("RUN_MAIN") == "true"
) or any(name in _argv0 for name in ("gunicorn", "uwsgi", "daphne"))
_flag = _os.environ.get("RECRUITMENT_SCHEDULER", "").strip()
_start = _flag == "1" or (_flag != "0" and _is_server)


def _claim_scheduler_lock():
    """
    One scheduler per container: gunicorn runs several workers, each loading
    the app. The first to take this non-blocking file lock runs the scheduler;
    the rest skip. The lock is held for the worker's lifetime and released by
    the OS when it exits, so a replacement worker (gunicorn max_requests) takes
    over.
    """
    import tempfile

    try:
        import fcntl
    except ImportError:  # non-POSIX (Windows dev): no cross-process lock
        return True
    path = _os.path.join(tempfile.gettempdir(), "krew-recruitment-scheduler.lock")
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    globals()["_scheduler_lock_handle"] = handle  # keep it open = keep the lock
    return True


if _start and not _claim_scheduler_lock():
    _start = False  # another worker in this container runs the scheduler

scheduler = BackgroundScheduler()
if _start:
    try:
        from django_apscheduler.jobstores import DjangoJobStore

        scheduler.add_jobstore(DjangoJobStore(), "default")
        # Not needed for Krew recruitment.
        # scheduler.add_job(candidate_convert, "interval", minutes=5)
        scheduler.add_job(
            recruitment_close,
            "interval",
            hours=1,
            id="recruitment_close",
            name="Recruitment: auto-close job openings at End Date",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        scheduler.start()
    except Exception:
        logger.exception("Recruitment scheduler could not start")
