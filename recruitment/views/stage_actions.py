"""
recruitment/views/stage_actions.py

The pipeline's decision actions: Move Forward, Bulk Move Forward, Bulk Reject.

Thin views. Direction and role (a drive Manager moves any way, a Stage Manager
only forward out of their own stage), the mandatory remark, the rejection email
and the audit event all live in recruitment.services.candidate, so the kanban
drag, these buttons and the API enforce one identical rule. These functions
only collect the remark and turn business errors into messages.

Single-candidate Reject is not here: it stays on the existing
AddToRejectedCandidatesView modal (recruitment/cbv/candidates.py), which now
calls the same service. Adding a second rejection screen would be exactly the
parallel implementation the PRD work has been avoiding.
"""

import json
import logging

from django.contrib import messages
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from horilla.decorators import hx_request_required, login_required
from horilla.http import HorillaRedirect
from recruitment.forms import MoveForwardForm
from recruitment.services import candidate as candidate_service
from recruitment.services.errors import RecruitmentError

logger = logging.getLogger(__name__)

MOVE_FORWARD_TEMPLATE = "cbv/pipeline/move_forward_form.html"


def _selected_ids(request):
    """
    The candidate ids a bulk action was asked to act on.

    The pipeline's bulk bar posts a JSON array in ``ids`` (the convention the
    existing bulk endpoints use); a plain repeated form field is accepted too.
    Unparseable input yields an empty selection rather than an exception.
    """
    raw = request.POST.get("ids")
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            return [str(value) for value in parsed]
    return request.POST.getlist("ids")


@login_required
@hx_request_required
def move_forward(request, pk):
    """
    Move one candidate to the next stage, with the mandatory remark.

    GET renders the remark modal; POST performs the move. The target stage is
    decided by the pipeline's own order inside the service -- it is never taken
    from the request, so this endpoint cannot be used to jump a candidate to an
    arbitrary stage.
    """
    try:
        candidate = candidate_service.get_candidate_for_user(
            request.user, pk, candidate_service.CHANGE_PERMISSION
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    # The step into Hired is the hiring handoff, not a stage move: it carries
    # the designation, offered CTC, joining date and the Form 2 questions with
    # it. So Move Forward on a candidate whose next stage is Hired opens that
    # form instead of a remark box -- the handoff performs the move itself once
    # it is complete. The same view serves the Candidate Details tab, so there
    # is one handoff screen, not two.
    next_stage = candidate_service.next_stage_for(candidate)
    if next_stage is not None and next_stage.stage_type == "hired":
        from recruitment.views.candidate_pool import candidate_handoff_tab

        return candidate_handoff_tab(request, pk)

    if request.method == "POST":
        form = MoveForwardForm(request.POST)
        if form.is_valid():
            try:
                moved = candidate_service.move_forward(
                    request.user, pk, remark=form.cleaned_data["remark"]
                )
            except RecruitmentError as error:
                form.add_error(None, str(error))
            else:
                messages.success(
                    request,
                    _("Moved to %(stage)s.") % {"stage": moved.stage_id.stage},
                )
                return HorillaRedirect(request)
    else:
        form = MoveForwardForm()

    return render(
        request,
        MOVE_FORWARD_TEMPLATE,
        {
            # Shown in the modal so the user knows where the candidate is going
            # before writing the remark -- the stage is not theirs to choose.
            "form": form,
            "candidate": candidate,
            "next_stage": next_stage,
            "post_url": request.path,
        },
    )


@login_required
@hx_request_required
def move_backward(request, pk):
    """
    Move one candidate back one stage, with the mandatory remark (Manager only).

    GET renders the remark modal; POST performs the move. The target is the
    previous stage in pipeline order, decided by the service.
    """
    try:
        candidate = candidate_service.get_candidate_for_user(
            request.user, pk, candidate_service.CHANGE_PERMISSION
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    previous_stage = candidate_service.previous_stage_for(candidate)
    if request.method == "POST":
        form = MoveForwardForm(request.POST)
        if form.is_valid():
            try:
                moved = candidate_service.move_backward(
                    request.user, pk, remark=form.cleaned_data["remark"]
                )
            except RecruitmentError as error:
                form.add_error(None, str(error))
            else:
                messages.success(
                    request,
                    _("Moved back to %(stage)s.") % {"stage": moved.stage_id.stage},
                )
                return HorillaRedirect(request)
    else:
        form = MoveForwardForm()

    return render(
        request,
        "cbv/pipeline/move_backward_form.html",
        {
            "form": form,
            "candidate": candidate,
            "previous_stage": previous_stage,
            "post_url": request.path,
        },
    )


def _report(request, result, *, done, none_done):
    """Turn a BulkResult into user-facing messages."""
    if result.succeeded:
        messages.success(request, done % {"count": result.succeeded})
    else:
        messages.error(request, none_done)

    # Every refusal is named, so "3 of 5" is never left unexplained. Reasons
    # repeat across rows, so they are grouped rather than listed per candidate.
    if result.failures:
        for reason in sorted(set(result.failures.values())):
            affected = sum(1 for value in result.failures.values() if value == reason)
            messages.warning(
                request,
                _("%(count)s not actioned: %(reason)s")
                % {"count": affected, "reason": reason},
            )


@login_required
@require_http_methods(["POST"])
def send_application_link(request, pk):
    """
    Re-send the public Form 1 link to one candidate.

    The link is sent automatically when a candidate is entered by hand, but
    that fails if the opening was not published yet (or the mail server was
    down), so the action stays available rather than being a one-shot.

    POST-only: sending mail is not something a link should do.
    """
    from recruitment.services.candidate_mail import (
        send_application_link as send_link,
    )

    try:
        candidate = candidate_service.get_candidate_for_user(
            request.user, pk, candidate_service.CHANGE_PERMISSION
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    if send_link(candidate, actor=request.user, request=request):
        messages.success(request, _("Application link emailed to the candidate."))
    else:
        messages.warning(
            request,
            _(
                "The application link could not be emailed. The candidate "
                "needs an email address and a job opening, and the Mail Server "
                "must be configured."
            ),
        )
    return HorillaRedirect(request)


@login_required
@require_http_methods(["POST"])
def bulk_move_forward(request):
    """
    Move every selected candidate to their own next stage.

    No remark: the PRD's deliberate bulk exception. Everything else still
    applies per candidate, and one refusal does not undo the rest.
    """
    result = candidate_service.bulk_move_forward(request.user, _selected_ids(request))
    _report(
        request,
        result,
        done=_("%(count)s candidate(s) moved forward."),
        none_done=_("No candidate was moved forward."),
    )
    return HorillaRedirect(request)


@login_required
@require_http_methods(["POST"])
def bulk_reject(request):
    """
    Reject every selected candidate, emailing each one.

    No remark, per the PRD. The notification is still sent per candidate: it is
    the candidate's to receive, and bulk is no reason to withhold it.
    """
    result = candidate_service.bulk_reject(request.user, _selected_ids(request))
    _report(
        request,
        result,
        done=_("%(count)s candidate(s) rejected and notified."),
        none_done=_("No candidate was rejected."),
    )
    return HorillaRedirect(request)
