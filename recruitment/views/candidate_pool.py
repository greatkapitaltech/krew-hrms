"""
recruitment/views/candidate_pool.py

Candidate Pool actions: mapping into a job opening, permanent notes, documents.

Thin views. Every rule -- company scope, object-level authorization, the
PUBLISHED-only mapping rule, PDF/size validation, idempotency and the audit
event -- lives in recruitment.services.candidate, so the UI and the API enforce
exactly the same thing. These functions only translate business errors into
user-facing messages.

Notes are permanent: there is deliberately no update or delete view here, and
the templates render no edit or delete control. CandidateNote refuses both at
the model level regardless.
"""

import logging

from django.contrib import messages
from django.db import transaction
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from horilla.decorators import hx_request_required, login_required
from horilla.http import HorillaRedirect
from recruitment.services import candidate as candidate_service
from recruitment.services.errors import RecruitmentError

logger = logging.getLogger(__name__)


@login_required
@hx_request_required
def map_candidate_form(request, pk):
    """
    The Map to Job Opening modal for one candidate.

    GET-only: it just offers the choice. The mapping itself stays on the
    POST-only map_candidate_to_job_opening below, so an application can never
    be created by following a link.

    Only PUBLISHED openings are listed, mirroring the service's rule; the
    service remains the authority, so a posted id for any other status is still
    refused.
    """
    from recruitment.models import Recruitment

    try:
        candidate = candidate_service.get_candidate_for_user(
            request.user, pk, candidate_service.CHANGE_PERMISSION
        )
    except RecruitmentError as exc:
        # Includes the cross-company case, which reports as not-found.
        messages.error(request, str(exc))
        return HorillaRedirect(request)

    return render(
        request,
        "cbv/candidate_pool/map_form.html",
        {
            "candidate": candidate,
            # Company-scoped by the manager; PUBLISHED by the Feature 1 rule.
            # The candidate's own opening is left out: they are already in it.
            "openings": Recruitment.objects.filter(
                status=Recruitment.Status.PUBLISHED, is_active=True
            )
            .exclude(pk=candidate.recruitment_id_id)
            .order_by("title"),
        },
    )


@login_required
@require_http_methods(["POST"])
def map_candidate_to_job_opening(request, pk):
    """
    Map a Candidate Pool candidate into a PUBLISHED job opening.

    POST-only: this creates an application, so it must not be reachable by a
    link, prefetch or crawler.

    Creates a NEW application and leaves any existing one untouched, so the same
    person can hold applications to several openings. Mapping the same candidate
    to the same opening twice is idempotent -- the second attempt reports that
    they are already mapped rather than creating a duplicate or a second audit
    event.
    """
    job_opening_id = request.POST.get("job_opening_id")
    if not job_opening_id:
        messages.error(request, _("Select a job opening to map this candidate into."))
        return HorillaRedirect(request)

    try:
        application, created = candidate_service.map_to_job_opening(
            request.user, pk, job_opening_id
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    if created:
        messages.success(
            request,
            _("Candidate mapped to %(opening)s.")
            % {"opening": application.recruitment_id},
        )
    else:
        messages.info(
            request,
            _("This candidate is already mapped to %(opening)s.")
            % {"opening": application.recruitment_id},
        )
    return HorillaRedirect(request)


@login_required
def candidate_notes_tab(request, pk):
    """
    Permanent internal notes for a candidate.

    GET renders the notes; POST posts a new one. A note cannot be edited or
    deleted once posted, so the template offers neither control.
    """
    error = None
    if request.method == "POST":
        try:
            candidate_service.add_note(
                request.user, pk, request.POST.get("description", "")
            )
            messages.success(request, _("Note added."))
        except RecruitmentError as exc:
            error = str(exc)
            messages.error(request, error)

    try:
        notes = candidate_service.notes_for_candidate(request.user, pk)
        candidate = candidate_service.get_candidate_for_user(request.user, pk)
    except RecruitmentError as exc:
        # Includes the cross-company case, which reports as not-found.
        messages.error(request, str(exc))
        return HorillaRedirect(request)

    return render(
        request,
        "cbv/candidate_pool/notes_tab.html",
        {"candidate": candidate, "notes": notes, "error": error},
    )


@login_required
def candidate_documents_tab(request, pk):
    """
    Candidate documents, from every recruitment flow, in one place.

    Visibility is not stage-specific: anyone authorized for this candidate sees
    all of their documents. Uploads are validated server-side (real PDF, 15 MB)
    and a rejected upload creates no document row and no audit event.
    """
    if request.method == "POST":
        try:
            candidate_service.upload_document(
                request.user,
                pk,
                request.FILES.get("document"),
                title=request.POST.get("title"),
            )
            messages.success(request, _("Document uploaded."))
        except RecruitmentError as exc:
            messages.error(request, str(exc))

    try:
        documents = candidate_service.documents_for_candidate(request.user, pk)
        candidate = candidate_service.get_candidate_for_user(request.user, pk)
    except RecruitmentError as exc:
        messages.error(request, str(exc))
        return HorillaRedirect(request)

    return render(
        request,
        "cbv/candidate_pool/documents_tab.html",
        {"candidate": candidate, "documents": documents},
    )


@login_required
def candidate_handoff_tab(request, pk):
    """
    PRD Form 2: the Final HR Round -> Hired handoff.

    The gate is candidate_status(), not a stage name, so the three
    representations of rejection stay reconciled in the one place that already
    does it. Being shown the tab is not authorization -- authority, company
    scope and status are all re-checked here, because the tab's accessibility
    callable only decides whether a link appears.

    Carried fields are read-only: name, email and mobile come from the
    application, designation and budget from the job opening. Only CTC and date
    of joining are entered.

    Submitting freezes the Form 2 questions (this is Form 2's snapshot
    boundary -- Form 1 freezes at publication), stores the answers against that
    snapshot, and hires the candidate, all in ONE transaction. A rejected answer
    set therefore cannot leave a hired candidate behind, and a failed hire
    cannot leave orphaned answers.
    """
    from recruitment.forms import HiringHandoffForm, SurveyForm
    from recruitment.models import FORM_TWO, RecruitmentAuditEvent
    from recruitment.services.audit import RecruitmentAuditService
    from recruitment.services.screening import build_snapshot, submit_answers

    try:
        candidate = candidate_service.get_candidate_for_user(
            request.user, pk, candidate_service.CHANGE_PERMISSION
        )
    except RecruitmentError as exc:
        # Includes the cross-company case, which reports as not-found.
        messages.error(request, str(exc))
        return HorillaRedirect(request)

    opening = candidate.recruitment_id
    status = candidate_service.candidate_status(candidate)
    can_hand_off = (
        opening is not None and status == candidate_service.STATUS_FINAL_HR_ROUND
    )
    form = HiringHandoffForm(instance=candidate)

    if request.method == "POST":
        if not can_hand_off:
            messages.error(
                request,
                _(
                    "This candidate is not in the Final HR Round, so the hiring "
                    "handoff does not apply to them."
                ),
            )
            return HorillaRedirect(request)

        form = HiringHandoffForm(request.POST, instance=candidate)
        if form.is_valid():
            try:
                with transaction.atomic():
                    build_snapshot(opening, actor=request.user, form_type=FORM_TWO)
                    handoff = form.save(commit=False)
                    # Only the columns this form owns: Candidate.save() derives
                    # `hired` from the stage, and hire_candidate() below is what
                    # moves the stage. Persisting more here would fight that.
                    # job_position_id is included because the designation is
                    # settled at the handoff -- leaving it out silently
                    # discarded the edit.
                    handoff.save(
                        update_fields=[
                            "job_position_id",
                            "offered_ctc",
                            "joining_date",
                            "handoff_budget_min",
                            "handoff_budget_max",
                        ]
                    )
                    submit_answers(
                        candidate,
                        request.POST,
                        request.FILES,
                        actor=request.user,
                        form_type=FORM_TWO,
                    )
                    # Reuses the existing service, which emits CANDIDATE_HIRED
                    # via move_to_stage. reject_candidate() is untouched.
                    candidate_service.hire_candidate(request.user, pk)
                    RecruitmentAuditService.record(
                        event_type=RecruitmentAuditEvent.EventType.HANDOFF_FORM_SUBMITTED,
                        actor=request.user,
                        company=candidate.company_id,
                        candidate=candidate,
                        job_opening=opening,
                        # Identifiers and flags. The offered amount is
                        # deliberately not copied into the audit trail.
                        details={
                            "candidate_id": candidate.pk,
                            "job_opening_id": opening.pk,
                            "has_offered_ctc": candidate.offered_ctc is not None,
                            "has_joining_date": candidate.joining_date is not None,
                        },
                    )
            except RecruitmentError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, _("Hiring handoff submitted."))
                return HorillaRedirect(request)

    return render(
        request,
        "cbv/candidate_pool/handoff_form.html",
        {
            "candidate": candidate,
            "job_opening": opening,
            "form": form,
            "can_hand_off": can_hand_off,
            "status": status,
            # Designation falls back to the opening's position, matching how
            # Candidate.save() fills it for a non-event-based opening.
            "designation": candidate.job_position_id
            or getattr(opening, "job_position_id", None),
            # Form 2's questions. Before the first handoff there is no snapshot
            # yet, so SurveyForm renders what the handoff will freeze.
            "survey": SurveyForm(
                recruitment=opening, embedded=True, form_type=FORM_TWO
            ).form
            if opening is not None
            else "",
        },
    )


@login_required
def candidate_pool_export(request):
    """
    Candidate Pool export (PRD): the six Pool columns, as an Excel file, for
    the rows the current search and filter select. Gated on export_candidate
    and audited without the rows themselves.
    """
    from io import BytesIO

    from django.http import HttpResponse
    from openpyxl import Workbook

    from recruitment.filters import CandidatePoolFilter

    try:
        candidate_service.assert_can_export(request.user)
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    allowed = candidate_service.accessible_candidates(request.user)
    queryset = (
        CandidatePoolFilter(request.GET, queryset=allowed)
        .qs.select_related("recruitment_id", "stage_id")
        .order_by("-id")
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Candidate Pool"
    sheet.append(
        [
            str(_("Number")),
            str(_("Candidate Name")),
            str(_("Job Applied To")),
            str(_("Date Applied")),
            str(_("Current Status")),
            str(_("Contact Verification")),
        ]
    )
    for candidate in queryset:
        applied = candidate.pool_date_applied()
        sheet.append(
            [
                candidate.pool_number(),
                candidate.get_full_name(),
                candidate.pool_job_applied_to(),
                applied.isoformat() if hasattr(applied, "isoformat") else applied,
                str(candidate.pool_status()),
                str(candidate.pool_contact_verification()),
            ]
        )

    candidate_service.record_export(
        request.user,
        queryset,
        export_format="xlsx",
        filters={k: v for k, v in request.GET.items() if v},
    )
    buffer = BytesIO()
    workbook.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="candidate_pool.xlsx"'
    return response
