"""
recruitment/services/audit.py

Centralized writer for Recruitment business-event audit records.

Business events ("published", "closed", "rejected", "handed off") are not
field changes, so they cannot be expressed by the two field-diff audit
systems already in the project (simple-history's HorillaAuditLog and
django-auditlog). Those keep recording diffs; this records *what happened*.

Every Recruitment business operation writes through this service rather than
calling RecruitmentAuditEvent.objects.create() directly, so actor, company
and object resolution stay consistent across UI, API and background jobs.
"""

import logging

from horilla.horilla_middlewares import _thread_locals

logger = logging.getLogger(__name__)

# Username of the system account used when no human performed the action.
# Created by `manage.py createhorillauser` and already used as the actor for
# background work across the project (base/scheduler.py, asset/scheduler.py,
# pms/views.py), so Recruitment follows the same convention.
SYSTEM_ACTOR_USERNAME = "Horilla Bot"


def _current_user():
    """The request user, when called inside a request cycle."""
    request = getattr(_thread_locals, "request", None)
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


def _system_actor():
    """
    The "Horilla Bot" user, or None.

    Nullable on purpose: every existing call site in the project resolves the
    bot with ``.first()`` and no fallback, so a database provisioned without
    `createhorillauser` genuinely has no bot row. A missing actor must not
    stop a business event from being recorded -- the event with a null actor
    is far more useful than no event at all.
    """
    from horilla_auth.models import HorillaUser

    return (
        HorillaUser.objects.filter(username=SYSTEM_ACTOR_USERNAME)
        .only("id")
        .first()
    )


class RecruitmentAuditService:
    """Append-only writer for RecruitmentAuditEvent."""

    @staticmethod
    def record(
        *,
        event_type,
        actor=None,
        company=None,
        job_opening=None,
        candidate=None,
        stage=None,
        object_type=None,
        object_id=None,
        details=None,
        system=False,
    ):
        """
        Write one business-event audit record and return it.

        ``actor`` defaults to the current request user; pass ``system=True``
        for background work so the Horilla Bot is recorded instead of
        whichever user happened to trigger the job. ``company``,
        ``object_type`` and ``object_id`` are derived from the related object
        when not given, so callers stay terse at the call site.

        Never raises: a failure to audit must not roll back or mask the
        business operation itself. Failures are logged at ERROR so they are
        still visible in application logs.
        """
        from recruitment.models import RecruitmentAuditEvent

        try:
            if actor is None:
                actor = _system_actor() if system else _current_user()

            target = job_opening or candidate or stage
            if company is None:
                company = RecruitmentAuditService._resolve_company(target)

            if object_type is None and target is not None:
                object_type = target._meta.object_name
            if object_id is None and target is not None:
                object_id = target.pk

            return RecruitmentAuditEvent.objects.create(
                event_type=event_type,
                actor=actor,
                company_id=company,
                job_opening=job_opening,
                candidate=candidate,
                stage=stage,
                object_type=object_type or "",
                object_id=object_id,
                details=details or {},
            )
        except Exception:
            logger.exception(
                "Failed to record recruitment audit event %s (object_id=%s)",
                event_type,
                object_id,
            )
            return None

    @staticmethod
    def _resolve_company(target):
        """Walk from a Recruitment/Candidate/Stage to its owning Company."""
        if target is None:
            return None
        # Recruitment holds company_id directly; Candidate and Stage reach it
        # through recruitment_id, mirroring their HorillaCompanyManager paths.
        company = getattr(target, "company_id", None)
        if company is not None:
            return company
        recruitment = getattr(target, "recruitment_id", None)
        if recruitment is not None:
            return getattr(recruitment, "company_id", None)
        return None
