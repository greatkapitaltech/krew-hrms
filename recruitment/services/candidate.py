"""
Candidate Pool: authorization, stage lifecycle, mapping, notes, documents
and export.

Candidate is the application record -- one row per (person, job opening) -- and
may also exist with no job opening at all.

Every state-changing operation here resolves the candidate through a
company-scoped lookup (another tenant's id is indistinguishable from a missing
one), authorizes against that candidate's own company/opening/stage rather than
"manages something somewhere", and writes its audit event inside the same
transaction so a rollback leaves no success event.
"""

import datetime
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from recruitment.services.audit import RecruitmentAuditService
from recruitment.services.authorization import (
    company_in_scope,
    has_company_wide_job_opening_authority,
    manages_job_opening,
)
from recruitment.services.errors import (
    CandidateCompanyUndeterminable,
    CandidateDocumentInvalid,
    CandidateNotFound,
    HiringHandoffRequired,
    InvalidStageTransition,
    RecruitmentError,
    RecruitmentPermissionDenied,
    StageMoveNotPermitted,
    StageRemarkRequired,
)

logger = logging.getLogger(__name__)

VIEW_PERMISSION = "recruitment.view_candidate"
CHANGE_PERMISSION = "recruitment.change_candidate"
EXPORT_PERMISSION = "recruitment.export_candidate"
NOTE_PERMISSION = "recruitment.add_stagenote"
DOCUMENT_PERMISSION = "recruitment.add_candidatedocument"


# ---------------------------------------------------------------------------
# Status buckets
# ---------------------------------------------------------------------------

#: The Candidate Pool reports five general buckets, NOT the pipeline's custom
#: stage names. A client can call a stage anything ("Client Interview",
#: "Document Review"); the Pool still has to answer "roughly where is this
#: person" in a vocabulary that is stable across every company's pipeline.
STATUS_APPLIED = "Applied"
STATUS_IN_PROGRESS = "In Progress"
STATUS_FINAL_HR_ROUND = "Final HR Round"
STATUS_HIRED = "Hired"
STATUS_REJECTED = "Rejected"

#: stage_type -> bucket. Anything not listed (including every custom
#: "interview"/"test" stage a client invents) falls through to In Progress,
#: which is exactly the PRD's rule.
_STAGE_TYPE_BUCKETS = {
    "applied": STATUS_APPLIED,
    "initial": STATUS_APPLIED,
    "final_hr_round": STATUS_FINAL_HR_ROUND,
    "hired": STATUS_HIRED,
    "cancelled": STATUS_REJECTED,
}


def candidate_status(candidate):
    """
    The Candidate Pool status bucket for one candidate.

    Rejection wins over everything else: a rejected candidate reads as Rejected
    regardless of which stage they were parked in. Three representations of
    rejection exist in this schema and are reconciled here in one place --
    RejectedCandidate (the reason record), Candidate.canceled (the flag), and
    stage_type "cancelled" (the parking stage). Callers must not re-derive this.
    """
    if candidate.canceled:
        return STATUS_REJECTED
    # rejected_candidate is a OneToOne; hasattr avoids a query-per-row when the
    # caller has select_related it, and avoids RelatedObjectDoesNotExist.
    if getattr(candidate, "rejected_candidate", None) is not None:
        return STATUS_REJECTED
    if candidate.hired:
        return STATUS_HIRED
    stage = candidate.stage_id
    if stage is None:
        # No pipeline yet (sourced into the pool, never mapped).
        return STATUS_APPLIED
    return _STAGE_TYPE_BUCKETS.get(stage.stage_type, STATUS_IN_PROGRESS)


def contact_verification_display(candidate):
    """
    "Verified" / "Unverified" / "N/A" for the Pool's verification column.

    N/A when the job opening does not require contact verification, or when
    there is no opening to require it -- the question simply does not apply.
    """
    opening = candidate.recruitment_id
    if opening is None or not getattr(opening, "contact_verification_required", False):
        return "N/A"
    # Stamped by recruitment.services.verification.stamp_candidate when the
    # applicant completed both factors for this opening.
    return "Verified" if candidate.contact_verified_at else "Unverified"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _manages_candidate_directly(user, candidate):
    """
    True when the user manages THIS candidate's own opening or stage.

    Deliberately narrow. The legacy decorators ask
    ``Stage.objects.filter(stage_managers=employee).exists()`` -- "do you manage
    any stage anywhere" -- which lets a manager of one drive reach every
    candidate in the database, across companies. This asks only about the
    objects the candidate actually belongs to.
    """
    employee = getattr(user, "employee_get", None)
    if employee is None:
        return False

    opening = candidate.recruitment_id
    if opening is not None and manages_job_opening(user, opening):
        return True

    stage = candidate.stage_id
    if stage is not None and stage.stage_managers.filter(pk=employee.pk).exists():
        return True

    return False


#: What a user is allowed to do with a candidate's stage.
#:
#: MOVE_ANY_DIRECTION is the drive Manager's override: the PRD makes them
#: accountable for the whole pipeline, so they may also pull a candidate back.
#: MOVE_FORWARD_ONLY is the Stage Manager's authority: they own one stage and
#: may only push a candidate out of it, never back into an earlier one and
#: never out of a stage that is not theirs.
MOVE_ANY_DIRECTION = "any"
MOVE_FORWARD_ONLY = "forward_only"


def stage_move_authority(user, candidate):
    """
    How far this user may move THIS candidate: a MOVE_* value, or None.

    Resolved from the objects the candidate actually belongs to -- their own
    job opening and their own current stage -- never from a role name and never
    from "manages some stage somewhere".

    The order matters: drive authority is checked first, so a drive Manager who
    also happens to manage one stage is not demoted to forward-only.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if candidate is None:
        return None

    if user.is_superuser or has_company_wide_job_opening_authority(user):
        return MOVE_ANY_DIRECTION

    opening = candidate.recruitment_id
    if opening is not None and manages_job_opening(user, opening):
        return MOVE_ANY_DIRECTION

    employee = getattr(user, "employee_get", None)
    stage = candidate.stage_id
    if (
        employee is not None
        and stage is not None
        and stage.stage_managers.filter(pk=employee.pk).exists()
    ):
        return MOVE_FORWARD_ONLY

    return None


def user_can_access_candidate(user, candidate, permission):
    """
    Authorization gate for every candidate operation.

    In order:

    1. authenticated;
    2. the candidate's company is inside the user's company scope;
    3. the user holds ``permission`` (being a manager never substitutes for it);
    4. object scope: company-wide authority, or managing this candidate's own
       opening/stage.

    Superusers bypass 3 and 4, matching Django.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if candidate is None:
        return False

    if not company_in_scope(user, candidate.company_id_id):
        return False

    if user.is_superuser:
        return True

    if not user.has_perm(permission):
        return False

    if has_company_wide_job_opening_authority(user):
        return True

    return _manages_candidate_directly(user, candidate)


def get_candidate_for_user(user, pk, permission=VIEW_PERMISSION):
    """
    Fetch a candidate by id, enforcing company scope and authorization.

    Raises CandidateNotFound when missing OR out of company scope -- the same
    answer for both, so a guessed id cannot confirm another tenant's candidate
    exists. Raises RecruitmentPermissionDenied only for a candidate the user can
    legitimately see the existence of.

    Use this instead of Candidate.objects.get()/find() in any authorization
    sensitive path.
    """
    from recruitment.models import Candidate

    candidate = (
        Candidate.objects.entire()
        .select_related("recruitment_id", "stage_id", "company_id")
        .filter(pk=pk)
        .first()
    )
    if candidate is None or not company_in_scope(user, candidate.company_id_id):
        raise CandidateNotFound()
    if not user_can_access_candidate(user, candidate, permission):
        raise RecruitmentPermissionDenied()
    return candidate


def accessible_candidates(user):
    """
    The Candidate Pool queryset for this user, company-scoped at the database.

    Company scoping is applied explicitly rather than relying on the selected
    company alone, because a NULL company passes the manager's filter through to
    every tenant. Rows with no company are excluded here: an unscoped candidate
    must never appear in a tenant's pool.
    """
    from base.auth_backends import get_allowed_company_ids
    from recruitment.models import Candidate

    queryset = Candidate.objects.entire().select_related(
        "recruitment_id", "stage_id", "job_position_id", "company_id"
    )
    if getattr(user, "is_superuser", False):
        return queryset
    allowed = get_allowed_company_ids(user)
    if not allowed:
        return queryset.none()
    return queryset.filter(company_id__in=allowed)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def resolve_company_for_candidate(user, job_opening=None, company=None):
    """
    The company a new candidate belongs to. Never taken from client input.

    Priority: the job opening's company (authoritative when there is one), then
    an explicitly passed company re-verified against the user's authorized
    companies, then the user's own resolved company.
    """
    from recruitment.services.authorization import (
        resolve_company_for_new_job_opening,
        selectable_companies_for_user,
    )

    if job_opening is not None and job_opening.company_id_id:
        return job_opening.company_id

    if company is not None:
        # Re-verified server-side: a submitted company is a request, not a fact.
        company_pk = getattr(company, "pk", company)
        verified = selectable_companies_for_user(user).filter(pk=company_pk).first()
        if verified is None:
            raise RecruitmentPermissionDenied()
        return verified

    resolved = resolve_company_for_new_job_opening(user)
    if resolved is None:
        raise CandidateCompanyUndeterminable()
    return resolved


def existing_candidate_with_email(email, *, job_opening=None, company=None, exclude_pk=None):
    """
    The candidate already holding this email where a new one would clash.

    One email is one candidate per job opening (PRD): the same person may apply
    to other openings with the same or a different email, but never twice to
    the same one. A candidate added to the Pool with no job is the same person
    as any other row of theirs in that company, so a job-less add clashes with
    the email anywhere in the company. Compared case-insensitively -- the
    database constraint is case-sensitive, so "A@x.com" would otherwise slip
    past "a@x.com".
    """
    from recruitment.models import Candidate

    email = (email or "").strip()
    if not email:
        return None
    rows = Candidate.objects.entire().filter(email__iexact=email)
    if job_opening is not None:
        rows = rows.filter(recruitment_id=job_opening)
    elif company is not None:
        rows = rows.filter(company_id=company)
    if exclude_pk:
        rows = rows.exclude(pk=exclude_pk)
    return rows.first()


def create_candidate(user, *, company=None, job_opening=None, **fields):
    """
    Create a Candidate Pool record, with or without a job opening.

    A candidate with no opening is explicitly supported: they are sourced into
    the pool and mapped later. They still get a company, because a company-less
    candidate would be visible to every tenant.
    """
    from recruitment.models import Candidate, RecruitmentAuditEvent

    resolved_company = resolve_company_for_candidate(user, job_opening, company)

    with transaction.atomic():
        candidate = Candidate(
            recruitment_id=job_opening,
            company_id=resolved_company,
            **fields,
        )
        candidate.save()

        RecruitmentAuditService.record(
            event_type=RecruitmentAuditEvent.EventType.CANDIDATE_CREATED,
            actor=user,
            company=resolved_company,
            candidate=candidate,
            job_opening=job_opening,
            details={
                "has_job_opening": job_opening is not None,
                "job_opening_id": job_opening.pk if job_opening else None,
            },
        )
        return candidate


# ---------------------------------------------------------------------------
# Stage lifecycle
# ---------------------------------------------------------------------------


def entry_stage(job_opening):
    """
    The stage a candidate entering this opening's pipeline lands in.

    The PRD's entry stage is Applied, and every opening is seeded with one
    (recruitment.signals.create_initial_stage). The sequence fallback covers
    openings predating that seeding, and openings whose Applied stage a client
    has renamed away -- it is matched on stage_type, so a rename is harmless.

    The single definition for both the public application form and the internal
    "Add Candidate" screens: a candidate must never be saved stage-less.
    """
    if job_opening is None:
        return None
    from recruitment.models import STAGE_APPLIED

    stages = job_opening.stage_set.all()
    return (
        stages.filter(stage_type=STAGE_APPLIED).first()
        or stages.order_by("sequence").first()
    )


def _assert_move_allowed(user, candidate, previous_stage, stage):
    """
    Enforce the PRD's movement rules for one move.

    A drive Manager may move in any direction. A Stage Manager may only move a
    candidate FORWARD, and only out of the stage they manage -- which is
    already what gave them the authority, so the current stage needs no second
    check here.

    "Forward" is by Stage.sequence, the same order the pipeline renders in. A
    candidate with no stage yet is entering the pipeline, which is forward by
    definition.

    Not a UI concern: the kanban hides backward drops, but this is the check
    that actually decides, so a crafted POST is refused too.
    """
    authority = stage_move_authority(user, candidate)

    if authority == MOVE_ANY_DIRECTION:
        return
    if authority is None:
        # user_can_access_candidate already passed, so this is a user who holds
        # the permission but manages neither the opening nor the current stage.
        raise StageMoveNotPermitted(
            _(
                "Only this job opening's manager, or the manager of the "
                "candidate's current stage, can move this candidate."
            )
        )

    # MOVE_FORWARD_ONLY
    if previous_stage is None:
        return
    if stage.sequence > previous_stage.sequence:
        return

    raise StageMoveNotPermitted(
        _(
            "As the manager of %(stage)s you can only move a candidate "
            "forward. Ask this job opening's manager to move them back."
        )
        % {"stage": previous_stage.stage}
    )


def next_stage_for(candidate):
    """
    The stage immediately after the candidate's current one, or None.

    "Immediately after" is by Stage.sequence within the candidate's own
    opening. The cancelled stage is skipped: rejection is its own action with
    its own remark and email, never the result of pressing Move Forward.
    """
    from recruitment.models import Stage

    if candidate.recruitment_id_id is None:
        return None

    stages = Stage.objects.entire().filter(
        recruitment_id=candidate.recruitment_id_id
    ).exclude(stage_type="cancelled")

    current = candidate.stage_id
    if current is None:
        return stages.order_by("sequence").first()
    return stages.filter(sequence__gt=current.sequence).order_by("sequence").first()


def previous_stage_for(candidate):
    """The stage immediately before the candidate's current one, or None."""
    from recruitment.models import Stage

    current = candidate.stage_id
    if candidate.recruitment_id_id is None or current is None:
        return None
    return (
        Stage.objects.entire()
        .filter(
            recruitment_id=candidate.recruitment_id_id,
            sequence__lt=current.sequence,
        )
        .exclude(stage_type="cancelled")
        .order_by("-sequence")
        .first()
    )


def can_move_backward(user, candidate):
    """
    Whether Move Backward applies: a drive Manager / HR, a candidate who is
    neither hired nor rejected, and an earlier stage to go back to.
    """
    if stage_move_authority(user, candidate) != MOVE_ANY_DIRECTION:
        return False
    if candidate.hired or candidate_status(candidate) == STATUS_REJECTED:
        return False
    stage = candidate.stage_id
    if stage is not None and stage.stage_type == "hired":
        return False
    return previous_stage_for(candidate) is not None


def move_backward(user, candidate_pk, *, remark):
    """
    Pull one candidate back to the previous stage (PRD: Manager only).

    One stage at a time, with a mandatory remark. Hired is the end of
    Recruitment's authority (reversals belong to Pre-Onboarding) and rejection
    is terminal, so neither moves back.
    """
    remark = (remark or "").strip()
    if not remark:
        raise StageRemarkRequired()

    candidate = get_candidate_for_user(user, candidate_pk, CHANGE_PERMISSION)
    if stage_move_authority(user, candidate) != MOVE_ANY_DIRECTION:
        raise StageMoveNotPermitted(
            _("Only this job opening's manager can move a candidate back.")
        )
    if not can_move_backward(user, candidate):
        raise InvalidStageTransition(
            _("This candidate cannot be moved back.")
        )
    stage = previous_stage_for(candidate)
    return move_to_stage(user, candidate_pk, stage.pk, remark=remark)


def move_forward(user, candidate_pk, *, remark):
    """
    Push one candidate to the next stage of their pipeline.

    The PRD's single-candidate action: the next stage is decided by the
    pipeline order rather than picked by the user, and a remark is mandatory
    every time -- it is the record of why this person advanced.

    Bulk Move Forward deliberately does not ask for one; see
    bulk_move_forward().
    """
    remark = (remark or "").strip()
    if not remark:
        raise StageRemarkRequired()

    candidate = get_candidate_for_user(user, candidate_pk, CHANGE_PERMISSION)
    stage = next_stage_for(candidate)
    if stage is None:
        raise InvalidStageTransition(
            _("This candidate is already at the last stage of the pipeline.")
        )
    return move_to_stage(user, candidate_pk, stage.pk, remark=remark)


def move_to_stage(
    user,
    candidate_pk,
    stage_pk,
    *,
    permission=CHANGE_PERMISSION,
    remark=None,
    via_handoff=False,
):
    """
    Move a candidate to another stage of their OWN job opening.

    The target stage must belong to the candidate's opening. Moving a candidate
    into a stage of a different opening would silently re-home the application,
    so it is rejected rather than reinterpreted.

    Direction and role are enforced here (see _assert_move_allowed), so every
    caller -- the kanban drag, the pipeline dropdown, the bulk actions and the
    API -- gets the same rule.

    The Hired stage is not reachable by a plain move. Hiring carries Form 2
    with it (designation, offered CTC, joining date, handoff answers), so a
    drag onto Hired is refused with HiringHandoffRequired and the user is sent
    to the handoff form; ``via_handoff=True`` is how that form -- through
    hire_candidate() -- completes the move once it has the data. Without this
    gate a candidate could be hired with none of it recorded.

    ``remark`` is optional at this level: Move Forward requires one and passes
    it in, bulk moves and the internal hire/map paths do not.

    Locks the candidate row and re-reads the stage inside the transaction, so
    two concurrent moves cannot both act on stale state.
    """
    from recruitment.models import Candidate, RecruitmentAuditEvent, Stage

    with transaction.atomic():
        candidate = get_candidate_for_user(user, candidate_pk, permission)
        locked = (
            Candidate.objects.entire().select_for_update().get(pk=candidate.pk)
        )

        if locked.recruitment_id_id is None:
            raise InvalidStageTransition(
                _("This candidate is not attached to a job opening, so they have no pipeline.")
            )

        stage = (
            Stage.objects.entire()
            .filter(pk=stage_pk, recruitment_id=locked.recruitment_id_id)
            .first()
        )
        if stage is None:
            raise InvalidStageTransition(
                _("That stage does not belong to this candidate's job opening.")
            )

        previous_stage = locked.stage_id
        if previous_stage is not None and previous_stage.pk == stage.pk:
            # Idempotent: no change, no event.
            return locked

        # End states (PRD). Checked before anything is saved or recorded, so a
        # refused move never appears in History.
        if locked.canceled or (
            previous_stage is not None and previous_stage.stage_type == "cancelled"
        ):
            # "Once someone is rejected, there's no way to bring them back into
            # that pipeline -- not even the Manager can undo it."
            raise InvalidStageTransition(
                _("This candidate is rejected and can't be moved.")
            )
        if locked.hired or (
            previous_stage is not None and previous_stage.stage_type == "hired"
        ):
            # Hired is the end of Recruitment's authority; any reversal is
            # Pre-Onboarding's job.
            raise InvalidStageTransition(
                _(
                    "This candidate is hired. Changes after hiring are handled "
                    "in Pre-Onboarding."
                )
            )
        if stage.stage_type == "cancelled":
            # Rejection carries a remark, a rejection record and the email;
            # only Reject / Bulk Reject do that.
            raise InvalidStageTransition(_("Use Reject to reject a candidate."))

        if stage.stage_type == "hired" and not via_handoff:
            raise HiringHandoffRequired()

        _assert_move_allowed(user, locked, previous_stage, stage)

        locked.stage_id = stage
        # hired/canceled are derived by Candidate.save() from the stage type.
        locked.save()

        _record_stage_event(user, locked, previous_stage, stage, remark=remark)
        return locked


def _record_stage_event(user, candidate, previous_stage, stage, *, remark=None):
    """One audit event per stage move, typed by what the move means."""
    from recruitment.models import RecruitmentAuditEvent

    event_type = RecruitmentAuditEvent.EventType.CANDIDATE_STAGE_CHANGED
    if stage.stage_type == "hired":
        event_type = RecruitmentAuditEvent.EventType.CANDIDATE_HIRED
    elif stage.stage_type == "cancelled":
        event_type = RecruitmentAuditEvent.EventType.CANDIDATE_REJECTED

    RecruitmentAuditService.record(
        event_type=event_type,
        actor=user,
        company=candidate.company_id,
        candidate=candidate,
        job_opening=candidate.recruitment_id,
        stage=stage,
        details={
            "previous_stage_id": previous_stage.pk if previous_stage else None,
            "previous_stage": str(previous_stage) if previous_stage else None,
            "new_stage_id": stage.pk,
            "new_stage": str(stage),
            "new_stage_type": stage.stage_type,
            "status": candidate_status(candidate),
            # The remark is the decision record the PRD asks History to show.
            # Author-written business context, not PII.
            "remark": (remark or "").strip(),
        },
    )


def reject_candidate(
    user,
    candidate_pk,
    *,
    reason=None,
    reject_reason_ids=None,
    require_reason=True,
    notify_candidate=True,
):
    """
    Reject a candidate within their current job opening.

    Reconciles the three existing representations in one operation: sets
    ``canceled``, moves them to the opening's cancelled stage (Candidate.save()
    does this), and records a RejectedCandidate row carrying the reason.

    Per the PRD a remark is mandatory -- it is the record of why this person was
    stopped -- and the candidate is emailed automatically from the editable
    "Candidate Rejection" mail template. ``require_reason=False`` is for Bulk
    Reject, the one place the PRD deliberately skips the remark step;
    ``notify_candidate=False`` exists for callers that have already told the
    candidate themselves.

    The email is sent before the audit event is written so the event records
    what actually happened. A mail failure does not undo the rejection: the
    hiring decision stands and the failure is recorded and logged.

    Rejection is terminal for that opening -- Feature 2 established there is no
    restore. The candidate remains in the Candidate Pool and can be mapped to a
    different PUBLISHED opening.
    """
    from recruitment.models import Candidate, RecruitmentAuditEvent, RejectedCandidate
    from recruitment.services.candidate_mail import send_rejection_email

    reason = (reason or "").strip()
    if require_reason and not reason:
        raise StageRemarkRequired(
            _("Enter a remark explaining why this candidate is being rejected.")
        )

    with transaction.atomic():
        candidate = get_candidate_for_user(user, candidate_pk, CHANGE_PERMISSION)
        locked = Candidate.objects.entire().select_for_update().get(pk=candidate.pk)

        if locked.canceled:
            # Idempotent: already rejected, no second event and no second email.
            locked.rejection_email_sent = None
            return locked

        previous_stage = locked.stage_id
        locked.canceled = True
        locked.save()

        rejection, _created = RejectedCandidate.objects.get_or_create(
            candidate_id=locked,
            defaults={"description": reason},
        )
        if reject_reason_ids:
            rejection.reject_reason_id.set(reject_reason_ids)

        email_sent = None
        if notify_candidate:
            email_sent = send_rejection_email(locked, actor=user)

        RecruitmentAuditService.record(
            event_type=RecruitmentAuditEvent.EventType.CANDIDATE_REJECTED,
            actor=user,
            company=locked.company_id,
            candidate=locked,
            job_opening=locked.recruitment_id,
            details={
                "previous_stage_id": previous_stage.pk if previous_stage else None,
                # The free-text reason is business context, not PII.
                "reason": reason,
                "reject_reason_ids": list(reject_reason_ids or []),
                # None = not attempted; False = attempted and not delivered.
                "rejection_email_sent": email_sent,
            },
        )
        # Transient, for the caller's user-facing message: a view must not tell
        # the user an email went out when it did not.
        locked.rejection_email_sent = email_sent
        return locked


def hire_candidate(user, candidate_pk):
    """
    Move a candidate into their opening's hired stage.

    The completion step of the hiring handoff, and the ONLY sanctioned way
    into the Hired stage -- which is why it passes via_handoff=True through the
    gate in move_to_stage. Call it from the handoff flow, once Form 2 has been
    validated and stored, not as a shortcut for "mark as hired": doing that
    would hire someone with no designation, CTC, joining date or handoff
    answers, which is exactly what the gate exists to prevent.

    Recruitment provides no reversal: un-hiring belongs to Pre-Onboarding, per
    the PRD. The candidate stays in the Candidate Pool.
    """
    from recruitment.models import Candidate, Stage

    candidate = get_candidate_for_user(user, candidate_pk, CHANGE_PERMISSION)
    if candidate.recruitment_id_id is None:
        raise InvalidStageTransition(
            _("This candidate is not attached to a job opening.")
        )
    hired_stage = (
        Stage.objects.entire()
        .filter(recruitment_id=candidate.recruitment_id_id, stage_type="hired")
        .first()
    )
    if hired_stage is None:
        raise InvalidStageTransition(
            _("This job opening has no hired stage configured.")
        )
    # move_to_stage emits CANDIDATE_HIRED via _record_stage_event.
    return move_to_stage(user, candidate_pk, hired_stage.pk, via_handoff=True)


# ---------------------------------------------------------------------------
# Bulk stage actions
# ---------------------------------------------------------------------------
#
# The PRD offers Bulk Move Forward and Bulk Reject for clearing a stage after a
# screening round, and deliberately drops the per-candidate remark there -- one
# remark pasted onto fifty people is not accountability, it is noise.
#
# Everything else still applies to every single candidate: the same
# authorization, the same direction/role rule, the same rejection email, the
# same audit event. These helpers are thin loops over the single-candidate
# services precisely so the two paths cannot drift apart.
#
# Each candidate is processed in its own transaction, so one refusal (a stage
# manager hitting a candidate outside their stage, a candidate already at the
# last stage) does not roll back the ones that succeeded. The caller gets a
# per-candidate report and shows the failures.


class BulkResult:
    """
    Outcome of a bulk action: what succeeded, and why each failure failed.

    ``failures`` maps candidate pk -> user-safe message, so the view can list
    exactly which rows were refused instead of a bare "some failed".
    """

    def __init__(self):
        self.moved = []
        self.failures = {}

    @property
    def succeeded(self):
        return len(self.moved)

    @property
    def failed(self):
        return len(self.failures)

    def __bool__(self):
        return bool(self.moved)


def _bulk(candidate_pks, operation):
    result = BulkResult()
    for candidate_pk in candidate_pks:
        try:
            operation(candidate_pk)
        except RecruitmentError as exc:
            result.failures[candidate_pk] = str(exc)
        else:
            result.moved.append(candidate_pk)
    return result


def _require_drive_manager(user, candidate):
    """PRD: bulk actions are drive-level -- the Manager only, not Stage Managers."""
    if stage_move_authority(user, candidate) != MOVE_ANY_DIRECTION:
        raise StageMoveNotPermitted(
            _("Bulk actions are available to the job opening's managers only.")
        )


def bulk_move_forward(user, candidate_pks, *, remark=None):
    """
    Push several candidates to their own next stage.

    Each candidate advances within their OWN pipeline, so a selection spanning
    two job openings is fine. No remark is required (the PRD's bulk exception),
    but one may be passed and is then recorded against every candidate.
    """
    remark = (remark or "").strip()

    def advance(candidate_pk):
        candidate = get_candidate_for_user(user, candidate_pk, CHANGE_PERMISSION)
        _require_drive_manager(user, candidate)
        stage = next_stage_for(candidate)
        if stage is None:
            raise InvalidStageTransition(
                _("Already at the last stage of the pipeline.")
            )
        move_to_stage(user, candidate_pk, stage.pk, remark=remark)

    return _bulk(candidate_pks, advance)


def bulk_reject(user, candidate_pks, *, reason=None, reject_reason_ids=None):
    """
    Reject several candidates, emailing each one.

    No remark is required (the PRD's bulk exception). The rejection email is
    still sent per candidate -- it is the candidate's notification, not an
    internal record, so bulk is no reason to withhold it.
    """
    def reject(candidate_pk):
        _require_drive_manager(
            user, get_candidate_for_user(user, candidate_pk, CHANGE_PERMISSION)
        )
        reject_candidate(
            user,
            candidate_pk,
            reason=reason,
            reject_reason_ids=reject_reason_ids,
            require_reason=False,
        )

    return _bulk(candidate_pks, reject)


# ---------------------------------------------------------------------------
# Mapping into a job opening
# ---------------------------------------------------------------------------


def map_to_job_opening(user, candidate_pk, job_opening_pk):
    """
    Map a Candidate Pool candidate into a PUBLISHED job opening.

    Creates a NEW application rather than moving the existing one, so the
    original keeps its stage, screening answers, documents and audit history.
    The same person can therefore hold applications to several openings at once.

    Idempotency is enforced by the database, not by a check-then-insert: the
    unique constraint on (email, recruitment_id) is the authority, so two
    concurrent maps of the same candidate to the same opening collapse to one
    row and produce exactly one CANDIDATE_MAPPED_TO_JOB_OPENING event.

    Only a PUBLISHED opening may receive a mapping -- enforced here in the
    service, by the Feature 1 rule, not in the UI.
    """
    from recruitment.models import Candidate, RecruitmentAuditEvent, Recruitment, Stage
    from recruitment.services.job_opening import assert_accepts_new_candidates

    with transaction.atomic():
        source = get_candidate_for_user(user, candidate_pk, CHANGE_PERMISSION)

        opening = (
            Recruitment.default.select_related("company_id")
            .filter(pk=job_opening_pk)
            .first()
        )
        # Cross-company mapping is reported as not-found, consistent with every
        # other object lookup in this codebase.
        if opening is None or not company_in_scope(user, opening.company_id_id):
            raise CandidateNotFound(
                _("No job opening found matching the query.")
            )
        if not user_can_access_candidate(user, source, CHANGE_PERMISSION):
            raise RecruitmentPermissionDenied()
        # A candidate may never be mapped across tenants.
        if source.company_id_id and opening.company_id_id != source.company_id_id:
            raise RecruitmentPermissionDenied()

        # DRAFT / REVIEW / CLOSED / REMOVED all refuse new candidates.
        assert_accepts_new_candidates(opening)

        initial_stage = (
            Stage.objects.entire()
            .filter(recruitment_id=opening, stage_type="applied")
            .order_by("sequence", "id")
            .first()
            or Stage.objects.entire()
            .filter(recruitment_id=opening)
            .order_by("sequence", "id")
            .first()
        )

        job_position = (
            source.job_position_id
            if source.job_position_id_id
            and opening.open_positions.filter(pk=source.job_position_id_id).exists()
            else opening.job_position_id
        )

        try:
            with transaction.atomic():
                # Case-insensitive: "A@x.com" already in this opening IS this
                # application, not a second one.
                same = existing_candidate_with_email(source.email, job_opening=opening)
                lookup_email = same.email if same is not None else source.email
                application, created = Candidate.objects.entire().get_or_create(
                    email=lookup_email,
                    recruitment_id=opening,
                    defaults={
                        "name": source.name,
                        "mobile": source.mobile,
                        "resume": source.resume,
                        "company_id": opening.company_id,
                        "stage_id": initial_stage,
                        "job_position_id": job_position,
                        "gender": source.gender,
                        "source": source.source,
                    },
                )
        except IntegrityError:
            # Lost a race on the unique constraint: the other transaction
            # created it, so re-read rather than reporting an error.
            created = False
            application = Candidate.objects.entire().get(
                email=source.email, recruitment_id=opening
            )

        if not created:
            # Already mapped. Return the existing application without a second
            # (misleading) audit event.
            return application, False

        RecruitmentAuditService.record(
            event_type=RecruitmentAuditEvent.EventType.CANDIDATE_MAPPED_TO_JOB_OPENING,
            actor=user,
            company=opening.company_id,
            candidate=application,
            job_opening=opening,
            stage=initial_stage,
            details={
                "source_candidate_id": source.pk,
                "source_job_opening_id": source.recruitment_id_id,
                "target_job_opening_id": opening.pk,
                "initial_stage_id": initial_stage.pk if initial_stage else None,
            },
        )
        return application, True


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def add_note(user, candidate_pk, description):
    """
    Post a permanent internal note against a candidate.

    Notes cannot be edited or deleted afterwards (CandidateNote enforces this at
    the model), so there is deliberately no update/delete counterpart here.
    """
    from recruitment.models import CandidateNote, RecruitmentAuditEvent

    text = (description or "").strip()
    if not text:
        raise InvalidStageTransition(_("A note cannot be empty."))

    with transaction.atomic():
        candidate = get_candidate_for_user(user, candidate_pk, NOTE_PERMISSION)

        note = CandidateNote(
            candidate_id=candidate,
            stage_id=candidate.stage_id,
            company_id=candidate.company_id,
            description=text,
        )
        note.save()

        RecruitmentAuditService.record(
            event_type=RecruitmentAuditEvent.EventType.CANDIDATE_NOTE_ADDED,
            actor=user,
            company=candidate.company_id,
            candidate=candidate,
            job_opening=candidate.recruitment_id,
            stage=candidate.stage_id,
            # The note body is internal commentary; only its identity is
            # audited, never the text.
            details={"note_id": note.pk, "length": len(text)},
        )
        return note


def notes_for_candidate(user, candidate_pk):
    """A candidate's notes, newest first, after an access check."""
    from recruitment.models import CandidateNote

    candidate = get_candidate_for_user(user, candidate_pk, VIEW_PERMISSION)
    return (
        CandidateNote.objects.entire()
        .filter(candidate_id=candidate)
        .select_related("created_by", "stage_id")
        .order_by("-created_at", "-id")
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def upload_document(user, candidate_pk, uploaded_file, *, title=None):
    """
    Attach a document to a candidate on their behalf.

    Validation is server-side and content-based: PDF only, 15 MB, verified from
    the file's own bytes rather than its filename or Content-Type. A rejected
    upload creates no CandidateDocument row and no audit event.
    """
    from django.core.exceptions import ValidationError

    from recruitment.models import (
        CandidateDocument,
        RecruitmentAuditEvent,
        validate_candidate_document,
    )

    if uploaded_file is None:
        raise CandidateDocumentInvalid(_("No file was uploaded."))

    with transaction.atomic():
        candidate = get_candidate_for_user(user, candidate_pk, DOCUMENT_PERMISSION)

        # Validate BEFORE creating anything, so a bad upload cannot leave a
        # partial row behind.
        try:
            validate_candidate_document(uploaded_file)
        except ValidationError as error:
            raise CandidateDocumentInvalid(" ".join(error.messages))

        document = CandidateDocument(
            candidate_id=candidate,
            title=(title or getattr(uploaded_file, "name", "Document"))[:250],
            document=uploaded_file,
            status="approved",
        )
        document.save()

        RecruitmentAuditService.record(
            event_type=RecruitmentAuditEvent.EventType.CANDIDATE_DOCUMENT_UPLOADED,
            actor=user,
            company=candidate.company_id,
            candidate=candidate,
            job_opening=candidate.recruitment_id,
            object_type="CandidateDocument",
            object_id=document.pk,
            details={
                "document_id": document.pk,
                "title": document.title,
                "size_bytes": getattr(uploaded_file, "size", None),
            },
        )
        return document


def sync_resume_document(candidate):
    """
    Keep the candidate's resume in the documents table (PRD: one store).

    candidate.resume stays as the shortcut to the current resume. When it
    points to a file that is not yet the active resume document, the previous
    active resume is kept but marked inactive and a new active row is added.
    """
    from recruitment.models import CandidateDocument, RecruitmentAuditEvent

    name = getattr(candidate.resume, "name", "") or ""
    if not name:
        return None
    resumes = CandidateDocument.objects.entire().filter(
        candidate_id=candidate, document_type="resume"
    )
    active = resumes.filter(is_active=True).first()
    if active is not None and active.document.name == name:
        return active
    with transaction.atomic():
        resumes.filter(is_active=True).update(is_active=False)
        document = CandidateDocument.objects.create(
            candidate_id=candidate,
            title="Resume",
            document=name,
            document_type="resume",
            status="approved",
            is_active=True,
        )
    RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.CANDIDATE_DOCUMENT_UPLOADED,
        company=candidate.company_id,
        job_opening=candidate.recruitment_id,
        candidate=candidate,
        details={
            "document_id": document.pk,
            "document_type": "resume",
            "replaced_previous": active is not None,
        },
    )
    return document


def documents_for_candidate(user, candidate_pk):
    """
    Every document on a candidate, from whichever flow produced it.

    Visibility is not stage-specific: anyone authorized for the candidate sees
    all of their documents.
    """
    from recruitment.models import CandidateDocument

    candidate = get_candidate_for_user(user, candidate_pk, VIEW_PERMISSION)
    from django.db.models import Case, IntegerField, Value, When

    # Current resume first, then everything else newest first; earlier resume
    # versions are included (shown as previous versions).
    return (
        CandidateDocument.objects.entire()
        .filter(candidate_id=candidate)
        .select_related("document_request_id", "job_opening_question")
        .annotate(
            current_resume=Case(
                When(document_type="resume", is_active=True, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("current_resume", "-created_at", "-id")
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def assert_can_export(user):
    """
    Candidate Pool export requires the explicit export_candidate permission.

    Deliberately NOT base.methods.has_export_access: that helper returns True
    for everyone when a company has no DefaultExportPermission row configured,
    which is too permissive for bulk candidate PII.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        raise RecruitmentPermissionDenied()
    if getattr(user, "is_superuser", False):
        return True
    if not user.has_perm(EXPORT_PERMISSION):
        raise RecruitmentPermissionDenied()
    return True


def record_export(user, queryset, *, export_format=None, filters=None):
    """
    Audit a successful Candidate Pool export.

    Records how much was exported and under what filter, never the exported rows
    themselves -- an audit trail must not become a second copy of the PII.
    """
    from recruitment.models import RecruitmentAuditEvent

    company = None
    first = queryset.first() if hasattr(queryset, "first") else None
    if first is not None:
        company = first.company_id

    return RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.CANDIDATE_EXPORTED,
        actor=user,
        company=company,
        details={
            "candidate_count": queryset.count() if hasattr(queryset, "count") else None,
            "format": export_format,
            "filters": filters or {},
        },
    )
