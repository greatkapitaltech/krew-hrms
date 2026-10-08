"""
views.py

Plain function-based views (matching this codebase's older FBV convention,
e.g. recruitment/views/mail_templates.py) for:
  - DocumentTemplate CRUD + preview (the CKEditor-based template editor)
  - TemplateAsset CRUD (uploading reusable logos/signatures/attachments)
  - generating a document from a template for an employee/candidate, and
    downloading the result

Multi-tenancy is automatic: every queryset below goes through
HorillaCompanyManager (see docs/TECH_OVERVIEW.md §3), so a user only ever
sees templates/assets/generated documents for their selected company.
"""

import mammoth
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import Context, Template as DjangoTemplate
from django.urls import reverse
from django.utils.translation import gettext as _

from base.methods import paginator_qry
from horilla.decorators import hx_request_required, login_required, permission_required
from horilla.http import HorillaRedirect

from .forms import DocumentTemplateForm, GenerateDocumentForm, TemplateAssetForm
from .merge_context import SAMPLE_CONTEXT, build_context
from .models import (
    DocumentTemplate,
    DocumentType,
    GeneratedDocument,
    TemplateAsset,
    resolve_default_template,
)
from .pdf import html_to_pdf


# ---------------------------------------------------------------------------
# Document templates
# ---------------------------------------------------------------------------


@login_required
@permission_required("document_templates.view_documenttemplate")
def document_template_list(request):
    templates = DocumentTemplate.objects.all()

    active_type = request.GET.get("type")
    if active_type:
        templates = templates.filter(document_type=active_type)

    templates = paginator_qry(templates, request.GET.get("page"))
    return render(
        request,
        "document_templates/template_list.html",
        {
            "templates": templates,
            "document_types": DocumentType.choices,
            "active_type": active_type,
        },
    )


def _apply_docx_upload(form: DocumentTemplateForm) -> list:
    """
    If a .docx was uploaded, convert it to HTML (mammoth) and use that as
    the instance's content + keep the original file as source_file - run
    on the form's not-yet-saved instance, before form.save(). Returns
    mammoth's own list of "couldn't fully convert this formatting"
    messages (empty list when no .docx was uploaded, or none to report).
    """
    docx_file = form.cleaned_data.get("docx_file")
    if not docx_file:
        return []
    result = mammoth.convert_to_html(docx_file)
    form.instance.content = result.value
    docx_file.seek(0)
    form.instance.source_file = docx_file
    return [str(m) for m in result.messages]


def _save_document_template_form(request, form: DocumentTemplateForm):
    """
    Shared by create/update: applies a .docx upload if present, then
    re-validates (content set by mammoth was never checked by the form's
    own is_valid() - that ran before _apply_docx_upload touched it, same
    reasoning as template_asset_create's own post-validation full_clean
    below) before saving. Returns the saved instance, or None with errors
    added onto `form` if validation now fails.
    """
    conversion_messages = _apply_docx_upload(form)
    try:
        form.instance.full_clean()
    except ValidationError as exc:
        for field, errs in exc.message_dict.items():
            for err in errs:
                form.add_error(field if field in form.fields else None, err)
        return None

    template = form.save()
    for msg in conversion_messages:
        messages.warning(request, msg)
    if template.unmapped_placeholders:
        messages.warning(
            request,
            _("Unrecognized placeholders: %(fields)s")
            % {"fields": ", ".join(template.unmapped_placeholders)},
        )
    return template


@login_required
@hx_request_required
@permission_required("document_templates.add_documenttemplate")
def document_template_create(request):
    if request.method == "POST":
        form = DocumentTemplateForm(request.POST, request.FILES)
        if form.is_valid():
            template = _save_document_template_form(request, form)
            if template:
                messages.success(request, _("Document template created."))
                return HorillaRedirect(request, reverse("document-template-list"))
    else:
        form = DocumentTemplateForm()
    return render(request, "document_templates/template_form.html", {"form": form})


@login_required
@hx_request_required
@permission_required("document_templates.change_documenttemplate")
def document_template_update(request, pk):
    template = get_object_or_404(DocumentTemplate, pk=pk)
    if request.method == "POST":
        form = DocumentTemplateForm(request.POST, request.FILES, instance=template)
        if form.is_valid():
            saved = _save_document_template_form(request, form)
            if saved:
                messages.success(request, _("Document template updated."))
                return HorillaRedirect(request, reverse("document-template-list"))
    else:
        form = DocumentTemplateForm(instance=template)
    return render(
        request,
        "document_templates/template_form.html",
        {"form": form, "template": template},
    )


@login_required
@permission_required("document_templates.delete_documenttemplate")
def document_template_delete(request, pk):
    template = get_object_or_404(DocumentTemplate, pk=pk)
    if request.method == "POST":
        template.delete()
        messages.success(request, _("Document template deleted."))
    return redirect("document-template-list")


@login_required
@permission_required("document_templates.view_documenttemplate")
def document_template_preview(request, pk):
    """Render the template with dummy sample data - no PDF, no save."""
    template = get_object_or_404(DocumentTemplate, pk=pk)
    rendered = _render_template_content(template.content, SAMPLE_CONTEXT)
    return render(
        request,
        "document_templates/template_preview.html",
        {"template": template, "rendered": rendered},
    )


def _render_template_content(content: str, context: dict) -> str:
    # autoescape stays on (Django's default): merge-field values (candidate
    # name, address, ...) are untrusted data and wkhtmltopdf executes JS in
    # the page it renders, so an unescaped "<script>" in a data field would
    # run during PDF generation. Only the HR-authored template HTML itself
    # (already trusted - see module docstring) is inserted unescaped.
    return DjangoTemplate(content).render(Context(context))


# ---------------------------------------------------------------------------
# Template assets (logos, signatures, letterheads, reference documents)
# ---------------------------------------------------------------------------


@login_required
@permission_required("document_templates.view_templateasset")
def template_asset_list(request):
    assets = TemplateAsset.objects.all()
    assets = paginator_qry(assets, request.GET.get("page"))
    return render(request, "document_templates/asset_list.html", {"assets": assets})


@login_required
@hx_request_required
@permission_required("document_templates.add_templateasset")
def template_asset_create(request):
    if request.method == "POST":
        form = TemplateAssetForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                asset = form.save(commit=False)
                asset.full_clean()
                asset.save()
                messages.success(request, _("Asset uploaded."))
                return HorillaRedirect(request, reverse("document-template-asset-list"))
            except ValidationError as exc:
                for field, errs in exc.message_dict.items():
                    for err in errs:
                        form.add_error(field if field in form.fields else None, err)
    else:
        form = TemplateAssetForm()
    return render(request, "document_templates/asset_form.html", {"form": form})


@login_required
@permission_required("document_templates.delete_templateasset")
def template_asset_delete(request, pk):
    asset = get_object_or_404(TemplateAsset, pk=pk)
    if request.method == "POST":
        asset.delete()
        messages.success(request, _("Asset deleted."))
    return redirect("document-template-asset-list")


# ---------------------------------------------------------------------------
# Generate / download
# ---------------------------------------------------------------------------


def _resolve_generate_target(form: GenerateDocumentForm):
    """employee, candidate, target - exactly one of employee/candidate set,
    target is whichever model instance merge_context.build_context() needs
    for this document_type (a Payslip for PAYSLIP, not just its employee)."""
    if form.cleaned_data.get("payslip_id"):
        # PAYSLIP templates merge from the payslip's own computed
        # pay_head_data, not just the employee - but a GeneratedDocument
        # still records the employee it's for (same as every other type).
        payslip = form.cleaned_data["payslip_id"]
        return payslip.employee_id, None, payslip
    if form.cleaned_data.get("employee_id"):
        employee = form.cleaned_data["employee_id"]
        return employee, None, employee
    candidate = form.cleaned_data["candidate_id"]
    return None, candidate, candidate


def _target_company(employee, candidate, target):
    """The company to resolve a default template against, from whichever
    of employee/candidate/payslip this generation is for - same FK paths
    merge_context.py's own builders already walk."""
    if employee is not None:
        work_info = getattr(employee, "employee_work_info", None)
        return getattr(work_info, "company_id", None)
    if candidate is not None:
        recruitment = getattr(candidate, "recruitment_id", None)
        return getattr(recruitment, "company_id", None)
    return None


def _field_label(field):
    return field.replace("_", " ").title()


def _extra_field_specs(template):
    """One {name, label} dict per placeholder in the template that has no
    automatic data source (not in RECOGNIZED_FIELDS for this document
    type) - these get a manual text input on the Generate form instead."""
    return [{"name": field, "label": _field_label(field)} for field in template.unmapped_placeholders]


def _extra_field_values(request, template):
    """User-submitted values for a template's manual-entry fields, keyed
    exactly as they appear in the template content (so they merge
    straight into the render context alongside the automatic fields).
    Anything left blank renders as a "[Field Name]" placeholder instead of
    disappearing, so it's obvious on the printed page what still needs to
    be filled in by hand."""
    values = {}
    for field in template.unmapped_placeholders:
        value = request.POST.get(f"extra__{field}", "").strip()
        values[field] = value or f"[{_field_label(field)}]"
    return values


def _generate_document(request, template, employee, candidate, target, extra_values=None):
    """Render -> PDF -> save GeneratedDocument -> redirect to its download
    URL. Shared by the per-template and auto-select generate views."""
    context_data = build_context(template.document_type, target)
    context_data.update(extra_values or {})
    rendered_html = _render_template_content(template.content, context_data)
    pdf_bytes = html_to_pdf(rendered_html)

    generated = GeneratedDocument(
        template=template,
        document_type=template.document_type,
        employee=employee,
        candidate=candidate,
        context_snapshot=context_data,
    )
    generated.full_clean(exclude=["rendered_file"])
    generated.rendered_file.save(
        f"{template.document_type.lower()}-{employee.pk if employee else candidate.pk}.pdf",
        ContentFile(pdf_bytes),
        save=False,
    )
    generated.save()

    messages.success(request, _("Document generated."))
    # Deliberately NOT an HX-Redirect straight to the PDF: htmx would set
    # `location.href` to it, and whether that actually shows/downloads
    # anything to the user depends on the browser's own PDF-handling
    # setting - some browsers navigate and silently download with no
    # visible feedback at all. A plain link (same pattern already used on
    # the generated-documents list page) behaves predictably everywhere.
    return render(
        request,
        "document_templates/generate_success.html",
        {"generated": generated},
    )


@login_required
@hx_request_required
@permission_required("document_templates.add_generateddocument")
def document_template_generate(request, pk):
    template = get_object_or_404(DocumentTemplate, pk=pk)

    document_type_display = template.get_document_type_display()
    extra_fields = _extra_field_specs(template)

    if request.method != "POST":
        return render(
            request,
            "document_templates/generate_form.html",
            {
                "template": template,
                "document_type": template.document_type,
                "document_type_display": document_type_display,
                "extra_fields": extra_fields,
                "form": GenerateDocumentForm(),
            },
        )

    form = GenerateDocumentForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "document_templates/generate_form.html",
            {
                "template": template,
                "document_type": template.document_type,
                "document_type_display": document_type_display,
                "extra_fields": extra_fields,
                "form": form,
            },
        )

    employee, candidate, target = _resolve_generate_target(form)
    extra_values = _extra_field_values(request, template)
    return _generate_document(request, template, employee, candidate, target, extra_values)


@login_required
@hx_request_required
@permission_required("document_templates.add_generateddocument")
def document_template_generate_auto(request, document_type):
    """
    Same employee/candidate/payslip picker as document_template_generate,
    but doesn't take a template pk - resolves the target's own company and
    picks that company's default template for this document_type, falling
    back to the global (company=None) default, per resolve_default_template.
    """
    if document_type not in DocumentType.values:
        return HttpResponse(status=404)
    document_type_display = DocumentType(document_type).label

    if request.method != "POST":
        return render(
            request,
            "document_templates/generate_form.html",
            {
                "document_type": document_type,
                "document_type_display": document_type_display,
                "form": GenerateDocumentForm(),
            },
        )

    form = GenerateDocumentForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "document_templates/generate_form.html",
            {
                "document_type": document_type,
                "document_type_display": document_type_display,
                "form": form,
            },
        )

    employee, candidate, target = _resolve_generate_target(form)
    company = _target_company(employee, candidate, target)
    template = resolve_default_template(document_type, company)
    if template is None:
        form.add_error(
            None,
            _(
                "No default template is configured for %(type)s - set one as "
                "Default for this company, or a global Default, first."
            )
            % {"type": document_type_display},
        )
        return render(
            request,
            "document_templates/generate_form.html",
            {
                "document_type": document_type,
                "document_type_display": document_type_display,
                "form": form,
            },
        )

    extra_fields = _extra_field_specs(template)
    if extra_fields and request.POST.get("extra_fields_submitted") != "1":
        # The template is only known once the employee/candidate/payslip
        # is picked (it depends on their company), so a template with
        # manual-entry fields needs a second step: re-show the same form,
        # now with the target locked in (the bound `form` keeps its
        # selected value) plus one text input per unmapped field.
        return render(
            request,
            "document_templates/generate_form.html",
            {
                "document_type": document_type,
                "document_type_display": document_type_display,
                "template": template,
                "extra_fields": extra_fields,
                "form": form,
            },
        )

    extra_values = _extra_field_values(request, template)
    return _generate_document(request, template, employee, candidate, target, extra_values)


def _can_access_generated_document(request, generated: GeneratedDocument) -> bool:
    employee = getattr(request.user, "employee_get", None)
    if generated.employee_id and employee and generated.employee_id == employee.pk:
        return True
    return request.user.has_perm("document_templates.view_generateddocument")


@login_required
def generated_document_download(request, pk):
    generated = get_object_or_404(GeneratedDocument, pk=pk)
    if not _can_access_generated_document(request, generated):
        return HttpResponse(status=403)
    return FileResponse(
        generated.rendered_file.open("rb"),
        as_attachment=False,
        filename=f"{generated.document_type.lower()}-{generated.pk}.pdf",
    )


@login_required
@permission_required("document_templates.view_generateddocument")
def generated_document_list(request):
    generated_documents = GeneratedDocument.objects.select_related(
        "template", "employee", "candidate"
    ).all()

    employee_id = request.GET.get("employee_id")
    if employee_id:
        generated_documents = generated_documents.filter(employee_id=employee_id)

    doc_type = request.GET.get("type")
    if doc_type:
        generated_documents = generated_documents.filter(document_type=doc_type)

    query = request.GET.get("q", "").strip()
    if query:
        generated_documents = generated_documents.filter(
            Q(template__title__icontains=query)
            | Q(employee__employee_first_name__icontains=query)
            | Q(employee__employee_last_name__icontains=query)
            | Q(candidate__name__icontains=query)
        )

    sort = request.GET.get("sort", "-created_at")
    allowed_sorts = {"created_at", "-created_at", "document_type", "-document_type"}
    if sort in allowed_sorts:
        generated_documents = generated_documents.order_by(sort)

    generated_documents = paginator_qry(generated_documents, request.GET.get("page"))

    return render(
        request,
        "document_templates/generated_list.html",
        {
            "generated_documents": generated_documents,
            "document_types": DocumentType.choices,
            "active_type": doc_type or "",
            "query": query,
            "sort": sort,
        },
    )
