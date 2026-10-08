"""
recruitment
"""

from typing import Any

from django import forms
from django.contrib import messages
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import (
    HorillaDetailedView,
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)
from recruitment.filters import RecruitmentFilter
from recruitment.forms import AddCandidateForm, RecruitmentCreationForm, SkillsForm
from recruitment.models import Candidate, Recruitment, Skill
from recruitment.services.authorization import (
    company_in_scope,
    resolve_company_for_new_job_opening,
    selectable_companies_for_user,
)
from recruitment.services.job_opening import (
    publication_blockers,
    record_created,
    record_updated,
)
from recruitment.views.linkedin import delete_post, post_recruitment_in_linkedin


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.view_recruitment"), name="dispatch"
)
class RecruitmentView(TemplateView):
    """
    Recuitment page
    """

    template_name = "cbv/recruitment/recruitment.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.view_recruitment"), name="dispatch"
)
class RecruitmentList(HorillaListView):
    """
    List view of recruitment
    """

    model = Recruitment
    filter_class = RecruitmentFilter
    view_id = "rec-view-container"

    # `closed` deliberately excluded: the generic bulk update writes via
    # Model.objects.bulk_update() (horilla_views/generic/cbv/views.py:1193),
    # which bypasses save() entirely -- so exposing any lifecycle field here
    # would let a user change state with no permission check, no transition
    # validation and no audit event. Lifecycle changes go through
    # recruitment.services.job_opening only.
    bulk_update_fields = ["vacancy", "start_date", "end_date"]

    template_name = "cbv/recruitment/rec_main.html"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("list-recruitment")

    def get_queryset(self, queryset=None, filtered=False, *args, **kwargs):
        self.queryset = (
            super()
            .get_queryset(queryset, filtered, *args, **kwargs)
            .filter(is_active=self.request.GET.get("is_active", True))
        )
        return self.queryset

    columns = [
        (_("Job Opening"), "recruitment_column"),
        (_("Managers"), "managers_column"),
        (_("Open Positions"), "open_job_col"),
        (_("Vacancies"), "vacancy"),
        (_("Total Hires"), "tot_hires"),
        (_("Start Date"), "start_date"),
        (_("End Date"), "end_date"),
        (_("Status"), "status_col"),
    ]
    action_method = "rec_actions"

    header_attrs = {
        "recruitment_column": 'style="width : 200px !important"',
        "action": 'style="width : 180px !important"',
    }

    row_status_indications = [
        (
            "closed--dot",
            _("Closed"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=closed]').val('true');
                $('#applyFilter').click();

            "
            """,
        ),
        (
            "open--dot",
            _("Open"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=closed]').val('false');
                $('#applyFilter').click();

            "
            """,
        ),
    ]

    row_status_class = "closed-{closed}"

    sortby_mapping = [
        (_("Job Opening"), "recruitment_column"),
        (_("Vacancies"), "vacancy"),
        (_("Start Date"), "start_date"),
        (_("End Date"), "end_date"),
    ]

    row_attrs = """
                class="oh-permission-table--collapsed"
                hx-get='{recruitment_detail_view}?instance_ids={ordered_ids}'
                hx-target="#genericModalBody"
                data-target="#genericModal"
                data-toggle="oh-modal-toggle"
                """


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.view_recruitment"), name="dispatch"
)
class RecruitmentNav(HorillaNavView):
    """
    For nav bar
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("list-recruitment")

        self.create_attrs = f"""
                            hx-get='{reverse_lazy('recruitment-create')}'
                            hx-target="#genericModalBody"
                            data-target="#genericModal"
                            data-toggle="oh-modal-toggle"
                            """
        # Career Page: job link, embed code and allowed career sites.
        self.actions = []
        if self.request.user.has_perm("recruitment.change_recruitment"):
            self.actions.append(
                {
                    "action": _("Career Page"),
                    "attrs": f"""
                        hx-get="{reverse('job-opening-career-page')}"
                        hx-target="#genericModalBody"
                        data-target="#genericModal"
                        data-toggle="oh-modal-toggle"
                        style="cursor: pointer;"
                    """,
                }
            )

    nav_title = _("Job Openings")
    filter_instance = RecruitmentFilter()
    filter_form_context_name = "form"
    search_swap_target = "#listContainer"
    filter_body_template = "cbv/recruitment/filters.html"


class RecruitmentCreationFormExtended(RecruitmentCreationForm):
    """
    extended form view for create
    """

    #: One column, labels above inputs (PRD). The |col filter defaults to 6 --
    #: half width -- so every field has to be named here or it silently renders
    #: two-up as a grid. Budget is the one deliberate pair: a min and a max
    #: input side by side read as one range.
    cols = {
        "title": 12,
        "description": 12,
        "open_positions": 12,
        "recruitment_managers": 12,
        "start_date": 12,
        "end_date": 12,
        "vacancy": 12,
        "budget_min": 6,
        "budget_max": 6,
        "form1_templates": 12,
        "form2_templates": 12,
        "contact_verification_required": 12,
        "company_id": 12,
    }

    class Meta:
        """
        Meta class to add the additional info
        """

        model = Recruitment
        # Four fields are absent deliberately. A child Meta replaces the
        # parent's, so RecruitmentCreationForm.Meta.exclude does NOT reach
        # this form -- anything unwanted has to be left out of `fields` here.
        #
        #   is_published  -> a mirror of `status`. Publishing is an explicit
        #                    lifecycle transition, never a checkbox.
        #   company_id    -> derived from the creator's authorized tenant in
        #                    form_valid(); the PRD says it is never shown.
        #   optional_profile_image / optional_resume
        #                 -> excluded from MVP by the PRD; Resume stays
        #                    mandatory on the public application form.
        fields = [
            "title",
            "description",
            "open_positions",
            "recruitment_managers",
            "start_date",
            "end_date",
            "vacancy",
            "budget_min",
            "budget_max",
            "contact_verification_required",
            # survey_templates is NOT a plain field here: it is offered as two
            # separate selectors (Form 1 / Form 2), added in __init__ below and
            # written back by apply_template_selection(). Two selectors over the
            # one existing M2M, so no schema change.
            #
            # skills is gone -- the PRD defers skills tagging out of the MVP
            # ("Skills tagging at the job-opening/candidate level -- deferred to
            # a future iteration"). Recruitment.skills stays on the model and the
            # Skills settings module and Talent Pool are untouched; it is simply
            # not part of the Job Opening form.
        ]
        exclude = ["is_active"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            # Plain text box: the description is stored as text, not HTML.
            "description": forms.Textarea(attrs={"rows": 6}),
            "vacancy": forms.NumberInput(attrs={"min": 1}),
        }
        labels = {
            "title": _("Title"),
            "description": _("Description"),
            "start_date": _("Start Date"),
            "end_date": _("End Date"),
            "vacancy": _("Vacancies"),
            "open_positions": _("Job Position"),
            "recruitment_managers": _("Managers"),
            "budget_min": _("Budget (minimum)"),
            "budget_max": _("Budget (maximum)"),
            "contact_verification_required": _("Contact Verification"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._add_template_selectors()
        if not self.instance.pk:
            self.fields["vacancy"].initial = 1
            self._add_client_company_field()

        # Minimal mandatory fields: only Description, Start Date and Vacancies
        # block Save & Review. Job Position and Managers are both optional --
        # an opening can be posted before anyone is assigned to it, and high
        # volume hiring should not stall on either.
        for optional in ("open_positions", "recruitment_managers"):
            if optional in self.fields:
                self.fields[optional].required = False

        # End Date must start blank: leaving it empty is what keeps an opening
        # from auto-closing, and the PRD says so in the helper text. Horilla's
        # base form seeds every DateField with today, which quietly contradicted
        # that -- an opening created without touching the field would have
        # expired the same day.
        if not self.instance.pk and "end_date" in self.fields:
            self.fields["end_date"].initial = None

        # Labels sit above the inputs, so a placeholder repeating the label is
        # just noise on every row. Cleared only where it duplicates the label --
        # a genuinely useful placeholder elsewhere is left alone.
        for field in self.fields.values():
            placeholder = field.widget.attrs.get("placeholder")
            if placeholder and str(placeholder).strip() == str(field.label).strip():
                field.widget.attrs.pop("placeholder", None)

        # The order the PRD specifies, top to bottom. The two template
        # selectors are added in _add_template_selectors() and would otherwise
        # sit after the verification toggle; the application form belongs
        # before it. order_fields() ignores names that are not present, so the
        # conditional client-company field is unaffected.
        self.order_fields(
            [
                "title",
                "description",
                "open_positions",
                "recruitment_managers",
                "start_date",
                "end_date",
                "vacancy",
                "budget_min",
                "budget_max",
                "form1_templates",
                "form2_templates",
                "contact_verification_required",
            ]
        )

        # Clean form: labels and inputs only, no helper lines. Cleared last so
        # help text from the model, the parent form and the dynamically added
        # selectors is all covered.
        for field in self.fields.values():
            field.help_text = ""

    def clean(self):
        """
        The PRD's two cross-field rules, checked before a draft is saved.

        publication_blockers() re-checks the dates at publish time; this is the
        earlier, friendlier stop so a reviewer never reaches the summary screen
        with a date range that cannot publish.
        """
        # The parent clean() returns None -- it calls super().clean() without
        # returning it -- so self.cleaned_data is the only reliable source here.
        super().clean()
        cleaned_data = self.cleaned_data

        start = cleaned_data.get("start_date")
        end = cleaned_data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", _("End date cannot be before the start date."))

        low = cleaned_data.get("budget_min")
        high = cleaned_data.get("budget_max")
        if low is not None and high is not None and low > high:
            self.add_error(
                "budget_max",
                _("Maximum budget cannot be less than the minimum budget."),
            )

        return cleaned_data
        # LinkedIn auto-posting is out of scope for MVP (PRD: "No LinkedIn
        # auto-post in MVP"), so publish_in_linkedin / linkedin_account_id are
        # no longer form fields at all. The model fields and the posting code
        # in views/linkedin.py are left intact, so the capability can be
        # restored by re-adding the two fields to Meta.fields above.

    #: Extra markup rendered straight after a given field by
    #: generic/form.html. Used to put the resolved question set and the
    #: "+ Add Question" actions directly under the two template selectors,
    #: keeping the whole Application Form section together and above Contact
    #: Verification. Other forms set nothing and render unchanged.
    #: Both selectors get their own section, so Form 1's questions and
    #: "+ Add Question" sit directly under the Form 1 template and Form 2's
    #: under the Form 2 template -- rather than both in one block below the
    #: second selector, where neither template plainly owned its own.
    field_extras = {
        "form1_templates": "cbv/recruitment/job_opening_questions_inline.html",
        "form2_templates": "cbv/recruitment/job_opening_questions_inline.html",
    }

    #: Maps each selector to the form_type whose templates it offers.
    TEMPLATE_SELECTORS = (("form1_templates", "form1"), ("form2_templates", "form2"))

    def _add_template_selectors(self):
        """
        Offer the Form 1 and Form 2 base templates as two separate selectors.

        The PRD keeps the two question banks apart: Form 1 is the public
        application's screening questions, Form 2 the Final HR Round -> Hired
        handoff. One combined selector let a Form 2 template be attached as a
        Form 1 template (and vice versa), which publication would then freeze
        onto the wrong form.

        Both write to the single Recruitment.survey_templates M2M -- see
        apply_template_selection() -- so this needs no schema change.

        The querysets are built here rather than on the class: SurveyTemplate
        .objects is company-scoped through a ContextVar, so a queryset
        evaluated at import time would pin one tenant's templates for the whole
        process.
        """
        from recruitment.models import FORM_ONE, FORM_TWO, SurveyTemplate

        labels = {
            FORM_ONE: _("Screening Questions (Form 1)"),
            FORM_TWO: _("Hiring Handoff Questions (Form 2)"),
        }
        selected = (
            list(self.instance.survey_templates.all()) if self.instance.pk else []
        )
        for field_name, form_type in self.TEMPLATE_SELECTORS:
            self.fields[field_name] = forms.ModelMultipleChoiceField(
                queryset=SurveyTemplate.objects.filter(form_type=form_type),
                required=False,
                label=labels[form_type],
            )
            self.fields[field_name].initial = [
                template.pk
                for template in selected
                if template.form_type == form_type
            ]
            self.fields[field_name].widget.attrs.update(
                {"class": "oh-select oh-select-2 w-100"}
            )

    def apply_template_selection(self, recruitment):
        """
        Write both selectors back to Recruitment.survey_templates.

        Called explicitly from the view because survey_templates is not in
        Meta.fields any more, so form.save_m2m() does not cover it. The M2M is
        replaced with the union of the two selections, which is what makes
        clearing a selector actually remove those templates.
        """
        chosen = []
        for field_name, _form_type in self.TEMPLATE_SELECTORS:
            chosen.extend(self.cleaned_data.get(field_name) or [])
        recruitment.survey_templates.set(chosen)

    def _add_client_company_field(self):
        """
        Offer a client-company choice only when the login context is ambiguous.

        Client HR belongs to one company, so their company is derived silently
        and no field appears -- the PRD's "never shown" behaviour. VWS staff
        hire on behalf of many clients, so when their login authorizes more
        than one company and they have not pinned one in the company switcher,
        they must say which client this opening is for.

        The choices come from the user's authorized set, and form_valid()
        re-verifies the submitted value against that same set -- a company id
        posted by a client is never trusted on its own.
        """
        from horilla.horilla_middlewares import _thread_locals, get_selected_company
        from recruitment.services.authorization import selectable_companies_for_user

        request = getattr(_thread_locals, "request", None)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return

        selected = get_selected_company()
        if selected and selected != "all":
            # A specific company is already pinned in the switcher; that is
            # the selection, so nothing to ask.
            return

        companies = selectable_companies_for_user(user)
        if companies.count() < 2:
            return

        self.fields["company_id"] = forms.ModelChoiceField(
            queryset=companies,
            required=True,
            label=_("Client company"),
        )


@method_decorator(login_required, name="dispatch")
class RecruitmentNewSkillForm(HorillaFormView):
    """
    form view for add new skill
    """

    model = Skill
    form_class = SkillsForm
    new_display_title = _("Skills")
    is_dynamic_create_view = True

    def form_valid(self, form: SkillsForm) -> HttpResponse:
        if form.is_valid():
            message = _("New Skill Created Successfully")
            form.save()
            messages.success(self.request, message)
            return self.HttpResponse()
        return super().form_valid(form)


@method_decorator(login_required, name="dispatch")
class RecruitmentForm(HorillaFormView):
    """
    Create / edit a job opening.

    Permission is resolved per operation in dispatch() rather than by a single
    class-level decorator, because create and edit need different permissions
    and edit additionally needs object scope:

        create -> recruitment.add_recruitment
        edit   -> recruitment.change_recruitment, scoped to THIS opening

    The previous decorator required only `view_recruitment` -- a read
    permission guarding a write -- and applied the same check to both paths.
    """

    model = Recruitment
    form_class = RecruitmentCreationFormExtended
    new_display_title = _("Create Job Opening")
    # No dynamic-create hooks: the only one was ("skills", ...), and Skills is
    # no longer a field on this form (PRD defers skills tagging out of the
    # MVP). Leaving it listed raised KeyError: 'skills' when the form was
    # built. RecruitmentNewSkillForm and the Skills settings module are
    # untouched and still reachable from Configuration.
    dynamic_create_fields = []
    # template_name = "cbv/recruitment/recruitment_form.html"

    def dispatch(self, request, *args, **kwargs):
        from horilla.methods import handle_no_permission
        from recruitment.services.authorization import user_can_manage_job_opening

        pk = kwargs.get("pk")
        if pk:
            # Company-scoped manager, so another tenant's pk resolves to None
            # and is reported as "not found" rather than "forbidden".
            job_opening = Recruitment.objects.filter(pk=pk).first()
            if job_opening is None:
                return handle_no_permission(
                    request, message=_("No job opening found matching the query.")
                )
            if not user_can_manage_job_opening(
                request.user, job_opening, "recruitment.change_recruitment"
            ):
                return handle_no_permission(request)
            # A closed (or removed) opening is read-only: Duplicate it instead.
            if job_opening.status in (
                Recruitment.Status.CLOSED,
                Recruitment.Status.REMOVED,
            ):
                return handle_no_permission(
                    request,
                    message=_(
                        "A closed job opening can't be edited. Use Duplicate to "
                        "post it again."
                    ),
                )
        else:
            if not request.user.has_perm("recruitment.add_recruitment"):
                return handle_no_permission(request)
            # The company is resolved from the login context, or chosen from
            # the user's authorized companies when that context is ambiguous
            # (VWS staff hiring for several clients). Refuse only when the
            # login authorizes no company at all -- a null-company opening
            # would be visible to every tenant.
            if not selectable_companies_for_user(request.user).exists():
                return handle_no_permission(
                    request,
                    message=_(
                        "Your account is not linked to any company, so a job "
                        "opening cannot be created. Contact your administrator."
                    ),
                )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """
        Return context data with optional verbose name for form based on instance state.
        """
        context = super().get_context_data(**kwargs)

        if self.form.instance.pk:
            self.form_class.verbose_name = "Edit Recruitment"

        # Offer "Save & Review" only where it leads somewhere: a brand-new
        # opening, or one still in DRAFT. From REVIEW onwards the summary is
        # reached from the list instead, so the button would be a no-op.
        # generic/form.html renders it only when this attribute is truthy.
        instance = self.form.instance
        self.form.save_and_review = instance.pk is None or instance.status in (
            Recruitment.Status.DRAFT,
            Recruitment.Status.REVIEW,
        )
        # The resolved question set is rendered WITH the form rather than
        # lazy-loaded: an hx-trigger="load" nested inside content htmx has just
        # swapped into the modal did not fire reliably, leaving the section
        # stuck on its placeholder. Computing it here needs no client-side step
        # at all. Distinct context names so nothing in generic/form.html is
        # shadowed.
        # Set on the FORM, not the view context: generic/form.html is rendered
        # by the form's own `structured` method, whose context is just {"form":
        # form} -- view context never reaches it, which is why save_and_review
        # and visible_help_text are form attributes too.
        if instance.pk:
            from recruitment.services.screening import question_sections

            # Keyed by the selector each section belongs under, and each value
            # is a one-item list so the shared table partial -- which loops
            # `sections` -- renders unchanged for one section or both.
            sections = question_sections(instance)
            self.form.question_sections = {
                "form1_templates": [sections[0]],
                "form2_templates": [sections[1]],
            }
            # The form shows only the questions added for THIS opening; the
            # template's own questions appear on the Save & Review summary.
            self.form.extra_questions = {
                "form1_templates": [
                    q for q in sections[0]["questions"] if q["source"] == "job_opening"
                ],
                "form2_templates": [
                    q for q in sections[1]["questions"] if q["source"] == "job_opening"
                ],
            }
            self.form.questions_can_add = (
                instance.status != Recruitment.Status.PUBLISHED
            )
        else:
            from recruitment.views.surveys import (
                clear_pending_questions,
                pending_questions_for_form,
            )

            # A fresh create starts empty: anything held from an earlier,
            # abandoned attempt would otherwise be attached to this opening.
            # Only on GET -- a POST that failed validation re-renders the form
            # and must keep what the user has added.
            if self.request.method == "GET":
                clear_pending_questions(self.request)
            self.form.pending_questions = pending_questions_for_form(self.request)

        # No helper text on this form at all (cleared in the form's __init__).
        return context

    def form_valid(self, form: RecruitmentCreationFormExtended) -> HttpResponse:
        """
        Process form submission to save or update a Recruitment object and display success message.
        """
        targets_to_reload = []
        is_update = bool(form.instance.pk)

        if form.instance.pk:
            recruitment = form.save()
            form.apply_template_selection(recruitment)
            recruitment_managers = self.request.POST.getlist("recruitment_managers")
            if recruitment_managers:
                recruitment.recruitment_managers.set(recruitment_managers)
            if recruitment.publish_in_linkedin and recruitment.linkedin_account_id:
                delete_post(recruitment)
                post_recruitment_in_linkedin(
                    self.request, recruitment, recruitment.linkedin_account_id
                )
            message = _("Recruitment Updated Successfully")
        else:
            recruitment = form.save(commit=False)
            # Company precedence: an explicitly chosen client company (VWS
            # staff hiring for a client), otherwise derived from the login
            # context (Client HR, who only ever has one).
            #
            # A submitted choice is re-verified against the user's authorized
            # companies before it is used -- a posted company id is never
            # trusted, so tampering with the form cannot place an opening in
            # another tenant.
            chosen = form.cleaned_data.get("company_id")
            if chosen is not None and not selectable_companies_for_user(
                self.request.user
            ).filter(pk=chosen.pk).exists():
                chosen = None
            recruitment.company_id = chosen or resolve_company_for_new_job_opening(
                self.request.user
            )
            recruitment.save()
            form.save_m2m()
            form.apply_template_selection(recruitment)
            # Questions added on the create screen before this opening existed
            # are written now and attached to it alone.
            from recruitment.views.surveys import attach_pending_questions

            attach_pending_questions(self.request, recruitment)
            recruitment_managers = self.request.POST.getlist("recruitment_managers")
            if recruitment_managers:
                recruitment.recruitment_managers.set(recruitment_managers)
            if recruitment.publish_in_linkedin and recruitment.linkedin_account_id:
                post_recruitment_in_linkedin(
                    self.request, recruitment, recruitment.linkedin_account_id
                )
            message = _("Recruitment Created Successfully")

        # Business audit for create/edit. A new job opening is always born
        # DRAFT -- publication is a separate, explicit transition audited by
        # the lifecycle service, never implied by saving this form.
        if is_update:
            record_updated(self.request.user, recruitment, form.changed_data)
        else:
            record_created(self.request.user, recruitment)

        messages.success(self.request, message)

        # PRD "Save & Review": save the opening, then show the review summary
        # in place of the form. The modal loaded this form with
        # hx-swap="innerHTML", so returning the summary here swaps it into the
        # same modal body -- the user sees every outstanding blocker at once
        # instead of discovering them one failed publish at a time.
        #
        # Saving still only ever produces a DRAFT. This button reviews; it does
        # not submit or publish, both of which remain explicit POSTs to the
        # lifecycle service.
        if self.request.POST.get("save_and_review") == "true":
            # Same summary as the row's Review & Publish action.
            from recruitment.views.lifecycle import publication_checklist_context

            summary = render_to_string(
                "cbv/recruitment/publication_checklist.html",
                publication_checklist_context(recruitment),
                request=self.request,
            )
            # Flush the queued success message only. Deliberately NOT
            # #applyFilter: re-rendering the list section while this summary
            # sits in the open modal can swap the modal out from under it. The
            # list refreshes when Submit/Publish redirects from the summary.
            script = "<script>$('#reloadMessagesButton').click();</script>"
            return HttpResponse(script + summary)

        if self.request.GET.get("pipeline") == "true" or (
            self.request.resolver_match
            and self.request.resolver_match.url_name == "recruitment-update-pipeline"
        ):
            # Refresh pipeline container only, instead of reloading the whole page.
            targets_to_reload.append("#applyFilter")

        return self.HttpResponse(targets_to_reload=targets_to_reload)

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))


@method_decorator(login_required, name="dispatch")
class AddCandidateFormView(HorillaFormView):
    """
    form view for add candidate
    """

    form_class = AddCandidateForm
    model = Candidate
    new_display_title = _("Add Candidate")

    def get_initial(self) -> dict:
        initial = super().get_initial()
        stage_id = self.request.GET.get("stage_id")
        rec_id = self.request.GET.get("rec_id")
        initial["stage_id"] = stage_id
        initial["rec_id"] = rec_id
        return initial

    def form_valid(self, form: AddCandidateForm) -> HttpResponse:
        if form.is_valid():
            from recruitment.views.views import (
                _send_application_link_for_new_candidate,
            )

            candidate = form.save()
            from recruitment.models import RecruitmentAuditEvent
            from recruitment.services.audit import RecruitmentAuditService

            RecruitmentAuditService.record(
                event_type=RecruitmentAuditEvent.EventType.CANDIDATE_CREATED,
                actor=self.request.user,
                job_opening=candidate.recruitment_id,
                candidate=candidate,
                stage=candidate.stage_id,
                details={"source": "added_to_stage", "stage": str(candidate.stage_id)},
            )
            messages.success(self.request, _("Candidate Added successfully."))
            # PRD Form 3: the candidate is emailed the Form 1 link for this
            # opening. Best-effort: a failed send warns, it never un-adds them.
            _send_application_link_for_new_candidate(self.request, candidate)
            return self.HttpResponse("<script>window.location.reload()</script>")
        return super().form_valid(form)


def copy_stage_setup(source, target):
    """
    Copy the source opening's pipeline onto a duplicate.

    The fixed stages (Applied, Final HR Round, Hired) already exist on the
    target -- seeded on create -- so only their managers are copied. Custom
    stages are recreated with the same name, type, order and managers. The
    Rejected parking stage is never copied; it is created on first rejection.
    """
    from recruitment.models import Stage

    target_stages = {
        s.stage_type: s
        for s in Stage.objects.entire().filter(
            recruitment_id=target, stage_type__in=["applied", "final_hr_round", "hired"]
        )
    }
    for stage in (
        Stage.objects.entire()
        .filter(recruitment_id=source)
        .exclude(stage_type="cancelled")
        .order_by("sequence")
    ):
        managers = list(stage.stage_managers.all())
        if stage.stage_type in target_stages:
            copy = target_stages[stage.stage_type]
        else:
            copy, _created = Stage.objects.get_or_create(
                recruitment_id=target,
                stage=stage.stage,
                defaults={"stage_type": stage.stage_type, "sequence": stage.sequence},
            )
        if managers:
            copy.stage_managers.set(managers)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.add_recruitment"), name="dispatch"
)
class RecruitmentFormDuplicate(HorillaFormView):
    """
    Duplicate an existing job opening as the starting point for a new one.

    This is the supported way to run a role again -- there is no reopen for a
    CLOSED opening. The copy is a genuinely new job opening: its own DRAFT
    lifecycle, its own audit history, nothing inherited from the source's
    published/closed state.

    Gated on add_recruitment (it creates a record), not the view permission
    the original decorator used.
    """

    model = Recruitment
    form_class = RecruitmentCreationFormExtended

    def get_context_data(self, **kwargs):
        """
        Return context data for duplicating a Recruitment object form with modified initial values.
        """
        context = super().get_context_data(**kwargs)
        original_object = Recruitment.objects.get(id=self.kwargs["pk"])
        form = self.form_class(instance=original_object)
        # Fields are copied exactly as they are -- no "(copy)" suffix on the
        # title or description. Upstream appended one to every text field:
        # for field_name, field in form.fields.items():
        #     if isinstance(field, forms.CharField):
        #         if field.initial:
        #             initial_value = field.initial
        #         else:
        #             initial_value = f"{form.initial.get(field_name, '')} (copy)"
        #         form.initial[field_name] = initial_value
        #         form.fields[field_name].initial = initial_value
        # The original's extra questions are listed under the template
        # selectors, as on the create screen, and saved with the copy.
        from recruitment.views.surveys import (
            pending_questions_for_form,
            seed_pending_from_opening,
        )

        if self.request.method == "GET":
            seed_pending_from_opening(self.request, original_object)
        form.is_duplicate = True
        form.pending_questions = pending_questions_for_form(self.request)
        context["form"] = form
        self.form_class.verbose_name = _("Duplicate")
        return context

    def form_valid(self, form: RecruitmentCreationFormExtended) -> HttpResponse:
        """
        Process form submission to add a new recruitment.
        """
        form = self.form_class(self.request.POST)
        if form.is_valid():
            # The source opening, read through the tenant-scoped manager --
            # the same scoping this view already used -- so another tenant's id
            # resolves to nothing. Deliberately NOT gated on view_recruitment:
            # duplicating is an add_recruitment action (the dispatch decorator
            # enforces it), and a user may legitimately hold add/change
            # without view.
            original = Recruitment.objects.filter(pk=self.kwargs.get("pk")).first()
            if original is None:
                messages.error(
                    self.request, _("No job opening found matching the query.")
                )
                return self.HttpResponse()

            recruitment = form.save(commit=False)

            # The duplicate belongs to the SAME company as its source. Without
            # this the copy was saved with company_id NULL, and
            # HorillaCompanyManager passes a null-company row to EVERY tenant
            # -- so duplicating an opening exposed it in every other client's
            # list. The create path already derives a company
            # (resolve_company_for_new_job_opening); this one did not.
            #
            # The source's company is only inherited when it is actually in the
            # user's scope; otherwise -- including a source that itself has no
            # company -- it is derived from the login context, so a duplicate
            # can never be created unscoped.
            if original.company_id_id and company_in_scope(
                self.request.user, original.company_id_id
            ):
                recruitment.company_id = original.company_id
            else:
                recruitment.company_id = resolve_company_for_new_job_opening(
                    self.request.user
                )

            # Explicit, not inherited: a duplicate always starts a fresh
            # lifecycle in DRAFT with no publication/closure history, even
            # when cloned from a PUBLISHED or CLOSED opening.
            recruitment.apply_status(Recruitment.Status.DRAFT)
            recruitment.submitted_for_review_at = None
            recruitment.submitted_for_review_by = None
            recruitment.published_at = None
            recruitment.published_by = None
            recruitment.closed_at = None
            recruitment.closed_by = None
            recruitment.save()
            form.save_m2m()
            # survey_templates is not in Meta.fields -- it is offered as the
            # two Form 1 / Form 2 selectors and written back here -- so
            # save_m2m() does not cover it. Without this call a duplicate was
            # created with no screening or handoff template at all, even though
            # the form pre-selected the original's.
            #
            form.apply_template_selection(recruitment)

            # The duplicate also inherits the questions written for the
            # original alone, as new rows attached only to it -- running the
            # same role again should not mean retyping them, and editing the
            # copy must not reach back into the original. Questions the
            # duplicate already gets through a selected template are skipped,
            # so nothing is asked twice.
            #
            # Normally this is whatever the form carried: the list is seeded
            # from the original when the modal opens, and HR may add to or
            # remove from it before saving. That list lives in the session, so
            # a POST that never ran the seeding GET -- a stale tab, an expired
            # session, a direct post -- would silently produce a duplicate with
            # none of the original's own questions. Seeding here in that case
            # keeps Duplicate's promise to clone everything.
            #
            # The key's presence, not its contents, is the test: an empty list
            # means HR deliberately removed them and must be respected, while
            # a missing key means the form never offered them at all.
            from recruitment.views.surveys import (
                PENDING_QUESTIONS_KEY,
                attach_pending_questions,
                seed_pending_from_opening,
            )

            if PENDING_QUESTIONS_KEY not in self.request.session:
                seed_pending_from_opening(self.request, original)
            attach_pending_questions(self.request, recruitment)
            message = _("Recruitment added")
            recruitment_managers = self.request.POST.getlist("recruitment_managers")
            job_positions = self.request.POST.getlist("open_positions")
            if recruitment_managers:
                recruitment.recruitment_managers.set(recruitment_managers)
            if job_positions:
                recruitment.open_positions.set(job_positions)
            # PRD: Duplicate clones the whole drive, stage setup included --
            # custom stages in their order, and every stage's managers.
            copy_stage_setup(original, recruitment)
            record_created(self.request.user, recruitment)
            messages.success(self.request, message)
            return self.HttpResponse(targets_to_reload=["#applyFilter"])

        return super().form_valid(form)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.view_recruitment"), name="dispatch"
)
class RecruitmentDetailView(HorillaDetailedView):
    """
    detail view of page
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.body = [
            (_("Managers"), "managers_detail"),
            (_("Open Positions"), "open_job_detail"),
            (_("Vacancies"), "vacancy"),
            (_("Total Hires"), "tot_hires"),
            (_("Start Date"), "start_date"),
            (_("End Date"), "end_date"),
        ]

    action_method = "detail_actions"

    model = Recruitment
    title = _("Details")
    header = {
        "title": "title",
        "subtitle": "status_col",
        "avatar": "get_avatar",
    }
