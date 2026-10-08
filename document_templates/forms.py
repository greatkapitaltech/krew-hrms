from django import forms
from django.utils.translation import gettext_lazy as _

from base.forms import ModelForm
from employee.models import Employee
from payroll.models.models import Payslip
from recruitment.models import Candidate

from .models import DocumentTemplate, TemplateAsset

SELECT2_ATTRS = {"class": "oh-select oh-select-2"}


class DocumentTemplateForm(ModelForm):
    """
    `docx_file` is deliberately not in Meta.fields - it's not a model field
    itself, just an alternate way to fill `content`: the view converts it to
    HTML (via mammoth) and writes the result into `content` before saving,
    same as if it had been typed into the CKEditor box directly.
    """

    docx_file = forms.FileField(
        required=False,
        label=_("Upload .docx (optional)"),
        help_text=_(
            "Upload a Word document with {{ field }}-style placeholders instead "
            "of typing content below - it replaces whatever is in the editor."
        ),
    )

    class Meta:
        model = DocumentTemplate
        fields = ["title", "document_type", "company", "content", "is_default"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # content can come from docx_file instead of being typed in -
        # enforced by clean() below, not by the field's own required flag.
        self.fields["content"].required = False

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("content") and not cleaned_data.get("docx_file"):
            raise forms.ValidationError(
                _("Either type content in the editor or upload a .docx file.")
            )
        return cleaned_data


class TemplateAssetForm(ModelForm):
    class Meta:
        model = TemplateAsset
        fields = ["name", "asset_type", "company", "file"]


class GenerateDocumentForm(forms.Form):
    """
    Who/what to generate the document for - exactly one of the three.
    Which one is expected depends on the template's document_type: a
    Candidate for OFFER_LETTER, an existing Payslip for PAYSLIP (merge data
    comes from the payslip's own computed pay_head_data, not just the
    employee), an Employee for everything else.
    """

    employee_id = forms.ModelChoiceField(
        queryset=Employee.objects.filter(is_active=True),
        required=False,
        label="Employee",
        widget=forms.Select(attrs=SELECT2_ATTRS),
    )
    candidate_id = forms.ModelChoiceField(
        queryset=Candidate.objects.all(),
        required=False,
        label="Candidate",
        widget=forms.Select(attrs=SELECT2_ATTRS),
    )
    payslip_id = forms.ModelChoiceField(
        queryset=Payslip.objects.all(),
        required=False,
        label="Payslip",
        widget=forms.Select(attrs=SELECT2_ATTRS),
    )

    def clean(self):
        cleaned_data = super().clean()
        provided = [
            cleaned_data.get("employee_id"),
            cleaned_data.get("candidate_id"),
            cleaned_data.get("payslip_id"),
        ]
        if sum(bool(v) for v in provided) != 1:
            raise forms.ValidationError(
                "Provide exactly one of employee_id, candidate_id or payslip_id."
            )
        return cleaned_data
