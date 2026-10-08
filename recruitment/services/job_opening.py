"""
recruitment/services/job_opening.py

The job-opening lifecycle:

    DRAFT -> REVIEW -> PUBLISHED -> CLOSED

CLOSED is terminal. There is deliberately no reopen path -- the PRD's answer
to "post this role again" is Duplicate, which produces a new job opening with
its own lifecycle and audit history.

Every transition is the only supported way to change lifecycle state. The
`status` field is authoritative; `is_published` / `closed` / `is_active` are
maintained here purely as backward-compatible mirrors for the ~15 existing
read sites (public listing, filters, reports, dashboards) and must never be
written anywhere else.
"""

import logging

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from recruitment.services.audit import RecruitmentAuditService
from recruitment.services.authorization import get_job_opening_for_user
from recruitment.services.errors import (
    InvalidLifecycleTransition,
    JobOpeningNotAcceptingCandidates,
    PublicationValidationError,
)

logger = logging.getLogger(__name__)

CHANGE_PERMISSION = "recruitment.change_recruitment"
REMOVE_PERMISSION = "recruitment.archive_recruitment"


def allowed_transitions():
    """
    Permitted status transitions.

    REVIEW -> DRAFT ("sent back for changes") is included because it is a
    required audit event; it is the only backward move in the lifecycle.
    Everything absent here is rejected, including DRAFT -> PUBLISHED,
    DRAFT -> CLOSED, CLOSED -> PUBLISHED and PUBLISHED -> DRAFT.
    """
    from recruitment.models import Recruitment

    status = Recruitment.Status
    return {
        status.DRAFT: {status.REVIEW},
        status.REVIEW: {status.PUBLISHED, status.DRAFT},
        status.PUBLISHED: {status.CLOSED},
        status.CLOSED: set(),
        status.REMOVED: set(),
    }


def publication_blockers(job_opening):
    """
    Human-readable reasons this job opening cannot be published yet.

    Re-derived from current data on every call -- never trusts that an
    earlier check still holds. An empty list means ready to publish.
    """
    blockers = []
    # Company is not a form field (derived from the creator's tenant), but an
    # opening with no company is visible to every tenant via
    # HorillaCompanyManager's isnull passthrough -- it must never go live.
    if job_opening.company_id_id is None:
        blockers.append(_("Company could not be determined for this job opening."))
    if not (job_opening.title or "").strip():
        blockers.append(_("Title is required."))
    if not (job_opening.description or "").strip():
        blockers.append(_("Description is required."))
    if job_opening.start_date is None:
        blockers.append(_("Start date is required."))
    if not job_opening.vacancy or job_opening.vacancy < 1:
        blockers.append(_("Vacancies must be at least 1."))
    if not job_opening.recruitment_managers.exists():
        blockers.append(_("At least one manager must be assigned."))
    if (
        job_opening.end_date
        and job_opening.start_date
        and job_opening.end_date < job_opening.start_date
    ):
        blockers.append(_("End date cannot be earlier than start date."))
    # PRD: no stage goes live without at least one Stage Manager.
    from recruitment.models import Stage

    for stage in Stage.objects.entire().filter(
        recruitment_id=job_opening, stage_managers__isnull=True
    ).exclude(stage_type="cancelled"):
        blockers.append(
            _("Stage \"%(stage)s\" has no stage manager.") % {"stage": stage.stage}
        )
    return blockers


def accepts_new_candidates(job_opening):
    """
    True when a new candidate/application may be attached.

    Only PUBLISHED qualifies: DRAFT and REVIEW are not live yet, CLOSED and
    REMOVED are past it. Candidates already attached are unaffected.
    """
    from recruitment.models import Recruitment

    return job_opening is not None and (
        job_opening.status == Recruitment.Status.PUBLISHED
    )


def pipeline_hidden_statuses():
    """
    Statuses whose pipeline is not offered.

    DRAFT and REVIEW are not live recruitment: they accept no applicants and
    their question set is not frozen, so there is nothing to work through. The
    single definition is shared by the pipeline tab list (which excludes them
    from the queryset) and the stages endpoint (which refuses one directly
    requested), so the two cannot drift apart.

    CLOSED is deliberately NOT here: the existing pipeline keeps showing a
    closed opening's history via its own `closed=False` condition, and that
    behaviour is left exactly as it was.
    """
    from recruitment.models import Recruitment

    return [Recruitment.Status.DRAFT, Recruitment.Status.REVIEW]


def appears_in_pipeline(job_opening):
    """True when this opening's pipeline may be shown."""
    return (
        job_opening is not None
        and job_opening.status not in pipeline_hidden_statuses()
    )


def assert_accepts_new_candidates(job_opening):
    """Guard for candidate-creation / pool-mapping paths."""
    if job_opening is None:
        return
    if not accepts_new_candidates(job_opening):
        raise JobOpeningNotAcceptingCandidates()


def _transition(
    *,
    user,
    pk,
    to_status,
    event_type,
    permission=CHANGE_PERMISSION,
    validate=None,
    on_transition=None,
    extra_details=None,
    system=False,
):
    """
    Shared transition runner: lock, validate, move, audit -- atomically.

    The row is locked with select_for_update() and its status re-read *inside*
    the transaction, so two concurrent requests cannot both see the old state.
    The second one finds the new status and raises InvalidLifecycleTransition,
    which is what keeps a double-click from producing two successful
    transitions and two audit events.

    The audit record is written in the same transaction as the status change,
    so a rolled-back operation can never leave a successful-looking event
    behind.
    """
    from recruitment.models import Recruitment

    with transaction.atomic():
        # Authorize first (company scope + permission + object scope), then
        # re-fetch under a row lock -- authorization reads must not race the
        # transition either.
        if system and user is None:
            # Background job (e.g. auto-close at End Date): there is no person
            # to authorize -- the scheduler acts for the system and is recorded
            # as Horilla Bot. Authorizing a None user crashed, so no opening
            # was ever auto-closed.
            locked = Recruitment.default.select_for_update().get(pk=pk)
        else:
            job_opening = get_job_opening_for_user(user, pk, permission)
            locked = Recruitment.default.select_for_update().get(pk=job_opening.pk)

        previous_status = locked.status
        if to_status not in allowed_transitions().get(previous_status, set()):
            raise InvalidLifecycleTransition(
                _("A job opening in %(current)s cannot move to %(target)s.")
                % {
                    "current": locked.get_status_display(),
                    "target": Recruitment.Status(to_status).label,
                }
            )

        if validate is not None:
            validate(locked)

        locked.apply_status(to_status, actor=user if not system else None)
        locked.save()

        # Side effects that must be atomic with the transition itself (e.g.
        # freezing screening questions on publish). Raising here rolls back the
        # status change and the audit event together.
        if on_transition is not None:
            on_transition(locked)

        details = {
            "previous_status": previous_status,
            "new_status": to_status,
        }
        if extra_details:
            details.update(extra_details)

        RecruitmentAuditService.record(
            event_type=event_type,
            actor=None if system else user,
            system=system,
            job_opening=locked,
            details=details,
        )
        return locked


def submit_for_review(user, pk):
    """DRAFT -> REVIEW."""
    from recruitment.models import Recruitment, RecruitmentAuditEvent

    return _transition(
        user=user,
        pk=pk,
        to_status=Recruitment.Status.REVIEW,
        event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_SUBMITTED_FOR_REVIEW,
    )


def send_back_for_changes(user, pk, remark=None):
    """REVIEW -> DRAFT."""
    from recruitment.models import Recruitment, RecruitmentAuditEvent

    return _transition(
        user=user,
        pk=pk,
        to_status=Recruitment.Status.DRAFT,
        event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_SENT_BACK_FOR_CHANGES,
        extra_details={"remark": remark} if remark else None,
    )


def publish(user, pk):
    """
    REVIEW -> PUBLISHED.

    This is the publication boundary later features hook into: once a job
    opening is PUBLISHED its screening questions are frozen (the snapshot
    itself belongs to a later feature, but this is the transition it hangs
    off). Publication is never implied by creation.
    """
    from recruitment.models import (
        FORM_ONE,
        FORM_TWO,
        Recruitment,
        RecruitmentAuditEvent,
    )

    from recruitment.services.screening import build_snapshot

    def _validate(locked):
        blockers = publication_blockers(locked)
        if blockers:
            raise PublicationValidationError(blockers=blockers)

    def _on_transition(locked):
        # Freeze the screening questions in the SAME transaction as the status
        # change. If this raises, the whole publication rolls back: the opening
        # stays in REVIEW, no snapshot rows survive, and no successful
        # JOB_OPENING_PUBLISHED event is written. A published opening without
        # its question snapshot must not be reachable.
        #
        # BOTH forms are frozen here, not just Form 1. The hiring-handoff
        # screen derives its field names from the snapshot rows, so leaving
        # Form 2 unfrozen until submission would render the live bank's
        # question ids and then validate against freshly created snapshot ids
        # -- the names would not match, every answer would be dropped and every
        # mandatory Form 2 question would read as unanswered. build_snapshot is
        # idempotent per form, so the handoff may still call it for an opening
        # published before this.
        build_snapshot(locked, actor=user, form_type=FORM_ONE)
        build_snapshot(locked, actor=user, form_type=FORM_TWO)

    return _transition(
        user=user,
        pk=pk,
        to_status=Recruitment.Status.PUBLISHED,
        event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_PUBLISHED,
        validate=_validate,
        on_transition=_on_transition,
    )


def close(user, pk, *, reason=None, system=False):
    """
    PUBLISHED -> CLOSED. Terminal.

    Closing stops new applications only. Candidates, applications, stages,
    Candidate Pool rows, history and audit records are all left intact and
    remain accessible.
    """
    from recruitment.models import Recruitment, RecruitmentAuditEvent

    return _transition(
        user=user,
        pk=pk,
        to_status=Recruitment.Status.CLOSED,
        event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_CLOSED,
        extra_details={"reason": reason} if reason else None,
        system=system,
    )


def close_expired(job_opening):
    """
    Auto-close at End Date, called by the scheduler with no request user.

    Skips anything not currently PUBLISHED so a DRAFT with a past end date is
    never dragged into CLOSED, and records the Horilla Bot as the actor.
    """
    from recruitment.models import Recruitment

    if job_opening.status != Recruitment.Status.PUBLISHED:
        return None
    return _transition(
        user=None,
        pk=job_opening.pk,
        to_status=Recruitment.Status.CLOSED,
        event_type="JOB_OPENING_CLOSED",
        permission=CHANGE_PERMISSION,
        extra_details={"reason": "end_date_reached", "automated": True},
        system=True,
    )


def remove(user, pk, *, reason=None):
    """
    Remove (take down) a job opening from any state.

    The PRD's Remove is an archive, not a delete: the record is withdrawn
    from active use everywhere but kept in an audit-visible archive. Nothing
    is deleted -- not the opening, its candidates, applications, stages,
    history or audit trail. Gated on the existing (previously unused)
    ``archive_recruitment`` permission rather than ``delete_recruitment``.
    """
    from recruitment.models import Recruitment, RecruitmentAuditEvent

    with transaction.atomic():
        job_opening = get_job_opening_for_user(user, pk, REMOVE_PERMISSION)
        locked = Recruitment.default.select_for_update().get(pk=job_opening.pk)

        previous_status = locked.status
        if previous_status == Recruitment.Status.REMOVED:
            raise InvalidLifecycleTransition(
                _("This job opening has already been removed.")
            )

        locked.apply_status(Recruitment.Status.REMOVED, actor=user)
        locked.save()

        RecruitmentAuditService.record(
            event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_REMOVED,
            actor=user,
            job_opening=locked,
            details={
                "previous_status": previous_status,
                "new_status": Recruitment.Status.REMOVED,
                **({"reason": reason} if reason else {}),
            },
        )
        return locked


def record_created(user, job_opening):
    """Audit a newly created job opening (always born DRAFT)."""
    from recruitment.models import RecruitmentAuditEvent

    return RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_CREATED,
        actor=user,
        job_opening=job_opening,
        details={"status": job_opening.status},
    )


def record_updated(user, job_opening, changed_fields=None):
    """Audit an edit to a job opening's configuration."""
    from recruitment.models import RecruitmentAuditEvent

    return RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_UPDATED,
        actor=user,
        job_opening=job_opening,
        details={
            "status": job_opening.status,
            **({"changed_fields": list(changed_fields)} if changed_fields else {}),
        },
    )


# ---------------------------------------------------------------------------
# Public job listing cache (Redis when REDIS_URL is set)
# ---------------------------------------------------------------------------

#: Version stamp in every cached listing key; bumping it retires them all.
PUBLIC_LISTING_VERSION_KEY = "recruitment:public-listing:version"
#: Safety net: a cached listing is never older than this.
PUBLIC_LISTING_TTL = 300


def public_listing_cache_key(company):
    from django.core.cache import cache

    version = cache.get(PUBLIC_LISTING_VERSION_KEY) or 0
    return f"recruitment:public-listing:{version}:{company or 'all'}"


def invalidate_public_listing():
    """Called whenever a job opening changes, so careers pages update at once."""
    import time

    from django.core.cache import cache

    cache.set(PUBLIC_LISTING_VERSION_KEY, time.time_ns(), None)


# ---------------------------------------------------------------------------
# Career page embedding: per-company allowed domains
# ---------------------------------------------------------------------------

#: Hosts allowed over plain http, for local testing only.
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def normalize_career_origin(value):
    """
    "careers.acme.com" / "https://careers.acme.com/jobs" -> "https://careers.acme.com".
    Raises ValueError for anything that is not a plain site address.
    """
    from urllib.parse import urlsplit

    value = (value or "").strip()
    if not value:
        raise ValueError("empty")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host or "*" in value:
        raise ValueError(value)
    if parts.scheme == "http" and host not in _LOCAL_HOSTS:
        raise ValueError(value)
    if any(ch in host for ch in " ;,'\""):
        raise ValueError(value)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def _career_origins_key(company_id):
    return f"recruitment:career-origins:{company_id}"


def career_page_origins(company_id):
    """The allowed embedding origins for a company (cached in Redis)."""
    from django.core.cache import cache

    from recruitment.models import RecruitmentGeneralSetting

    if not company_id:
        return []
    key = _career_origins_key(company_id)
    origins = cache.get(key)
    if origins is None:
        raw = (
            RecruitmentGeneralSetting.objects.entire()
            .filter(company_id=company_id)
            .values_list("career_page_domains", flat=True)
            .first()
        ) or ""
        origins = []
        for line in raw.splitlines():
            try:
                origins.append(normalize_career_origin(line))
            except ValueError:
                continue
        cache.set(key, origins, PUBLIC_LISTING_TTL)
    return origins


def invalidate_career_origins(company_id):
    from django.core.cache import cache

    cache.delete(_career_origins_key(company_id))


def apply_career_page_framing(response, company_id):
    """
    Allow the listing to be embedded only by this company's listed career
    sites (plus Krew itself). Without a company, or with no domains listed,
    no other site may embed it.
    """
    allowed = " ".join(["'self'", *career_page_origins(company_id)])
    response["Content-Security-Policy"] = f"frame-ancestors {allowed}"
    # CSP frame-ancestors is the control; drop the SAMEORIGIN default.
    response.xframe_options_exempt = True
    if "X-Frame-Options" in response:
        del response["X-Frame-Options"]
    return response


def _career_slug_key(slug):
    return f"recruitment:career-slug:{slug}"


def career_page_slug(company):
    """
    The company's public job-list slug: its name with hyphens, e.g.
    "acme-manufacturing" (a number is added only if two names collide).
    Created on first use and then kept, so the link never changes.
    """
    from django.utils.text import slugify

    from recruitment.models import RecruitmentGeneralSetting

    setting, _created = RecruitmentGeneralSetting.objects.entire().get_or_create(
        company_id=company
    )
    if setting.career_page_slug:
        return setting.career_page_slug
    base = slugify(company.company)[:70].strip("-") or "careers"
    slug, n = base, 2
    while RecruitmentGeneralSetting.objects.entire().filter(career_page_slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    setting.career_page_slug = slug
    setting.save(update_fields=["career_page_slug"])
    return slug


def company_id_for_career_slug(slug):
    """The company behind a public job-list slug, or None (cached in Redis)."""
    from django.core.cache import cache

    from recruitment.models import RecruitmentGeneralSetting

    key = _career_slug_key(slug)
    company_id = cache.get(key)
    if company_id is None:
        company_id = (
            RecruitmentGeneralSetting.objects.entire()
            .filter(career_page_slug=slug)
            .values_list("company_id", flat=True)
            .first()
        ) or 0
        cache.set(key, company_id, PUBLIC_LISTING_TTL)
    return company_id or None


def public_opening_for_slug(slug, opening_id):
    """
    A listed (Published/Closed, active) job opening of the company behind this
    career-page slug, or None. Another company's opening is never returned, so
    changing the number in a career-page URL cannot reach other clients.
    """
    from recruitment.models import Recruitment

    company_id = company_id_for_career_slug(slug)
    if company_id is None:
        return None
    return (
        Recruitment.default.filter(
            pk=opening_id,
            company_id=company_id,
            is_active=True,
            status__in=[Recruitment.Status.PUBLISHED, Recruitment.Status.CLOSED],
        )
        .select_related("company_id")
        .first()
    )

