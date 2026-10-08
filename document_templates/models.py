"""
models.py

HR-authored document templates (offer letter, relieving letter, payslip,
experience letter, ...), reusable image/document assets to insert into them
(logo, signature, letterhead), and a record of every document generated from
a template for a specific employee/candidate.
"""

import os

from ckeditor_uploader.fields import RichTextUploadingField
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company
from employee.models import Employee
from horilla.models import HorillaModel, upload_path

# Images are deliberately excluded from assets to avoid inline script
# execution when an asset is referenced by URL/embedded in a rendered
# document (same reasoning as horilla_documents.Document.clean's guard
# against double-extension / disguised uploads, GHSA-p68r-g665-5cm9).
ASSET_ALLOWED_EXTENSIONS = {
    "IMAGE": {"png", "jpg", "jpeg"},
    "DOCUMENT": {"pdf", "docx"},
}


class DocumentType(models.TextChoices):
    OFFER_LETTER = "OFFER_LETTER", _("Offer Letter")
    RELIEVING_LETTER = "RELIEVING_LETTER", _("Relieving Letter")
    EXPERIENCE_LETTER = "EXPERIENCE_LETTER", _("Experience Letter")
    PAYSLIP = "PAYSLIP", _("Payslip")
    CUSTOM = "CUSTOM", _("Custom")


class DocumentTemplate(HorillaModel):
    """
    One HR-authored, CKEditor-edited document template. `content` holds HTML
    with Django-template merge-field placeholders (e.g. "{{ employee_name }}")
    that get resolved per document_type by document_templates.merge_context.
    """

    title = models.CharField(max_length=150, verbose_name=_("Title"))
    document_type = models.CharField(
        max_length=30,
        choices=DocumentType.choices,
        verbose_name=_("Document Type"),
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Company"),
        help_text=_("Leave blank to make this template available to every company."),
    )
    content = RichTextUploadingField(
        verbose_name=_("Content"),
        config_name="document_template",
    )
    is_default = models.BooleanField(
        default=False,
        verbose_name=_("Is Default"),
        help_text=_(
            "The template used when generating a document of this type for this "
            "company, if more than one exists."
        ),
    )
    source_file = models.FileField(
        upload_to=upload_path,
        null=True,
        blank=True,
        verbose_name=_("Source .docx"),
        help_text=_(
            "The originally uploaded Word document, if this template's content "
            "was uploaded rather than typed in directly - kept for reference "
            "and re-upload, not used at render time."
        ),
    )
    unmapped_placeholders = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Unmapped Placeholders"),
        help_text=_(
            "{{ field }} placeholders found in content that aren't recognized "
            "merge fields for this document type - recomputed on every save."
        ),
    )

    objects = HorillaCompanyManager(related_company_field="company")

    class Meta:
        verbose_name = _("Document Template")
        verbose_name_plural = _("Document Templates")
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.title} ({self.get_document_type_display()})"

    def save(self, *args, **kwargs):
        # Local import: placeholders.py imports DocumentType from this
        # module, so importing it at module level here would be circular.
        from .placeholders import unmapped_placeholders as compute_unmapped

        self.unmapped_placeholders = compute_unmapped(self.document_type, self.content)
        super().save(*args, **kwargs)
        if self.is_default:
            DocumentTemplate.objects.filter(
                company=self.company, document_type=self.document_type
            ).exclude(pk=self.pk).update(is_default=False)

    def get_edit_url(self):
        return reverse_lazy("document-template-update", args=[self.pk])

    def get_delete_url(self):
        return reverse_lazy("document-template-delete", args=[self.pk])

    def get_preview_url(self):
        return reverse_lazy("document-template-preview", args=[self.pk])

    def get_generate_url(self):
        return reverse_lazy("document-template-generate", args=[self.pk])


def resolve_default_template(document_type, company):
    """
    The template auto-generation should use for this document_type and
    company: that company's own default if it set one, else the global
    (company=None) default, else None if neither exists. Both "the default
    for this company" and "the global default" are already unique rows -
    DocumentTemplate.save() unsets is_default on every sibling of the same
    (company, document_type) pair whenever a new one is marked default.

    `company` here is the *target's* company (the employee/candidate/payslip
    this document is being generated for), which may not be the same company
    the requesting HR user currently has selected - HorillaCompanyManager's
    queryset auto-filters by the latter (see docs/TECH_OVERVIEW.md §3), so a
    plain `DocumentTemplate.objects.filter(company=company)` would silently
    return nothing whenever those two differ. Resolve under the target's own
    company context instead, same pattern horilla.testkit's
    CompanyFilterTestMixin uses for this exact situation, restoring
    whatever was selected before regardless of outcome.
    """
    from horilla.horilla_middlewares import get_selected_company, set_selected_company

    previous = get_selected_company()
    try:
        set_selected_company(company.pk if company else None)
        company_default = DocumentTemplate.objects.filter(
            document_type=document_type, company=company, is_default=True
        ).first()
        if company_default:
            return company_default
        return DocumentTemplate.objects.filter(
            document_type=document_type, company__isnull=True, is_default=True
        ).first()
    finally:
        set_selected_company(previous)


class TemplateAsset(HorillaModel):
    """
    Reusable image/document HR can insert into a DocumentTemplate (company
    logo, authorized signatory's signature, a standard attachment) -
    distinct from CKEditor's own inline "drop an image while typing" upload,
    which is for one-off images rather than named, reusable assets.
    """

    class AssetType(models.TextChoices):
        IMAGE = "IMAGE", _("Image")
        DOCUMENT = "DOCUMENT", _("Document")

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Company"),
    )
    name = models.CharField(max_length=150, verbose_name=_("Name"))
    asset_type = models.CharField(
        max_length=10, choices=AssetType.choices, verbose_name=_("Asset Type")
    )
    file = models.FileField(upload_to=upload_path, verbose_name=_("File"))

    objects = HorillaCompanyManager(related_company_field="company")

    class Meta:
        verbose_name = _("Template Asset")
        verbose_name_plural = _("Template Assets")
        ordering = ("-created_at",)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.file:
            # True final extension only - a double extension such as
            # "logo.png.html" must be rejected, matching
            # horilla_documents.Document.clean's reasoning.
            ext = os.path.splitext(self.file.name)[1].lstrip(".").lower()
            allowed = ASSET_ALLOWED_EXTENSIONS.get(self.asset_type, set())
            if ext not in allowed:
                raise ValidationError(
                    {
                        "file": _("Allowed file types for %(type)s: %(exts)s")
                        % {
                            "type": self.get_asset_type_display(),
                            "exts": ", ".join(sorted(allowed)),
                        }
                    }
                )

    def get_delete_url(self):
        return reverse_lazy("document-template-asset-delete", args=[self.pk])


class GeneratedDocument(HorillaModel):
    """
    Audit record + the actual produced PDF for one "generate a document from
    a template for this person" action.
    """

    template = models.ForeignKey(
        DocumentTemplate,
        on_delete=models.PROTECT,
        related_name="generated_documents",
        verbose_name=_("Template"),
    )
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="generated_documents",
        verbose_name=_("Employee"),
    )
    candidate = models.ForeignKey(
        "recruitment.Candidate",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="generated_documents",
        verbose_name=_("Candidate"),
    )
    rendered_file = models.FileField(upload_to=upload_path, verbose_name=_("File"))
    context_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Merge Data"),
        help_text=_("The merge-field data used to render this document, for audit."),
    )

    # Scoped via the template's company rather than employee/candidate -
    # a GeneratedDocument always has exactly one of those two set (whichever
    # the source was, employee or candidate), so there's no single field
    # path that covers both; template.company is always present.
    objects = HorillaCompanyManager(related_company_field="template__company")

    class Meta:
        verbose_name = _("Generated Document")
        verbose_name_plural = _("Generated Documents")
        ordering = ("-created_at",)

    def __str__(self):
        target = self.employee or self.candidate
        return f"{self.get_document_type_display()} - {target}"

    def clean(self):
        super().clean()
        if bool(self.employee_id) == bool(self.candidate_id):
            raise ValidationError(
                _("Set exactly one of employee or candidate, not both or neither.")
            )

    def get_download_url(self):
        return reverse_lazy("generated-document-download", args=[self.pk])
