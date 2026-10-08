"""
forms.py

This module contains the form classes used in the application.

Each form represents a specific functionality or data input in the
application. They are responsible for validating
and processing user input data.

Classes:
- YourForm: Represents a form for handling specific data input.

Usage:
from django import forms

class YourForm(forms.Form):
    field_name = forms.CharField()

    def clean_field_name(self):
        # Custom validation logic goes here
        pass
"""

import logging
import uuid
from ast import Dict
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from django import forms
from django.apps import apps
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

from base.forms import Form
from base.forms import ModelForm as BaseModelForm
from base.methods import reload_queryset
from base.widgets import CustomTextInputWidget
from employee.filters import EmployeeFilter
from employee.models import Employee
from horilla import horilla_middlewares
from horilla.horilla_middlewares import _thread_locals
from horilla_widgets.widgets.horilla_multi_select_field import HorillaMultiSelectField
from horilla_widgets.widgets.select_widgets import HorillaMultiSelectWidget
from recruitment import widgets
from recruitment.models import (
    MAX_FILES_CEILING,
    Candidate,
    CandidateDocument,
    CandidateDocumentRequest,
    InterviewSchedule,
    JobPosition,
    LinkedInAccount,
    Recruitment,
    RecruitmentSurvey,
    RejectedCandidate,
    RejectReason,
    Resume,
    Skill,
    SkillZone,
    SkillZoneCandidate,
    Stage,
    StageFiles,
    StageNote,
    SurveyTemplate,
    SurveyTemplateQuestion,
)

logger = logging.getLogger(__name__)


class ModelForm(forms.ModelForm):
    """
    Override of Django ModelForm to add initial styling and defaults.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        reload_queryset(self.fields)

        request = getattr(horilla_middlewares._thread_locals, "request", None)

        today = date.today()
        now = datetime.now()

        default_input_class = "oh-input w-100"
        select_class = "oh-select oh-select-2 select2-hidden-accessible"
        checkbox_class = "oh-switch__checkbox"

        for field_name, field in self.fields.items():
            widget = field.widget
            label = _(field.label) if field.label else ""

            # Date field
            if isinstance(widget, forms.DateInput):
                field.initial = today
                widget.input_type = "date"
                widget.format = "%Y-%m-%d"
                field.input_formats = ["%Y-%m-%d"]

                existing_class = widget.attrs.get("class", default_input_class)
                widget.attrs.update(
                    {
                        "class": f"{existing_class} form-control",
                        "placeholder": label,
                    }
                )

            # Time field
            elif isinstance(widget, forms.TimeInput):
                field.initial = now.strftime("%H:%M")
                widget.input_type = "time"
                widget.format = "%H:%M"
                field.input_formats = ["%H:%M"]

                existing_class = widget.attrs.get("class", default_input_class)
                widget.attrs.update(
                    {
                        "class": f"{existing_class} form-control",
                        "placeholder": label,
                    }
                )

            # Number, Email, Text, File, URL fields
            elif isinstance(
                widget,
                (
                    forms.NumberInput,
                    forms.EmailInput,
                    forms.TextInput,
                    forms.FileInput,
                    forms.URLInput,
                ),
            ):
                existing_class = widget.attrs.get("class", default_input_class)
                widget.attrs.update(
                    {
                        "class": f"{existing_class} form-control",
                        "placeholder": _(field.label.title()) if field.label else "",
                    }
                )

            # Select fields
            elif isinstance(widget, forms.Select):
                if not isinstance(field, forms.ModelMultipleChoiceField):
                    field.empty_label = _("---Choose {label}---").format(label=label)
                existing_class = widget.attrs.get("class", select_class)
                widget.attrs.update({"class": existing_class})

            # Textarea
            elif isinstance(widget, forms.Textarea):
                existing_class = widget.attrs.get("class", default_input_class)
                widget.attrs.update(
                    {
                        "class": f"{existing_class} form-control",
                        "placeholder": label,
                        "rows": 2,
                        "cols": 40,
                    }
                )

            # Checkbox types
            elif isinstance(
                widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)
            ):
                existing_class = widget.attrs.get("class", checkbox_class)
                widget.attrs.update({"class": existing_class})

        # Set employee_id and company_id once
        if request:
            employee = getattr(request.user, "employee_get", None)
            if employee:
                if "employee_id" in self.fields:
                    self.fields["employee_id"].initial = employee

                if "company_id" in self.fields:
                    company_field = self.fields["company_id"]
                    company = getattr(employee, "get_company", None)
                    if company:
                        queryset = company_field.queryset
                        company_field.initial = (
                            company if company in queryset else queryset.first()
                        )


class RegistrationForm(forms.ModelForm):
    """
    Overriding django default model form to apply some styles
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        reload_queryset(self.fields)
        for field_name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.Select,)):
                label = ""
                if field.label is not None:
                    label = _(field.label)
                field.empty_label = _("---Choose {label}---").format(label=label)
                self.fields[field_name].widget.attrs.update(
                    {"id": uuid.uuid4, "class": "oh-select-2 oh-select--sm w-100"}
                )
            elif isinstance(widget, (forms.TextInput)):
                field.widget.attrs.update(
                    {
                        "class": "oh-input w-100",
                    }
                )
            elif isinstance(
                widget,
                (
                    forms.CheckboxInput,
                    forms.CheckboxSelectMultiple,
                ),
            ):
                field.widget.attrs.update({"class": "oh-switch__checkbox "})


class DropDownForm(forms.ModelForm):
    """
    Overriding django default model form to apply some styles
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        reload_queryset(self.fields)
        for field_name, field in self.fields.items():
            widget = field.widget
            if isinstance(
                widget,
                (
                    forms.NumberInput,
                    forms.EmailInput,
                    forms.TextInput,
                    forms.FileInput,
                    forms.URLInput,
                ),
            ):
                if field.label is not None:
                    label = _(field.label)
                    field.widget.attrs.update(
                        {
                            "class": "oh-input oh-input--small oh-table__add-new-row d-block w-100",
                            "placeholder": label,
                        }
                    )
            elif isinstance(widget, (forms.Select,)):
                self.fields[field_name].widget.attrs.update(
                    {
                        "class": "oh-select-2 oh-select--xs-forced ",
                        "id": uuid.uuid4(),
                    }
                )
            elif isinstance(widget, (forms.Textarea)):
                if field.label is not None:
                    label = _(field.label)
                    field.widget.attrs.update(
                        {
                            "class": "oh-input oh-input--small oh-input--textarea",
                            "placeholder": label,
                            "rows": 1,
                            "cols": 40,
                        }
                    )
            elif isinstance(
                widget,
                (
                    forms.CheckboxInput,
                    forms.CheckboxSelectMultiple,
                ),
            ):
                field.widget.attrs.update({"class": "oh-switch__checkbox "})


class RecruitmentCreationForm(BaseModelForm):
    """
    Form for Recruitment model
    """

    cols = {
        "is_published": 4,
        "optional_profile_image": 4,
        "optional_resume": 4,
    }

    class Meta:
        """
        Meta class to add the additional info
        """

        model = Recruitment
        fields = "__all__"
        exclude = [
            "is_active",
            "linkedin_post_id",
            # Lifecycle state belongs to recruitment.services.job_opening.
            # Exposing any of these as form fields would let a user publish
            # (or un-close) a job opening by POSTing a checkbox, skipping the
            # permission check, transition validation and audit event.
            "status",
            "is_published",
            "closed",
            "submitted_for_review_at",
            "submitted_for_review_by",
            "published_at",
            "published_by",
            "closed_at",
            "closed_by",
            "removed_at",
            "removed_by",
        ]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            # Was: forms.Textarea(attrs={"data-summernote": ""}) -- rich text
            # stored HTML; the description is plain text now.
            "description": forms.Textarea(attrs={"rows": 6}),
        }

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("horilla_form.html", context)
        return table_html

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        reload_queryset(self.fields)
        self.fields["open_positions"].required = True
        if not self.instance.pk:
            self.fields["vacancy"].initial = 1
            self.fields["recruitment_managers"] = HorillaMultiSelectField(
                queryset=Employee.objects.filter(is_active=True),
                widget=HorillaMultiSelectWidget(
                    filter_route_name="employee-widget-filter",
                    filter_class=EmployeeFilter,
                    filter_instance_context_name="f",
                    filter_template_path="employee_filters.html",
                    required=True,
                ),
                label=f"{self._meta.model()._meta.get_field('recruitment_managers').verbose_name}",
            )

        # Guarded for the same reason as linkedin_account_id below: the Job
        # Opening form (RecruitmentCreationFormExtended) drops `skills`,
        # because the PRD defers skills tagging out of the Recruitment MVP.
        # A child Meta replaces the parent's, so this __init__ must not assume
        # the field is present -- unguarded it raised KeyError: 'skills' and
        # took out both the create and edit forms. Other forms that still list
        # skills keep their choices exactly as before.
        if "skills" in self.fields:
            skill_choices = [("", _("---Choose Skills---"))] + list(
                self.fields["skills"].queryset.values_list("id", "title")
            )
            self.fields["skills"].choices = skill_choices
            self.fields["skills"].choices += [("create", _("Create new skill "))]
        # LinkedIn auto-posting is out of scope for the Krew MVP, so the Job
        # Opening form (RecruitmentCreationFormExtended) no longer lists these
        # two fields. Older forms and templates that still render them keep
        # working, so configure each one only when it is actually present --
        # a subclass narrowing Meta.fields must not break this __init__.
        if "linkedin_account_id" in self.fields:
            self.fields["linkedin_account_id"].queryset = (
                LinkedInAccount.objects.filter(is_active=True)
            )
        if "publish_in_linkedin" in self.fields:
            self.fields["publish_in_linkedin"].widget.attrs.update(
                {"onchange": "toggleLinkedIn()"}
            )

    # def create_option(self, *args,**kwargs):
    #     option = super().create_option(*args,**kwargs)

    def clean(self):
        if isinstance(self.fields["recruitment_managers"], HorillaMultiSelectField):
            ids = self.data.getlist("recruitment_managers")
            if ids:
                self.errors.pop("recruitment_managers", None)
        # .get() on both sides: a subclass may omit either field, in which case
        # there is nothing to validate rather than a KeyError.
        if self.cleaned_data.get("publish_in_linkedin") and not self.cleaned_data.get(
            "linkedin_account_id"
        ):
            raise forms.ValidationError(
                {
                    "linkedin_account_id": _(
                        "LinkedIn account is required for publishing."
                    )
                }
            )
        super().clean()


class StageCreationForm(BaseModelForm):
    """
    Form for Stage model
    """

    class Meta:
        """
        Meta class to add the additional info
        """

        model = Stage
        fields = "__all__"
        # stage_type is not offered: a stage added here is a plain custom
        # stage (the model default). The fixed stages are seeded per opening.
        exclude = ["sequence", "is_active", "stage_type"]
        labels = {
            "stage": _("Stage Name"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        reload_queryset(self.fields)
        if not self.instance.pk:
            self.fields["stage_managers"] = HorillaMultiSelectField(
                queryset=Employee.objects.filter(is_active=True),
                widget=HorillaMultiSelectWidget(
                    filter_route_name="employee-widget-filter",
                    filter_class=EmployeeFilter,
                    filter_instance_context_name="f",
                    filter_template_path="employee_filters.html",
                    required=True,
                ),
                label=f"{self._meta.model()._meta.get_field('stage_managers').verbose_name}",
            )
        # Every stage needs someone accountable for it (PRD). The same person
        # may manage several stages, and a stage may have several managers --
        # said here because the field gives no hint either way.
        self.fields["stage_managers"].required = True

    def clean(self):
        """
        Enforce at least one stage manager, on create and on update alike.

        The widget's own `required` only covers the create form (the field is
        swapped for a HorillaMultiSelectField there), and the model field is
        a plain M2M that would happily be left empty -- so an edit could clear
        every manager and leave the stage unowned. The check lives here because
        M2M values are not available to Model.clean().
        """
        managers_field = self.fields["stage_managers"]
        posted = (
            self.data.getlist("stage_managers")
            if hasattr(self.data, "getlist")
            else self.data.get("stage_managers") or []
        )
        if isinstance(managers_field, HorillaMultiSelectField) and posted:
            # The multi-select widget posts ids the field itself cannot
            # validate; a non-empty selection clears its spurious error.
            self.errors.pop("stage_managers", None)
        if not posted:
            self.add_error(
                "stage_managers",
                _("Select at least one stage manager for this stage."),
            )
        super().clean()


class StageManagersForm(StageCreationForm):
    """
    Stage Managers only -- the "Edit Managers" action on a fixed stage
    (Applied, Final HR Round, Hired), whose name, order and type never change.
    """

    class Meta(StageCreationForm.Meta):
        fields = ["stage_managers"]
        exclude = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stage_managers"].help_text = ""


class CandidateCreationForm(BaseModelForm):
    """
    Form for Candidate model
    """

    load = forms.CharField(widget=widgets.RecruitmentAjaxWidget, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].initial = "software"
        # `profile` is no longer part of this form (see Meta.fields); guarded
        # so a subclass that puts it back still gets the right widget.
        if "profile" in self.fields:
            self.fields["profile"].widget.attrs["accept"] = ".jpg, .jpeg, .png"
            self.fields["profile"].required = False
        self.fields["resume"].widget.attrs["accept"] = ".pdf"
        self.fields["resume"].required = False
        if self.instance.recruitment_id is not None:
            if self.instance is not None:
                self.fields["job_position_id"] = forms.ModelChoiceField(
                    queryset=self.instance.recruitment_id.open_positions.all(),
                    label=_("Job Position"),
                )
        self.fields["recruitment_id"].widget.attrs = {"data-widget": "ajax-widget"}
        self.fields["job_position_id"].widget.attrs = {"data-widget": "ajax-widget"}

    class Meta:
        """
        Meta class to add the additional info
        """

        model = Candidate
        # `profile` (the photo) is deliberately not offered: a recruiter adding
        # a candidate by hand has no photo to upload, and the candidate
        # supplies their own details through the application link this form
        # sends them. The model field is untouched.
        fields = [
            "name",
            "portfolio",
            "email",
            "mobile",
            "recruitment_id",
            "job_position_id",
            "dob",
            "gender",
            "address",
            "source",
            "country",
            "state",
            "zip",
            "resume",
            "referral",
            "canceled",
            "is_active",
        ]

        widgets = {
            "scheduled_date": forms.DateInput(attrs={"type": "date"}),
            "dob": forms.DateInput(attrs={"type": "date"}),
        }

    def save(self, commit: bool = ...):
        candidate = self.instance
        recruitment = candidate.recruitment_id
        stage = candidate.stage_id
        candidate.hired = False
        candidate.start_onboard = False
        if stage is not None:
            if stage.stage_type == "hired" and candidate.canceled is False:
                candidate.hired = True
                candidate.start_onboard = True
        candidate.recruitment_id = recruitment
        candidate.stage_id = stage
        job_id = self.data.get("job_position_id")
        if job_id:
            job_position = JobPosition.objects.get(id=job_id)
            self.instance.job_position_id = job_position
        return super().save(commit)

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string(
            "candidate/candidate_create_form_as_p.html", context
        )
        return table_html

    def clean(self):
        errors = {}
        resume = self.cleaned_data.get("resume")
        recruitment: Recruitment = self.cleaned_data["recruitment_id"]
        # Only a PUBLISHED opening accepts new candidates. Enforced here (and
        # in the API) rather than by hiding a button, so a direct POST is
        # rejected too. Candidates already attached to a CLOSED opening are
        # untouched -- this guards creation only.
        if (
            recruitment
            and not self.instance.pk
            and not recruitment.accepts_new_candidates()
        ):
            raise ValidationError(
                {
                    "recruitment_id": _(
                        "This job opening is not accepting new candidates. "
                        "Only a published job opening can receive applications."
                    )
                }
            )
        if not resume and not recruitment.optional_resume:
            errors["resume"] = _("This field is required")
        # No profile-photo requirement: the field is not on this form, so
        # demanding one would make the form impossible to submit. The
        # opening's optional_profile_image setting still governs the candidate
        # -facing forms that do ask for a photo.
        if self.instance.name is not None:
            self.errors.pop("job_position_id", None)
            if (
                self.instance.job_position_id is None
                or self.data.get("job_position_id") == ""
            ):
                errors["job_position_id"] = _("This field is required")
            if (
                self.instance.job_position_id
                not in self.instance.recruitment_id.open_positions.all()
            ):
                errors["job_position_id"] = _("Choose valid choice")
        if errors:
            raise ValidationError(errors)
        return super().clean()


class ApplicationForm(RegistrationForm):
    """
    Form for create Candidate
    """

    load = forms.CharField(widget=widgets.RecruitmentAjaxWidget, required=False)
    # Starts empty and is populated per-instance in __init__ below. As a class
    # attribute the queryset was evaluated once, when this module was first
    # imported, so the set of selectable openings froze at process start --
    # anything published afterwards never appeared until a restart.
    recruitment_id = forms.ModelChoiceField(queryset=Recruitment.objects.none())

    class Meta:
        """
        Meta class to add the additional info
        """

        model = Candidate
        # An allowlist, deliberately not `exclude`. This form is POSTed by
        # anonymous applicants, so a denylist silently grants public write
        # access to every column added to Candidate afterwards -- which is how
        # `company_id` (the tenant key) and `offered_ctc` became writable from
        # the public internet. Listing a field here is a conscious decision to
        # accept it from an untrusted client.
        #
        # Never accepted from input, by design:
        #   company_id            derived in Candidate.save() from the opening
        #   converted,
        #   converted_employee_id owned by the employee-conversion flow
        #   offered_ctc           owned by the hiring handoff (Form 2)
        #   dob                   not rendered by either application template
        # The PRD's Form 1 asks for four things: full name, email, phone and
        # resume. Everything an applicant could once also send -- photo,
        # portfolio, gender, address, country, state, city, zip -- is gone from
        # the form, not merely hidden in the page.
        #
        # Two structural fields remain, neither of them applicant content:
        #
        #   recruitment_id    hidden, and its posted value is ignored (the view
        #                     resolves the opening server-side). Kept so a
        #                     duplicate application is reported by the form's
        #                     unique check for ("email", "recruitment_id")
        #                     rather than as a database IntegrityError.
        #   job_position_id   required by Candidate.save() for an event-based
        #                     opening, which has no single position to inherit.
        #                     The page renders it only in that case.
        fields = (
            "name",
            "email",
            "mobile",
            "resume",
            "recruitment_id",
            "job_position_id",
        )
        widgets = {
            "recruitment_id": forms.TextInput(
                attrs={
                    "required": "required",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["resume"].widget.attrs["accept"] = ".pdf"
        # Required-ness is enforced in clean() rather than here, so the message
        # is the same whether or not the input was rendered.
        self.fields["resume"].required = False

        # Resolved per form instance, so a newly published opening is
        # selectable immediately. Only PUBLISHED openings accept applications
        # -- a CLOSED one stays listed publicly but must not be applied to.
        self.fields["recruitment_id"].queryset = Recruitment.objects.filter(
            status=Recruitment.Status.PUBLISHED, is_active=True
        )

        self.fields["recruitment_id"].widget.attrs = {"data-widget": "ajax-widget"}
        self.fields["job_position_id"].widget.attrs = {"data-widget": "ajax-widget"}

    def clean(self, *args, **kwargs):
        errors = {}
        resume = self.cleaned_data.get("resume")
        recruitment: Recruitment = self.cleaned_data.get("recruitment_id")

        # Resume is mandatory whatever the opening's optional_resume flag says:
        # the PRD makes it one of the four Form 1 fields. Checked outside any
        # `recruitment` guard, so a missing opening cannot quietly make it
        # optional. This is a deliberate behaviour change for openings that
        # had optional_resume set.
        if not resume:
            errors["resume"] = _("This field is required")

        # An event-based opening has no single position to inherit, and
        # Candidate.save() raises ValidationError when one is missing. Reported
        # here as a field error instead, so an applicant never meets that as a
        # server error.
        if (
            recruitment
            and recruitment.is_event_based
            and not self.cleaned_data.get("job_position_id")
        ):
            errors["job_position_id"] = _("This field is required")

        # One application per email per opening, ignoring letter case (the
        # database constraint alone is case-sensitive).
        from recruitment.services.candidate import existing_candidate_with_email

        if recruitment and existing_candidate_with_email(
            self.cleaned_data.get("email"),
            job_opening=recruitment,
            exclude_pk=self.instance.pk,
        ):
            errors["email"] = _(
                "An application with this email already exists for this job."
            )

        if errors:
            raise ValidationError(errors)

        super().clean()
        return self.cleaned_data


class HiringHandoffForm(ModelForm):
    """
    PRD Form 2: the two values captured at the Final HR Round -> Hired handoff.

    Both are mandatory HERE even though the model leaves them nullable. A
    candidate legitimately has neither for the whole pipeline; they become
    required only at this boundary, so the requirement belongs to this form
    rather than to the column.

    Everything else on the handoff screen is carried read-only (name, email and
    mobile from the application; designation and budget from the job opening),
    so there is deliberately nothing else to post here.
    """

    verbose_name = _("Hiring Handoff")
    cols = {"offered_ctc": 6, "joining_date": 6, "job_position_id": 6}

    class Meta:
        model = Candidate
        # job_position_id is the designation handed to onboarding. It is
        # prefilled from the job opening but editable here: the role actually
        # offered is settled in the final round and is not always the one the
        # opening was raised for.
        fields = [
            "job_position_id",
            "offered_ctc",
            "joining_date",
            "handoff_budget_min",
            "handoff_budget_max",
        ]
        labels = {
            "handoff_budget_min": _("Budget (minimum)"),
            "handoff_budget_max": _("Budget (maximum)"),
            "job_position_id": _("Designation"),
            "offered_ctc": _("Offered CTC"),
            "joining_date": _("Date of Joining"),
        }
        help_texts = {
            "job_position_id": _(
                "Prefilled from the job opening. Change it if the offer is for "
                "a different role."
            ),
            "offered_ctc": _("The agreed figure, which may differ from the budget."),
        }
        widgets = {"joining_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = True
            field.widget.attrs.setdefault("class", "oh-input w-100")
        # Budget is optional (as on the opening), prefilled from the opening
        # and editable here (PRD Form 2).
        source = getattr(self.instance, "recruitment_id", None)
        for name, attr in (
            ("handoff_budget_min", "budget_min"),
            ("handoff_budget_max", "budget_max"),
        ):
            self.fields[name].required = False
            if source is not None and getattr(self.instance, name) is None:
                self.fields[name].initial = getattr(source, attr)

        # Designations are offered from the candidate's own opening first, so
        # the usual choice is one click away, with the rest still available.
        opening = getattr(self.instance, "recruitment_id", None)
        if opening is not None and "job_position_id" in self.fields:
            self.fields["job_position_id"].empty_label = None
            if self.instance.job_position_id_id is None:
                self.fields["job_position_id"].initial = (
                    opening.job_position_id_id
                    or opening.open_positions.values_list("pk", flat=True).first()
                )

    def clean(self):
        cleaned_data = super().clean()
        low = cleaned_data.get("handoff_budget_min")
        high = cleaned_data.get("handoff_budget_max")
        if low is not None and high is not None and low > high:
            self.add_error(
                "handoff_budget_max",
                _("Maximum budget cannot be less than the minimum budget."),
            )
        return cleaned_data


class RecruitmentDropDownForm(DropDownForm):
    """
    Form for Recruitment model
    """

    class Meta:
        """
        Meta class to add the additional info
        """

        fields = "__all__"
        model = Recruitment
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            # Was: forms.Textarea(attrs={"data-summernote": ""}) -- rich text
            # stored HTML; the description is plain text now.
            "description": forms.Textarea(attrs={"rows": 6}),
        }
        labels = {"description": _("Description"), "vacancy": _("Vacancy")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["job_position_id"].widget.attrs.update({"id": uuid.uuid4})
        self.fields["recruitment_managers"].widget.attrs.update({"id": uuid.uuid4})
        field = self.fields["is_active"]
        field.widget = field.hidden_widget()


class PoolCandidateForm(ModelForm):
    """
    Add a candidate straight into the Candidate Pool, with no job opening
    (PRD: "added purely for future reference"). They can be mapped to a live
    job opening later.
    """

    verbose_name = _("Add Candidate")
    cols = {"name": 12, "email": 12, "mobile": 12, "resume": 12}

    class Meta:
        model = Candidate
        fields = ["name", "email", "mobile", "resume"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.fields:
            self.fields[name].required = True
            self.fields[name].help_text = ""
        self.fields["resume"].widget.attrs["accept"] = ".pdf"

    def clean(self):
        cleaned_data = super().clean()
        from horilla.horilla_middlewares import _thread_locals
        from recruitment.services.authorization import resolve_company_for_new_job_opening
        from recruitment.services.candidate import existing_candidate_with_email

        request = getattr(_thread_locals, "request", None)
        company = (
            resolve_company_for_new_job_opening(request.user)
            if request is not None and request.user.is_authenticated
            else None
        )
        if existing_candidate_with_email(cleaned_data.get("email"), company=company):
            self.add_error(
                "email",
                _(
                    "This email already belongs to a candidate in the Candidate "
                    "Pool. Map that candidate to a job instead."
                ),
            )
        return cleaned_data


class AddCandidateForm(ModelForm):
    """
    Form for Candidate model
    """

    verbose_name = "Add Candidate"

    class Meta:
        """
        Meta class to add the additional info
        """

        model = Candidate
        # `profile` (the photo) is deliberately not offered: a recruiter adding
        # a candidate into a stage has no photo to upload. The model field is
        # untouched, and the candidate-facing forms that do ask for one are
        # unaffected.
        # PRD Form 3: Full Name, Email, Phone, Resume, plus the stage. Job
        # Position is offered only when the opening has several (see __init__).
        fields = [
            "name",
            "email",
            "mobile",
            "resume",
            # "gender",  # Hidden per PRD: Form 3 is Name, Email, Phone, Resume only.
            "stage_id",
            "job_position_id",
        ]

    def clean(self):
        """
        Reject a new candidate aimed at an opening that is not PUBLISHED.

        Mirrors the guard on CandidateCreationForm so manual creation into a
        stage cannot sidestep it. Existing candidates on a CLOSED opening are
        unaffected -- this only blocks new ones.
        """
        cleaned_data = super().clean()
        stage = cleaned_data.get("stage_id") or self.instance.stage_id
        if stage is not None and getattr(stage, "stage_type", None) in ("hired", "cancelled"):
            raise forms.ValidationError(
                _("A candidate cannot be added straight into Hired or Rejected.")
            )
        recruitment = getattr(stage, "recruitment_id", None) or self.instance.recruitment_id
        # The opening is not a field on this form, so Django's own
        # (email, recruitment_id) uniqueness check never runs -- without this
        # a duplicate reached the database and crashed.
        from recruitment.services.candidate import existing_candidate_with_email

        if recruitment and existing_candidate_with_email(
            cleaned_data.get("email"), job_opening=recruitment, exclude_pk=self.instance.pk
        ):
            self.add_error(
                "email",
                _("A candidate with this email is already in this job opening."),
            )
        if (
            recruitment
            and not self.instance.pk
            and not recruitment.accepts_new_candidates()
        ):
            raise ValidationError(
                _(
                    "This job opening is not accepting new candidates. Only a "
                    "published job opening can receive applications."
                )
            )
        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        initial = (kwargs.get("initial") or {}).get("stage_id")
        if initial:
            recruitment = Stage.objects.get(id=initial).recruitment_id
            self.instance.recruitment_id = recruitment
            # Any stage up to Final HR Round (PRD Form 3); never straight into
            # Hired (needs the handoff form) or Rejected (needs the reject flow).
            self.fields["stage_id"].queryset = self.fields["stage_id"].queryset.filter(
                recruitment_id=recruitment
            ).exclude(stage_type__in=["hired", "cancelled"])
            positions = recruitment.open_positions.all()
            if positions.count() > 1:
                self.fields["job_position_id"].queryset = positions
                self.fields["job_position_id"].empty_label = None
            else:
                # One (or no) position: Candidate.save() fills it in.
                del self.fields["job_position_id"]
        # Resume is always mandatory (PRD), PDF only.
        self.fields["resume"].required = True
        self.fields["resume"].widget.attrs["accept"] = ".pdf"
        # self.fields["gender"].empty_label = None  # gender hidden per PRD
        self.fields["stage_id"].empty_label = None

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html


class StageDropDownForm(DropDownForm):
    """
    Form for Stage model
    """

    class Meta:
        """
        Meta class to add the additional info
        """

        model = Stage
        fields = "__all__"
        # stage_type is not offered: a stage added here is a plain custom
        # stage (the model default). The fixed stages are seeded per opening.
        exclude = ["sequence", "is_active", "stage_type"]
        labels = {
            "stage": _("Stage Name"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stage = Stage.objects.last()
        if stage is not None and stage.sequence is not None:
            self.instance.sequence = stage.sequence + 1
        else:
            self.instance.sequence = 1


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = [
                single_file_clean(data, initial),
            ]
        return result[0] if result else []


class StageNoteForm(ModelForm):
    """
    Form for StageNote model
    """

    class Meta:
        """
        Meta class to add the additional info
        """

        model = StageNote
        # exclude = (
        #     "updated_by",
        #     "stage_id",
        # )
        fields = ["description"]
        exclude = ["is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # field = self.fields["candidate_id"]
        # field.widget = field.hidden_widget()
        self.fields["stage_files"] = MultipleFileField(label="files")
        self.fields["stage_files"].required = False

    def save(self, commit: bool = ...) -> Any:
        attachment = []
        multiple_attachment_ids = []
        attachments = None
        if self.files.getlist("stage_files"):
            attachments = self.files.getlist("stage_files")
            self.instance.attachement = attachments[0]
            multiple_attachment_ids = []

            for attachment in attachments:
                file_instance = StageFiles()
                file_instance.files = attachment
                file_instance.save()
                multiple_attachment_ids.append(file_instance.pk)
        instance = super().save(commit)
        if commit:
            instance.stage_files.add(*multiple_attachment_ids)
        return instance, multiple_attachment_ids


class StageNoteUpdateForm(ModelForm):
    class Meta:
        """
        Meta class to add the additional info
        """

        model = StageNote
        exclude = ["updated_by", "stage_id", "stage_files", "is_active"]
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["candidate_id"]
        field.widget = field.hidden_widget()


class QuestionForm(ModelForm):
    """
    QuestionForm
    """

    cols = {"options": 12, "template_id": 12, "question": 12}

    verbose_name = "Survey Questions"

    recruitment = forms.ModelMultipleChoiceField(
        queryset=Recruitment.objects.filter(is_active=True),
        required=False,
        label=_("Recruitment"),
    )
    options = forms.CharField(
        widget=forms.TextInput, label=_("Options"), required=False
    )

    class Meta:
        """
        Class Meta for additional options
        """

        model = RecruitmentSurvey
        fields = "__all__"
        # allow_multiple_files is excluded because it is no longer authored:
        # HR sets "Max files allowed" and the model derives the flag from it.
        # Offering both would let the two disagree.
        exclude = [
            "recruitment_ids",
            "job_position_ids",
            "is_active",
            "options",
            "allow_multiple_files",
        ]
        labels = {
            "question": _("Question"),
            "sequence": _("Sequence"),
            "type": _("Type"),
            "options": _("Options"),
            "is_mandatory": _("Is Mandatory"),
            "max_files": _("Max files allowed"),
        }
        help_texts = {
            "max_files": _(
                "File upload questions only. 1 accepts a single file; a higher "
                "number lets the candidate attach up to that many."
            ),
        }

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        return render_to_string("survey/question_fields.html", {"form": self})

    @property
    def option_values(self):
        """The choices to show in the options editor: posted, else saved."""
        if self.data:
            return [
                value
                for key, value in self.data.items()
                if key.startswith("options") and value
            ]
        if self.instance and self.instance.pk and self.instance.options:
            return [o.strip() for o in self.instance.options.split(",") if o.strip()]
        return []

    def clean(self):
        cleaned_data = super().clean()
        recruitment = self.cleaned_data.get("recruitment")
        question_type = self.cleaned_data.get("type")
        options = self.cleaned_data.get("options")
        self.recruitment = (
            recruitment if recruitment is not None else Recruitment.objects.none()
        )
        if question_type in ["options", "multiple"] and (
            options is None or options == ""
        ):
            raise ValidationError({"options": "Options field is required"})

        # The file cap only means something on a file question. Left blank it
        # is 1, and on any other type it is forced to 1 so a stale number
        # cannot survive a type change and quietly allow several files.
        if "max_files" in self.fields:
            if question_type == "file":
                cleaned_data["max_files"] = max(1, cleaned_data.get("max_files") or 1)
            else:
                cleaned_data["max_files"] = 1

        # A question and the templates it joins must belong to the SAME form.
        # Publication resolves the template path by the TEMPLATE's form_type
        # but the direct path by the QUESTION's, so a Form 2 question sitting
        # in a Form 1 template would be frozen into Form 1 -- that is, onto the
        # public application page. Refused here, the one place where both
        # values are known before the M2M is written.
        # Removed: the check that refused a "Form 2 question" in a "Form 1
        # template". The template decides the form; the bank is shared.
        # form_type = self.cleaned_data.get("form_type")
        # mismatched = [... template.form_type != form_type ...]
        # if mismatched: raise ValidationError({"template_id": ...})
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.type in ["options", "multiple"]:
            additional_options = []
            for key, value in self.cleaned_data.items():
                if key.startswith("options") and value:
                    additional_options.append(value)

            instance.options = ", ".join(additional_options)
            if commit:
                instance.save()
                self.save_m2m()
        else:
            instance.options = ""
        return instance

    #: The PRD's answer formats, in its own words. Display only -- the stored
    #: values are unchanged, so this needs no migration and no data fix.
    #: Horilla's own labels made the list hard to read: "Choices" is a
    #: single-select MCQ while "Multiple Choice" is actually multi-select, and
    #: "Yes/No" is stored as "checkbox". The PRD formats are listed first; the
    #: remaining platform types stay available and keep working.
    TYPE_LABELS = {
        "text": _("Short answer"),
        "textarea": _("Long answer"),
        "options": _("Multiple choice (pick one)"),
        "multiple": _("Checkboxes (pick several)"),
        "number": _("Number"),
        "date": _("Date"),
        "file": _("File upload (PDF)"),
        "checkbox": _("Yes / No"),
        "percentage": _("Percentage"),
        "rating": _("Rating"),
    }
    TYPE_ORDER = (
        "text",
        "textarea",
        "options",
        "multiple",
        "number",
        "date",
        "file",
        "checkbox",
        "percentage",
        "rating",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance", None)
        self.option_count = 1

        if "type" in self.fields:
            available = dict(RecruitmentSurvey.question_types)
            ordered = [
                (value, self.TYPE_LABELS.get(value, available[value]))
                for value in self.TYPE_ORDER
                if value in available
            ]
            # Anything the platform offers that this map does not name keeps
            # its own label rather than disappearing from the dropdown.
            ordered += [
                (value, label)
                for value, label in RecruitmentSurvey.question_types
                if value not in self.TYPE_ORDER
            ]
            self.fields["type"].choices = [("", _("---Choose answer format---"))] + ordered

        if "form_type" in self.fields:
            # Not a user choice: a bank question is just a question, and the
            # template it is used in decides application vs handoff.
            self.fields["form_type"].required = False
            self.fields["form_type"].widget = forms.HiddenInput()
            if not self.instance.pk and not self.initial.get("form_type"):
                self.fields["form_type"].initial = "form1"
        if "sequence" in self.fields:
            # Order is configured per template, never while writing a question.
            self.fields["sequence"].required = False
            self.fields["sequence"].widget = forms.HiddenInput()
        if "recruitment" in self.fields:
            # Attaching to job openings happens on the Job Opening form.
            self.fields["recruitment"].widget = forms.MultipleHiddenInput()
        if "max_files" in self.fields:
            # Only meaningful for a file question, so never demanded: a text
            # question submitted without it is complete, and clean() settles
            # the value at 1.
            self.fields["max_files"].required = False
            self.fields["max_files"].initial = 1
            self.fields["max_files"].widget.attrs.setdefault("min", 1)
            self.fields["max_files"].widget.attrs.setdefault("max", MAX_FILES_CEILING)

        def create_options_field(option_key, initial=None):
            self.fields[option_key] = forms.CharField(
                widget=forms.TextInput(
                    attrs={
                        "name": option_key,
                        "id": f"id_{option_key}",
                        "class": "oh-input w-100",
                    }
                ),
                label=_("Options"),
                required=False,
                initial=initial,
            )

        def create_options_field_more(option_key, initial=None):
            self.fields[option_key] = forms.CharField(
                widget=CustomTextInputWidget(
                    delete_url="add-remove-options-field",
                    attrs={
                        "name": option_key,
                        "id": f"{option_key}",
                        "class": "oh-input w-100",
                    },
                ),
                required=False,
                initial=initial,
            )

        if instance:
            split_options = instance.options.split(",")
            for i, option in enumerate(split_options):
                if i == 0:
                    create_options_field("options", option)
                else:
                    self.option_count += 1
                    create_options_field_more(f"options{i}", option)

        if instance:
            self.fields["recruitment"].initial = instance.recruitment_ids.all()
        self.fields["type"].widget.attrs.update(
            {"class": " w-100", "style": "border:solid 1px #6c757d52;height:50px;"}
        )
        for key, value in self.data.items():
            if key.startswith("options"):
                self.option_count += 1
                create_options_field(key, initial=value)
        fields_order = list(self.fields.keys())
        fields_order.remove("recruitment")
        fields_order.insert(2, "recruitment")
        self.fields = {field: self.fields[field] for field in fields_order}


class SurveyForm(forms.Form):
    """
    The screening questions a candidate actually answers.

    For a PUBLISHED job opening these come from the immutable
    JobOpeningQuestion snapshot, never from the live question bank. That is the
    whole point of the snapshot: a candidate must see the question set as it was
    frozen at publication, even if the reusable question or its template has
    been edited since.

    Before publication (Draft/Review preview) there is no snapshot yet, so the
    reusable questions are collected the same way publication will collect them
    -- union of the template path and the direct recruitment_ids path,
    deduplicated -- so the preview matches what will be frozen.
    """

    def __init__(
        self, recruitment, *args, embedded=False, form_type=None, **kwargs
    ) -> None:
        # NOTE: `recruitment` is deliberately not forwarded to forms.Form.
        # It used to be passed as the first positional argument, which
        # forms.Form reads as `data` -- binding the form to a Recruitment
        # object. Nothing depended on that, and it made the form spuriously
        # "bound".
        super().__init__(*args, **kwargs)
        from recruitment.services.screening import (
            collect_questions_for_publication,
            field_name_for,
            published_questions,
        )

        self.job_opening = recruitment
        # Scoped to one form. Form 1 is frozen at publication; Form 2 is frozen
        # later, at the hiring handoff -- so before that boundary this falls
        # through to the preview path below, which is correct rather than a
        # missing snapshot.
        snapshot = list(published_questions(recruitment, form_type))

        if snapshot:
            questions = snapshot
            self.is_snapshot = True
        else:
            # Not yet published: render what publication would freeze.
            self.is_snapshot = False
            questions = [
                SimpleNamespace(
                    pk=spec["source_question"].pk,
                    wording=spec["wording"],
                    question_type=spec["question_type"],
                    options=spec["options"],
                    is_mandatory=spec["is_mandatory"],
                    display_order=spec["display_order"],
                    # Carried so the preview renders a multi-file question the
                    # same way publication will freeze it.
                    allow_multiple_files=spec["allow_multiple_files"],
                    max_files=spec["max_files"],
                    choices=lambda opts=spec["options"]: (opts or "").split(", "),
                )
                for spec in collect_questions_for_publication(recruitment, form_type)
            ]

        # Field names are derived from the question id, never the wording, so
        # two questions sharing wording cannot collide and renaming a question
        # cannot orphan an answer.
        rendered = [
            {"question": question, "field_name": field_name_for(question)}
            for question in questions
        ]
        # `embedded` drops the template's own submit button and footer so the
        # questions can sit inside a larger form -- the public application page
        # submits the candidate's details and their answers together, and a
        # second submit button there would be a second way to post the page.
        context = {"form": self, "questions": rendered, "embedded": embedded}
        self.form = render_to_string("survey_form.html", context)
        self.questions = questions


class SurveyPreviewForm(forms.Form):
    """
    Preview of one template's questions, with its per-template configuration.

    Reads mandatory/optional and ordering from SurveyTemplateQuestion (the
    through model) rather than from the question, so a question that is
    mandatory in this template and optional in another previews correctly here.
    """

    def __init__(self, template, *args, **kwargs) -> None:
        # See SurveyForm: `template` is not forwarded as form `data`.
        super().__init__(*args, **kwargs)
        from recruitment.models import SurveyTemplateQuestion

        rows = (
            SurveyTemplateQuestion.objects.filter(surveytemplate=template)
            .select_related("recruitmentsurvey")
            .order_by("sequence", "id")
        )
        questions = [
            SimpleNamespace(
                pk=row.recruitmentsurvey.pk,
                id=row.recruitmentsurvey.pk,
                question=row.recruitmentsurvey.question,
                type=row.recruitmentsurvey.type,
                options=row.recruitmentsurvey.options,
                # Per-template value, not the question's own.
                is_mandatory=row.is_mandatory,
                choices=lambda opts=row.recruitmentsurvey.options: (opts or "").split(
                    ", "
                ),
            )
            for row in rows
        ]
        context = {"form": self, "questions": questions}
        self.form = render_to_string("survey_preview_form.html", context)


class TemplateForm(BaseModelForm):
    """
    Create / edit a question template (PRD: "Base Templates").

    The template decides whether it is an application (Form 1) or hiring
    handoff (Form 2) template; questions come from one shared bank and are
    picked here, in order, each with its own Mandatory tick. They are linked
    through SurveyTemplateQuestion (position, mandatory, wording copy);
    questions removed from the list are unlinked (they stay in the bank).
    """

    cols = {"title": 12, "description": 12, "company_id": 12}

    verbose_name = "Template"

    questions = forms.ModelMultipleChoiceField(
        queryset=RecruitmentSurvey.objects.none(),
        required=False,
        widget=forms.MultipleHiddenInput,
        label=_("Questions"),
    )

    class Meta:
        model = SurveyTemplate
        fields = "__all__"
        exclude = ["is_active"]
        labels = {"title": _("Template name"), "form_type": _("Used for")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.db.models import Q

        # The bank: every question except job-specific extras (attached to an
        # opening and in no template). A question's own form_type is ignored --
        # the template decides which form it is for.
        bank = (
            RecruitmentSurvey.objects.filter(
                Q(recruitment_ids__isnull=True) | Q(template_id__isnull=False)
            )
            .distinct()
            .order_by("question")
        )
        self.fields["questions"].queryset = bank
        if "form_type" in self.fields:
            self.fields["form_type"].choices = [
                ("form1", _("Application form")),
                ("form2", _("Hiring handoff")),
            ]
            self.fields["form_type"].widget = forms.RadioSelect(
                choices=self.fields["form_type"].choices
            )
        if "is_general_template" in self.fields:
            del self.fields["is_general_template"]

        # What the popup shows: the template's questions in order with their
        # mandatory flag, and the rest of the bank to add from.
        by_id = {q.pk: q for q in bank}
        if self.is_bound:
            ids = [int(i) for i in self.data.getlist("questions") if str(i).isdigit()]
            mandatory = {int(i) for i in self.data.getlist("mandatory") if str(i).isdigit()}
        elif self.instance.pk:
            links = SurveyTemplateQuestion.objects.filter(
                surveytemplate=self.instance
            ).order_by("sequence", "id")
            ids = [link.recruitmentsurvey_id for link in links]
            mandatory = {link.recruitmentsurvey_id for link in links if link.is_mandatory}
        else:
            ids, mandatory = [], set()
        self.selected_rows = [(by_id[i], i in mandatory) for i in ids if i in by_id]
        chosen = set(ids)
        self.bank_rows = [q for q in bank if q.pk not in chosen]

    def save(self, commit=True):
        template = super().save(commit=commit)
        if commit:
            self.save_questions(template)
        return template

    def save_questions(self, template):
        """
        Make the template's links match the popup: the posted order becomes
        the sequence (1..N), ticked ones are mandatory, missing ones are
        unlinked. Every link is saved through the model, so the wording copy is
        kept and changes are audited.
        """
        order = [int(i) for i in self.data.getlist("questions") if str(i).isdigit()]
        mandatory = {int(i) for i in self.data.getlist("mandatory") if str(i).isdigit()}
        valid = set(self.fields["questions"].queryset.values_list("pk", flat=True))
        order = [i for i in dict.fromkeys(order) if i in valid]

        links = {
            link.recruitmentsurvey_id: link
            for link in SurveyTemplateQuestion.objects.filter(surveytemplate=template)
        }
        for question_id, link in links.items():
            if question_id not in order:
                link.delete()
        for position, question_id in enumerate(order, start=1):
            link = links.get(question_id)
            if link is None:
                SurveyTemplateQuestion.objects.create(
                    surveytemplate=template,
                    recruitmentsurvey_id=question_id,
                    is_mandatory=question_id in mandatory,
                    sequence=position,
                )
            elif link.sequence != position or link.is_mandatory != (question_id in mandatory):
                link.sequence = position
                link.is_mandatory = question_id in mandatory
                link.save()


class AddQuestionForm(Form):
    """
    AddQuestionForm
    """

    verbose_name = "Add Question"
    question_ids = forms.ModelMultipleChoiceField(
        queryset=RecruitmentSurvey.objects.all(), label="Questions"
    )
    template_ids = forms.ModelMultipleChoiceField(
        queryset=SurveyTemplate.objects.all(), label="Templates"
    )

    def save(self):
        """
        Manual save/adding of questions to the templates
        """
        for question in self.cleaned_data["question_ids"]:
            question.template_id.add(*self.data["template_ids"])

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html


exclude_fields = [
    "id",
    "profile",
    "portfolio",
    "resume",
    "sequence",
    "schedule_date",
    "created_at",
    "created_by",
    "modified_by",
    "is_active",
    "last_updated",
    "horilla_history",
]


class CandidateExportForm(forms.Form):
    model_fields = Candidate._meta.get_fields()
    field_choices = [
        (field.name, field.verbose_name.capitalize())
        for field in model_fields
        if hasattr(field, "verbose_name") and field.name not in exclude_fields
    ]
    field_choices = field_choices + [
        ("rejected_candidate__description", "Rejected Description"),
    ]
    selected_fields = forms.MultipleChoiceField(
        choices=field_choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[
            "name",
            "recruitment_id",
            "job_position_id",
            "stage_id",
            "email",
            "mobile",
            "hired",
            "joining_date",
        ],
    )


class SkillZoneCreateForm(BaseModelForm):

    cols = {"title": 12, "description": 12, "company_id": 12}

    class Meta:
        """
        Class Meta for additional options
        """

        model = SkillZone
        fields = "__all__"
        exclude = ["is_active"]


class SkillZoneCandidateForm(ModelForm):

    cols = {"skill_zone_id": 12, "candidate_id": 12, "reason": 12}
    verbose_name = "Talent Pool Candidate"
    candidate_id = forms.ModelMultipleChoiceField(
        queryset=Candidate.objects.all(),
        widget=forms.SelectMultiple,
        label=_("Candidate"),
    )

    class Meta:
        """
        Class Meta for additional options
        """

        model = SkillZoneCandidate
        fields = ["skill_zone_id", "reason"]

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html

    def clean_candidate_id(self):
        candidate_field = self.cleaned_data["candidate_id"]

        if isinstance(candidate_field, Candidate):
            return candidate_field

        if hasattr(candidate_field, "__iter__"):
            for candidate in candidate_field:
                if not isinstance(candidate, Candidate):
                    raise forms.ValidationError(_("Invalid candidate selected."))
            return candidate_field

        return candidate_field

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["candidate_id"].empty_label = None

        self.fields = {
            "skill_zone_id": self.fields["skill_zone_id"],
            "candidate_id": self.fields["candidate_id"],
            "reason": self.fields["reason"],
        }

        if self.instance.pk:
            self.verbose_name = (
                self.instance.candidate_id.name
                + " / "
                + self.instance.skill_zone_id.title
            )
            self.fields["candidate_id"] = forms.ModelChoiceField(
                queryset=Candidate.objects.all(),
                widget=forms.Select(attrs={"class": "oh-select oh-select2 w-100"}),
                label=_("Candidate"),
            )

    def save(self, commit: bool = True) -> SkillZoneCandidate:

        if not self.instance.pk:
            candidates = Candidate.objects.filter(
                id__in=list((self.data.getlist("candidate_id")))
            )
            skill_zone = self.cleaned_data["skill_zone_id"]
            reason = self.cleaned_data["reason"]
            for candidate in candidates:
                zone_cand = SkillZoneCandidate()
                zone_cand.skill_zone_id = skill_zone
                zone_cand.candidate_id = candidate
                zone_cand.reason = reason
                zone_cand.save()
        else:
            instance = super().save()

        return self.instance


class ToSkillZoneForm(ModelForm):

    verbose_name = "Add to Talent Pool"
    skill_zone_ids = forms.ModelMultipleChoiceField(
        queryset=SkillZone.objects.all(), label=_("Talent Pools")
    )

    cols = {"reason": 12, "skill_zone_ids": 12}

    class Meta:
        """
        Class Meta for additional options
        """

        model = SkillZoneCandidate
        fields = "__all__"
        exclude = [
            "skill_zone_id",
            "is_active",
            "candidate_id",
        ]
        error_messages = {
            NON_FIELD_ERRORS: {
                "unique_together": "This candidate alreay exist in this talent pool",
            }
        }

    def clean(self):
        cleaned_data = super().clean()
        candidate = cleaned_data.get("candidate_id")
        skill_zones = cleaned_data.get("skill_zone_ids")
        skill_zone_list = []
        for skill_zone in skill_zones:
            # Check for the unique together constraint manually
            if SkillZoneCandidate.objects.filter(
                candidate_id=candidate, skill_zone_id=skill_zone
            ).exists():
                # Raise a ValidationError with a custom error message
                skill_zone_list.append(skill_zone)
        if len(skill_zone_list) > 0:
            skill_zones_str = ", ".join(
                str(skill_zone) for skill_zone in skill_zone_list
            )
            raise ValidationError(f"{candidate} already exists in {skill_zones_str}.")

            # cleaned_data['skill_zone_id'] =skill_zone
        return cleaned_data

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html


class RejectReasonForm(ModelForm):
    """
    RejectReasonForm
    """

    cols = {"title": 12, "description": 12, "company_id": 12}

    verbose_name = _("Rejection Reason")

    class Meta:
        model = RejectReason
        fields = "__all__"
        exclude = ["is_active"]

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html


class RejectedCandidateForm(ModelForm):
    """
    RejectedCandidateForm
    """

    verbose_name = "Rejected Candidate"

    cols = {"description": 12}
    # Hidden per PRD (remark only, no reason picker):
    # cols = {"reject_reason_id": 12, "description": 12}

    class Meta:
        model = RejectedCandidate
        fields = "__all__"
        # Remark only (PRD): no rejection-reason picker.
        exclude = ["is_active", "reject_reason_id"]

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Hidden per PRD (remark only): self.fields["reject_reason_id"].empty_label = None
        self.fields["candidate_id"].widget = self.fields["candidate_id"].hidden_widget()
        # The PRD makes the remark mandatory on every rejection: it is the
        # record of why this person was stopped, and the service refuses a
        # blank one (StageRemarkRequired). Required here too so the user sees a
        # field error instead of a page-level message.
        self.fields["description"].required = True
        self.fields["description"].label = _("Remark")


class MoveForwardForm(forms.Form):
    """
    The remark the PRD requires on every single-candidate Move Forward.

    Deliberately not a ModelForm: the remark lives on the stage-change audit
    event, not in a table of its own, so there is no model to bind to.
    """

    verbose_name = _("Move Forward")

    cols = {"remark": 12}

    remark = forms.CharField(
        label=_("Remark"),
        required=True,
        widget=forms.Textarea(attrs={"rows": 3, "class": "oh-input w-100"}),
        help_text=_(
            "Why is this candidate moving forward? Recorded in their history."
        ),
    )

    def as_p(self, *args, **kwargs):
        return render_to_string("common_form.html", {"form": self})


class ScheduleInterviewForm(BaseModelForm):
    """
    ScheduleInterviewForm
    """

    cols = {
        "interview_date": 12,
        "interview_time": 12,
        "candidate_id": 12,
        "description": 12,
        "employee_id": 12,
    }

    verbose_name = "Schedule Interview"

    class Meta:
        model = InterviewSchedule
        fields = "__all__"
        exclude = ["is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interview_date"].widget = forms.DateInput(
            attrs={"type": "date", "class": "oh-input w-100"}
        )
        if self.instance.pk:
            # Update mode: keep this permissive and normalize manually in clean()
            # so unchanged browser values do not fail with "Enter a valid time".
            self.fields["interview_time"] = forms.CharField(
                required=False,
                widget=forms.TimeInput(
                    attrs={"type": "time", "class": "oh-input w-100"}
                ),
            )
        else:
            self.fields["interview_time"] = forms.TimeField(
                required=True,
                input_formats=["%H:%M", "%I:%M %p", "%H:%M:%S", "%I:%M:%S %p"],
                widget=forms.TimeInput(
                    attrs={"type": "time", "class": "oh-input w-100"}
                ),
            )
        candidate_attr = {
            "hx-include": "#InterviewCreateForm",
            "hx-target": "#id_employee_id_parent_div",
            "hx-get": "/recruitment/get-interview-managers/",
            "hx-swap": "innerHTML",
            "hx-select": "#id_employee_id_parent_div",
            "hx-trigger": "change, load delay:300ms",
        }

        if self.instance.pk:
            candidate_attr["hx-get"] += f"?pk={self.instance.pk}"

        self.fields["candidate_id"].widget.attrs.update(candidate_attr)

    def clean(self):

        instance = self.instance
        cleaned_data = super().clean() or {}
        interview_date = cleaned_data.get("interview_date")
        interview_time = cleaned_data.get("interview_time")
        raw_interview_time = (self.data.get("interview_time") or "").strip()
        managers = cleaned_data.get("employee_id") or []

        if instance.pk:
            parsed_time = None
            if raw_interview_time:
                for fmt in (
                    "%H:%M",
                    "%I:%M %p",
                    "%H:%M:%S",
                    "%I:%M:%S %p",
                    "%I:%M%p",
                    "%H:%M:%S.%f",
                ):
                    try:
                        parsed_time = datetime.strptime(raw_interview_time, fmt).time()
                        break
                    except ValueError:
                        continue
            cleaned_data["interview_time"] = parsed_time or instance.interview_time
            interview_time = cleaned_data.get("interview_time")

        if not instance.pk and interview_date and interview_date < date.today():
            self.add_error("interview_date", _("Interview date cannot be in the past."))

        if not instance.pk and interview_time:
            now = datetime.now().time()
            if (
                not instance.pk
                and interview_date == date.today()
                and interview_time < now
            ):
                self.add_error(
                    "interview_time", _("Interview time cannot be in the past.")
                )

        if managers and apps.is_installed("leave"):
            from leave.models import LeaveRequest

            leave_employees = LeaveRequest.objects.filter(
                employee_id__in=managers, status="approved"
            )
        else:
            leave_employees = []

        employees = [
            leave.employee_id.get_full_name()
            for leave in leave_employees
            if interview_date and interview_date in leave.requested_dates()
        ]

        if employees:
            self.add_error(
                "employee_id", _(f"{employees} have approved leave on this date")
            )

        return cleaned_data

    def as_p(self, *args, **kwargs):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html


class SkillsForm(ModelForm):
    cols = {
        "title": 12,
    }

    class Meta:
        model = Skill
        fields = ["title"]


class ResumeForm(ModelForm):
    class Meta:
        model = Resume
        fields = ["file", "recruitment_id"]
        widgets = {"recruitment_id": forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].widget.attrs.update(
            {
                "onchange": "submitForm($(this))",
            }
        )


class CandidateDocumentRequestForm(ModelForm):
    class Meta:
        model = CandidateDocumentRequest
        fields = "__all__"
        exclude = ["is_active"]


class CandidateDocumentUpdateForm(ModelForm):
    """form to Update a Document"""

    verbose_name = "CandidateDocument"

    class Meta:
        model = CandidateDocument
        fields = "__all__"
        exclude = ["is_active", "document_request_id"]


class CandidateDocumentRejectForm(ModelForm):
    """form to add rejection reason while rejecting a Document"""

    class Meta:
        model = CandidateDocument
        fields = ["reject_reason"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reject_reason"].widget.attrs["required"] = True


class CandidateDocumentForm(ModelForm):
    """form to create a new Document"""

    verbose_name = "Document"

    class Meta:
        model = CandidateDocument
        fields = "__all__"
        exclude = ["document_request_id", "status", "reject_reason", "is_active"]
        widgets = {
            "employee_id": forms.HiddenInput(),
        }

    def as_p(self):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("horilla_form.html", context)
        return table_html


class StageChangeForm(forms.ModelForm):
    """
    StageChangeForm
    """

    class Meta:
        """
        Meta class for additional options
        """

        model = Candidate
        fields = [
            "stage_id",
        ]


class LinkedInAccountForm(BaseModelForm):
    """
    LinkedInAccount form
    """

    class Meta:
        model = LinkedInAccount
        fields = [
            "username",
            "email",
            "api_token",
            "is_active",
            "company_id",
        ]
