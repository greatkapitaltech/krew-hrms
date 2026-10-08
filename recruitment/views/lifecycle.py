"""
recruitment/views/lifecycle.py

Job-opening lifecycle actions:

    DRAFT -> REVIEW -> PUBLISHED -> CLOSED

Every view here is a thin wrapper: it delegates to
recruitment.services.job_opening, which owns company scoping, the
object-scoped permission check, transition validation, row locking and the
business audit event. Nothing in this module decides authorization itself,
so the same rules apply identically to the UI and the API.

Business errors are surfaced as user-facing messages -- never a stack trace
or database detail.
"""

import logging

from django.contrib import messages
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from horilla.decorators import login_required
from horilla.http import HorillaRedirect
from recruitment.services import job_opening as lifecycle
from recruitment.services.errors import PublicationValidationError, RecruitmentError

logger = logging.getLogger(__name__)


def _run(request, operation, success_message, *args, **kwargs):
    """
    Execute one lifecycle operation and translate the outcome into messages.

    PublicationValidationError is handled separately so the user sees every
    blocker at once rather than only the first, matching how the Mark-as-Active
    gate reports blockers in krew_company_onboarding.
    """
    try:
        operation(*args, **kwargs)
    except PublicationValidationError as error:
        for blocker in error.blockers or [error.message]:
            messages.error(request, blocker)
    except RecruitmentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, success_message)
    return HorillaRedirect(request)


@login_required
@require_http_methods(["POST"])
def submit_for_review(request, rec_id):
    """DRAFT -> REVIEW."""
    return _run(
        request,
        lifecycle.submit_for_review,
        _("Job opening submitted for review."),
        request.user,
        rec_id,
    )


@login_required
@require_http_methods(["POST"])
def send_back_for_changes(request, rec_id):
    """REVIEW -> DRAFT, with an optional remark recorded on the audit event."""
    return _run(
        request,
        lifecycle.send_back_for_changes,
        _("Job opening sent back for changes."),
        request.user,
        rec_id,
        remark=(request.POST.get("remark") or "").strip() or None,
    )


def _review_then_publish(user, rec_id):
    from recruitment.models import Recruitment

    opening = Recruitment.objects.filter(pk=rec_id).first()
    if (
        opening is not None
        and opening.status == Recruitment.Status.DRAFT
        and not lifecycle.publication_blockers(opening)
    ):
        lifecycle.submit_for_review(user, rec_id)
    return lifecycle.publish(user, rec_id)


@login_required
@require_http_methods(["POST"])
def publish(request, rec_id):
    """
    DRAFT/REVIEW -> PUBLISHED, from the Save & Review summary screen.

    The PRD flow is Save & Review -> Publish, with no separate "submit for
    review" step, so a DRAFT is passed through REVIEW first. Both transitions
    stay audited. A draft with publication blockers is left as it is, and
    publish() then reports why it cannot go live.

    The publication boundary: from here the opening is live on the public
    listing and accepts applications, and later features treat this
    transition as the point at which its screening questions freeze.
    """
    return _run(
        request,
        _review_then_publish,
        _("Job opening published."),
        request.user,
        rec_id,
    )


@login_required
@require_http_methods(["POST"])
def close(request, rec_id):
    """
    PUBLISHED -> CLOSED. Terminal.

    Stops new applications only. Candidates, applications, stages, Candidate
    Pool rows, history and audit records are all left intact.
    """
    return _run(
        request,
        lifecycle.close,
        _("Job opening closed."),
        request.user,
        rec_id,
        reason=(request.POST.get("reason") or "").strip() or None,
    )


@login_required
@require_http_methods(["POST"])
def remove(request, rec_id):
    """
    Take a job opening down (PRD "Remove").

    An archive, not a delete: the record is withdrawn from active use but
    kept for audit, and no candidate, application, stage or history is
    touched. Gated on archive_recruitment.
    """
    return _run(
        request,
        lifecycle.remove,
        _("Job opening removed."),
        request.user,
        rec_id,
        reason=(request.POST.get("reason") or "").strip() or None,
    )


@login_required
def publication_checklist(request, rec_id):
    """
    Read-only preview of what still blocks publication.

    Powers the Save & Review summary screen, so a user sees the outstanding
    items before attempting to publish rather than discovering them one
    error at a time.
    """
    from recruitment.services.authorization import get_job_opening_for_user

    try:
        opening = get_job_opening_for_user(
            request.user, rec_id, "recruitment.change_recruitment"
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    return render(
        request,
        "cbv/recruitment/publication_checklist.html",
        publication_checklist_context(opening),
    )


def publication_checklist_context(opening):
    """
    Everything the Review & Publish summary shows. One definition, used both by
    the row action and right after Create -> Save & Review, so the two screens
    are always identical.
    """
    from recruitment.services.screening import question_sections

    return {
        "job_opening": opening,
        "blockers": lifecycle.publication_blockers(opening),
        "can_publish": opening.status in (opening.Status.DRAFT, opening.Status.REVIEW),
        # The actual question set that will be frozen (or already was), so the
        # reviewer sees the questions themselves rather than only the template
        # names.
        "sections": question_sections(opening),
        # Review is where someone checks the question set before going live, so
        # it is also where a missing question should be addable -- until
        # publication freezes it.
        "can_add": opening.status != opening.Status.PUBLISHED,
    }
