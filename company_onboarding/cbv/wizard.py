"""
company_onboarding/cbv/wizard.py

The 3-step Company Registration wizard. Not built on HorillaFormView (that
framework is single-model/single-form, built around "one form -> save ->
close modal -> refresh list") — this is full-page, multi-model-per-step,
and doubles as the ongoing edit screen post-Active. See the Company Setup
plan for the full design rationale.

Repeatable rows use Django's ModelForm `prefix=` support
(`RowForm(data, prefix=f"{prefix}{index}")`) rather than a Django formset —
see wizard_utils.discover_row_indices for how the row count is recovered
from POST on submit.
"""

from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from base.models import Company
from horilla.decorators import hx_request_required, login_required, permission_required
from horilla.methods import handle_no_permission

from company_onboarding.forms import (
    CompanyBankDetailsForm,
    CompanyComplianceForm,
    CompanyContractForm,
    CompanyIdentityForm,
    POCContactRowForm,
    SignatoryRowForm,
    StateRegistrationRowForm,
)
from company_onboarding.models import (
    CompanyBankDetails,
    CompanyBrandedTemplate,
    CompanyContract,
    CompanyDocument,
    CompanyPOCContact,
    CompanySignatory,
    CompanyStateRegistration,
)
from company_onboarding.wizard_utils import (
    CONTRACT_TERM_FIELDS,
    contract_terms_changed,
    discover_row_indices,
    get_bank_details,
    validate_for_active,
)


def _existing_instance(model, data, prefix, company):
    """
    Every row partial renders a hidden `{prefix}-row_id` field alongside
    its real ModelForm fields (not a model field itself — just wizard
    bookkeeping) so that reconstructing a row form from POST can bind it
    back to the SAME existing instance it started as, rather than always
    landing on an unbound/blank instance that would silently duplicate the
    row on save instead of updating it.
    """
    row_id = data.get(f"{prefix}-row_id")
    if not row_id:
        return None
    return model.objects.filter(pk=row_id, company=company).first()


def _build_state_row_forms(company, data=None, strict=False):
    """GET (data=None): one form per existing row, prefixed "state{i}"."""
    if data is None:
        return [
            StateRegistrationRowForm(instance=row, prefix=f"state{i}")
            for i, row in enumerate(company.state_registrations.all(), start=1)
        ]
    forms = []
    for index in discover_row_indices(data, "state", "state"):
        prefix = f"state{index}"
        instance = _existing_instance(
            CompanyStateRegistration, data, prefix, company
        )
        forms.append(
            StateRegistrationRowForm(
                data, prefix=prefix, instance=instance, strict=strict
            )
        )
    return forms


def _build_poc_row_forms(company, data=None, strict=False):
    if data is None:
        return [
            POCContactRowForm(instance=row, prefix=f"poc{i}")
            for i, row in enumerate(company.poc_contacts.all(), start=1)
        ]
    forms = []
    for index in discover_row_indices(data, "poc", "name"):
        prefix = f"poc{index}"
        instance = _existing_instance(CompanyPOCContact, data, prefix, company)
        forms.append(
            POCContactRowForm(data, prefix=prefix, instance=instance, strict=strict)
        )
    return forms


def _build_signatory_row_forms(company, data=None, strict=False):
    if data is None:
        return [
            SignatoryRowForm(instance=row, prefix=f"signatory{i}")
            for i, row in enumerate(company.signatories.all(), start=1)
        ]
    forms = []
    for index in discover_row_indices(data, "signatory", "name"):
        prefix = f"signatory{index}"
        instance = _existing_instance(CompanySignatory, data, prefix, company)
        forms.append(
            SignatoryRowForm(data, prefix=prefix, instance=instance, strict=strict)
        )
    return forms


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required("base.add_company"), name="dispatch")
class Step1View(View):
    """
    Identity & Basic Info, Tax & Registration, Place of Work, Contacts,
    Banking, Contract & Billing.
    """

    template_name = "company_onboarding/step1.html"

    def get(self, request, company_id=None):
        company = get_object_or_404(Company, pk=company_id) if company_id else None
        context = self._build_context(company)
        return render(request, self.template_name, context)

    def post(self, request, company_id=None):
        action = request.POST.get("action", "draft")
        strict = action == "next"
        company = get_object_or_404(Company, pk=company_id) if company_id else Company()

        identity_form = CompanyIdentityForm(
            request.POST, request.FILES, instance=company
        )
        if not identity_form.is_valid():
            context = self._build_context(
                company if company.pk else None, identity_form=identity_form
            )
            messages.error(request, "Please correct the highlighted fields.")
            return render(request, self.template_name, context)

        company = identity_form.save()

        submitted_tax_country = request.POST.get("tax_country")
        tax_country_locked = (
            company.status == "ACTIVE"
            and submitted_tax_country
            and submitted_tax_country != company.tax_country
        )

        compliance_form = CompanyComplianceForm(
            request.POST, instance=company, strict=strict
        )
        if tax_country_locked:
            # is_valid() must run first — add_error() requires the form to
            # already have cleaned_data, which only exists post-validation.
            compliance_form.is_valid()
            compliance_form.add_error(
                "tax_country",
                "Tax & Registration Country cannot change after the client "
                "is Active.",
            )
            messages.error(request, "Tax & Registration Country is locked.")
            context = self._build_context(company, compliance_form=compliance_form)
            return render(request, self.template_name, context)
        bank_form = CompanyBankDetailsForm(
            request.POST,
            instance=get_bank_details(company) or CompanyBankDetails(company=company),
            strict=strict,
        )
        active_contract = company.contracts.filter(status="ACTIVE").first()
        amending_active_contract = company.status == "ACTIVE" and active_contract is not None
        
        old_contract_values = (
            {field: getattr(active_contract, field) for field in CONTRACT_TERM_FIELDS}
            if active_contract
            else None
        )
        old_msa_document = active_contract.msa_document if active_contract else None
        contract_form = CompanyContractForm(
            request.POST,
            request.FILES,
            instance=active_contract or CompanyContract(company=company),
            strict=strict,
        )

        state_row_forms = _build_state_row_forms(company, request.POST, strict=strict)
        poc_row_forms = _build_poc_row_forms(company, request.POST, strict=strict)

        extra_errors = []
        if strict and not any(
            f.is_valid() and f.cleaned_data.get("state") for f in state_row_forms
        ):
            extra_errors.append("At least one Place of Work state is required.")
        if strict and not any(
            f.is_valid() and f.cleaned_data.get("name") for f in poc_row_forms
        ):
            extra_errors.append("At least one point of contact is required.")

        if strict:
            # Mirrors wizard_utils.validate_for_active()'s field-presence
            # rules -- everything except the bank-verification-STATUS check,
            # which stays Active-only (you can't have verified a bank
            # account before you've even finished typing it in on this same
            # page). Checked against each form's own cleaned_data, not the
            # DB -- these rows/fields haven't been saved yet at this point
            # in the request, so validate_for_active() itself (which reads
            # straight from the DB) would incorrectly see stale state here.
            if compliance_form.is_valid():
                tax_country = compliance_form.cleaned_data.get("tax_country")
                if not tax_country:
                    extra_errors.append("Tax & Registration Country is required.")
                elif tax_country == "INDIA" and not compliance_form.cleaned_data.get("pan"):
                    extra_errors.append("PAN is required for domestic clients.")
                elif tax_country == "FOREIGN" and not compliance_form.cleaned_data.get(
                    "foreign_tax_id"
                ):
                    extra_errors.append("Foreign Tax ID is required for foreign clients.")

            if bank_form.is_valid() and not all(
                bank_form.cleaned_data.get(field)
                for field in (
                    "account_number",
                    "bank_name",
                    "ifsc_swift",
                    "account_holder_name",
                    "contact_number",
                )
            ):
                extra_errors.append("Bank details are required.")

            if contract_form.is_valid():
                cd = contract_form.cleaned_data
                contract_complete = (
                    cd.get("msa_reference_number")
                    and cd.get("start_date")
                    and cd.get("billing_model")
                    and cd.get("billing_value") is not None
                    and cd.get("msa_document")
                )
                if not contract_complete:
                    extra_errors.append(
                        "An MSA/Contract with all its fields (including the MSA "
                        "document) is required."
                    )

        forms_valid = (
            compliance_form.is_valid()
            and bank_form.is_valid()
            and contract_form.is_valid()
            and all(f.is_valid() for f in state_row_forms)
            and all(f.is_valid() for f in poc_row_forms)
        )

        # A real amendment (contract terms actually changing on an
        # already-Active company) has its own hard requirements, regardless
        # of Draft/Next -- unlike the relaxed-on-Draft checks below, these
        # aren't "a field is incomplete", they're "the action being
        # attempted isn't valid without this". Checked here, before any
        # section saves, so they abort the whole request the same way
        # tax_country_locked does above -- otherwise an error would only
        # show as a toast while Next silently carried on to Step 2 anyway.
        is_real_amendment = (
            amending_active_contract
            and contract_form.is_valid()
            and any(contract_form.cleaned_data.values())
            and contract_terms_changed(
                old_contract_values,
                contract_form.cleaned_data,
                new_file_uploaded=bool(request.FILES.get("msa_document")),
            )
        )

        amendment_errors = []
        if is_real_amendment and not request.FILES.get("msa_document"):
            amendment_errors.append(
                "Amending an active contract requires uploading the new "
                "MSA document."
            )

        # If the outgoing contract already has a defined end date, the new
        # one must start the very next day -- no gap, no overlap. (If it
        # doesn't have one yet, the save step below back-fills it to
        # exactly the day before the new contract's start date, so the
        # invariant always holds either way -- nothing to validate in that
        # case since there's nothing to conflict with.)
        if is_real_amendment and old_contract_values["end_date"]:
            expected_start = old_contract_values["end_date"] + timedelta(days=1)
            if contract_form.cleaned_data.get("start_date") != expected_start:
                amendment_errors.append(
                    "The new contract must start the day after the "
                    "current one ends, with no gap or overlap — expected "
                    f"start date {expected_start:%Y-%m-%d}."
                )
        elif (
            is_real_amendment
            and old_contract_values["start_date"]
            and contract_form.cleaned_data.get("start_date")
            and contract_form.cleaned_data["start_date"] <= old_contract_values["start_date"]
        ):
            # No end date to anchor to (back-filled below instead) -- but
            # a new start date on or before the CURRENT contract's own
            # start date can't be turned into a valid "day before" end
            # date for it, and doesn't make chronological sense anyway.
            amendment_errors.append(
                "The new contract's start date must be after the current "
                "contract's start date "
                f"({old_contract_values['start_date']:%Y-%m-%d})."
            )

        if amendment_errors:
            for error in amendment_errors:
                contract_form.add_error(None, error)
                messages.error(request, error)
            context = self._build_context(company, contract_form=contract_form)
            return render(request, self.template_name, context)

        if strict and (not forms_valid or extra_errors):
            for error in extra_errors:
                messages.error(request, error)
            context = self._build_context(
                company,
                compliance_form=compliance_form,
                bank_form=bank_form,
                contract_form=contract_form,
                state_row_forms=state_row_forms,
                poc_row_forms=poc_row_forms,
            )
            return render(request, self.template_name, context)

        # Draft/Back/Next-that-passed: save whatever validated, however
        # incomplete each piece is.
        if compliance_form.is_valid():
            compliance_form.save()
        if bank_form.is_valid() and any(bank_form.cleaned_data.values()):
            bank_form.save()
        if contract_form.is_valid() and any(contract_form.cleaned_data.values()):
            new_msa_document = request.FILES.get("msa_document")
            if is_real_amendment:
                # amendment_errors (above) already aborted the whole
                # request if new_msa_document were missing or the dates
                # didn't line up, so both are guaranteed valid by this
                # point.
                #
                # contract_form.is_valid() (in forms_valid, above) already
                # ran construct_instance() on active_contract (since
                # contract_form is bound to it as `instance=`, needed so
                # the model's own "one active contract per company"
                # clean() check correctly excludes itself via
                # instance.pk) -- that mutates active_contract's Python
                # attributes to the NEWLY SUBMITTED values in place, even
                # though nothing has been saved yet. Explicitly restore
                # the true pre-edit values before saving it as
                # TERMINATED, so the persisted history row reflects what
                # was actually in effect, not what's about to replace it.
                for field, value in old_contract_values.items():
                    setattr(active_contract, field, value)
                active_contract.msa_document = old_msa_document

                new_start_date = contract_form.cleaned_data.get("start_date")
                if not old_contract_values["end_date"]:
                    # No defined end date to preserve or validate against
                    # (checked above) -- back-fill it to exactly the day
                    # before the new contract starts, so the two contracts
                    # connect with no gap and no overlap by construction.
                    active_contract.end_date = new_start_date - timedelta(days=1)

                new_msa_ref = contract_form.cleaned_data.get("msa_reference_number")
                if (
                    old_contract_values["msa_reference_number"]
                    and new_msa_ref == old_contract_values["msa_reference_number"]
                ):
                    # msa_reference_number is globally unique even across
                    # TERMINATED rows (see CompanyContractTests.
                    # test_msa_reference_number_unique_across_companies) --
                    # the amendment normally keeps the same real-world MSA
                    # number, which would otherwise collide the instant the
                    # new row saves it. Free it up by tagging the outgoing
                    # row distinctly (its own pk is unique, so this can
                    # never collide with a sibling termination) rather than
                    # losing the reference entirely.
                    max_len = CompanyContract._meta.get_field(
                        "msa_reference_number"
                    ).max_length
                    active_contract.msa_reference_number = (
                        f"{old_contract_values['msa_reference_number']} "
                        f"[superseded #{active_contract.pk}]"
                    )[:max_len]
                active_contract.status = CompanyContract.Status.TERMINATED
                active_contract.save()

                new_contract = CompanyContract(
                    company=company,
                    status=CompanyContract.Status.ACTIVE,
                    msa_reference_number=new_msa_ref,
                    start_date=new_start_date,
                    end_date=contract_form.cleaned_data.get("end_date"),
                    billing_model=contract_form.cleaned_data.get("billing_model"),
                    billing_value=contract_form.cleaned_data.get("billing_value"),
                    msa_document=new_msa_document,
                )
                new_contract.save()
            elif not amending_active_contract:
                contract_form.save()
        for form in state_row_forms:
            if form.is_valid() and form.cleaned_data.get("state"):
                row = form.save(commit=False)
                row.company = company
                row.save()
        for form in poc_row_forms:
            if form.is_valid() and form.cleaned_data.get("name"):
                row = form.save(commit=False)
                row.company = company
                row.save()

        messages.success(request, "Saved.")

        if action == "next":
            return redirect("company-onboarding-step2", company_id=company.pk)
        if action == "back":
            return redirect("company-onboarding-list")
        return redirect("company-onboarding-step1", company_id=company.pk)

    def _build_context(self, company, **overrides):
        identity_form = overrides.get("identity_form") or CompanyIdentityForm(
            instance=company
        )
        compliance_form = overrides.get("compliance_form") or CompanyComplianceForm(
            instance=company
        )
        bank_details = get_bank_details(company) if company else None
        bank_form = overrides.get("bank_form") or CompanyBankDetailsForm(
            instance=bank_details
        )
        active_contract = (
            company.contracts.filter(status="ACTIVE").first() if company else None
        )
        # Once the company is Active and a contract already exists, editing
        # is an amendment (see contract_terms_changed() in post()), not an
        # in-place edit -- the form should start BLANK for the admin to
        # enter the new terms, not pre-filled with the current ones (which
        # are shown read-only instead, gated behind an explicit "Add New
        # Contract" toggle in the template). Before Active, there's nothing
        # live yet to amend, so pre-filling/editing in place is correct.
        amending_active_contract = bool(company and company.status == "ACTIVE" and active_contract)
        contract_form = overrides.get("contract_form") or CompanyContractForm(
            instance=CompanyContract(company=company) if amending_active_contract else active_contract
        )
        state_row_forms = overrides.get("state_row_forms") or (
            _build_state_row_forms(company) if company else []
        )
        poc_row_forms = overrides.get("poc_row_forms") or (
            _build_poc_row_forms(company) if company else []
        )
        return {
            "step": 1,
            "company": company,
            "identity_form": identity_form,
            "compliance_form": compliance_form,
            "bank_form": bank_form,
            "contract_form": contract_form,
            "active_contract": active_contract,
            "amending_active_contract": amending_active_contract,
            "state_row_forms": state_row_forms,
            "poc_row_forms": poc_row_forms,
            "next_state_index": len(state_row_forms),
            "next_poc_index": len(poc_row_forms),
            "verifications": bank_details.verifications.all() if bank_details else [],
        }


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required("base.add_company"), name="dispatch")
class Step2View(View):
    """Authorized Signatory, Payroll Sign-off."""

    template_name = "company_onboarding/step2.html"

    def get(self, request, company_id):
        company = get_object_or_404(Company, pk=company_id)
        context = self._build_context(company)
        return render(request, self.template_name, context)

    def post(self, request, company_id):
        action = request.POST.get("action", "draft")
        strict = action == "next"
        company = get_object_or_404(Company, pk=company_id)

        signatory_row_forms = _build_signatory_row_forms(
            company, request.POST, strict=strict
        )

        extra_errors = []
        if strict and not any(
            f.is_valid() and f.cleaned_data.get("name") and f.cleaned_data.get("is_enabled")
            for f in signatory_row_forms
        ):
            extra_errors.append("At least one enabled signatory is required.")

        if strict and (
            not all(f.is_valid() for f in signatory_row_forms) or extra_errors
        ):
            for error in extra_errors:
                messages.error(request, error)
            context = self._build_context(company, signatory_row_forms=signatory_row_forms)
            return render(request, self.template_name, context)

        for form in signatory_row_forms:
            if form.is_valid() and form.cleaned_data.get("name"):
                row = form.save(commit=False)
                row.company = company
                row.save()

        require_signoff = request.POST.get("require_payroll_signoff") == "on"
        approver_id = request.POST.get("payroll_approver")
        if require_signoff and not approver_id:
            if strict:
                messages.error(
                    request,
                    "Select a payroll approver, or turn Require Payroll "
                    "Sign-off off.",
                )
                context = self._build_context(company)
                return render(request, self.template_name, context)
        else:
            company.poc_contacts.update(is_payroll_approver=False)
            if approver_id:
                company.poc_contacts.filter(pk=approver_id).update(
                    is_payroll_approver=True
                )
        company.require_payroll_signoff = require_signoff
        company.save(update_fields=["require_payroll_signoff"])

        messages.success(request, "Saved.")

        if action == "next":
            return redirect("company-onboarding-step3", company_id=company.pk)
        if action == "back":
            return redirect("company-onboarding-step1", company_id=company.pk)
        return redirect("company-onboarding-step2", company_id=company.pk)

    def _build_context(self, company, **overrides):
        signatory_row_forms = overrides.get(
            "signatory_row_forms"
        ) or _build_signatory_row_forms(company)
        return {
            "step": 2,
            "company": company,
            "signatory_row_forms": signatory_row_forms,
            "next_signatory_index": len(signatory_row_forms),
            "poc_contacts": company.poc_contacts.all(),
        }


@method_decorator(login_required, name="dispatch")
class Step3View(View):
    """
    Branded Templates, Documents, Mark as Active.

    Deliberately no class-level base.add_company gate (unlike Step1View/
    Step2View) — Ventura HR needs to reach this view to upload branded
    templates/documents, which Step1/Step2 (core company data) don't allow
    them at all. Permission is instead checked per-action: viewing the page
    requires either the Admin or the HR grant; mark_active and the two
    upload actions each check their own specific permission individually.
    """

    template_name = "company_onboarding/step3.html"

    def get(self, request, company_id):
        if not (
            request.user.has_perm("base.add_company")
            or request.user.has_perm("company_onboarding.add_companybrandedtemplate")
            or request.user.has_perm("company_onboarding.add_companydocument")
        ):
            return handle_no_permission(request)
        company = get_object_or_404(Company, pk=company_id)
        context = self._build_context(company)
        return render(request, self.template_name, context)

    def post(self, request, company_id):
        action = request.POST.get("action", "draft")
        company = get_object_or_404(Company, pk=company_id)

        if action in ("upload_template", "upload_document"):
            required_perm = (
                "company_onboarding.add_companybrandedtemplate"
                if action == "upload_template"
                else "company_onboarding.add_companydocument"
            )
            if not request.user.has_perm(required_perm):
                messages.error(request, "You do not have permission to do that.")
                return redirect("company-onboarding-step3", company_id=company.pk)
            self._handle_document_upload(request, company, action)
            return redirect("company-onboarding-step3", company_id=company.pk)

        if action == "mark_active":
            if not request.user.has_perm("base.change_status_company"):
                messages.error(request, "You do not have permission to do that.")
                return redirect("company-onboarding-step3", company_id=company.pk)
            blockers = validate_for_active(company)
            if blockers:
                context = self._build_context(company, active_gate_errors=blockers)
                return render(request, self.template_name, context)
            company.status = "ACTIVE"
            company.save(update_fields=["status"])
            messages.success(request, "Company is now Active.")
            return redirect("company-onboarding-detail", company_id=company.pk)

        if action == "back":
            return redirect("company-onboarding-step2", company_id=company.pk)
        return redirect("company-onboarding-step3", company_id=company.pk)

    def _handle_document_upload(self, request, company, action):
        # Distinct field names (template_file / document_file), not both
        # named "file" — Step 3 has two separate upload sections inside one
        # shared <form>, and giving them the same field name would make
        # which upload actually happened ambiguous.
        if action == "upload_template":
            template_type = request.POST.get("template_type")
            file = request.FILES.get("template_file")
            if template_type and file:
                CompanyBrandedTemplate.objects.update_or_create(
                    company=company,
                    template_type=template_type,
                    defaults={"file": file},
                )
                messages.success(request, "Template uploaded.")
        elif action == "upload_document":
            tag_name = request.POST.get("tag_name")
            file = request.FILES.get("document_file")
            if tag_name and file:
                CompanyDocument.objects.create(
                    company=company, tag_name=tag_name, file=file
                )
                messages.success(request, "Document uploaded.")

    def _build_context(self, company, active_gate_errors=None):
        return {
            "step": 3,
            "company": company,
            "branded_templates": company.branded_templates.all(),
            "documents": company.documents.all(),
            "active_gate_errors": active_gate_errors or [],
        }


@login_required
@hx_request_required
@permission_required("base.add_company")
def add_state_registration_row(request):
    index = int(request.GET.get("count", 0)) + 1
    form = StateRegistrationRowForm(prefix=f"state{index}")
    return render(
        request,
        "company_onboarding/partials/state_registration_row.html",
        {"index": index, "form": form},
    )


@login_required
@hx_request_required
@permission_required("base.add_company")
def remove_state_registration_row(request):
    return HttpResponse()


@login_required
@hx_request_required
@permission_required("base.add_company")
def add_poc_contact_row(request):
    index = int(request.GET.get("count", 0)) + 1
    form = POCContactRowForm(prefix=f"poc{index}")
    return render(
        request,
        "company_onboarding/partials/poc_contact_row.html",
        {"index": index, "form": form},
    )


@login_required
@hx_request_required
@permission_required("base.add_company")
def remove_poc_contact_row(request):
    return HttpResponse()


@login_required
@hx_request_required
@permission_required("base.add_company")
def add_signatory_row(request):
    index = int(request.GET.get("count", 0)) + 1
    form = SignatoryRowForm(prefix=f"signatory{index}")
    return render(
        request,
        "company_onboarding/partials/signatory_row.html",
        {"index": index, "form": form},
    )


@login_required
@hx_request_required
@permission_required("base.add_company")
def remove_signatory_row(request):
    return HttpResponse()
