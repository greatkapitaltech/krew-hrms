"""
surveys.py

This module is used to write views related to the survey features
"""

import json
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import ProtectedError
from django import forms
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from base.methods import closest_numbers
from horilla.decorators import (
    hx_request_required,
    is_recruitment_manager,
    login_required,
    permission_required,
)
from horilla.http import HorillaRedirect
from recruitment.filters import SurveyFilter
from recruitment.forms import (
    AddQuestionForm,
    ApplicationForm,
    QuestionForm,
    SurveyForm,
    SurveyPreviewForm,
    TemplateForm,
)
from recruitment.models import (
    ApplicationContactVerification,
    Candidate,
    JobPosition,
    Recruitment,
    RecruitmentAuditEvent,
    RecruitmentSurvey,
    RecruitmentSurveyAnswer,
    Resume,
    Stage,
    SurveyTemplate,
)
from recruitment.pipeline_grouper import group_by_queryset
from recruitment.views.paginator_qry import paginator_qry


@login_required
# recId identifies the job opening, so manager authority is scoped to THAT
# opening rather than to managing any recruitment at all.
@is_recruitment_manager(
    perm="recruitment.add_recruitmentsurvey", recruitment_param="recId"
)
def survey_form(request):
    """
    This method is used to render survey wform
    """
    recruitment_id = request.GET.get("recId")
    recruitment = Recruitment.find(recruitment_id)
    if not recruitment_id or not recruitment:
        message = (
            _("Missing Recruitment ID")
            if not recruitment_id
            else _("No Recruitment found matching the query.")
        )
        return HorillaRedirect(request, message=message)

    form = SurveyForm(recruitment=recruitment).form
    return render(request, "survey/form.html", {"form": form})


@login_required
@is_recruitment_manager(perm="recruitment.add_recruitmentsurvey")
def survey_preview(request, pk=None):
    """
    Used to render survey form to the candidate
    """
    title = request.GET.get("title")
    template = SurveyTemplate.objects.filter(title=title).first()
    if not title or not template:
        message = (
            _("Missing Survey Template Title")
            if not title
            else _("No Survey Template found matching the query.")
        )
        return HorillaRedirect(request, message=message)

    form = SurveyPreviewForm(template=template).form
    preview_template = "survey/survey_preview.html"
    if request.META.get("HTTP_HX_REQUEST") == "true":
        preview_template = "survey/survey_preview_container.html"
    return render(
        request,
        preview_template,
        {"form": form, "template": template},
    )


@login_required
def question_order_update(request):
    if request.method == "POST":
        # Extract data from the request
        question_id = request.POST.get("question_id")
        new_position = int(request.POST.get("new_position"))
        qs = RecruitmentSurvey.objects.get(id=question_id)

        if qs.sequence > new_position:
            new_position = new_position
        if qs.sequence <= new_position:
            new_position = new_position - 1

        old_qs = RecruitmentSurvey.objects.filter(sequence=new_position)
        for i in old_qs:

            i.sequence = new_position + 1
            i.save()
        qs.sequence = int(new_position)
        qs.save()
        return JsonResponse(
            {"success": True, "message": "Question order updated successfully"}
        )

    return JsonResponse({"error": "Invalid request method"}, status=405)


@login_required
@is_recruitment_manager(perm="recruitment.view_recruitmentsurvey")
def view_question_template(request):
    """
    This method is used to view the question template
    """
    recs = Recruitment.objects.all()
    ids = []
    for i in recs:
        for manager in i.recruitment_managers.all():
            if request.user.employee_get == manager:
                ids.append(i.id)
    if request.user.has_perm("recruitment.view_recruitmentsurvey"):
        questions = RecruitmentSurvey.objects.all()
    else:
        questions = RecruitmentSurvey.objects.filter(recruitment_ids__in=ids)
    # See the matching fix/comment in recruitment/views/search.py's
    # filter_survey() - group_by_queryset() already paginates correctly, but
    # the unused (0-question) templates it can't see get appended afterward
    # and the combined list gets paginated a second time on the same
    # "template_page" param, so page 2+ silently lost has_previous. Fetch
    # every templates-with-questions group here (unpaginated) and paginate
    # the merged list exactly once below instead.
    templates = group_by_queryset(
        questions.filter(template_id__isnull=False).distinct(),
        "template_id__title",
        page=1,
        page_name="template_page",
        records_per_page=1000000,
    )
    all_template_object_list = []
    for template in templates:
        all_template_object_list.append(template)

    survey_templates = SurveyTemplate.objects.all()
    all_templates = survey_templates.values_list("title", flat=True)
    used_templates = questions.values_list("template_id__title", flat=True)

    unused_templates = list(set(all_templates) - set(used_templates))
    unused_groups = []
    for template_name in unused_templates:
        unused_groups.append(
            {
                "grouper": template_name,
                "list": [],
                "dynamic_name": "",
            }
        )
    all_template_object_list = all_template_object_list + unused_groups
    # Application form vs Hiring handoff tabs.
    all_template_object_list = tag_template_groups(all_template_object_list)

    templates = paginator_qry(
        all_template_object_list, request.GET.get("template_page")
    )
    survey_templates = paginator_qry(
        survey_templates, request.GET.get("survey_template_page")
    )
    filter_obj = SurveyFilter()
    requests_ids = json.dumps(
        [
            instance.id
            for instance in paginator_qry(
                questions, request.GET.get("page")
            ).object_list
        ]
    )
    return render(
        request,
        "survey/view_question_templates.html",
        {
            "questions": paginator_qry(questions, request.GET.get("page")),
            "templates": templates,
            "survey_templates": survey_templates,
            "f": filter_obj,
            "requests_ids": requests_ids,
        },
    )


@login_required
@hx_request_required
@permission_required(perm="recruitment.change_recruitmentsurvey")
def update_question_template(request, survey_id):
    """
    This view method is used to update question template
    """
    instance = RecruitmentSurvey.objects.get(id=survey_id)
    form = QuestionForm(
        instance=instance,
    )
    if request.method == "POST":
        form = QuestionForm(request.POST, instance=instance)
        if form.is_valid():
            instance = form.save(commit=False)
            instance.save()
            instance.template_id.set(form.cleaned_data["template_id"])
            instance.recruitment_ids.set(form.recruitment)
            # instance.job_position_ids.set(form.job_positions)
            messages.success(request, _("New survey question updated."))
            return HorillaRedirect(request)
    return render(request, "survey/template_update_form.html", {"form": form})


@login_required
@hx_request_required
@permission_required(perm="recruitment.add_recruitmentsurvey")
def create_question_template(request):
    """
    This view method is used to create question template
    """
    form = QuestionForm()
    if request.method == "POST":
        form = QuestionForm(request.POST)
        if form.is_valid():
            instance = form.save(commit=False)
            instance.save()
            instance.recruitment_ids.set(form.recruitment)
            instance.template_id.set(form.cleaned_data["template_id"])
            # instance.job_position_ids.set(form.job_positions)
            messages.success(request, _("New survey question created."))
            return HorillaRedirect(request)
    return render(request, "survey/template_form.html", {"form": form})


@login_required
@permission_required(perm="recruitment.delete_recruitmentsurvey")
def delete_survey_question(request, survey_id):
    """
    This method is used to delete the survey instance
    """
    try:
        RecruitmentSurvey.objects.get(id=survey_id).delete()
        messages.success(request, _("Question was deleted successfully"))
    except RecruitmentSurvey.DoesNotExist:
        messages.error(request, _("Question not found."))
    except ProtectedError:
        messages.error(request, _("You cannot delete this question"))
    except ValidationError as error:
        # Used by a job opening (directly or through a template).
        messages.error(request, " ".join(error.messages))
    if request.META.get("HTTP_HX_REQUEST") == "true":
        from recruitment.views.search import filter_survey

        return filter_survey(request)
    return redirect(view_question_template)


#: Questions added on the CREATE form, before the job opening exists.
#:
#: A question is attached to an opening through RecruitmentSurvey
#: .recruitment_ids, so there is nothing to attach to until the opening is
#: saved. They are held here per form type and written in form_valid() by
#: attach_pending_questions(), so "pick a template, then add a few questions
#: for this opening" works in one pass on the create screen.
PENDING_QUESTIONS_KEY = "pending_job_opening_questions"


def _pending_questions(request):
    return request.session.get(PENDING_QUESTIONS_KEY) or {}


def _store_pending_questions(request, pending):
    request.session[PENDING_QUESTIONS_KEY] = pending
    request.session.modified = True


def clear_pending_questions(request):
    """Drop anything held from an earlier, abandoned create."""
    if PENDING_QUESTIONS_KEY in request.session:
        del request.session[PENDING_QUESTIONS_KEY]
        request.session.modified = True


def pending_questions_for_form(request):
    """
    Held questions keyed by the selector they belong under.

    Shaped for the template: the Job Opening form looks them up by field name,
    the same way it looks up the saved sections.
    """
    pending = _pending_questions(request)
    return {
        "form1_templates": pending.get("form1") or [],
        "form2_templates": pending.get("form2") or [],
    }


def seed_pending_from_opening(request, source):
    """
    Pre-load the held list with the questions written for `source` alone.

    Used by Duplicate: the copy's form shows the original's extra Form 1 and
    Form 2 questions in the same list as "+ Add Question", so HR can see,
    remove or add to them, and attach_pending_questions() saves whatever is
    left. Questions that reach the original through its templates are not
    included -- the duplicate gets those from the same template selection.
    """
    from recruitment.models import RecruitmentSurvey, SurveyTemplateQuestion

    through_template = set(
        SurveyTemplateQuestion.objects.filter(
            surveytemplate__in=source.survey_templates.all()
        ).values_list("recruitmentsurvey_id", flat=True)
    )
    pending = {}
    for question in (
        RecruitmentSurvey.objects.entire()
        .filter(recruitment_ids=source)
        .order_by("sequence", "id")
    ):
        if question.pk in through_template:
            continue
        pending.setdefault(question.form_type, []).append(
            {
                "question": question.question,
                "type": question.type,
                "options": question.options or "",
                "type_label": str(
                    QuestionForm.TYPE_LABELS.get(question.type, question.type)
                ),
                "is_mandatory": bool(question.is_mandatory),
                "sequence": question.sequence or 0,
                "max_files": max(1, getattr(question, "max_files", 1) or 1),
            }
        )
    _store_pending_questions(request, pending)


def attach_pending_questions(request, job_opening):
    """
    Turn the held questions into real ones on a freshly created opening.

    Each becomes a RecruitmentSurvey attached to this opening alone --
    `template_id` is left empty, so the reusable template is untouched, exactly
    as adding a question to a saved opening behaves.
    """
    from recruitment.models import RecruitmentSurvey
    from recruitment.services.screening import sequence_for_position

    pending = _pending_questions(request)
    created = []
    for form_type, entries in pending.items():
        for entry in entries:
            question = RecruitmentSurvey.objects.create(
                question=entry.get("question", ""),
                type=entry.get("type", "text"),
                options=entry.get("options", ""),
                is_mandatory=bool(entry.get("is_mandatory")),
                # Appended after the template's questions; HR can move it
                # later from the opening's Edit screen.
                sequence=sequence_for_position(job_opening, form_type, "end"),
                form_type=form_type,
                # allow_multiple_files is derived from this by save().
                max_files=max(1, entry.get("max_files") or 1),
            )
            question.recruitment_ids.add(job_opening)
            created.append(question)
    clear_pending_questions(request)
    return created


#: Each form's section on the Job Opening form carries its own wrapper id, so
#: adding a question refreshes only that selector's list. The suffix is the
#: form field the section sits under (see field_extras on
#: RecruitmentCreationFormExtended).
FORM_TYPE_WRAPPER_IDS = {
    "form1": "jobOpeningQuestions-form1_templates",
    "form2": "jobOpeningQuestions-form2_templates",
}


@login_required
def job_opening_questions(request, rec_id):
    """
    The resolved Form 1 and Form 2 question sets for one job opening.

    A read-only fragment, loaded into the Job Opening form and refreshed after
    a question is added. Authorization is the same object-scoped check the rest
    of the job-opening screens use, so another tenant's opening reads as
    not-found rather than forbidden.
    """
    from recruitment.services.authorization import get_job_opening_for_user
    from recruitment.services.errors import RecruitmentError
    from recruitment.services.screening import question_sections

    try:
        opening = get_job_opening_for_user(
            request.user, rec_id, "recruitment.change_recruitment"
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    sections = question_sections(opening)
    can_add = opening.status != Recruitment.Status.PUBLISHED
    # ?form_type= is the Job Opening form refreshing one selector's block after
    # a question is added: it shows only this opening's own questions.
    wanted = request.GET.get("form_type")
    if wanted:
        sections = [s for s in sections if s["form_type"] == wanted]
        return render(
            request,
            "cbv/recruitment/job_opening_extra_questions.html",
            {
                "job_opening": opening,
                "form_type": wanted,
                "wrapper_id": FORM_TYPE_WRAPPER_IDS.get(wanted, "jobOpeningQuestions"),
                "extras": [
                    q
                    for section in sections
                    for q in section["questions"]
                    if q["source"] == "job_opening"
                ],
                "can_add": can_add,
            },
        )

    return render(
        request,
        "cbv/recruitment/job_opening_questions.html",
        {
            "job_opening": opening,
            "sections": sections,
            "wrapper_id": FORM_TYPE_WRAPPER_IDS.get(wanted, "jobOpeningQuestions"),
            # Once published the set is frozen, so adding is withdrawn here as
            # well as refused in the add view itself.
            "can_add": opening.status != Recruitment.Status.PUBLISHED,
        },
    )


def _valid_form_type(value):
    from recruitment.models import FORM_ONE, FORM_TYPES

    return value if value in {v for v, _label in FORM_TYPES} else FORM_ONE


@login_required
@hx_request_required
@permission_required(perm="recruitment.add_recruitment")
def job_opening_pending_questions(request):
    """One form's held questions, for the create screen."""
    form_type = _valid_form_type(request.GET.get("form_type"))
    return render(
        request,
        "cbv/recruitment/job_opening_pending_questions.html",
        {
            "form_type": form_type,
            "pending": _pending_questions(request).get(form_type) or [],
        },
    )


@login_required
@hx_request_required
@permission_required(perm="recruitment.add_recruitment")
def job_opening_pending_question_add(request):
    """
    Add a question on the create screen, before the opening is saved.

    Held in the session and written by attach_pending_questions() when the
    opening is created. Nothing is stored in the question bank until then, so
    abandoning the form leaves nothing behind.
    """
    from recruitment.forms import QuestionForm

    form_type = _valid_form_type(
        request.GET.get("form_type") or request.POST.get("form_type")
    )

    def _prepare(form):
        # sequence is not asked: ordering is configured per template
        # (Configure Questions), not when writing a question.
        for hidden in ("form_type", "template_id", "recruitment", "sequence"):
            if hidden in form.fields:
                form.fields[hidden].widget = forms.HiddenInput()
                form.fields[hidden].required = False
        return form

    if request.method == "POST":
        form = _prepare(QuestionForm(request.POST))
        if form.is_valid():
            pending = _pending_questions(request)
            entries = list(pending.get(form_type) or [])
            answer_type = form.cleaned_data.get("type", "text")
            entries.append(
                {
                    "question": form.cleaned_data.get("question", ""),
                    "type": answer_type,
                    # Only the choice formats carry options, matching
                    # QuestionForm.save(); keeping them on a number or date
                    # question would store input the question never uses.
                    "options": (
                        form.cleaned_data.get("options") or ""
                        if answer_type in ("options", "multiple")
                        else ""
                    ),
                    # Stored alongside the value so the held list can show the
                    # same wording as the dropdown rather than the raw value.
                    "type_label": str(
                        QuestionForm.TYPE_LABELS.get(answer_type, answer_type)
                    ),
                    "is_mandatory": bool(form.cleaned_data.get("is_mandatory")),
                    "sequence": form.cleaned_data.get("sequence") or 0,
                    "max_files": max(1, form.cleaned_data.get("max_files") or 1),
                }
            )
            pending[form_type] = entries
            _store_pending_questions(request, pending)
            return HttpResponse(
                "<script>"
                "$('#objectCreateModal').removeClass('oh-modal--show');"
                "htmx.ajax('GET', '%s?form_type=%s', {target: '#pendingQuestions-%s'});"
                "</script>"
                % (reverse("job-opening-pending-questions"), form_type, form_type)
            )
    else:
        form = _prepare(QuestionForm(initial={"form_type": form_type}))

    return render(
        request,
        "cbv/recruitment/job_opening_question_form.html",
        {
            "form": form,
            "job_opening": None,
            "form_type": form_type,
            "post_url": "%s?form_type=%s"
            % (reverse("job-opening-pending-question-add"), form_type),
        },
    )


@login_required
@hx_request_required
@permission_required(perm="recruitment.add_recruitment")
def job_opening_pending_question_remove(request):
    """Drop one held question before the opening is saved."""
    form_type = _valid_form_type(request.GET.get("form_type"))
    pending = _pending_questions(request)
    entries = list(pending.get(form_type) or [])
    try:
        index = int(request.GET.get("index", ""))
    except (TypeError, ValueError):
        index = -1
    if 0 <= index < len(entries):
        entries.pop(index)
        pending[form_type] = entries
        _store_pending_questions(request, pending)
    return render(
        request,
        "cbv/recruitment/job_opening_pending_questions.html",
        {"form_type": form_type, "pending": entries},
    )


@login_required
@hx_request_required
def job_opening_question_add(request, rec_id):
    """
    Add a screening question to THIS job opening only.

    The question is attached through RecruitmentSurvey.recruitment_ids, which
    publication already honours as the additive path alongside templates. The
    reusable template is never touched -- `template_id` is left alone -- so the
    same template used by another opening is unaffected.

    `form_type` comes from the URL and is forced onto the instance, so a Form 2
    question cannot be added into the Form 1 set (or the reverse) by posting a
    different value.

    Refused once the opening is PUBLISHED: its question snapshot is frozen, and
    a question added afterwards would be silently ignored.
    """
    from recruitment.models import FORM_ONE, FORM_TYPES
    from recruitment.services.authorization import get_job_opening_for_user
    from recruitment.services.errors import RecruitmentError

    try:
        opening = get_job_opening_for_user(
            request.user, rec_id, "recruitment.change_recruitment"
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    valid_form_types = {value for value, _label in FORM_TYPES}
    form_type = request.GET.get("form_type") or request.POST.get("form_type")
    if form_type not in valid_form_types:
        form_type = FORM_ONE

    if opening.status == Recruitment.Status.PUBLISHED:
        messages.error(
            request,
            _(
                "This job opening is published, so its question set is frozen "
                "and cannot be added to."
            ),
        )
        return HorillaRedirect(request)

    def _prepare(form):
        # The opening and the form are decided by the URL, not by the user, so
        # they are not offered as choices. template_id is hidden because adding
        # a question here must never modify a shared template.
        # sequence is not asked: ordering is configured per template
        # (Configure Questions), not when writing a question.
        for hidden in ("form_type", "template_id", "recruitment", "sequence"):
            if hidden in form.fields:
                form.fields[hidden].widget = forms.HiddenInput()
                form.fields[hidden].required = False
        form.fields["form_type"].initial = form_type
        from recruitment.services.screening import position_choices

        form.fields["position"] = forms.ChoiceField(
            choices=position_choices(opening, form_type),
            initial="end",
            required=False,
            label=_("Position"),
        )
        return form

    if request.method == "POST":
        # Prepared BEFORE validation: _prepare() is what makes form_type,
        # template_id and recruitment optional, and they are not rendered for
        # the user to fill in. Validating first would fail on form_type, which
        # is a required model field this screen decides from the URL.
        form = _prepare(QuestionForm(request.POST))
        if form.is_valid():
            question = form.save(commit=False)
            # Forced, never taken from the posted body.
            question.form_type = form_type
            # Placed where HR chose (PRD: anywhere in the order); end by default.
            from recruitment.services.screening import sequence_for_position

            question.sequence = sequence_for_position(
                opening, form_type, form.cleaned_data.get("position") or "end"
            )
            question.save()
            # add(), not set(): additive, and it leaves any other opening's
            # attachment to this question intact. save_m2m() is deliberately
            # NOT called, so template_id stays empty.
            question.recruitment_ids.add(opening)
            messages.success(request, _("Question added to this job opening."))
            # Close the secondary modal and refresh just the question list, so
            # the Job Opening form behind it is left as the user had it.
            # Refresh only this form's list, so a Form 1 addition leaves the
            # Form 2 section (and the rest of the open form) untouched.
            list_url = "%s?form_type=%s" % (
                reverse("job-opening-questions", kwargs={"rec_id": opening.pk}),
                form_type,
            )
            return HttpResponse(
                "<script>"
                "$('#objectCreateModal').removeClass('oh-modal--show');"
                "htmx.ajax('GET', '%s', {target: '#%s', swap: 'outerHTML'});"
                "</script>"
                % (list_url, FORM_TYPE_WRAPPER_IDS.get(form_type, "jobOpeningQuestions"))
            )
    else:
        form = _prepare(QuestionForm(initial={"form_type": form_type}))

    return render(
        request,
        "cbv/recruitment/job_opening_question_form.html",
        {
            "form": form,
            "job_opening": opening,
            "form_type": form_type,
            "post_url": "%s?form_type=%s"
            % (
                reverse("job-opening-question-add", kwargs={"rec_id": opening.pk}),
                form_type,
            ),
        },
    )


#: Where a public applicant's in-progress verification attempt is remembered.
#: The primary key lives in the session, never in a form field or URL, so one
#: applicant cannot address another applicant's attempt.
VERIFICATION_SESSION_KEY = "application_verification_id"


def _published_opening(value):
    """
    A PUBLISHED job opening by id, or None.

    Only a PUBLISHED opening accepts applications. A CLOSED one stays listed
    publicly but must reject submissions, so the gate is the authoritative
    ``status`` rather than the ``is_published`` mirror. A malformed id answers
    None rather than raising, because the id comes from the public internet.
    """
    try:
        return Recruitment.objects.filter(
            id=value, status=Recruitment.Status.PUBLISHED
        ).first()
    except (ValueError, TypeError, OverflowError):
        return None


def _application_url(job_opening):
    """The public application page for one opening (its career-page address)."""
    # return f"{reverse('application-form')}?recruitmentId={job_opening.pk}"
    from recruitment.services.job_opening import career_page_slug

    if job_opening.company_id is None:
        return f"{reverse('application-form')}?recruitmentId={job_opening.pk}"
    return reverse(
        "career-apply", args=[career_page_slug(job_opening.company_id), job_opening.pk]
    )


def _applied_stage(job_opening):
    """
    The stage a brand-new application lands in.

    Delegates to the service so the public form and the internal Add Candidate
    screens cannot drift apart on where a new candidate enters the pipeline.
    """
    from recruitment.services.candidate import entry_stage

    return entry_stage(job_opening)


def _restrict_to_opening(form, job_opening):
    """
    Narrow an application form's object choices to THIS job opening.

    Applied BEFORE validation, and derived from the server-resolved opening
    rather than from anything posted.

    Without it the form accepted any JobPosition in the database, and
    Candidate.save() then raised a Django ValidationError ("Choose valid
    choice") that this view does not catch -- a 500, on a public page, from a
    client-supplied id.

    Narrowing recruitment_id the same way also makes the form's
    ("email", "recruitment_id") uniqueness check run against the opening the
    application is actually for. Checked against a posted id instead, a repeat
    applicant could slip past it and reach a database IntegrityError.
    """
    form.fields["recruitment_id"].queryset = Recruitment.objects.filter(
        pk=job_opening.pk
    )
    form.fields["job_position_id"].queryset = job_opening.open_positions.all()


def _session_verification(request, job_opening):
    """
    The verification attempt this browser started for this opening, if any.

    Looked up by the session's primary key AND the resolved opening, so a
    verification proven for one opening cannot surface on another's page.
    """
    pk = request.session.get(VERIFICATION_SESSION_KEY)
    if not pk:
        return None
    return ApplicationContactVerification.objects.filter(
        pk=pk, job_opening=job_opening
    ).first()


def application_form(request):
    """
    The public application page (PRD Form 1).

    One page, one submit: the applicant's details, the frozen Form 1 screening
    questions and -- when the opening asks for it -- contact verification all
    live here, and the whole submission is written in a single transaction so a
    missing mandatory answer cannot leave a half-created candidate behind.

    Public by design: the applicant has no account. Consequently nothing here
    trusts client-supplied scope. The opening is resolved server-side from the
    query string; the form also posts a ``recruitment_id``, but that value
    never decides which opening (or which company) the application belongs to,
    because a client can change it. The resolved opening wins, and the
    candidate's company is derived from it.
    """
    recruitment_id = request.GET.get("recruitmentId")
    resume_id = request.GET.get("resumeId")
    resume_obj = Resume.objects.filter(id=resume_id).first()

    if request.method == "GET" and not recruitment_id:
        messages.error(request, _("Recruitment ID is missing"))
        return redirect("open-recruitments")

    recruitment = _published_opening(recruitment_id)
    if not recruitment:
        messages.error(request, _("Recruitment not found"))
        return redirect("open-recruitments")

    # A company-less opening would produce a candidate with no company, and the
    # company manager passes NULL rows through to every tenant -- so refuse to
    # create one rather than leak the applicant across companies.
    if recruitment.company_id_id is None:
        messages.error(request, _("This job opening is not available."))
        return redirect("open-recruitments")

    # The emailed link for a candidate HR added by hand (PRD Form 3): the form
    # opens pre-filled and completes THAT candidate instead of creating one.
    from recruitment.services.candidate_mail import invited_candidate

    invite = invited_candidate(request.GET.get("invite"), recruitment)

    # Visitors reach this through their company's career page
    # (/careers/<slug>/apply/<id>/) or a valid emailed invite link. A bare
    # ?recruitmentId= (or a made-up invite) is refused for them, so changing
    # the number cannot open another company's application form.
    if (
        not request.user.is_authenticated
        and not getattr(request, "krew_career_apply", False)
        and invite is None
    ):
        from django.http import Http404

        raise Http404("No job opening found.")

    if request.POST:
        if "resume" not in request.FILES and resume_id:
            if resume_obj and resume_obj.file:
                file_content = resume_obj.file.read()
                pdf_file = SimpleUploadedFile(
                    resume_obj.file.name, file_content, content_type="application/pdf"
                )
                request.FILES["resume"] = pdf_file

        form = ApplicationForm(request.POST, request.FILES, instance=invite)
        _restrict_to_opening(form, recruitment)
        if form.is_valid():
            from recruitment.services.audit import RecruitmentAuditService
            from recruitment.services.candidate import create_candidate
            from recruitment.services.errors import RecruitmentError
            from recruitment.services.screening import submit_answers
            from recruitment.services.verification import stamp_candidate

            # recruitment_id is dropped deliberately (the resolved opening is
            # authoritative, see the docstring); `load` is a widget helper, not
            # a model field.
            fields = {
                name: value
                for name, value in form.cleaned_data.items()
                if name not in ("recruitment_id", "load")
            }
            try:
                with transaction.atomic():
                    # Always a NEW application. Candidate.Meta.unique_together
                    # ("email", "recruitment_id") already refuses a second
                    # application to the same opening, and the form reports
                    # that to the applicant.
                    #
                    # Deliberately NOT "look up an existing candidate by email
                    # and reuse it": that would turn this public endpoint into
                    # a write primitive over an application that already
                    # exists. Anyone knowing an applicant's email address could
                    # rewrite their name, phone, resume and answers by posting
                    # the address in different letter case, which slips past
                    # the case-sensitive unique constraint.
                    #
                    # No actor: an anonymous applicant is not a user, and
                    # passing AnonymousUser here would fail the audit event's
                    # user FK.
                    if invite is not None:
                        # Completing an invited candidate: their details are
                        # updated in place and they keep the stage HR put
                        # them in. The signed token, not the email address,
                        # is what identifies them.
                        candidate = form.save()
                    else:
                        candidate = create_candidate(
                            None,
                            job_opening=recruitment,
                            stage_id=_applied_stage(recruitment),
                            **fields,
                        )
                    submit_answers(candidate, request.POST, request.FILES)
                    stamp_candidate(candidate)
                    RecruitmentAuditService.record(
                        event_type=RecruitmentAuditEvent.EventType.APPLICATION_SUBMITTED,
                        company=recruitment.company_id,
                        job_opening=recruitment,
                        candidate=candidate,
                        # Identifiers and flags only -- never answers, contact
                        # details, tokens or codes.
                        details={
                            "candidate_id": candidate.pk,
                            "contact_verified": candidate.contact_verified_at
                            is not None,
                        },
                    )
            except RecruitmentError as error:
                messages.error(request, str(error))
            else:
                request.session.pop(VERIFICATION_SESSION_KEY, None)
                if resume_obj:
                    resume_obj.is_candidate = True
                    resume_obj.save()
                messages.success(request, _("Application saved."))
                return render(request, "candidate/success.html")
        for field_name, field_errors in form.errors.items():
            if field_name == "__all__":
                for error in field_errors:
                    messages.error(request, error)
            else:
                field_label = (
                    form.fields.get(field_name).label
                    if form.fields.get(field_name)
                    else field_name
                )
                for error in field_errors:
                    messages.error(
                        request,
                        _("%(field_label)s: %(error)s")
                        % {"field_label": field_label, "error": error},
                    )
    else:
        # 811
        initial_data = {"resume": resume_obj.file.url} if resume_obj else {}
        form = ApplicationForm(initial=initial_data, instance=invite)
        _restrict_to_opening(form, recruitment)

    verification = _session_verification(request, recruitment)
    return render(
        request,
        "candidate/application_form.html",
        {
            "form": form,
            "recruitment": recruitment,
            "resume": resume_obj,
            # The frozen Form 1 questions, rendered without their own submit
            # button so this page keeps a single submit.
            "survey": SurveyForm(recruitment=recruitment, embedded=True).form,
            "verification_required": recruitment.contact_verification_required,
            "verification": verification,
            "verification_state": _verification_state(
                verification
                if verification is not None and verification.is_usable()
                else None
            ),
        },
    )


def _verification_state(verification):
    """What the application page needs to draw the Verify buttons."""
    if verification is None:
        return {"email": "", "email_verified": False, "mobile": "",
                "otp_sent": False, "mobile_verified": False, "resend_in": 0}
    return {
        "email": verification.email,
        "email_verified": bool(verification.email_verified_at),
        "mobile": verification.mobile,
        "otp_sent": bool(verification.otp_sent_at),
        "mobile_verified": bool(verification.mobile_verified_at),
        # Seconds until "Resend code" is available (every 30 seconds).
        "resend_in": verification.resend_wait_seconds(),
    }


def _verification_reply(verification, message, ok=True, status=200):
    return JsonResponse(
        {"ok": ok, "message": str(message), **_verification_state(verification)},
        status=status,
    )


def application_start_verification(request):
    """
    Email "Verify" button: send the verification link.

    Public: the applicant has no account. The opening is resolved server-side
    from the query string, never from a posted id. Verification never blocks
    submission (PRD): an unverified application is accepted and shows as
    Unverified in the Candidate Pool. Answers JSON; the page stays put so
    nothing typed is lost.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from recruitment.services.errors import RecruitmentError
    from recruitment.services.verification import start_verification

    recruitment = _published_opening(request.GET.get("recruitmentId"))
    if recruitment is None:
        return _verification_reply(None, _("Recruitment not found"), False, 404)

    try:
        verification = start_verification(
            request,
            recruitment,
            request.POST.get("email"),
            request.POST.get("mobile"),
        )
    except RecruitmentError as error:
        return _verification_reply(None, error, False, 400)

    request.session[VERIFICATION_SESSION_KEY] = verification.pk
    return _verification_reply(
        verification,
        _("We sent a verification link to your email. It expires in 10 minutes."),
    )


def application_verify_email(request, token):
    """
    Consume an emailed verification link.

    Usually opened in a new tab: the application tab notices through
    application_verification_status, so this page only confirms. The token is
    single-use and never echoed back; an unknown, used or lapsed token gives the
    same answer as a wrong one so the endpoint cannot be probed.
    """
    from recruitment.services.errors import ContactVerificationError
    from recruitment.services.verification import verify_email

    try:
        verification = verify_email(token)
    except ContactVerificationError as error:
        messages.error(request, str(error))
        return redirect("open-recruitments")

    request.session[VERIFICATION_SESSION_KEY] = verification.pk
    return render(
        request,
        "candidate/email_verified.html",
        {
            "application_url": _application_url(verification.job_opening),
            # False when the SMS could not be sent (gateway error / no provider).
            "sms_sent": bool(verification.otp_sent_at),
        },
    )


def application_verification_status(request):
    """Polled by the application page while it waits for the email link."""
    recruitment = _published_opening(request.GET.get("recruitmentId"))
    verification = (
        _session_verification(request, recruitment) if recruitment else None
    )
    if verification is not None and not verification.is_usable():
        verification = None
    return JsonResponse(_verification_state(verification))


def application_send_otp(request):
    """"Resend code": a fresh OTP to the number given when verification started."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from recruitment.services.errors import ContactVerificationError
    from recruitment.services.verification import send_otp

    pk = request.session.get(VERIFICATION_SESSION_KEY)
    if not pk:
        return _verification_reply(
            None, _("Verify your email address first."), False, 400
        )
    try:
        verification, new_code = send_otp(pk)
    except ContactVerificationError as error:
        verification = ApplicationContactVerification.objects.filter(pk=pk).first()
        return _verification_reply(verification, error, False, 400)
    if new_code:
        message = _("We sent a code to your mobile number.")
    else:
        message = _("We sent your code again. It is the same code as before.")
    return _verification_reply(verification, message)


def application_verify_otp(request):
    """
    Confirm the mobile OTP for the attempt held in this session.

    The attempt is taken from the session, so the posted body carries only the
    code -- there is no verification id to tamper with.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from recruitment.services.errors import ContactVerificationError
    from recruitment.services.verification import verify_otp

    pk = request.session.get(VERIFICATION_SESSION_KEY)
    if not pk:
        return _verification_reply(
            None, _("Start verification again to receive a new code."), False, 400
        )

    try:
        verification = verify_otp(pk, request.POST.get("otp"))
    except ContactVerificationError as error:
        verification = ApplicationContactVerification.objects.filter(pk=pk).first()
        # Wrong code / expired code / too many tries each get their own message.
        return _verification_reply(verification, error, False, 400)
    return _verification_reply(verification, _("Mobile number verified."))


@login_required
@hx_request_required
@is_recruitment_manager(perm="recruitment.view_recruitmentsurvey")
def single_survey(request, survey_id):
    """
    This view method is used to single view of question template
    """
    question = RecruitmentSurvey.objects.get(id=survey_id)
    requests_ids_json = request.GET.get("instances_ids")
    context = {"question": question}
    if requests_ids_json:
        requests_ids = json.loads(requests_ids_json)
        previous_id, next_id = closest_numbers(requests_ids, survey_id)
        context["previous"] = previous_id
        context["next"] = next_id
        context["requests_ids"] = requests_ids_json
    return render(request, "survey/view_single_template.html", context)


@login_required
@hx_request_required
def create_template(request):
    """
    Create question template views
    """
    # Check if the user has any of the two permissions
    if not (
        request.user.has_perm("recruitment.add_surveytemplate")
        or request.user.has_perm("recruitment.change_surveytemplate")
    ):
        messages.info(request, _("You dont have permission."))
        return HorillaRedirect(request)

    title = request.GET.get("title")
    instance = None
    if title:
        instance = SurveyTemplate.objects.filter(title=title).first()
    form = TemplateForm(instance=instance)
    if request.method == "POST":
        form = TemplateForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, _("Template saved"))
            return HorillaRedirect(request)
    return render(request, "survey/main_form.html", {"form": form})


@login_required
@permission_required("recruitment.delete_surveytemplate")
def delete_template(request):
    """
    This method is used to delete the survey template group
    """
    title = request.GET.get("title")
    # SurveyTemplate.objects.filter(title=str(title)).delete()
    # if title == "None":
    #     messages.info(request, _("This template group cannot be deleted"))
    # else:
    #     messages.success(request, _("Template group deleted"))
    #
    # Company-scoped lookup, an honest result, and the same rule as bank
    # questions: a template any job opening uses can't be deleted (it would
    # silently change those openings' questions).
    template = SurveyTemplate.objects.filter(title=str(title)).first()
    if template is None or title == "None":
        messages.info(request, _("This template group cannot be deleted"))
    else:
        openings = list(
            Recruitment._base_manager.filter(survey_templates=template)
            .order_by("title")
            .values_list("title", flat=True)
            .distinct()[:4]
        )
        if openings:
            shown = ", ".join(openings[:3]) + (" and others" if len(openings) > 3 else "")
            messages.error(
                request,
                _(
                    "This template can't be deleted: it is used by job opening(s) "
                    "%(openings)s. Remove it from those openings first."
                )
                % {"openings": shown},
            )
        else:
            template.delete()
            messages.success(request, _("Template deleted."))

    if request.META.get("HTTP_HX_REQUEST") == "true":
        return HttpResponse("<script>$('#filterSubmit').click();</script>")
    return HorillaRedirect(request)


@login_required
@hx_request_required
@permission_required("recruitment.change_surveytemplate")
def question_add(request):
    """
    This method is used to add survey question to the templates
    """
    template = None
    title = request.GET.get("title")
    if title:
        template = SurveyTemplate.objects.filter(title=title).first

    form = AddQuestionForm(initial={"template_ids": template})
    if request.method == "POST":
        form = AddQuestionForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, _("Question added"))
            if request.META.get("HTTP_HX_REQUEST") == "true":
                return HttpResponse(
                    "<script>"
                    "$('#templateModal').removeClass('oh-modal--show');"
                    "$('#genericModal').removeClass('oh-modal--show');"
                    "$('#filterSubmit').click();"
                    "</script>"
                )
            return HorillaRedirect(request)
    return render(request, "survey/add_form.html", {"form": form})

def tag_template_groups(groups):
    """
    Mark each template group with its "Used for" (form1 = Application form,
    form2 = Hiring handoff), so the Templates tab can show them separately.
    Application templates first, then handoff, each in title order.
    """
    from recruitment.models import SurveyTemplate

    types = dict(SurveyTemplate.objects.values_list("title", "form_type"))
    tagged = []
    for group in groups:
        group = dict(group)
        group["form_type"] = types.get(group.get("grouper"), "form1")
        tagged.append(group)
    return sorted(tagged, key=lambda g: (g["form_type"], str(g.get("grouper") or "")))

