"""
models.py

This module is used to register models for recruitment app

"""

import ast
import json
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import urlencode
from uuid import uuid4

import django
import requests
from django import forms
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.storage import default_storage
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Avg, Min
from django.templatetags.static import static
from django.urls import reverse, reverse_lazy
from django.utils import timezone as tz
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company, JobPosition
from employee.models import Employee
from horilla.horilla_middlewares import _thread_locals
from horilla.models import HorillaModel, upload_path
from horilla_audit.methods import get_diff
from horilla_audit.models import HorillaAuditInfo, HorillaAuditLog
from horilla_auth.models import HorillaUser
from horilla_views.cbv_methods import render_template

# Create your models here.


def validate_mobile(value):
    """
    This method is used to validate the mobile number using regular expression
    """
    pattern = r"^\+[0-9 ]+$|^[0-9 ]+$"

    if re.match(pattern, value) is None:
        if "+" in value:
            raise forms.ValidationError(
                "Invalid input: Plus symbol (+) should only appear at the beginning \
                    or no other characters allowed."
            )
        raise forms.ValidationError(
            _("Invalid input: Only digits and spaces are allowed.")
        )


def validate_pdf(value):
    """
    This method is used to validate pdf
    """
    ext = os.path.splitext(value.name)[1]  # Get file extension
    if ext.lower() != ".pdf":
        raise ValidationError(_("File must be a PDF."))


def html_to_plain_text(value):
    """
    Plain text from possibly-HTML input: paragraphs and <br> become line
    breaks, list items become "- " lines, other tags are dropped and entities
    (&amp; &nbsp; ...) decoded. Text that has no tags is returned unchanged.
    """
    import html
    import re

    from django.utils.html import strip_tags

    if not value or "<" not in value:
        return value
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|ul|ol|tr)>", "\n", text)
    text = html.unescape(strip_tags(text)).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


#: Candidate Pool document limit (PRD: PDF only, 15 MB per file).
CANDIDATE_DOCUMENT_MAX_BYTES = 15 * 1024 * 1024

#: Canonical Stage.stage_type values. The PRD's pipeline order is
#: Applied -> custom stages -> Final HR Round -> Hired. Referenced by
#: recruitment.signals, recruitment.cbv.stage_view and the services layer so
#: the raw strings are not repeated across the app.
STAGE_APPLIED = "applied"
STAGE_INITIAL = "initial"
STAGE_FINAL_HR_ROUND = "final_hr_round"
STAGE_HIRED = "hired"

#: The two terminal stages, as (stage_type, default name, sequence).
#:
#: Seeded for every job opening: the hiring handoff is gated on Final HR Round,
#: and hire_candidate() moves the candidate into the Hired stage -- which is
#: also what Onboarding consumes (onboarding/cbv/onboarding_candidates.py
#: selects on stage_type="hired").
#:
#: The sequences sit far above any realistic custom-stage sequence so the pair
#: keeps sorting last without being renumbered whenever a custom stage is
#: added. Names are plain ASCII rather than translated strings because
#: Stage.Meta.unique_together is ("recruitment_id", "stage") -- a translated
#: name would make that key locale-dependent.
TERMINAL_STAGE_DEFAULTS = (
    (STAGE_FINAL_HR_ROUND, "Final HR Round", 9998),
    (STAGE_HIRED, "Hired", 9999),
)
#: The Rejected parking stage always sorts after Hired.
REJECTED_STAGE_SEQUENCE = 10000

TERMINAL_STAGE_TYPES = frozenset(
    stage_type for stage_type, _name, _sequence in TERMINAL_STAGE_DEFAULTS
)
TERMINAL_STAGE_SEQUENCES = {
    stage_type: sequence for stage_type, _name, sequence in TERMINAL_STAGE_DEFAULTS
}
#: Every PDF begins with this signature.
PDF_MAGIC = b"%PDF-"

#: Upper bound on a file-upload question's "Max files allowed". Not a PRD
#: number -- it exists so a typo in the HR form cannot ask a candidate for
#: hundreds of uploads, and so the candidate-side counter stays meaningful.
MAX_FILES_CEILING = 20

#: What a question that only recorded "multiple files allowed" is worth once
#: the cap became a number. The old boolean carried no count, so this is the
#: migration's documented choice, not recovered data.
DEFAULT_MAX_FILES_WHEN_MULTIPLE = 5


def validate_candidate_document(value):
    """
    Validate an uploaded candidate document: real PDF, within the size limit.

    The filename extension and the browser-supplied Content-Type are both
    attacker-controlled, so neither is sufficient on its own -- "payload.html"
    renamed to "payload.pdf" passes an extension check and can then be served
    back as HTML. This reads the file's own leading bytes instead.

    No magic-byte library is installed in this project, and a PDF's signature is
    a fixed 5-byte prefix, so the check is done directly rather than by adding a
    dependency. The read is bounded and the file pointer is restored, so this is
    safe to call from a form validator before the file is saved.
    """
    if value is None:
        return

    size = getattr(value, "size", None)
    if size is not None and size > CANDIDATE_DOCUMENT_MAX_BYTES:
        raise ValidationError(
            _("File is larger than the %(limit)s MB limit.")
            % {"limit": CANDIDATE_DOCUMENT_MAX_BYTES // (1024 * 1024)}
        )

    # Extension first: cheap, and keeps the error message obvious for honest
    # mistakes. It is a convenience, never the security boundary.
    ext = os.path.splitext(getattr(value, "name", "") or "")[1].lower()
    if ext != ".pdf":
        raise ValidationError(_("File must be a PDF."))

    try:
        position = value.tell() if hasattr(value, "tell") else 0
        value.seek(0)
        header = value.read(len(PDF_MAGIC))
        value.seek(position)
    except Exception as error:  # unreadable upload
        raise ValidationError(_("The uploaded file could not be read.")) from error

    if not header.startswith(PDF_MAGIC):
        raise ValidationError(
            _("File must be a PDF. The uploaded file's contents are not a PDF.")
        )


def validate_image(value):
    """
    This method is used to validate the image
    """
    return value


def candidate_photo_upload_path(instance, filename):
    ext = filename.split(".")[-1]
    filename = f"{instance.name.replace(' ', '_')}_{filename}_{uuid4()}.{ext}"
    return os.path.join("recruitment/profile/", filename)


#: Form 1 (public candidate application) and Form 2 (Final HR Round -> Hired
#: handoff) keep independent question banks and templates. A question created
#: for one must never appear in the other, which is a filter on this field
#: rather than a second set of models.
FORM_TYPES = [
    ("form1", _("Application Form")),
    ("form2", _("Hiring Handoff Form")),
]
FORM_ONE = "form1"
FORM_TWO = "form2"


class SurveyTemplate(HorillaModel):
    """
    SurveyTemplate Model
    """

    form_type = models.CharField(
        max_length=10, choices=FORM_TYPES, default=FORM_ONE, verbose_name=_("Form")
    )
    title = models.CharField(max_length=50, unique=True)
    description = models.TextField(null=True, blank=True)
    is_general_template = models.BooleanField(default=False, editable=False)
    company_id = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Company"),
    )
    objects = HorillaCompanyManager("company_id")

    def __str__(self) -> str:
        return self.title

    class Meta:
        verbose_name = _("Survey Template")
        verbose_name_plural = _("Survey Templates")
        ordering = ["-id"]


class Skill(HorillaModel):
    title = models.CharField(max_length=100)

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        title = self.title
        self.title = title.capitalize()
        super().save(*args, **kwargs)

    def get_sino(self):
        """
        for get serial nos
        """
        all_instances = list(Skill.objects.order_by("id"))
        sino = all_instances.index(self) + 1
        return sino

    def get_update_url(self):
        """
        This method to get update url
        """
        url = reverse_lazy("settings-update-skills", kwargs={"pk": self.pk})
        return url

    def get_delete_url(self):
        """
        This method to get delete url
        """
        base_url = reverse_lazy("delete-skills")
        skill_id = self.pk
        url = f"{base_url}?ids={skill_id}"
        return url

    def get_delete_instance(self):
        """
        to get instance for delete
        """

        return self.pk

    def __str__(self) -> str:
        return f"{self.title}"

    class Meta:
        verbose_name = _("Skill")
        verbose_name_plural = _("Skills")


class Recruitment(HorillaModel):
    """
    Recruitment model
    """

    title = models.CharField(
        max_length=50, null=True, blank=True, verbose_name=_("Title")
    )
    description = models.TextField(null=True, verbose_name=_("Description"))
    is_event_based = models.BooleanField(
        default=False,
        help_text=_("To start recruitment for multiple job positions"),
    )
    closed = models.BooleanField(
        default=False,
        help_text=_(
            "To close the recruitment, If closed then not visible on pipeline view."
        ),
        verbose_name=_("Closed"),
    )
    is_published = models.BooleanField(
        # Mirror of `status == PUBLISHED`, so it must start False: a new job
        # opening is born DRAFT. The old default of True meant creation
        # implied publication, which put un-reviewed drafts straight onto the
        # public Open Jobs listing. Written only by apply_status().
        default=False,
        help_text=_(
            "Derived from the job opening's status -- true only while it is "
            "Published. Do not set directly; use the lifecycle service."
        ),
        verbose_name=_("Is Published"),
    )
    open_positions = models.ManyToManyField(
        JobPosition,
        related_name="open_positions",
        blank=True,
        verbose_name=_("Job Position"),
    )
    job_position_id = models.ForeignKey(
        JobPosition,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="recruitment",
        verbose_name=_("Job Position"),
        editable=False,
    )
    vacancy = models.IntegerField(default=0, null=True, verbose_name=_("Vacancy"))
    recruitment_managers = models.ManyToManyField(Employee, verbose_name=_("Managers"))
    survey_templates = models.ManyToManyField(
        SurveyTemplate, blank=True, verbose_name=_("Survey Templates")
    )
    company_id = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name=_("Company"),
    )
    start_date = models.DateField(
        default=django.utils.timezone.now, verbose_name=_("Start Date")
    )
    end_date = models.DateField(blank=True, null=True, verbose_name=_("End Date"))
    skills = models.ManyToManyField(Skill, blank=True, verbose_name=_("Skills"))
    linkedin_account_id = models.ForeignKey(
        "recruitment.LinkedInAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name=_("LinkedIn Account"),
    )
    linkedin_post_id = models.CharField(max_length=150, null=True, blank=True)
    publish_in_linkedin = models.BooleanField(
        default=True,
        help_text=_(
            "To publish a recruitment in Linkedin, if active is false then it \
            will not post on LinkedIn."
        ),
        verbose_name=_("Post on LinkedIn"),
    )
    # ------------------------------------------------------------------
    # Krew job-opening lifecycle
    #
    # `status` is the single authoritative lifecycle state:
    #     DRAFT -> REVIEW -> PUBLISHED -> CLOSED   (CLOSED is terminal)
    # plus REVIEW -> DRAFT ("sent back for changes") and REMOVED (take-down).
    #
    # `is_published`, `closed` and `is_active` above are kept ONLY as
    # backward-compatible mirrors of this field, derived in apply_status()
    # so the existing read sites (public listing, filters, dashboards,
    # reports, scheduler) keep working. They are never independent truth --
    # write lifecycle state through recruitment.services.job_opening only.
    # ------------------------------------------------------------------
    class Status(models.TextChoices):
        """Job-opening lifecycle states."""

        DRAFT = "DRAFT", _("Draft")
        REVIEW = "REVIEW", _("In Review")
        PUBLISHED = "PUBLISHED", _("Published")
        CLOSED = "CLOSED", _("Closed")
        REMOVED = "REMOVED", _("Removed")

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        verbose_name=_("Status"),
    )
    # Internal-only pay range. Never shown to candidates or on the public
    # listing (PRD): it exists so the figure is available for reference at the
    # Final HR Round -> Hired handoff.
    budget_min = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Budget (minimum)"),
        help_text=_("Internal only. Never shown to candidates."),
    )
    budget_max = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Budget (maximum)"),
        help_text=_("Internal only. Never shown to candidates."),
    )
    # Per-opening, not a company-wide default (PRD): whether an applicant must
    # verify their email and mobile while applying.
    contact_verification_required = models.BooleanField(
        default=False,
        verbose_name=_("Require contact verification"),
        help_text=_(
            "When enabled, applicants are asked to verify their email address "
            "and mobile number. Verification never blocks submission."
        ),
    )
    submitted_for_review_at = models.DateTimeField(null=True, blank=True, editable=False)
    submitted_for_review_by = models.ForeignKey(
        HorillaUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="recruitment_submitted_for_review",
    )
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    published_by = models.ForeignKey(
        HorillaUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="recruitment_published",
    )
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    closed_by = models.ForeignKey(
        HorillaUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="recruitment_closed",
    )
    removed_at = models.DateTimeField(null=True, blank=True, editable=False)
    removed_by = models.ForeignKey(
        HorillaUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="recruitment_removed",
    )

    objects = HorillaCompanyManager()
    default = models.manager.Manager()
    optional_profile_image = models.BooleanField(
        default=False,
        help_text=_("Profile image not mandatory for candidate creation"),
        verbose_name=_("Optional Profile Image"),
    )
    optional_resume = models.BooleanField(
        default=False,
        help_text=_("Resume not mandatory for candidate creation"),
        verbose_name=_("Optional Resume"),
    )

    class Meta:
        """
        Meta class to add the additional info
        """

        unique_together = [
            (
                "job_position_id",
                "start_date",
            ),
            ("job_position_id", "start_date", "company_id"),
        ]
        permissions = (("archive_recruitment", "Archive Recruitment"),)
        verbose_name = _("Recruitment")
        verbose_name_plural = _("Recruitments")
        ordering = ["-id"]

    def apply_status(self, status, actor=None):
        """
        Set the lifecycle status and re-derive the legacy mirror fields.

        Call this from recruitment.services.job_opening only -- it does not
        validate the transition itself, it just applies one. `actor` stamps
        the who/when columns for the state being entered; it is None for
        automated transitions (the audit event records the Horilla Bot
        instead).

        Mirrors:
            is_published = status is PUBLISHED
            closed       = status is CLOSED or REMOVED
            is_active    = status is not REMOVED
        """
        status = self.Status(status)
        now = tz.now()
        self.status = status

        if status == self.Status.REVIEW:
            self.submitted_for_review_at = now
            self.submitted_for_review_by = actor
        elif status == self.Status.PUBLISHED:
            self.published_at = now
            self.published_by = actor
        elif status == self.Status.CLOSED:
            self.closed_at = now
            self.closed_by = actor
        elif status == self.Status.REMOVED:
            self.removed_at = now
            self.removed_by = actor

        self.is_published = status == self.Status.PUBLISHED
        self.closed = status in (self.Status.CLOSED, self.Status.REMOVED)
        self.is_active = status != self.Status.REMOVED
        return self

    def accepts_new_candidates(self):
        """
        True when a new candidate/application may be attached.

        Only a PUBLISHED opening accepts new candidates. Candidates already
        attached to a CLOSED/REMOVED opening are unaffected.
        """
        return self.status == self.Status.PUBLISHED

    def total_hires(self):
        """
        This method is used to get the count of
        hired candidates
        """
        return self.candidate.filter(hired=True).count()

    def __str__(self):
        title = (
            f"{self.job_position_id.job_position} {self.start_date}"
            if self.title is None and self.job_position_id
            else self.title
        )
        return str(title)

    def clean(self):
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_min > self.budget_max
        ):
            raise ValidationError(
                {"budget_max": _("Maximum budget cannot be less than the minimum.")}
            )
        if self.title is None:
            raise ValidationError({"title": _("This field is required")})
        if self.is_published:
            if self.vacancy <= 0:
                raise ValidationError(
                    {
                        "vacancy": _(
                            "Vacancy must be greater than zero if the recruitment is publishing."
                        )
                    }
                )

        if self.end_date is not None and (
            self.start_date is not None and self.start_date > self.end_date
        ):
            raise ValidationError(
                {"end_date": _("End date cannot be less than start date.")}
            )
        return super().clean()

    def save(self, *args, **kwargs):
        # The description is stored as plain text, never HTML -- whichever
        # form, API or import wrote it.
        self.description = html_to_plain_text(self.description)
        if not self.publish_in_linkedin:
            self.linkedin_account_id = None
            self.linkedin_post_id = None
        super().save(*args, **kwargs)  # Save the Recruitment instance first
        if self.is_event_based and self.open_positions is None:
            raise ValidationError({"open_positions": _("This field is required")})
        # Keep open_positions in sync on save — never in __str__ (that caused
        # SQLite write locks on every grouped stage list render).
        if not self.is_event_based and self.job_position_id_id is not None and self.pk:
            self.open_positions.add(self.job_position_id)

    def ordered_stages(self):
        """
        This method will returns all the stage respectively to the ascending order of stages
        """
        return self.stage_set.order_by("sequence")

    def recruitment_column(self):
        """
        This method for get custom column for recruitment.
        """

        return render_template(
            path="cbv/recruitment/recruitment_col.html",
            context={"instance": self},
        )

    def recruitment_detail_view(self):
        """
        detail view
        """
        url = reverse("recruitment-detail-view", kwargs={"pk": self.pk})
        return url

    def managers_column(self):
        """
        This method for get custom column for managers.
        """

        return render_template(
            path="cbv/recruitment/managers_col.html",
            context={"instance": self},
        )

    def managers_detail(self):
        """
        manager in detail view

        Returns the Employee queryset (rather than pre-rendered HTML) so the
        detail-view template's `linkify` filter can turn each manager into a
        related-object link to the Employee Related Detail View.
        """
        return self.recruitment_managers.all()

    def managers(self):
        manager_list = self.recruitment_managers.all()
        formatted_managers = [
            f"<div>{i + 1}. {manager}</div>" for i, manager in enumerate(manager_list)
        ]
        return "".join(formatted_managers)

    def detail_actions(self):
        """
        This method for get custom column for managers.
        """

        return render_template(
            path="cbv/recruitment/detail_action.html",
            context={"instance": self},
        )

    def open_job_col(self):
        """
        This method for get custom column for open jobs.
        """

        return render_template(
            path="cbv/recruitment/open_jobs.html",
            context={"instance": self},
        )

    def open_job_detail(self):
        """
        open jobs in detail view
        """
        jobs = self.open_positions.all()
        if jobs:
            jobs_names_string = "<br>".join([str(job) for job in jobs])
            return (
                f'<span class="oh-timeoff-modal__stat-count">{jobs_names_string}</span>'
            )
        else:
            return ""

    def tot_hires(self):
        """
        This method for get custom column for Total hires.
        """

        return render_template(
            path="cbv/recruitment/total_hires.html",
            context={"instance": self},
        )

    def status_col(self):
        """
        Lifecycle state for the list/detail Status column.

        Reads the authoritative `status` field, so the column now shows the
        real state (Draft / In Review / Published / Closed / Removed) instead
        of collapsing everything into Open-vs-Closed off the mirror flag.
        """
        return self.get_status_display()

    def rec_actions(self):
        """
        This method for get custom column for actions.
        """

        return render_template(
            path="cbv/recruitment/actions.html",
            context={"instance": self},
        )

    def get_avatar(self):
        """
        Method will retun the api to the avatar or path to the profile image
        """
        url = f"https://ui-avatars.com/api/?name={self.title}&background=random"
        return url

    def is_vacancy_filled(self):
        """
        This method is used to check wether the vaccancy for the recruitment is completed or not
        """
        hired_stage = Stage.objects.filter(
            recruitment_id=self, stage_type="hired"
        ).first()
        if hired_stage:
            hired_candidate = hired_stage.candidate_set.all().exclude(canceled=True)
            if len(hired_candidate) >= self.vacancy:
                return True


class Stage(HorillaModel):
    """
    Stage model
    """

    #: "final_hr_round" is an explicit stage type rather than an inference from
    #: position, because the Candidate Pool reports it as its own status bucket
    #: and guessing "the last stage before hired" would silently change meaning
    #: whenever a pipeline is reordered. Every pre-existing type keeps working.
    stage_types = [
        (STAGE_INITIAL, _("Initial")),
        (STAGE_APPLIED, _("Applied")),
        ("test", _("Test")),
        ("interview", _("Interview")),
        (STAGE_FINAL_HR_ROUND, _("Final HR Round")),
        ("cancelled", _("Cancelled")),
        (STAGE_HIRED, _("Hired")),
    ]
    recruitment_id = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="stage_set",
        verbose_name=_("Recruitment"),
    )
    stage_managers = models.ManyToManyField(Employee, verbose_name=_("Stage Managers"))
    stage = models.CharField(max_length=50, verbose_name=_("Stage"))
    stage_type = models.CharField(
        max_length=20,
        choices=stage_types,
        default="interview",
        verbose_name=_("Stage Type"),
    )
    sequence = models.IntegerField(null=True, default=0)
    objects = HorillaCompanyManager(related_company_field="recruitment_id__company_id")
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )

    class Meta:
        """
        Meta class to add the additional info
        """

        permissions = (("archive_Stage", "Archive Stage"),)
        unique_together = ["recruitment_id", "stage"]
        ordering = ["sequence"]
        verbose_name = _("Stage")
        verbose_name_plural = _("Stages")

    def __str__(self):
        return f"{self.stage} - ({self.recruitment_id.title})"

    def get_stage_managers_update_url(self):
        return reverse("stage-managers-update", kwargs={"pk": self.pk})

    @property
    def is_fixed(self):
        """Applied, Final HR Round, Hired and Rejected: never renamed, moved or removed."""
        return self.stage_type in (
            STAGE_APPLIED, STAGE_FINAL_HR_ROUND, STAGE_HIRED, "cancelled"
        )

    @property
    def is_rejected_stage(self):
        return self.stage_type == "cancelled"

    # --- PRD stage rules, enforced on every save/delete path (forms, API,
    # generic views), not only by which buttons the UI shows. ----------------

    def save(self, *args, **kwargs):
        if self.pk:
            original = (
                type(self)._base_manager.filter(pk=self.pk)
                .values("stage", "stage_type", "sequence", "recruitment_id_id")
                .first()
            )
            if original:
                if original["recruitment_id_id"] != self.recruitment_id_id:
                    raise ValidationError(
                        _("A stage cannot be moved to another job opening.")
                    )
                fixed_types = (
                    STAGE_APPLIED, STAGE_FINAL_HR_ROUND, STAGE_HIRED, "cancelled"
                )
                if original["stage_type"] in fixed_types:
                    if (
                        original["stage"] != self.stage
                        or original["stage_type"] != self.stage_type
                        or original["sequence"] != self.sequence
                    ):
                        raise ValidationError(
                            _(
                                "Applied, Final HR Round and Hired are fixed: they "
                                "cannot be renamed or reordered."
                            )
                        )
                else:
                    if self.stage_type in fixed_types:
                        raise ValidationError(
                            _("A custom stage cannot become a fixed stage.")
                        )
                    if original["sequence"] != self.sequence:
                        lowest_terminal = min(TERMINAL_STAGE_SEQUENCES.values())
                        applied = (
                            type(self)._base_manager.filter(
                                recruitment_id=self.recruitment_id_id,
                                stage_type=STAGE_APPLIED,
                            )
                            .values_list("sequence", flat=True)
                            .first()
                            or 0
                        )
                        if not (
                            self.sequence is not None
                            and applied < self.sequence < lowest_terminal
                        ):
                            raise ValidationError(
                                _(
                                    "A custom stage must stay between Applied "
                                    "and Final HR Round."
                                )
                            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_fixed:
            raise ValidationError(
                _(
                    "Applied, Final HR Round and Hired are fixed stages and "
                    "cannot be removed."
                )
            )
        # Removing a stage is drive-level (PRD). Checked here too so the
        # generic delete and the API follow it; background code has no request.
        request = getattr(_thread_locals, "request", None)
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            from recruitment.services.authorization import is_drive_manager

            if not is_drive_manager(user, self.recruitment_id):
                raise ValidationError(
                    _("Only this job opening's managers can remove stages.")
                )
        return super().delete(*args, **kwargs)

    def active_candidates(self):
        """
        This method is used to get all the active candidate like related objects
        """
        return {
            "all": Candidate.objects.filter(
                stage_id=self, canceled=False, is_active=True
            )
        }

    def stage_detail_view(self):
        """
        detail view
        """
        url = reverse("stage-detail-view", kwargs={"pk": self.pk})
        return url

    def detail_action(self):
        """
        For answerable employees  column
        """

        return render_template(
            path="cbv/stages/detail_action.html",
            context={"instance": self},
        )

    def title_col(self):
        """
        This method for get custome coloumn for title.
        """
        return render_template(
            path="cbv/stages/title.html",
            context={"instance": self},
        )

    def managers_count(self):
        """
        Active stage manager count from prefetch cache when available.
        """
        cache = getattr(self, "_prefetched_objects_cache", None)
        if cache is not None and "stage_managers" in cache:
            return len(cache["stage_managers"])
        return self.stage_managers.filter(is_active=True).count()

    def managers_col(self):
        """
        This method for get custome coloumn for managers.
        """

        return render_template(
            path="cbv/stages/managers.html",
            context={"instance": self},
        )

    def get_avatar(self):
        """
        Method will retun the api to the avatar or path to the profile image
        """
        url = (
            f"https://ui-avatars.com/api/?name={self.recruitment_id}&background=random"
        )
        return url

    def detail_managers_col(self):
        """
        Manager in detail view
        """
        employees = self.stage_managers.all()
        employee_names_string = "<br>".join([str(employee) for employee in employees])
        return employee_names_string

    def actions_col(self):
        """
        This method for get custome coloumn for actions.
        """

        return render_template(
            path="cbv/stages/actions.html",
            context={"instance": self},
        )

    def get_type(self):
        """
        Display label for this stage's type.

        Reads the field's own choices rather than repeating them: the local
        copy this replaced listed only five of the seven types, so "Applied"
        and "Final HR Round" both rendered blank.
        """
        return self.get_stage_type_display()

    def get_stage_update_url(self):
        """
        This method to get update url
        """
        return reverse("stage-update-pipeline", kwargs={"pk": self.id})

    def get_add_candidate_url(self):
        """
        This method to get add candidate url
        """
        return f'{reverse_lazy("add-candidate-to-stage")}?stage_id={self.id}'

    def get_send_email_url(self):
        """
        This method to get send email url
        """
        return f'{reverse_lazy("send-mail")}?stage_id={self.id}'

    def get_delete_url(self):
        """
        This method to get delete url
        """
        return f"{reverse_lazy('generic-delete')}?model=recruitment.Stage&pk={self.pk}"


def candidate_upload_path(instance, filename):
    """
    Generates a unique file path for candidate profile & resume uploads.
    """
    ext = filename.split(".")[-1]
    name_slug = slugify(instance.name) or "candidate"
    unique_filename = f"{name_slug}-{uuid4().hex[:8]}.{ext}"
    return f"recruitment/{name_slug}/{unique_filename}"


class Candidate(HorillaModel):
    """
    Candidate model
    """

    choices = [("male", _("Male")), ("female", _("Female")), ("other", _("Other"))]
    offer_letter_statuses = [
        ("not_sent", _("Not Sent")),
        ("sent", _("Sent")),
        ("accepted", _("Accepted")),
        ("rejected", _("Rejected")),
        ("joined", _("Joined")),
    ]
    source_choices = [
        ("application", _("Application Form")),
        ("software", _("Inside software")),
        ("other", _("Other")),
    ]
    name = models.CharField(max_length=100, null=True, verbose_name=_("Name"))
    profile = models.ImageField(upload_to=upload_path, null=True)  # 853
    portfolio = models.URLField(max_length=200, blank=True, verbose_name=_("Portfolio"))
    recruitment_id = models.ForeignKey(
        Recruitment,
        on_delete=models.PROTECT,
        null=True,
        related_name="candidate",
        verbose_name=_("Recruitment"),
    )
    job_position_id = models.ForeignKey(
        JobPosition,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name=_("Job Position"),
    )
    stage_id = models.ForeignKey(
        Stage,
        on_delete=models.PROTECT,
        null=True,
        verbose_name=_("Stage"),
    )
    converted_employee_id = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="candidate_get",
        verbose_name=_("Employee"),
    )
    schedule_date = models.DateTimeField(
        blank=True, null=True, verbose_name=_("Schedule date")
    )
    email = models.EmailField(max_length=254, verbose_name=_("Email"))
    mobile = models.CharField(
        max_length=15,
        blank=True,
        validators=[
            validate_mobile,
        ],
        verbose_name=_("Mobile"),
    )
    resume = models.FileField(
        upload_to=upload_path,  # 853
        validators=[
            # validate_pdf,
            # PRD: PDF only, 15 MB -- checked by content, not just the file
            # extension, the same check every other candidate document gets.
            validate_candidate_document,
        ],
        verbose_name=_("Resume"),
    )
    referral = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="candidate_referral",
        verbose_name=_("Referral"),
    )
    address = models.TextField(
        null=True, blank=True, verbose_name=_("Address"), max_length=255
    )
    country = models.CharField(
        max_length=30, null=True, blank=True, verbose_name=_("Country")
    )
    dob = models.DateField(null=True, blank=True, verbose_name=_("Date of Birth"))
    state = models.CharField(
        max_length=30, null=True, blank=True, verbose_name=_("State")
    )
    city = models.CharField(
        max_length=30, null=True, blank=True, verbose_name=_("City")
    )
    zip = models.CharField(
        max_length=30, null=True, blank=True, verbose_name=_("Zip Code")
    )
    gender = models.CharField(
        max_length=15,
        choices=choices,
        null=True,
        default="male",
        verbose_name=_("Gender"),
    )
    source = models.CharField(
        max_length=20,
        choices=source_choices,
        null=True,
        blank=True,
        verbose_name=_("Source"),
    )
    start_onboard = models.BooleanField(
        default=False, verbose_name=_("Start Onboarding")
    )
    hired = models.BooleanField(default=False, verbose_name=_("Hired"))
    canceled = models.BooleanField(default=False, verbose_name=_("Canceled"))
    converted = models.BooleanField(default=False, verbose_name=_("Converted"))
    joining_date = models.DateField(
        blank=True, null=True, verbose_name=_("Joining Date")
    )
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )
    sequence = models.IntegerField(null=True, default=0)

    probation_end = models.DateField(null=True, editable=False)
    offer_letter_status = models.CharField(
        max_length=10,
        choices=offer_letter_statuses,
        default="not_sent",
        editable=False,
        verbose_name=_("Offer Letter Status"),
    )
    #: Owning tenant. Authoritative for company scoping, and the reason this
    #: model can represent a candidate who has no job opening at all.
    #:
    #: Previously company was reachable only through recruitment_id, so a
    #: candidate with recruitment_id=NULL had no company owner and the manager
    #: (which was declared with no company path) filtered nothing -- every
    #: tenant saw every candidate. Kept nullable because the column is added to
    #: a populated table; migration 0014 backfills it from the job opening and
    #: reports any row it cannot resolve rather than guessing.
    #:
    #: Always derived server-side: Candidate.save() forces it to the job
    #: opening's company whenever one is set, and the service derives it from
    #: the acting user otherwise. A client-supplied value is never trusted.
    company_id = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="candidates",
        verbose_name=_("Company"),
    )
    #: Set only when BOTH email and mobile have been verified for this
    #: application. Verification never blocks submission, so this stays null on
    #: an unverified application.
    contact_verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    #: Agreed CTC, captured fresh by HR on the hiring handoff form.
    offered_ctc = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name=_("CTC")
    )
    #: Budget reference recorded at the hiring handoff (PRD Form 2): prefilled
    #: from the job opening's budget, editable by the stage/recruitment manager.
    handoff_budget_min = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name=_("Budget (minimum)"),
    )
    handoff_budget_max = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name=_("Budget (maximum)"),
    )
    objects = HorillaCompanyManager("company_id")
    last_updated = models.DateField(null=True, auto_now=True)

    converted_employee_id.exclude_from_automation = True
    mail_to_related_fields = [
        ("stage_id__stage_managers__get_mail", "Stage Managers"),
        ("recruitment_id__recruitment_managers__get_mail", "Recruitment Managers"),
    ]
    hired_date = models.DateField(null=True, blank=True, editable=False)

    def __str__(self):
        return f"{self.name}"

    def stage_drop_down(self):
        """
        Stage drop down
        """
        request = getattr(_thread_locals, "request", None)
        all_rec_stages = getattr(request, "all_rec_stages", {})
        if all_rec_stages.get(self.stage_id.recruitment_id.pk) is None:
            stages = Stage.objects.filter(recruitment_id=self.stage_id.recruitment_id)
            all_rec_stages[self.stage_id.recruitment_id.pk] = stages
            request.all_rec_stages = all_rec_stages
        return render_template(
            path="cbv/pipeline/stage_drop_down.html",
            context={
                "instance": self,
                "stages": request.all_rec_stages[self.stage_id.recruitment_id.pk],
            },
        )

    def rating_bar(self):
        """
        Rating bar
        """
        return render_template(
            path="cbv/pipeline/rating.html", context={"instance": self}
        )

    def get_avg_rating(self):
        """
        Docstring for get_avg_rating

        :param self: Candidate instance
        :return: Avg rating got for the candidate
        :rtype: float/int
        """
        return self.candidate_rating.aggregate(avg=Avg("rating"))["avg"]

    def get_total_interview(self):
        """
        Docstring for get_total_interview

        :param self: Total interview assigned for candidate
        :return: Total assigned
        :rtype: Any
        """
        return self.candidate_interview.count()

    def get_interview_count(self):
        """
        Scheduled interviews count
        """
        return render_template(
            path="cbv/pipeline/count_of_interviews.html", context={"instance": self}
        )

    def mail_indication(self):
        """
        Rating bar
        """
        return render_template(
            path="cbv/pipeline/mail_status.html", context={"instance": self}
        )

    def candidate_name(self):
        """
        Rating bar
        """
        now = tz.now()
        return render_template(
            path="cbv/pipeline/candidate_column.html",
            context={"instance": self, "now": now},
        )

    def get_contact(self):
        """
        to get contact no of candidates
        """
        return self.mobile

    def get_resume_url(self):
        return self.resume.url

    def onboarding_portal_html(self):
        return format_html(
            '<div class="oh-checkpoint-badge oh-checkpoint-badge--secondary">{}/4</div>',
            self.onboarding_portal.count,
        )

    def rating(self):
        """
        This method for get custome coloumn for rating.
        """

        return render_template(
            path="cbv/candidates/rating.html",
            context={"instance": self},
        )

    def onboarding_status_col(self):
        """
        This method for get custome coloumn for status.
        """

        return render_template(
            path="cbv/onboarding_view/status.html",
            context={"instance": self},
        )

    def onboarding_task_col(self):
        """
        This method for get custome coloumn for tasks.
        """
        from onboarding.models import CandidateStage, CandidateTask

        cand_stage = self.onboarding_stage.id
        cand_stage_obj = CandidateStage.objects.get(id=cand_stage)
        choices = CandidateTask.choice

        return render_template(
            path="cbv/onboarding_view/task.html",
            context={
                "instance": self,
                "candidate": cand_stage_obj,
                "choices": choices,
                "single_view": True,
            },
        )

    def archive_status(self):
        """
        archive status
        """
        if self.is_active:
            return _("Archive")
        else:
            return _("Un-Archive")

    def resume_pdf(self):
        """
        This method for get custome coloumn for resume.
        """

        return render_template(
            path="cbv/candidates/resume.html",
            context={"instance": self},
        )

    def options(self):
        """
        This method for get custom coloumn for options.
        """

        request = getattr(_thread_locals, "request", None)
        mails = getattr(request, "mails", None)

        if not mails:
            mails = list(Candidate.objects.values_list("email", flat=True))
            setattr(request, "mails", mails)

        emp_list = HorillaUser.objects.filter(username__in=mails).values_list(
            "email", flat=True
        )

        return render_template(
            path="cbv/candidates/option.html",
            context={"instance": self, "emp_list": emp_list},
        )

    def actions_col(self):
        """
        This method for get custom column for actions.
        """

        return render_template(
            path="cbv/candidates/actions.html",
            context={"instance": self},
        )

    def get_profile_url(self):
        """
        This method to get profile url
        """
        url = reverse_lazy("candidate-view", kwargs={"pk": self.pk})
        return url

    def get_update_url(self):
        """
        This method to get update url
        """
        url = reverse_lazy("rec-candidate-update", kwargs={"cand_id": self.pk})
        return url

    def get_skill_zone_url(self):
        """
        This method to get update url
        """
        url = reverse_lazy("to-skill-zone", kwargs={"cand_id": self.pk})
        return url

    def get_rejected_candidate_url(self):
        """
        This method to get the update URL with cand_id as a query parameter.
        """
        base_url = reverse_lazy("add-to-rejected-candidates")
        query_params = urlencode({"candidate_id": self.pk})
        return f"{base_url}?{query_params}"

    def get_move_forward_url(self):
        """The Move Forward modal for this candidate (remark is mandatory)."""
        return reverse_lazy("candidate-move-forward", kwargs={"pk": self.pk})

    def get_move_backward_url(self):
        """The Move Backward modal (Manager only, remark is mandatory)."""
        return reverse_lazy("candidate-move-backward", kwargs={"pk": self.pk})

    def get_handoff_url(self):
        """
        The hiring handoff form (PRD Form 2) for this candidate.

        Deliberately NOT offered as a pipeline button: the handoff is what the
        move into Hired *is*, so Move Forward opens it when the next stage is
        Hired (recruitment.views.stage_actions.move_forward). A separate button
        would present hiring as an action of its own, alongside the stage move
        it actually replaces.
        """
        return reverse_lazy("candidate-pool-handoff", kwargs={"pk": self.pk})

    def get_document_request(self):
        """
        This method to get the update URL with cand_id as a query parameter.
        """
        base_url = reverse_lazy("candidate-document-request")
        query_params = urlencode({"candidate_id": self.pk})
        return f"{base_url}?{query_params}"

    def get_view_note_url(self):
        """
        This method to get update url
        """
        url = reverse_lazy("view-note", kwargs={"cand_id": self.pk})
        return url

    def get_individual_url(self):
        """
        This method to get update url
        """
        url = reverse_lazy("candidate-view-individual", kwargs={"cand_id": self.pk})
        return url

    def get_push_url(self):
        """
        This method to get update url
        """
        url = reverse_lazy("candidate-view-individual", kwargs={"cand_id": self.pk})
        return url

    def get_convert_to_emp(self):
        """
        This method to get covert to employee url
        """
        url = reverse_lazy("candidate-conversion", kwargs={"cand_id": self.pk})
        return url

    def get_add_to_skill(self):
        """
        This method to get add to talent pool employee url
        """
        url = reverse_lazy("to-skill-zone", kwargs={"cand_id": self.pk})
        return url

    def get_add_to_reject(self):
        """
        This method to get add to reject zone employee url
        """
        url = reverse_lazy("add-to-rejected-candidates")
        return f"{url}?candidate_id={self.pk}"

    def get_archive_url(self):
        """
        This method to get archive  url
        """

        if self.is_active:
            action = "archive"
        else:
            action = "un-archive"

        message = f"Do you want to {action} this candidate?"
        url = reverse_lazy("rec-candidate-archive", kwargs={"cand_id": self.pk})

        return f"'{url}','{message}'"

    def get_archive_action_url(self):
        """
        This method returns just the archive/un-archive endpoint URL
        (without JS-formatted arguments), suitable for direct HTMX use.
        """
        return reverse_lazy("rec-candidate-archive", kwargs={"cand_id": self.pk})

    def get_delete_url(self):
        """
        This method to get delete url
        """
        url = reverse_lazy("rec-candidate-delete", kwargs={"cand_id": self.pk})
        return url

    def get_self_tracking_url(self):
        """
        This method to get self tracking url
        """
        url = reverse_lazy(
            "candidate-self-status-tracking", kwargs={"cand_id": self.pk}
        )
        return url

    def get_document_request_doc(self):
        """
        This method to get document request url
        """
        url = reverse_lazy("candidate-document-request") + f"?candidate_id={self.pk}"
        return url

    def is_employee_converted(self):
        """
        The method to get converted employee
        """
        request = getattr(_thread_locals, "request", None)
        if not getattr(request, "employees", None):
            request.employees = Employee.objects.all()

        if request.employees.filter(email=self.email).exists():
            return 'style="background-color: #f1ffd5;"'

    def get_details_candidate(self):
        """
        Candidate detail
        """
        url = reverse_lazy("candidate-detail", kwargs={"pk": self.pk})
        return url

    def detail_actions(self):
        """
        Candidate actions
        """
        return render_template(
            path="cbv/candidates/actions.html",
            context={"instance": self},
        )

    def get_send_mail(self):
        """
        Candidate detail
        """
        url = reverse_lazy("send-mail", kwargs={"cand_id": self.pk})
        return url

    def is_offer_rejected(self):
        """
        Is offer rejected checking method
        """
        first = RejectedCandidate.objects.filter(candidate_id=self).first()
        if first:
            return first.reject_reason_id.count() > 0
        return first

    def get_full_name(self):
        """
        Method will return employee full name
        """
        return str(self.name)

    def get_avatar(self):
        if self.profile and default_storage.exists(self.profile.name):
            return self.profile.url
        return static("images/ui/default_avatar.jpg")

    # ---- Candidate Pool columns -------------------------------------------
    # Presentation helpers for the Candidate Pool list. The status and
    # verification values are computed by recruitment.services.candidate so the
    # rules live in one place; these are thin accessors the list view can name.

    def pool_number(self):
        """Stable reference number for the Pool's first column."""
        return self.pk

    def pool_job_applied_to(self):
        """The job opening applied to, or blank for a pool-only candidate."""
        if self.recruitment_id_id is None:
            return "-"
        return str(self.recruitment_id)

    def pool_date_applied(self):
        """
        When this application was made.

        Blank when there is no job opening: a candidate sourced into the pool
        has not applied to anything, so a date here would be misleading.
        """
        if self.recruitment_id_id is None or not self.created_at:
            return "-"
        return self.created_at.date()

    def pool_status(self):
        """One of the five general Candidate Pool buckets."""
        from recruitment.services.candidate import candidate_status

        return candidate_status(self)

    def pool_actions(self):
        """
        Row actions for the Candidate Pool table.

        Open Candidate Details and Map to Job Opening -- both already existed
        as routes/services with no way to reach them from the pool.
        """
        return render_template(
            path="cbv/candidate_pool/row_actions.html",
            context={"instance": self},
        )

    def pool_contact_verification(self):
        """Verified / Unverified / N/A."""
        from recruitment.services.candidate import contact_verification_display

        return contact_verification_display(self)

    def get_company(self):
        """
        This method is used to return the company
        """
        return getattr(
            getattr(getattr(self, "recruitment_id", None), "company_id", None),
            "company",
            None,
        )

    def get_job_position(self):
        """
        This method is used to return the job position of the candidate
        """
        return self.job_position_id.job_position

    def get_email(self):
        """
        Return email
        """
        return self.email

    def get_mail(self):
        """ """
        return self.get_email()

    def phone(self):
        return self.mobile

    def tracking(self):
        """
        This method is used to return the tracked history of the instance
        """
        return get_diff(self)

    def get_last_sent_mail(self):
        """
        This method is used to get last send mail
        """
        from base.models import EmailLog

        return (
            EmailLog.objects.filter(to__icontains=self.email)
            .order_by("-created_at")
            .first()
        )

    def get_schedule_interview(self):
        url = reverse_lazy("interview-schedule", kwargs={"cand_id": self.pk})
        return url

    def get_interview_schedule_url(self):
        """Krew hop before Google: ties the event to an InterviewMeeting."""
        return reverse("interview-meeting-schedule", kwargs={"cand_id": self.pk})

    def get_interview_meeting(self):
        """The Google interview at the candidate's current stage, if any."""
        from recruitment.services.interview import current_meeting

        if "_interview_meeting" not in self.__dict__:
            self.__dict__["_interview_meeting"] = current_meeting(self)
        return self.__dict__["_interview_meeting"]

    def get_join_meeting_url(self):
        meeting = self.get_interview_meeting()
        return meeting.join_url if meeting else ""

    def get_interview_meeting_refresh_url(self):
        meeting = self.get_interview_meeting()
        if meeting is None:
            return ""
        return reverse("interview-meeting-refresh", kwargs={"pk": meeting.pk})

    def get_google_calendar_url(self, reference=None, account=None):
        """
        A prefilled Google Calendar event for interviewing this candidate.

        The PRD schedules interviews in the hiring team's own calendar rather
        than in a second, parallel calendar inside the HRMS: the interviewers
        already live in Google Calendar, and an invite there is what actually
        reaches the candidate and adds the Meet link.

        This only composes the event -- the user reviews the time and guests in
        Google and saves it there. Nothing is written here, and no Google
        credentials are involved.
        """
        title = _("Interview: %(name)s") % {"name": self.get_full_name()}
        position = self.job_position_id.job_position if self.job_position_id else ""
        opening = self.recruitment_id.title if self.recruitment_id else ""
        stage = self.stage_id.stage if self.stage_id else ""

        detail_lines = [str(_("Interview scheduled from Krew Recruitment."))]
        if position:
            detail_lines.append(f"{_('Job position')}: {position}")
        if opening:
            detail_lines.append(f"{_('Job opening')}: {opening}")
        if stage:
            detail_lines.append(f"{_('Stage')}: {stage}")
        if self.mobile:
            detail_lines.append(f"{_('Candidate contact')}: {self.mobile}")
        detail_lines.append(
            str(_("Use 'Add Google Meet video conferencing' to generate the link."))
        )
        if reference:
            # How Krew finds this event to fetch its Meet link.
            detail_lines.append(
                f"{_('Krew reference')}: {reference} "
                f"({_('keep this line so Krew can show the meeting link')})"
            )

        params = {
            "action": "TEMPLATE",
            "text": str(title),
            "details": "\n".join(str(line) for line in detail_lines),
        }
        if self.email:
            # Google invites the candidate as a guest.
            params["add"] = self.email
        if account:
            # Open Google as the person scheduling, not whichever account the
            # browser has as default; Google asks them to sign in if needed.
            params["authuser"] = account
        return f"https://calendar.google.com/calendar/render?{urlencode(params)}"

    def get_email_compose_url(self):
        """
        A mailto: link addressed to this candidate ("Email Candidate").

        Opens the recruiter's own default mail app (Gmail, Outlook, ...) rather
        than tying Krew to Gmail. The templated, auditable messages the system
        itself sends (rejection notice, verification) still go through the
        mail server.
        """
        from urllib.parse import quote

        url = f"mailto:{quote(self.email or '', safe='@')}"
        if self.recruitment_id:
            subject = str(
                _("Your application for %(job)s") % {"job": self.recruitment_id.title}
            )
            url += f"?subject={quote(subject)}"
        return url

    def get_interview(self):
        """
        This method is used to get the interview dates and times
        for the candidate for the mail templates
        """

        interviews = InterviewSchedule.objects.filter(candidate_id=self.id)
        if interviews:
            interview_info = "<table>"
            interview_info += "<tr><th>Sl No.</th><th>Date</th><th>Time</th><th>Is Completed</th></tr>"
            for index, interview in enumerate(interviews, start=1):
                interview_info += f"<tr><td>{index}</td>"
                interview_info += (
                    f"<td class='dateformat_changer'>{interview.interview_date}</td>"
                )
                interview_info += (
                    f"<td class='timeformat_changer'>{interview.interview_time}</td>"
                )
                interview_info += (
                    f"<td>{'Yes' if interview.completed else 'No'}</td></tr>"
                )
            interview_info += "</table>"
            return interview_info
        else:
            return ""

    def candidate_interview_view(self):
        interviews = InterviewSchedule.objects.filter(candidate_id=self.pk)
        return render_template(
            path="cbv/pipeline/interview_template.html",
            context={"instance": self, "interviews": interviews},
        )

    def save(self, *args, **kwargs):
        if self.stage_id is not None:
            self.hired = self.stage_id.stage_type == "hired"

        # Company is owned by the job opening whenever there is one, so a
        # candidate can never sit in a different tenant from the opening they
        # applied to. A candidate WITHOUT an opening keeps whatever company the
        # service derived from the acting user -- it is never taken from client
        # input. See recruitment.services.candidate.
        if self.recruitment_id is not None:
            self.company_id_id = self.recruitment_id.company_id_id

        should_validate_job_position = not self.pk
        if self.pk:
            previous = (
                # .entire(): this manager is company-scoped, and a candidate
                # being saved outside its own company context would otherwise
                # read as "no previous row" and re-run creation validation.
                Candidate.objects.entire()
                .filter(pk=self.pk)
                .values("recruitment_id", "job_position_id")
                .first()
            )
            if previous:
                should_validate_job_position = (
                    previous["recruitment_id"] != self.recruitment_id_id
                    or previous["job_position_id"] != self.job_position_id_id
                )

        # A candidate may exist with no job opening at all (Candidate Pool):
        # they were sourced, imported or added manually and have not been mapped
        # yet. Every job-opening-derived rule below is therefore conditional --
        # previously this dereferenced recruitment_id unconditionally and raised
        # AttributeError, which made such a candidate impossible to save.
        if self.recruitment_id is not None:
            if not self.recruitment_id.is_event_based and self.job_position_id is None:
                # Recruitment.job_position_id is editable=False, so openings
                # created through the UI only carry open_positions. Fall back
                # to the single open position when the FK was never set.
                self.job_position_id = self.recruitment_id.job_position_id
                if self.job_position_id is None:
                    positions = list(self.recruitment_id.open_positions.all()[:2])
                    if len(positions) == 1:
                        self.job_position_id = positions[0]
            if should_validate_job_position:
                if self.job_position_id not in self.recruitment_id.open_positions.all():
                    raise ValidationError({"job_position_id": _("Choose valid choice")})
                if self.recruitment_id.is_event_based and self.job_position_id is None:
                    raise ValidationError(
                        {"job_position_id": _("This field is required.")}
                    )

        if self.stage_id and self.stage_id.stage_type == "cancelled":
            self.canceled = True
        # The cancelled-stage parking spot only exists within a job opening.
        # Without one there is no pipeline to park in, and Stage.recruitment_id
        # is NOT NULL, so creating one here would raise IntegrityError.
        if self.canceled and self.recruitment_id is not None:
            cancelled_stage = Stage.objects.filter(
                recruitment_id=self.recruitment_id, stage_type="cancelled"
            ).first()
            if not cancelled_stage:
                # "Rejected" sits after Hired (9999): the last column of the
                # pipeline, never between the working stages.
                cancelled_stage = Stage.objects.create(
                    recruitment_id=self.recruitment_id,
                    stage="Rejected",
                    stage_type="cancelled",
                    sequence=REJECTED_STAGE_SEQUENCE,
                )
            self.stage_id = cancelled_stage
        if (
            self.converted_employee_id
            and Candidate.objects.entire()
            .filter(converted_employee_id=self.converted_employee_id)
            .exclude(id=self.id)
            .exists()
        ):
            raise ValidationError(_("Employee is uniques for candidate"))

        if self.converted:
            self.hired = False
            self.canceled = False

        super().save(*args, **kwargs)

    def last_email(self):
        """
        for last send mail column

        """

        return render_template(
            path="cbv/onboarding_candidates/cand_email.html",
            context={"instance": self},
        )

    def date_of_joining(self):
        """
        for joining date column

        """

        return render_template(
            path="cbv/onboarding_candidates/date_of_joining.html",
            context={"instance": self},
        )

    def probation_date(self):
        """
        for probation date column

        """

        return render_template(
            path="cbv/onboarding_candidates/probation_date.html",
            context={"instance": self},
        )

    def offer_letter(self):
        """
        for offer letter  column

        """

        return render_template(
            path="cbv/onboarding_candidates/offer_letter.html",
            context={"instance": self},
        )

    def rejected_candidate_class(self):
        """
        Returns the appropriate title attribute for rejected candidates.
        """
        if self.is_offer_rejected():
            return f'title="{_("Added In Rejected Candidates")}"'
        else:
            return f'title="{_("Add To Rejected Candidates")}"'

    def actions(self):
        """
        for actions  column

        """

        return render_template(
            path="cbv/onboarding_candidates/actions.html",
            context={"instance": self},
        )

    @classmethod
    @lru_cache(maxsize=1)
    def get_unique_questions(cls):
        return dict(
            RecruitmentSurvey.objects.values("question")
            .annotate(pk=Min("pk"))
            .values_list("pk", "question")
        )

    @cached_property
    def survey_answer_dict(self):
        answer_instance = (
            RecruitmentSurveyAnswer.objects.filter(candidate_id=self)
            .only("answer_json")
            .first()
        )

        if answer_instance and answer_instance.answer_json:
            return json.loads(
                answer_instance.answer_json
            )  # faster than ast.literal_eval
        return {}

    @cached_property
    def screening_answer_by_wording(self):
        """
        {question wording -> answer} from CandidateAnswer, for this application.

        Reads the frozen JobOpeningQuestion, so an exported answer is labelled
        with the question as it was published rather than as it reads today.
        One query, cached per instance, so exporting N columns for a candidate
        stays at one query rather than N.
        """
        return {
            row.job_opening_question.wording: row.answer
            for row in self.screening_answers.select_related(
                "job_opening_question"
            ).all()
        }

    def __getattr__(self, name):
        if name.startswith("get_survey_question_"):
            try:
                question_id = int(name.split("_")[-1])

                unique_questions = self.get_unique_questions()
                question_text = unique_questions.get(question_id)

                if not question_text:
                    return None

                # Prefer the row-based answer: it is keyed to the frozen
                # question for THIS application, so it cannot be affected by a
                # later edit to the reusable question. The wording-keyed blob
                # below remains only as the fallback for applications whose
                # answers migration 0013 could not map confidently
                # (unmatched/ambiguous), which are deliberately left in place.
                answer = self.screening_answer_by_wording.get(question_text)
                if answer is not None:
                    return answer

                result = self.survey_answer_dict.get(question_text)

                if isinstance(result, list):
                    return ",".join(result)

                return result
            except Exception:
                return None

        try:
            return super().__getattribute__(name)
        except ObjectDoesNotExist:
            raise
        except AttributeError:
            raise

    class Meta:
        """
        Meta class to add the additional info
        """

        unique_together = (
            "email",
            "recruitment_id",
        )
        permissions = (
            ("view_history", "View Candidate History"),
            ("archive_candidate", "Archive Candidate"),
        )
        ordering = ["sequence"]
        verbose_name = _("Candidate")
        verbose_name_plural = _("Candidates")


class RejectReason(HorillaModel):
    """
    RejectReason
    """

    title = models.CharField(
        max_length=50,
    )
    description = models.TextField(null=True, blank=True, max_length=255)
    company_id = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Company"),
    )
    objects = HorillaCompanyManager()

    def __str__(self) -> str:
        return self.title

    def get_update_url(self):
        """
        This method to get update url
        """

        url = reverse_lazy("update-reject-reason-view", kwargs={"pk": self.pk})
        return url

    def get_delete_url(self):
        """
        This method to get delete url
        """
        base_url = reverse_lazy("delete-reject-reasons")
        rej_id = self.pk
        url = f"{base_url}?id={rej_id}"
        return url

    def get_instance_id(self):
        return self.id

    class Meta:
        verbose_name = _("Rejection Reason")
        verbose_name_plural = _("Rejection Reasons")


class RejectedCandidate(HorillaModel):
    """
    RejectedCandidate
    """

    candidate_id = models.OneToOneField(
        Candidate,
        on_delete=models.PROTECT,
        verbose_name="Candidate",
        related_name="rejected_candidate",
    )
    reject_reason_id = models.ManyToManyField(
        RejectReason, verbose_name="Reject reason", blank=True
    )
    description = models.TextField(max_length=255)
    objects = HorillaCompanyManager(
        related_company_field="candidate_id__recruitment_id__company_id"
    )
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )

    def __str__(self) -> str:
        reasons = ", ".join(self.reject_reason_id.values_list("title", flat=True))
        return f"{self.candidate_id} - {reasons if reasons else _('No Reason')}"


class ApplicationContactVerification(models.Model):
    """
    Two-factor contact verification for a public application.

    Keyed on (job opening, email, mobile) rather than on Candidate, because the
    candidate does not exist yet: verification runs while the applicant is still
    filling in the form. On submission a verified row for the same opening and
    contact details stamps Candidate.contact_verified_at.

    Verification never blocks submission (PRD). The email link is valid for 10
    minutes from Verify. Following it sends the OTP, valid for 10 minutes;
    "Resend code" (every 30 seconds) re-sends that same code within those 10
    minutes. After that the attempt has expired and the applicant clicks Verify
    again (PRD: "after expiry the candidate must restart verification"). A
    completed verification stays usable for COMPLETED_VALIDITY, so finishing
    the form afterwards still counts.
    """

    VALIDITY = timedelta(minutes=10)
    # A finished verification stays good for submitting this long, so filling
    # in the rest of the form after verifying does not undo it.
    COMPLETED_VALIDITY = timedelta(hours=24)
    #: Wrong OTP guesses allowed per verification attempt. A 6-digit code left
    #: open for 10 minutes is brute-forceable at machine speed, and
    #: Fail2BanMiddleware only covers login, so the bound lives here. Exhausting
    #: it ends THIS attempt: the applicant restarts, which issues a fresh code
    #: and a fresh counter.
    MAX_OTP_ATTEMPTS = 15
    #: "Resend code" becomes available this long after the last send.
    OTP_RESEND_INTERVAL = timedelta(seconds=30)

    job_opening = models.ForeignKey(
        "recruitment.Recruitment",
        on_delete=models.CASCADE,
        related_name="contact_verifications",
    )
    email = models.EmailField()
    mobile = models.CharField(max_length=25)
    #: Unguessable, single-use: cleared the moment the link is followed.
    email_token = models.CharField(max_length=64, db_index=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    #: Issued only after the email is verified, so a bare POST cannot trigger an
    #: SMS send.
    otp_code = models.CharField(max_length=10, blank=True, default="")
    otp_sent_at = models.DateTimeField(null=True, blank=True)
    #: When the code stops working: 10 minutes after the email was verified
    #: (a resend of the same code does not extend it).
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    #: Incremented on each wrong guess, never reset in place -- a restart
    #: supersedes the row instead, so the counter cannot be cleared by retrying
    #: the same attempt.
    otp_attempts = models.PositiveIntegerField(default=0)
    mobile_verified_at = models.DateTimeField(null=True, blank=True)
    #: The application this verification was used for, set on submit. Empty
    #: while verifying (the candidate does not exist yet) or if never used.
    candidate = models.ForeignKey(
        "recruitment.Candidate",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contact_verifications",
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Application Contact Verification")
        verbose_name_plural = _("Application Contact Verifications")
        indexes = [models.Index(fields=["job_opening", "email", "mobile"])]

    def __str__(self):
        return f"{self.job_opening_id} - {self.email}"

    def is_expired(self):
        return tz.now() >= self.expires_at

    def is_usable(self):
        """
        Still counts for the application page: in progress within the
        10-minute link/OTP window, or completed within COMPLETED_VALIDITY.
        """
        if self.is_complete():
            return tz.now() < self.mobile_verified_at + self.COMPLETED_VALIDITY
        if self.email_verified_at:
            # PRD: the OTP stage lasts 10 minutes, then verification restarts.
            return tz.now() < self.otp_window_ends_at()
        return not self.is_expired()

    def otp_window_ends_at(self):
        """End of the 10-minute OTP stage that starts when the email is verified."""
        return self.email_verified_at + self.VALIDITY

    def otp_is_valid(self):
        return bool(
            self.otp_code and self.otp_expires_at and tz.now() < self.otp_expires_at
        )

    def resend_wait_seconds(self):
        """Seconds until "Resend code" is available (0 = now)."""
        if not self.otp_sent_at or self.mobile_verified_at:
            return 0
        remaining = (self.otp_sent_at + self.OTP_RESEND_INTERVAL) - tz.now()
        return max(0, int(remaining.total_seconds() + 0.999))

    def is_complete(self):
        return bool(self.email_verified_at and self.mobile_verified_at)


class CandidateNote(HorillaModel):
    """
    Permanent internal note on a candidate (Candidate Pool).

    Distinct from StageNote, which is left exactly as it is: StageNote requires
    a stage, and is editable and deletable through existing screens that other
    parts of Horilla rely on. Neither suits this feature, which requires notes
    that survive unchanged and can exist before a candidate has any stage.

    Immutability is the point of this model:

      * ``stage_id`` is nullable, so a candidate with no job opening can still
        be annotated;
      * ``save()`` refuses any update, so a posted note cannot be rewritten;
      * ``delete()`` refuses, so it cannot be removed;
      * there is deliberately no update or delete endpoint, and no edit control
        in the UI.

    Notes are internal. They are never shown to the candidate -- unlike
    StageNote, this model has no ``candidate_can_view`` flag to get wrong.
    """

    candidate_id = models.ForeignKey(
        Candidate,
        on_delete=models.CASCADE,
        related_name="candidate_notes",
        verbose_name=_("Candidate"),
    )
    #: Nullable: a Candidate Pool note may predate any pipeline placement.
    #: When present it records the stage the candidate was in at the time,
    #: which is context, not ownership.
    stage_id = models.ForeignKey(
        Stage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="candidate_notes",
        verbose_name=_("Stage"),
    )
    description = models.TextField(verbose_name=_("Note"))
    #: Denormalised from the candidate so a note stays company-scoped even if
    #: the candidate is later anonymised, and so scoping needs no join.
    company_id = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="candidate_notes",
        verbose_name=_("Company"),
    )

    objects = HorillaCompanyManager("company_id")

    class Meta:
        verbose_name = _("Candidate Note")
        verbose_name_plural = _("Candidate Notes")
        ordering = ["-created_at", "-id"]
        indexes = [
            # The only access pattern: a candidate's notes, newest first.
            models.Index(fields=["candidate_id", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.candidate_id} - {self.description[:40]}"

    def save(self, *args, **kwargs):
        """Insert-only: a posted note is a permanent record, not state."""
        if self.pk is not None:
            raise ValidationError(_("A candidate note cannot be edited once posted."))
        # created_by is stamped by HorillaModel.save() from the request user.
        if self.company_id_id is None and self.candidate_id_id is not None:
            self.company_id_id = self.candidate_id.company_id_id
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("A candidate note cannot be deleted."))


class StageFiles(HorillaModel):
    files = models.FileField(upload_to=upload_path, blank=True, null=True)

    def __str__(self):
        return self.files.name.split("/")[-1]


class StageNote(HorillaModel):
    """
    StageNote model
    """

    candidate_id = models.ForeignKey(Candidate, on_delete=models.CASCADE)
    description = models.TextField(verbose_name=_("Description"))  # 905
    stage_id = models.ForeignKey(Stage, on_delete=models.CASCADE)
    stage_files = models.ManyToManyField(StageFiles, blank=True)
    updated_by = models.ForeignKey(
        Employee, on_delete=models.CASCADE, null=True, blank=True
    )
    candidate_can_view = models.BooleanField(default=False)
    objects = HorillaCompanyManager(
        related_company_field="candidate_id__recruitment_id__company_id"
    )

    def __str__(self) -> str:
        return f"{self.description}"

    def updated_user(self):
        if self.updated_by:
            return self.updated_by
        else:
            return self.candidate_id


class RecruitmentSurvey(HorillaModel):
    """
    RecruitmentSurvey model
    """

    question_types = [
        ("checkbox", _("Yes/No")),
        ("options", _("Choices")),
        ("multiple", _("Multiple Choice")),
        ("text", _("Text")),
        ("number", _("Number")),
        ("percentage", _("Percentage")),
        ("date", _("Date")),
        ("textarea", _("Textarea")),
        ("file", _("File Upload")),
        ("rating", _("Rating")),
    ]
    # Explicit through model so mandatory/optional and ordering can be
    # configured PER TEMPLATE rather than once on the question. The through
    # model adopts the existing auto-created join table, so every existing
    # relationship row and this accessor keep working unchanged -- .add()/.set()
    # still work because every extra through column has a default.
    template_id = models.ManyToManyField(
        SurveyTemplate,
        verbose_name="Template",
        blank=True,
        through="recruitment.SurveyTemplateQuestion",
    )
    form_type = models.CharField(
        max_length=10, choices=FORM_TYPES, default=FORM_ONE, verbose_name=_("Form")
    )
    is_mandatory = models.BooleanField(default=False)
    recruitment_ids = models.ManyToManyField(
        Recruitment,
        verbose_name=_("Recruitment"),
    )
    question = models.TextField(null=False)
    job_position_ids = models.ManyToManyField(
        JobPosition, verbose_name=_("Job Positions"), editable=False
    )
    sequence = models.IntegerField(null=True, default=0)
    type = models.CharField(
        max_length=15,
        choices=question_types,
    )
    options = models.TextField(
        null=True, default="", help_text=_("Separate choices by ',  '"), max_length=255
    )
    #: Only meaningful for a "file" question: how many files the candidate may
    #: attach to this one answer. The authored value -- HR sets the cap, so a
    #: question asking for three certificates accepts three and says so.
    max_files = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_FILES_CEILING)],
        verbose_name=_("Max Files Allowed"),
        help_text=_("File-upload questions only. 1 means a single file."),
    )
    #: Derived from max_files, kept as a column because templates, the
    #: published snapshot and the API all read it. Never authored directly:
    #: save() keeps it in step, and max_files is the authority.
    allow_multiple_files = models.BooleanField(
        default=False,
        verbose_name=_("Allow Multiple Files"),
        help_text=_("File-upload questions only."),
    )
    objects = HorillaCompanyManager(related_company_field="recruitment_ids__company_id")

    def save(self, *args, **kwargs):
        """Keep the derived allow_multiple_files flag in step with max_files."""
        self.allow_multiple_files = (self.max_files or 1) > 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return str(self.question)

    def options_col(self):
        if self.type in ["options", "multiple"]:
            return render_template(
                "cbv/recruitment_survey/option_col.html",
                {"instance": self},
            )
        return ""

    def detail_actions(self):
        """
        This method for get custom column for details actions.
        """
        return render_template(
            path="cbv/recruitment_survey/detail_actions.html",
            context={"instance": self},
        )

    def get_edit_url(self):

        url = reverse(
            "recruitment-survey-question-template-edit", kwargs={"pk": self.pk}
        )
        return url

    def get_delete_url(self):

        url = reverse(
            "recruitment-survey-question-template-delete", kwargs={"survey_id": self.pk}
        )
        return url

    def openings_using(self):
        """
        Job openings that use this question: through a template the opening
        has selected, or attached directly as a job-specific question.
        """
        from django.db.models import Q

        template_ids = SurveyTemplateQuestion.objects.filter(
            recruitmentsurvey=self
        ).values_list("surveytemplate_id", flat=True)
        return (
            Recruitment._base_manager.filter(
                Q(survey_templates__in=list(template_ids))
                | Q(pk__in=list(self.recruitment_ids.values_list("pk", flat=True)))
            )
            .distinct()
            .order_by("title")
        )

    def delete(self, *args, **kwargs):
        """
        A bank question can be deleted only when no job opening uses it --
        neither through one of its templates nor as a job-specific question.
        Templates that no opening uses lose it along with the question.
        """
        openings = list(self.openings_using().values_list("title", flat=True)[:4])
        if openings:
            shown = ", ".join(openings[:3]) + (" and others" if len(openings) > 3 else "")
            raise ValidationError(
                _(
                    "This question can't be deleted: it is used by job opening(s) "
                    "%(openings)s. A question can be deleted only when none of "
                    "its templates is part of a job opening."
                )
                % {"openings": shown}
            )
        return super().delete(*args, **kwargs)

    def recruitment_col(self):
        """
        Manager in detail view
        """
        recruitment = self.recruitment_ids.all()
        recruitment_string = "<br>".join([str(rec) for rec in recruitment])
        return recruitment_string

    def get_question_type(self):
        return dict(self.question_types).get(self.type)

    def choices(self):
        """
        Used to split the choices
        """
        return self.options.split(", ")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.template_id is None:
            general_template = SurveyTemplate.objects.filter(
                is_general_template=True
            ).first()
            if general_template:
                self.template_id.add(general_template)
                super().save(*args, **kwargs)

    class Meta:
        ordering = [
            "sequence",
        ]


class QuestionOrdering(HorillaModel):
    """
    Survey Template model
    """

    question_id = models.ForeignKey(RecruitmentSurvey, on_delete=models.CASCADE)
    recruitment_id = models.ForeignKey(Recruitment, on_delete=models.CASCADE)
    sequence = models.IntegerField(default=0)
    objects = HorillaCompanyManager(related_company_field="recruitment_ids__company_id")


class SurveyTemplateQuestion(models.Model):
    """
    Through model for RecruitmentSurvey.template_id.

    Exists so that mandatory/optional and ordering are properties of the
    TEMPLATE-QUESTION RELATIONSHIP, not of the question. One question reused by
    two templates can therefore be mandatory in one and optional in the other:

        Question 55 -> Template A -> is_mandatory=True
                    -> Template B -> is_mandatory=False

    RecruitmentSurvey.is_mandatory / .sequence are kept (not dropped) because
    questions can also be attached straight to a job opening through
    RecruitmentSurvey.recruitment_ids, where no template row exists to carry
    the configuration. They act as the fallback for that path only.

    Deliberately a plain models.Model, not HorillaModel: this adopts the
    existing auto-created join table
    (``recruitment_recruitmentsurvey_template_id``), whose columns are exactly
    id / recruitmentsurvey_id / surveytemplate_id. HorillaModel would add
    created_at/created_by/modified_by/is_active, turning a two-column addition
    into a much larger rewrite of a table that already holds live rows.

    Field names match what Django generated for the implicit through model, so
    the adopted columns line up without db_column overrides.
    """

    recruitmentsurvey = models.ForeignKey(
        RecruitmentSurvey,
        on_delete=models.CASCADE,
        verbose_name=_("Question"),
    )
    surveytemplate = models.ForeignKey(
        SurveyTemplate,
        on_delete=models.CASCADE,
        verbose_name=_("Template"),
    )
    #: Authoritative mandatory/optional value for this question IN THIS
    #: TEMPLATE. Snapshotted at publication; never read back for a published
    #: job opening.
    is_mandatory = models.BooleanField(default=False, verbose_name=_("Is Mandatory"))
    #: Per-template display order. Null sorts last, matching how
    #: RecruitmentSurvey.sequence behaves (it is also nullable).
    sequence = models.IntegerField(null=True, blank=True, default=0)
    #: Frozen at the moment the question joined this template, so editing or
    #: deleting the bank question cannot alter a template that already uses it.
    #: Blank values fall back to the live question (rows added before this
    #: freeze existed are backfilled by migration 0015).
    wording = models.TextField(blank=True, default="", verbose_name=_("Question"))
    question_type = models.CharField(
        max_length=15, choices=RecruitmentSurvey.question_types, blank=True, default=""
    )
    options = models.TextField(blank=True, default="")
    #: Nullable rather than False-defaulted: None means "this row predates the
    #: freeze" and falls back to the live question, which is the same
    #: blank-means-fallback rule the text columns above use. A frozen False is
    #: therefore distinguishable from never having been frozen at all.
    allow_multiple_files = models.BooleanField(null=True, blank=True, default=None)
    #: Frozen file cap, nullable on the same blank-means-fallback rule.
    max_files = models.PositiveSmallIntegerField(null=True, blank=True, default=None)

    def save(self, *args, **kwargs):
        """
        Freeze the question's wording and type as this row is created.

        Without this the freeze columns stayed blank on every new attachment
        and frozen() fell through to the live question forever, so the
        template-level freeze did nothing: editing a bank question still
        changed what an existing template would publish. Migration 0015
        backfilled the rows that already existed; this keeps new ones frozen.

        Only on INSERT. Re-freezing on update would defeat the purpose, and a
        value explicitly set by the caller is left alone.
        """
        if self._state.adding and self.recruitmentsurvey_id:
            question = self.recruitmentsurvey
            if not self.wording:
                self.wording = question.question
            if not self.question_type:
                self.question_type = question.type
            if not self.options:
                self.options = question.options or ""
            if self.allow_multiple_files is None:
                self.allow_multiple_files = question.allow_multiple_files
            if self.max_files is None:
                self.max_files = question.max_files
        super().save(*args, **kwargs)

    def frozen(self):
        """The question as this template froze it."""
        cap = self.max_files
        if cap is None:
            cap = self.recruitmentsurvey.max_files
        cap = max(1, cap or 1)
        return {
            "wording": self.wording or self.recruitmentsurvey.question,
            "question_type": self.question_type or self.recruitmentsurvey.type,
            "options": self.options or (self.recruitmentsurvey.options or ""),
            "max_files": cap,
            # Derived from the cap so the two can never disagree in a snapshot.
            "allow_multiple_files": cap > 1,
        }

    class Meta:
        # Adopts the table Django already created for the implicit M2M.
        db_table = "recruitment_recruitmentsurvey_template_id"
        unique_together = (("recruitmentsurvey", "surveytemplate"),)
        ordering = ["sequence", "id"]
        verbose_name = _("Template Question")
        verbose_name_plural = _("Template Questions")

    def __str__(self):
        return f"{self.surveytemplate} - {self.recruitmentsurvey}"


class JobOpeningQuestion(models.Model):
    """
    Immutable snapshot of one screening question, frozen at publication.

    This is the historical source of truth for a published job opening. It is
    written once, inside the REVIEW -> PUBLISHED transaction, and never again:
    editing the reusable question or its template afterwards cannot reach it.

        Question bank -> Template -> Job Opening -> PUBLISH -> snapshot

    Everything needed to re-render the question exactly as the candidate saw
    it is copied here, because a reference alone would still drift when the
    reusable question changed.

    Plain models.Model, not HorillaModel, for the same reason as
    RecruitmentAuditEvent: HorillaModel.save() stamps modified_by on every
    write, which contradicts immutability.
    """

    form_type = models.CharField(
        max_length=10, choices=FORM_TYPES, default=FORM_ONE, verbose_name=_("Form")
    )
    job_opening = models.ForeignKey(
        "recruitment.Recruitment",
        on_delete=models.CASCADE,
        related_name="snapshot_questions",
        verbose_name=_("Job Opening"),
    )
    # SET_NULL, not PROTECT or CASCADE: the reusable question may be deleted
    # under its own permissions, and that must neither be blocked by history
    # nor erase it. This link is traceability only -- nothing about rendering
    # or validating a published question reads through it.
    source_question = models.ForeignKey(
        RecruitmentSurvey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="snapshots",
        verbose_name=_("Source Question"),
    )
    wording = models.TextField(verbose_name=_("Question"))
    question_type = models.CharField(
        max_length=15,
        choices=RecruitmentSurvey.question_types,
        verbose_name=_("Type"),
    )
    #: Same comma-joined format as RecruitmentSurvey.options, so the existing
    #: rendering logic keeps working. Frozen: adding a choice to the reusable
    #: question later must not add it to an already-published opening.
    options = models.TextField(null=True, blank=True, default="")
    is_mandatory = models.BooleanField(default=False, verbose_name=_("Is Mandatory"))
    #: Materialized at publication. Later reordering of the template or the
    #: question bank cannot change the order a published opening presents.
    display_order = models.PositiveIntegerField(default=0)
    #: File-upload questions may accept several files; every other type takes
    #: exactly one answer. Both are frozen at publication -- max_files is the
    #: cap the candidate's form enforces, the boolean is kept in step with it.
    allow_multiple_files = models.BooleanField(default=False)
    max_files = models.PositiveSmallIntegerField(default=1)
    #: True when this row was reconstructed by a data migration for an opening
    #: that was already published before snapshots existed. Such a row uses the
    #: question wording as it was at migration time, which is the best evidence
    #: available -- it is NOT guaranteed to be the wording shown at the
    #: original publication.
    is_reconstructed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = HorillaCompanyManager("job_opening__company_id")

    class Meta:
        verbose_name = _("Job Opening Question")
        verbose_name_plural = _("Job Opening Questions")
        ordering = ["job_opening", "display_order", "id"]
        constraints = [
            # Exactly one snapshot per source question per job opening. This is
            # what makes the union of the template path and the direct
            # recruitment_ids path deduplicate at the database level rather
            # than only in the service.
            models.UniqueConstraint(
                fields=["job_opening", "source_question"],
                name="uniq_job_opening_source_question",
            ),
        ]
        indexes = [
            models.Index(fields=["job_opening", "display_order"]),
        ]

    def __str__(self):
        return f"{self.job_opening_id}#{self.display_order} {self.wording[:40]}"

    def choices(self):
        """Selectable options, split exactly like RecruitmentSurvey.choices()."""
        return (self.options or "").split(", ")

    def get_question_type(self):
        return dict(RecruitmentSurvey.question_types).get(self.question_type)

    def save(self, *args, **kwargs):
        """
        Insert-only. A published snapshot is historical record, not state.

        Guards the ORM path; the service layer never updates these rows, and
        the API exposes them read-only. Defence in depth, not the only guard.
        """
        if self.pk is not None:
            raise ValidationError(
                _(
                    "A published job opening question cannot be modified. "
                    "Publish a new job opening to change the question set."
                )
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """
        Refuse direct deletion so history cannot be quietly removed.

        Deleting the parent job opening still cascades (collector deletes run
        as queryset operations, not through this method), which is intended:
        no job opening, no snapshot.
        """
        raise ValidationError(
            _("A published job opening question cannot be deleted.")
        )


class CandidateAnswer(HorillaModel):
    """
    One row per (application, published question) -- never a blob.

    Replaces RecruitmentSurveyAnswer.answer_json, which stored every answer in
    one JSON object keyed by the question *wording*. That made historical
    answers depend on current wording and made "who answered Yes to question X"
    a full scan plus string match.

        Candidate (== the application) -> CandidateAnswer -> JobOpeningQuestion

    The answer points at the immutable snapshot, never at the reusable question
    and never at wording, so editing the question bank cannot change what a
    historical application means.
    """

    candidate = models.ForeignKey(
        Candidate,
        on_delete=models.CASCADE,
        related_name="screening_answers",
        verbose_name=_("Candidate"),
    )
    # PROTECT: an answer must never be orphaned from the question it answers.
    job_opening_question = models.ForeignKey(
        JobOpeningQuestion,
        on_delete=models.PROTECT,
        related_name="answers",
        verbose_name=_("Question"),
    )
    #: Canonical answer text. Multi-select answers are stored comma-joined, the
    #: same representation the previous blob used, so display and export need no
    #: per-type special casing.
    answer = models.TextField(blank=True, default="")
    attachment = models.FileField(upload_to=upload_path, null=True, blank=True)

    objects = HorillaCompanyManager(
        related_company_field="candidate__recruitment_id__company_id"
    )

    class Meta:
        verbose_name = _("Candidate Answer")
        verbose_name_plural = _("Candidate Answers")
        ordering = ["job_opening_question__display_order", "id"]
        constraints = [
            # One answer per question per application, enforced by the database
            # rather than by application code alone.
            models.UniqueConstraint(
                fields=["candidate", "job_opening_question"],
                name="uniq_candidate_job_opening_question",
            ),
        ]

    def __str__(self):
        return f"{self.candidate_id} - {self.job_opening_question_id}"


class RecruitmentSurveyAnswer(HorillaModel):
    """
    RecruitmentSurveyAnswer
    """

    candidate_id = models.ForeignKey(Candidate, on_delete=models.CASCADE)
    recruitment_id = models.ForeignKey(
        Recruitment,
        on_delete=models.PROTECT,
        verbose_name=_("Recruitment"),
        null=True,
    )
    job_position_id = models.ForeignKey(
        JobPosition,
        on_delete=models.PROTECT,
        verbose_name=_("Job Position"),
        null=True,
    )
    answer_json = models.JSONField()
    attachment = models.FileField(upload_to=upload_path, null=True, blank=True)
    objects = HorillaCompanyManager(related_company_field="recruitment_id__company_id")

    @property
    def answer(self):
        """
        Used to convert the json to dict
        """
        # Convert the JSON data to a dictionary
        try:
            return json.loads(self.answer_json)
        except json.JSONDecodeError:
            return {}  # Return an empty dictionary if JSON is invalid or empty

    def __str__(self) -> str:
        return f"{self.candidate_id.name}-{self.recruitment_id}"


class SkillZone(HorillaModel):
    """ "
    Model for talent pool
    """

    title = models.CharField(max_length=50, verbose_name="Talent Pool")
    description = models.TextField(verbose_name=_("Description"), max_length=255)
    company_id = models.ForeignKey(
        Company,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        verbose_name=_("Company"),
    )
    objects = HorillaCompanyManager()

    class Meta:
        verbose_name = _("Talent Pool")
        verbose_name_plural = _("Talent Pools")

    def get_active(self):
        return SkillZoneCandidate.objects.filter(is_active=True, skill_zone_id=self)

    def __str__(self) -> str:
        return self.title

    def get_avatar(self):
        """
        Method will retun the api to the avatar or path to the profile image
        """
        url = f"https://ui-avatars.com/api/?name={self.title}&background=random"
        return url

    def candidate_count_display(self):
        count = self.skillzonecandidate_set.count()
        if count != 1:
            return f"{count} { _('Candidates') }"
        else:
            return f"{count} { _('Candidate') }"

    def get_skill_zone_url(self):
        """
        This method returns the talent pool URL with the title as a query parameter.
        """
        base_url = reverse("skill-zone-view")
        query_string = urlencode({"search": self.title})
        return f"{base_url}?{query_string}"


class SkillZoneCandidate(HorillaModel):
    """
    Model for saving candidate data's for future recruitment
    """

    skill_zone_id = models.ForeignKey(
        SkillZone,
        verbose_name=_("Talent Pool"),
        related_name="skillzonecandidate_set",
        on_delete=models.PROTECT,
        null=True,
    )
    candidate_id = models.ForeignKey(
        Candidate,
        on_delete=models.PROTECT,
        null=True,
        related_name="skillzonecandidate_set",
        verbose_name=_("Candidate"),
    )
    # job_position_id=models.ForeignKey(
    #     JobPosition,
    #     on_delete=models.PROTECT,
    #     null=True,
    #     related_name="talent_pool",
    #     verbose_name=_("Job Position")
    # )

    reason = models.CharField(max_length=200, verbose_name=_("Reason"))
    added_on = models.DateField(auto_now_add=True)
    objects = HorillaCompanyManager(
        related_company_field="candidate_id__recruitment_id__company_id"
    )

    def clean(self):
        # Check for duplicate entries in the database
        duplicate_exists = (
            SkillZoneCandidate.objects.filter(
                candidate_id=self.candidate_id, skill_zone_id=self.skill_zone_id
            )
            .exclude(pk=self.pk)
            .exists()
        )

        if duplicate_exists:
            raise ValidationError(
                _(
                    f"Candidate {self.candidate_id} already exists in Talent Pool {self.skill_zone_id}."
                )
            )

        super().clean()

    def __str__(self) -> str:
        return str(self.candidate_id.get_full_name())

    class Meta:
        ordering = ["-id"]


class CandidateRating(HorillaModel):
    employee_id = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="candidate_rating"
    )
    candidate_id = models.ForeignKey(
        Candidate, on_delete=models.PROTECT, related_name="candidate_rating"
    )
    rating = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(5)]
    )

    class Meta:
        unique_together = ["employee_id", "candidate_id"]

    def __str__(self) -> str:
        return f"{self.employee_id} - {self.candidate_id} rating {self.rating}"


class RecruitmentGeneralSetting(HorillaModel):
    """
    RecruitmentGeneralSettings model
    """

    candidate_self_tracking = models.BooleanField(default=False)
    show_overall_rating = models.BooleanField(default=False)
    #: Career sites allowed to embed this company's public job listing in an
    #: iframe, one origin per line (e.g. https://careers.acme.com). Every other
    #: site is refused by the browser (CSP frame-ancestors).
    career_page_domains = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Career page domains"),
    )
    #: Public job-list address for this company: its name with hyphens
    #: (e.g. acme-manufacturing), used instead of the numeric company id.
    career_page_slug = models.SlugField(
        max_length=80, unique=True, null=True, blank=True,
        verbose_name=_("Career page link"),
    )
    company_id = models.OneToOneField(
        Company, on_delete=models.CASCADE, null=True, blank=True, unique=True
    )
    objects = HorillaCompanyManager()


class InterviewSchedule(HorillaModel):
    """
    Interview Scheduling Model
    """

    candidate_id = models.ForeignKey(
        Candidate,
        verbose_name=_("Candidate"),
        related_name="candidate_interview",
        on_delete=models.CASCADE,
    )
    employee_id = models.ManyToManyField(Employee, verbose_name=_("Interviewer"))
    interview_date = models.DateField(verbose_name=_("Interview Date"))
    interview_time = models.TimeField(verbose_name=_("Interview Time"))
    description = models.TextField(
        verbose_name=_("Description"),
        blank=True,
    )
    completed = models.BooleanField(
        default=False, verbose_name=_("Is Interview Completed")
    )
    objects = HorillaCompanyManager("candidate_id__recruitment_id__company_id")

    def __str__(self) -> str:
        return f"{self.candidate_id} -Interview."

    def candidate_custom_col(self):
        """
        method for candidate coloumn
        """
        return render_template(
            path="cbv/interview/candidate_custom_col.html",
            context={"instance": self},
        )

    def interviewer_custom_col(self):
        """
        method for interviewer coloumn
        """
        return render_template(
            path="cbv/interview/interviewer_custom_col.html",
            context={"instance": self},
        )

    def custom_color(self):
        """
        Custom background color for all rows with hover effect
        """
        # interviews = InterviewSchedule.objects.filter(
        #     employee_id=self.user.employee_get.id
        # )
        request = getattr(_thread_locals, "request", None)
        if not getattr(self, "request", None):
            self.request = request
        user = request.user
        if user.employee_get in self.employee_id.all():
            color = "rgba(255, 166, 0, 0.158)"
            hovering = "white"

            return (
                f'style="background-color: {color};" '
                f"onmouseover=\"this.style.backgroundColor='{hovering}';\" "
                f"onmouseout=\"this.style.backgroundColor='{color}';\""
            )

    def interviewer_detail(self):
        """
        interviewer in detail view
        """
        employees = self.employee_id.all()
        employee_names_string = ", ".join([str(employee) for employee in employees])
        return employee_names_string

    def detail_subtitle(self):
        """
        Return subtitle for detail view
        """
        return (
            f"{self.candidate_id.recruitment_id} / {self.candidate_id.job_position_id}"
        )

    def get_description(self):
        """
        get description
        """
        if self.description:
            return self.description
        else:
            return _("None")

    def status_custom_col(self):
        """
        method for status coloumn
        """
        now = datetime.now(tz=timezone.utc if settings.USE_TZ else None)
        return render_template(
            path="cbv/interview/status_custom_col.html",
            context={"instance": self, "now": now},
        )

    def custom_action_col(self):
        """
        method for actions coloumn
        """
        return render_template(
            path="cbv/interview/interview_actions.html",
            context={"instance": self},
        )

    def detail_view(self):
        """
        for detail view
        """

        url = reverse("interview-detail-view", kwargs={"pk": self.pk})
        return url

    def detail_view_actions(self):
        """
        detail view actions
        """
        return render_template(
            path="cbv/interview/detail_view_actions.html",
            context={"instance": self},
        )

    class Meta:
        verbose_name = _("Schedule Interview")
        verbose_name_plural = _("Schedule Interviews")


class Resume(models.Model):
    file = models.FileField(
        upload_to=upload_path,
        validators=[
            validate_pdf,
        ],
    )
    recruitment_id = models.ForeignKey(
        Recruitment, on_delete=models.CASCADE, related_name="resume"
    )
    is_candidate = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.recruitment_id} - Resume {self.pk}"


STATUS = [
    ("requested", "Requested"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
]

FORMATS = [
    ("any", "Any"),
    ("pdf", "PDF"),
    ("txt", "TXT"),
    ("docx", "DOCX"),
    ("xlsx", "XLSX"),
    ("jpg", "JPG"),
    ("png", "PNG"),
    ("jpeg", "JPEG"),
]


class CandidateDocumentRequest(HorillaModel):
    title = models.CharField(max_length=100, verbose_name=_("Title"))
    candidate_id = models.ManyToManyField(Candidate)
    format = models.CharField(choices=FORMATS, max_length=10, verbose_name=_("Format"))
    max_size = models.IntegerField(
        blank=True, null=True, verbose_name=_("Max size (In MB)")
    )
    description = models.TextField(blank=True, null=True, verbose_name=_("Description"))
    objects = HorillaCompanyManager(
        related_company_field="candidate_id__recruitment_id__company_id"
    )

    def __str__(self):
        return self.title


class CandidateDocument(HorillaModel):
    #: Company-scoped through the candidate, which now carries company_id
    #: directly. This model previously had NO manager declared at all, so
    #: CandidateDocument.objects returned every tenant's documents.
    objects = HorillaCompanyManager("candidate_id__company_id")

    title = models.CharField(max_length=250, verbose_name=_("Title"))
    candidate_id = models.ForeignKey(
        Candidate, on_delete=models.PROTECT, verbose_name=_("Candidate")
    )
    document_request_id = models.ForeignKey(
        CandidateDocumentRequest, on_delete=models.PROTECT, null=True
    )
    document = models.FileField(upload_to=upload_path, null=True)
    #: Set when this document is the answer to a file-upload question, so a
    #: multi-file answer is several documents rather than a second store.
    job_opening_question = models.ForeignKey(
        "recruitment.JobOpeningQuestion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="documents",
        verbose_name=_("Question"),
    )
    status = models.CharField(
        choices=STATUS, max_length=10, default="requested", verbose_name=_("Status")
    )
    reject_reason = models.TextField(
        blank=True, null=True, max_length=255, verbose_name=_("Rejection Reason")
    )
    #: Where the file came from. Every candidate file lives in this table,
    #: the resume included (PRD: one source of truth for documents).
    DOCUMENT_TYPES = (
        ("resume", _("Resume")),
        ("application", _("Application form")),
        ("handoff", _("Hiring handoff")),
        ("uploaded", _("Uploaded by HR")),
    )
    document_type = models.CharField(
        max_length=20, choices=DOCUMENT_TYPES, default="uploaded",
        verbose_name=_("Document type"),
    )

    class Meta:
        constraints = [
            # One current resume per application; older versions stay with
            # is_active=False.
            models.UniqueConstraint(
                fields=["candidate_id"],
                condition=models.Q(document_type="resume", is_active=True),
                name="recruitment_one_active_resume_per_candidate",
            )
        ]

    def __str__(self):
        return f"{self.candidate_id} - {self.title}"

    def clean(self, *args, **kwargs):
        super().clean(*args, **kwargs)
        file = self.document

        if len(self.title) < 3:
            raise ValidationError({"title": _("Title must be at least 3 characters")})

        if file and not self.document_request_id:
            # Uploaded directly against the candidate (HR uploading an
            # assignment, a document received by email, and so on). There is no
            # document request to carry a format or size limit, so previously
            # this branch validated NOTHING -- any file of any size was
            # accepted. The Candidate Pool rule applies instead: real PDF, 15 MB.
            try:
                validate_candidate_document(file)
            except ValidationError as error:
                raise ValidationError({"document": error.messages})

        if file and self.document_request_id:
            format = self.document_request_id.format
            max_size = self.document_request_id.max_size
            if max_size:
                if file.size > max_size * 1024 * 1024:
                    raise ValidationError(
                        {"document": _("File size exceeds the limit")}
                    )

            # Use the true final extension so a double extension such as
            # "file.pdf.html" cannot bypass the format check and enable stored
            # XSS when served. See GHSA-p68r-g665-5cm9.
            ext = os.path.splitext(file.name)[1].lstrip(".").lower()
            if format == "any":
                pass
            elif ext != format:
                raise ValidationError(
                    {"document": _("Please upload {} file only.").format(format)}
                )


class LinkedInAccount(HorillaModel):
    username = models.CharField(max_length=250, verbose_name=_("App Name"))
    email = models.EmailField(max_length=254, verbose_name=_("Email"))
    api_token = models.CharField(max_length=500, verbose_name=_("API Token"))
    sub_id = models.CharField(max_length=250, unique=True)
    company_id = models.ForeignKey(
        Company, on_delete=models.CASCADE, null=True, verbose_name=_("Company")
    )

    class Meta:
        verbose_name = _("LinkedIn Account")
        verbose_name_plural = _("LinkedIn Accounts")

    def __str__(self):
        return str(self.username)

    def clean(self, *args, **kwargs):
        super().clean(*args, **kwargs)
        url = "https://api.linkedin.com/v2/userinfo"
        headers = {"Authorization": f"Bearer {self.api_token}"}

        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            data = response.json()
            if not data["email"] == self.email:
                raise ValidationError({"email": _("Email mismatched.")})
            self.sub_id = response.json()["sub"]
        else:
            raise ValidationError(_("Check the credentials"))

    def action_template(self):
        """
        This method for get custom column for managers.
        """
        return render_template(
            path="linkedin/linkedin_action.html",
            context={"instance": self},
        )

    def is_active_toggle(self):
        """
        For toggle is_active field
        """
        url = f"update-isactive-linkedin-account/{self.id}"
        return render_template(
            path="is_active_toggle.html",
            context={"instance": self, "url": url},
        )


class RecruitmentAuditEvent(models.Model):
    """
    Append-only business-event audit trail for Recruitment.

    Distinct from the two field-diff audit systems already in the project
    (simple-history's HorillaAuditLog on Candidate/Stage/RejectedCandidate,
    and django-auditlog via AuditModelConfig). Those answer "which fields
    changed"; this answers "who performed what business action, when, for
    which company, against which object, and in what context" -- events like
    published/closed/rejected that are not field changes at all.

    Deliberately a plain models.Model, not HorillaModel: HorillaModel's
    save() stamps modified_by on every write, which contradicts an immutable
    record. base.EmailLog follows the same plain-model pattern.

    Always write through recruitment.services.audit.RecruitmentAuditService.
    """

    class EventType(models.TextChoices):
        """Business events. Extend as later Recruitment features land."""

        JOB_OPENING_CREATED = "JOB_OPENING_CREATED", _("Job opening created")
        JOB_OPENING_UPDATED = "JOB_OPENING_UPDATED", _("Job opening updated")
        JOB_OPENING_SUBMITTED_FOR_REVIEW = (
            "JOB_OPENING_SUBMITTED_FOR_REVIEW",
            _("Job opening submitted for review"),
        )
        JOB_OPENING_SENT_BACK_FOR_CHANGES = (
            "JOB_OPENING_SENT_BACK_FOR_CHANGES",
            _("Job opening sent back for changes"),
        )
        JOB_OPENING_PUBLISHED = "JOB_OPENING_PUBLISHED", _("Job opening published")
        JOB_OPENING_CLOSED = "JOB_OPENING_CLOSED", _("Job opening closed")
        JOB_OPENING_REMOVED = "JOB_OPENING_REMOVED", _("Job opening removed")

        # --- Screening questions (Feature 2) ---
        # Question bank and template configuration. Recorded against
        # object_type/object_id, since RecruitmentSurvey and SurveyTemplate have
        # no dedicated FK column here.
        QUESTION_CREATED = "QUESTION_CREATED", _("Screening question created")
        QUESTION_UPDATED = "QUESTION_UPDATED", _("Screening question updated")
        QUESTION_ARCHIVED = "QUESTION_ARCHIVED", _("Screening question archived")
        TEMPLATE_CREATED = "TEMPLATE_CREATED", _("Screening template created")
        TEMPLATE_UPDATED = "TEMPLATE_UPDATED", _("Screening template updated")
        QUESTION_ADDED_TO_TEMPLATE = (
            "QUESTION_ADDED_TO_TEMPLATE",
            _("Question added to template"),
        )
        QUESTION_REMOVED_FROM_TEMPLATE = (
            "QUESTION_REMOVED_FROM_TEMPLATE",
            _("Question removed from template"),
        )
        QUESTION_MADE_MANDATORY = (
            "QUESTION_MADE_MANDATORY",
            _("Question made mandatory in template"),
        )
        QUESTION_MADE_OPTIONAL = (
            "QUESTION_MADE_OPTIONAL",
            _("Question made optional in template"),
        )
        # Publication freeze. JOB_OPENING_PUBLISHED remains the lifecycle
        # event; this records what was frozen, in the same transaction.
        JOB_OPENING_QUESTIONS_SNAPSHOTTED = (
            "JOB_OPENING_QUESTIONS_SNAPSHOTTED",
            _("Job opening questions snapshotted"),
        )
        CANDIDATE_ANSWER_SUBMITTED = (
            "CANDIDATE_ANSWER_SUBMITTED",
            _("Candidate screening answers submitted"),
        )
        CANDIDATE_ANSWER_UPDATED = (
            "CANDIDATE_ANSWER_UPDATED",
            _("Candidate screening answers updated"),
        )

        # --- Candidate Pool (Feature 3) ---
        CANDIDATE_CREATED = "CANDIDATE_CREATED", _("Candidate created")
        CANDIDATE_MAPPED_TO_JOB_OPENING = (
            "CANDIDATE_MAPPED_TO_JOB_OPENING",
            _("Candidate mapped to job opening"),
        )
        CANDIDATE_DOCUMENT_UPLOADED = (
            "CANDIDATE_DOCUMENT_UPLOADED",
            _("Candidate document uploaded"),
        )
        CANDIDATE_NOTE_ADDED = "CANDIDATE_NOTE_ADDED", _("Candidate note added")
        CANDIDATE_STAGE_CHANGED = (
            "CANDIDATE_STAGE_CHANGED",
            _("Candidate stage changed"),
        )
        CANDIDATE_REJECTED = "CANDIDATE_REJECTED", _("Candidate rejected")
        CANDIDATE_HIRED = "CANDIDATE_HIRED", _("Candidate hired")
        CANDIDATE_EXPORTED = "CANDIDATE_EXPORTED", _("Candidate data exported")
        CANDIDATE_ANONYMIZED = "CANDIDATE_ANONYMIZED", _("Candidate anonymized")
        DOCUMENT_RETENTION_CLEANUP = (
            "DOCUMENT_RETENTION_CLEANUP",
            _("Document retention cleanup"),
        )

        # --- Public application / handoff (Feature 4) ---
        APPLICATION_SUBMITTED = (
            "APPLICATION_SUBMITTED",
            _("Application submitted"),
        )
        CONTACT_VERIFICATION_STARTED = (
            "CONTACT_VERIFICATION_STARTED",
            _("Contact verification started"),
        )
        CONTACT_VERIFICATION_COMPLETED = (
            "CONTACT_VERIFICATION_COMPLETED",
            _("Contact verification completed"),
        )
        HANDOFF_FORM_SUBMITTED = (
            "HANDOFF_FORM_SUBMITTED",
            _("Hiring handoff form submitted"),
        )
        APPLICATION_LINK_SENT = "APPLICATION_LINK_SENT", _("Application link emailed")
        CONTACT_EMAIL_VERIFIED = "CONTACT_EMAIL_VERIFIED", _("Email verified")
        CONTACT_OTP_SENT = "CONTACT_OTP_SENT", _("Verification code sent")
        CONTACT_OTP_SEND_FAILED = (
            "CONTACT_OTP_SEND_FAILED",
            _("Verification code could not be sent"),
        )
        CONTACT_OTP_FAILED = "CONTACT_OTP_FAILED", _("Wrong verification code")
        CANDIDATE_CONTACT_VERIFIED = (
            "CANDIDATE_CONTACT_VERIFIED",
            _("Candidate contact verified"),
        )

        # --- Pipeline set-up ---
        STAGE_CREATED = "STAGE_CREATED", _("Stage created")
        STAGE_UPDATED = "STAGE_UPDATED", _("Stage updated")
        STAGE_DELETED = "STAGE_DELETED", _("Stage deleted")
        STAGE_MANAGERS_CHANGED = "STAGE_MANAGERS_CHANGED", _("Stage managers changed")

        # --- Questions (remaining) ---
        QUESTION_DELETED = "QUESTION_DELETED", _("Screening question deleted")
        TEMPLATE_DELETED = "TEMPLATE_DELETED", _("Screening template deleted")
        JOB_OPENING_QUESTION_ADDED = (
            "JOB_OPENING_QUESTION_ADDED",
            _("Question added to job opening"),
        )
        JOB_OPENING_QUESTION_REMOVED = (
            "JOB_OPENING_QUESTION_REMOVED",
            _("Question removed from job opening"),
        )

        # --- Interviews (Google Calendar) ---
        INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED", _("Interview scheduled")
        INTERVIEW_RESCHEDULED = "INTERVIEW_RESCHEDULED", _("Interview rescheduled")
        INTERVIEW_CANCELLED = "INTERVIEW_CANCELLED", _("Interview cancelled")

    event_type = models.CharField(
        max_length=64,
        choices=EventType.choices,
        db_index=True,
        verbose_name=_("Event"),
    )
    # SET_NULL, not CASCADE: deleting a user must never erase the record of
    # what they did. Nullable also covers automated events on a database with
    # no "Horilla Bot" row.
    actor = models.ForeignKey(
        HorillaUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recruitment_audit_events",
        verbose_name=_("Actor"),
    )
    company_id = models.ForeignKey(
        Company,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Company"),
    )
    job_opening = models.ForeignKey(
        "recruitment.Recruitment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name=_("Job Opening"),
    )
    # Candidate doubles as the application in this schema -- one Candidate row
    # per (email, recruitment) pair is one application.
    candidate = models.ForeignKey(
        "recruitment.Candidate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name=_("Candidate"),
    )
    stage = models.ForeignKey(
        "recruitment.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name=_("Stage"),
    )
    # Kept alongside the typed FKs so an event survives its object being
    # deleted, and so events can be recorded against models that have no
    # dedicated column here.
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    # Business context only (previous/new status, remark, bulk id). Never
    # whole candidate records, documents or other unnecessary PII.
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = HorillaCompanyManager("company_id")

    class Meta:
        verbose_name = _("Recruitment Audit Event")
        verbose_name_plural = _("Recruitment Audit Events")
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["job_opening", "-timestamp"]),
            models.Index(fields=["candidate", "-timestamp"]),
            models.Index(fields=["event_type", "-timestamp"]),
            models.Index(fields=["object_type", "object_id"]),
        ]

    def __str__(self):
        return f"{self.event_type} - {self.object_type}#{self.object_id}"

    def save(self, *args, **kwargs):
        """Insert-only: an existing audit record can never be rewritten."""
        if self.pk is not None:
            raise ValidationError(
                _("Recruitment audit events are immutable and cannot be edited.")
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Audit records are historical evidence and are never deleted."""
        raise ValidationError(_("Recruitment audit events cannot be deleted."))


class InterviewMeeting(HorillaModel):
    """
    The Google Calendar interview for one candidate at one stage (PRD: one
    interview per stage).

    Scheduling stays in Google (PRD): "Schedule Interview" opens a prefilled
    Calendar event whose description carries ``reference``. Krew then finds
    that event in the scheduler's calendar (read through their connected
    Google account) and stores the Meet link, so "Join Meeting" works from
    Krew. The lookup runs when the user clicks "Fetch Meeting Link".
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Waiting for Google")
        LINKED = "linked", _("Scheduled")
        CANCELLED = "cancelled", _("Cancelled in Google")

    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="interview_meetings"
    )
    stage = models.ForeignKey(
        Stage, on_delete=models.CASCADE, related_name="interview_meetings"
    )
    #: Written into the Google event's description; how the event is found.
    reference = models.CharField(max_length=20, unique=True, editable=False)
    #: Whose Google calendar is searched: the person who clicked Schedule.
    scheduled_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    google_event_id = models.CharField(max_length=255, blank=True, default="")
    meet_url = models.URLField(blank=True, default="")
    #: The event in Google Calendar (fallback when it has no Meet link).
    event_url = models.URLField(max_length=500, blank=True, default="")
    start_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    objects = HorillaCompanyManager("candidate__company_id")

    class Meta:
        verbose_name = _("Interview Meeting")
        verbose_name_plural = _("Interview Meetings")
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "stage"],
                name="recruitment_one_interview_per_candidate_stage",
            )
        ]

    def __str__(self):
        return f"{self.candidate} - {self.stage} ({self.reference})"

    @property
    def join_url(self):
        return self.meet_url or self.event_url
