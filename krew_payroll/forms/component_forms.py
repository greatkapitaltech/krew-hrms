"""
These forms provide a convenient way to handle data input, validation, and customization
of form fields and widgets for the corresponding models in the payroll management system.
"""

import datetime
import logging
import uuid
from typing import Any

from django import forms
from django.apps import apps
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

import krew_payroll.models.models
from base.forms import Form, ModelForm
from base.methods import reload_queryset
from base.models import Company
from employee.filters import EmployeeFilter
from employee.models import BonusPoint, Employee
from horilla import horilla_middlewares
from horilla.horilla_middlewares import _thread_locals
from horilla.methods import get_horilla_model_class
from horilla_widgets.forms import HorillaForm, default_select_option_template
from horilla_widgets.widgets.horilla_multi_select_field import HorillaMultiSelectField
from horilla_widgets.widgets.select_widgets import HorillaMultiSelectWidget
from notifications.signals import notify
from krew_payroll.models import tax_models as models
from krew_payroll.models.models import (
    Contract,
    LoanAccount,
    Payslip,
    PayslipAutoGenerate,
    Reimbursement,
    ReimbursementMultipleAttachment,
)

logger = logging.getLogger(__name__)


class PayslipForm(ModelForm):
    """
    Form for Payslip
    """

    cols = {
        "employee_id": 12,
        "start_date": 12,
        "end_date": 12,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active_contracts = Contract.objects.filter(contract_status="active")
        self.fields["employee_id"].choices = [
            (contract.employee_id.id, contract.employee_id)
            for contract in active_contracts
            if contract.employee_id.is_active
        ]
        self.fields["employee_id"].widget.attrs.update(
            {
                "hx-get": "/payroll/check-contract-start-date",
                "hx-target": "#contractStartDateDiv",
                "hx-include": "#payslipCreateForm",
                "hx-trigger": "change delay:300ms",
                "hx-swap": "innerHTML",
            }
        )
        if self.instance.pk is None:
            self.initial["start_date"] = datetime.date.today().replace(day=1)
            self.initial["end_date"] = datetime.date.today()

    class Meta:
        """
        Meta class for additional options
        """

        model = krew_payroll.models.models.Payslip
        fields = [
            "employee_id",
            "start_date",
            "end_date",
        ]
        exclude = ["is_active"]
        widgets = {
            "start_date": forms.DateInput(
                attrs={
                    "type": "date",
                    "hx-get": "/payroll/check-contract-start-date",
                    "hx-target": "#contractStartDateDiv",
                    "hx-include": "#payslipCreateForm",
                    "hx-trigger": "change delay:300ms",
                    "hx-swap": "innerHTML",
                }
            ),
            "end_date": forms.DateInput(
                attrs={
                    "type": "date",
                }
            ),
        }


class GeneratePayslipForm(HorillaForm):
    """
    Form for Payslip
    """

    group_name = forms.CharField(
        label="Batch name",
        required=True,
        # help_text="Enter +-something if you want to generate payslips by batches",
    )
    employee_id = HorillaMultiSelectField(
        queryset=Employee.objects.none(),
        widget=HorillaMultiSelectWidget(
            filter_route_name="employee-widget-filter",
            filter_class=EmployeeFilter,
            filter_instance_context_name="f",
            filter_template_path="employee_filters.html",
        ),
        label="Employee",
        required=True,
    )
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")

        today = datetime.date.today()
        if end_date < start_date:
            raise forms.ValidationError(
                {
                    "end_date": "The end date must be greater than or equal to the start date."
                }
            )
        if start_date > today:
            raise forms.ValidationError(
                {"end_date": "The start date cannot be in the future."}
            )

        if end_date > today:
            raise forms.ValidationError(
                {"end_date": "The end date cannot be in the future."}
            )
        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee_id"].queryset = Employee.objects.filter(
            is_active=True,
            contract_set__isnull=False,
            contract_set__contract_status="active",
        )
        self.fields["employee_id"].widget.attrs.update(
            {"class": "oh-select oh-select-2", "id": uuid.uuid4()}
        )
        self.fields["start_date"].widget.attrs.update({"class": "oh-input w-100"})
        self.fields["group_name"].widget.attrs.update({"class": "oh-input w-100"})
        self.fields["end_date"].widget.attrs.update({"class": "oh-input w-100"})
        self.initial["start_date"] = datetime.date.today().replace(day=1)
        self.initial["end_date"] = datetime.date.today()

    class Meta:
        """
        Meta class for additional options
        """

        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }


class PayrollSettingsForm(ModelForm):
    """
    Form for PayrollSettings model
    """

    class Meta:
        """
        Meta class for additional options
        """

        model = models.PayrollSettings
        fields = "__all__"
        widgets = {
            "position": forms.Select(attrs={"class": "oh-select oh-select-2 w-100"}),
        }


excel_columns = [
    ("employee_id", _("Employee")),
    ("group_name", _("Batch")),
    ("start_date", _("Start Date")),
    ("end_date", _("End Date")),
    ("contract_wage", _("Contract Wage")),
    ("basic_pay", _("Basic Pay")),
    ("gross_pay", _("Gross Pay")),
    ("deduction", _("Deduction")),
    ("net_pay", _("Net Pay")),
    ("status", _("Status")),
    ("employee_id__employee_bank_details__bank_name", _("Bank Name")),
    ("employee_id__employee_bank_details__branch", _("Branch")),
    ("employee_id__employee_bank_details__account_number", _("Account Number")),
    ("employee_id__employee_bank_details__any_other_code1", _("Bank Code #1")),
    ("employee_id__employee_bank_details__any_other_code2", _("Bank Code #2")),
    ("employee_id__employee_bank_details__country", _("Country")),
    ("employee_id__employee_bank_details__state", _("State")),
    ("employee_id__employee_bank_details__city", _("City")),
]


class PayslipExportColumnForm(forms.Form):
    selected_fields = forms.MultipleChoiceField(
        choices=excel_columns,
        widget=forms.CheckboxSelectMultiple,
        initial=[
            "employee_id",
            "group_name",
            "start_date",
            "end_date",
            "basic_pay",
            "gross_pay",
            "net_pay",
            "status",
        ],
    )


exclude_fields = ["id", "contract_document", "is_active", "note", "note", "created_at"]


class ContractExportFieldForm(forms.Form):
    model_fields = Contract._meta.get_fields()
    field_choices = [
        (field.name, field.verbose_name)
        for field in model_fields
        if hasattr(field, "verbose_name") and field.name not in exclude_fields
    ]
    selected_fields = forms.MultipleChoiceField(
        choices=field_choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[
            "contract_name",
            "employee_id",
            "contract_start_date",
            "contract_end_date",
            "wage_type",
            "wage",
            "filing_status",
            "contract_status",
        ],
    )


from django.core.exceptions import ValidationError


def rate_validator(value):
    """
    Percentage validator
    """
    if value < 0:
        raise ValidationError(_("Rate must be greater than 0"))
    if value > 100:
        raise ValidationError(_("Rate must be less than 100"))


class BonusForm(Form):
    """
    A one-off bonus for one employee: an earning line on the payslip of its date.
    """

    title = forms.CharField(max_length=100)
    date = forms.DateField(widget=forms.DateInput(), required=False)
    employee_id = forms.IntegerField(label="Employee", widget=forms.HiddenInput())
    amount = forms.DecimalField(label=_("Amount"), min_value=0, decimal_places=2)
    is_taxable = forms.BooleanField(
        label=_("Taxable"), initial=True, required=False, widget=forms.CheckboxInput()
    )

    def save(self, commit=True):
        from datetime import date as _date

        from krew_payroll.methods.pay_components.adjustments import add_adjustment

        employee = Employee.objects.get(pk=self.cleaned_data["employee_id"])
        return add_adjustment(
            employee,
            "EARNING",
            self.cleaned_data["title"],
            self.cleaned_data["amount"],
            self.cleaned_data["date"] or _date.today(),
            source="BONUS",
            is_taxable=self.cleaned_data["is_taxable"],
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.update({"class": "oh-input w-100"})
        self.fields["date"].widget = forms.DateInput(
            attrs={"type": "date", "class": "oh-input w-100"}
        )
        self.fields["is_taxable"].widget.attrs.update({"class": "oh-switch__checkbox"})


class PayslipAllowanceForm(BonusForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].widget = forms.HiddenInput()


class PayslipDeductionForm(Form):
    """
    A one-off deduction on a payslip: a deduction line on that payslip's period.
    """

    verbose_name = _("Deduction")

    title = forms.CharField(max_length=200, label=_("Title"))
    amount = forms.DecimalField(label=_("Amount"), min_value=0, decimal_places=2)
    reduces_taxable_income = forms.BooleanField(
        label=_("Reduces taxable income"), required=False
    )

    def as_p(self):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("one_time_deduction.html", context)
        return table_html


class LoanAccountForm(ModelForm):
    """
    LoanAccountForm
    """

    verbose_name = _("Loans & Salary Advances")
    cols = {
        "description": 12,
    }

    class Meta:
        model = LoanAccount
        fields = "__all__"
        exclude = ["is_active", "settled_date"]
        widgets = {
            "provided_date": forms.DateTimeInput(attrs={"type": "date"}),
            "installment_start_date": forms.DateTimeInput(attrs={"type": "date"}),
        }

    def as_p(self):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["provided_date"] = str(datetime.date.today())
        self.initial["installment_start_date"] = str(datetime.date.today())
        if self.instance.pk:
            self.verbose_name = self.instance.title
            fields_to_exclude = ["employee_id", "installment_start_date"]
            if self.instance.has_paid_installments():
                fields_to_exclude = fields_to_exclude + [
                    "loan_amount",
                    "installments",
                    "installment_amount",
                ]
            self.initial["provided_date"] = str(self.instance.provided_date)
            for field in fields_to_exclude:
                if field in self.fields:
                    del self.fields[field]

    def clean(self, *args, **kwargs):
        cleaned_data = super().clean(*args, **kwargs)

        if not self.instance.pk and cleaned_data.get(
            "installment_start_date"
        ) < cleaned_data.get("provided_date"):
            raise forms.ValidationError(
                _(
                    "Installment start date should be greater than or equal to provided date"
                )
            )
        if cleaned_data.get("installments") != None:
            if cleaned_data.get("installments") <= 0:
                raise forms.ValidationError(
                    _("Installments needs to be a positive integer")
                )

        return cleaned_data


class AssetFineForm(LoanAccountForm):
    verbose_name = _("Asset Fine")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["loan_amount"].label = _("Fine Amount")
        self.fields["provided_date"].label = _("Fine Date")

        fields_to_exclude = [
            "employee_id",
            "type",
        ]
        for field in fields_to_exclude:
            if field in self.fields:
                del self.fields[field]
        field_order = [
            "title",
            "loan_amount",
            "provided_date",
            "description",
            "installments",
            "installment_start_date",
            "installment_amount",
            "settled",
        ]

        self.fields = {
            field: self.fields[field] for field in field_order if field in self.fields
        }


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
            result = [single_file_clean(data, initial)]
        return result[0] if result else None


class ReimbursementForm(ModelForm):
    """
    Optimized Reimbursement / Encashment Form
    """

    cols = {"description": 12}

    verbose_name = "Reimbursement / Encashment"

    class Meta:
        model = Reimbursement
        fields = "__all__"
        exclude = ["is_active", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.request = getattr(horilla_middlewares._thread_locals, "request", None)
        self.employee = self.get_employee()  # 819

        if not self.instance.pk:
            self.initial["allowance_on"] = str(datetime.date.today())

        self.initial["employee_id"] = self.employee.id if self.employee else None

        self.configure_fields()

    def get_employee(self):
        """Resolves employee either from form data or request."""
        if hasattr(self.instance, "employee_id") and self.instance.employee_id:
            return self.instance.employee_id

        employee_qs = self.fields["employee_id"].queryset
        employee_id = self.data.get("employee_id") if self.data else None

        if employee_id and (emp := employee_qs.filter(id=employee_id).first()):
            return emp

        if self.request and (emp := self.request.user.employee_get):
            if not self.instance.pk and emp in employee_qs:
                return emp
            if self.instance.pk and emp.id == self.instance.employee_id:
                return emp

        return employee_qs.first()

    def get_encashable_leaves(self, employee):
        LeaveType = get_horilla_model_class(app_label="leave", model="leavetype")
        return LeaveType.objects.filter(
            employee_available_leave__employee_id=employee,
            employee_available_leave__total_leave_days__gte=1,
            is_encashable=True,
        )

    def configure_fields(self):
        exclude_fields = []

        if self.request and not self.request.user.has_perm("krew_payroll.add_reimbursement"):
            exclude_fields.append("employee_id")

        self.setup_leave_fields()

        self.fields["type"].widget.attrs["onchange"] = "toggleReimbursmentType($(this))"
        self.fields["employee_id"].widget.attrs[
            "onchange"
        ] = "getAssignedLeave($(this))"

        self.fields["allowance_on"].widget = forms.DateInput(
            attrs={"type": "date", "class": "oh-input w-100"}
        )

        self.fields["attachment"] = MultipleFileField(label="Attachments")
        self.fields["attachment"].widget.attrs["accept"] = ".jpg, .jpeg, .png, .pdf"

        self.exclude_fields_by_type(exclude_fields)

        for field in exclude_fields:
            self.fields.pop(field, None)

    def setup_leave_fields(self):
        """Setup leave-related fields only if leave app is installed."""
        if not apps.is_installed("leave") or not self.employee:
            return

        AvailableLeave = get_horilla_model_class(
            app_label="leave", model="availableleave"
        )
        assigned_leaves = self.get_encashable_leaves(self.employee)

        self.assigned_leaves = AvailableLeave.objects.filter(
            leave_type_id__in=assigned_leaves, employee_id=self.employee
        )
        self.fields["leave_type_id"].queryset = assigned_leaves
        self.fields["leave_type_id"].empty_label = None
        self.fields["employee_id"].empty_label = None

    def exclude_fields_by_type(self, exclude_fields):
        """Determine which fields to exclude based on type."""
        type = (
            self.data.get("type")
            if self.data
            else self.instance.type if self.instance else None
        )
        is_edit = self.instance and self.instance.pk

        if type == "reimbursement" and (is_edit or self.data):
            exclude_fields += [
                "leave_type_id",
                "cfd_to_encash",
                "ad_to_encash",
                "bonus_to_encash",
            ]
        elif type == "leave_encashment" and (is_edit or self.data):
            exclude_fields += ["attachment", "amount", "bonus_to_encash"]
        elif type == "bonus_encashment" and (is_edit or self.data):
            exclude_fields += [
                "attachment",
                "amount",
                "leave_type_id",
                "cfd_to_encash",
                "ad_to_encash",
            ]

    def as_p(self):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html

    def clean(self):
        cleaned_data = super().clean()

        type_ = cleaned_data.get("type")
        employee = cleaned_data.get("employee_id")

        if not type_ or not employee:
            return cleaned_data

        if type_ == "bonus_encashment":
            bonus_to_encash = (
                self.instance.bonus_to_encash
                if self.instance.pk
                else cleaned_data.get("bonus_to_encash")
            )
            available_points = BonusPoint.objects.filter(employee_id=employee).first()

            if bonus_to_encash is not None:
                if bonus_to_encash <= 0:
                    self.add_error(
                        "bonus_to_encash", "Points must be greater than zero to redeem."
                    )
                elif not available_points or available_points.points < bonus_to_encash:
                    self.add_error(
                        "bonus_to_encash", "Not enough bonus points to redeem"
                    )

        elif type_ == "leave_encashment":
            leave_type = (
                self.instance.leave_type_id
                if self.instance.pk
                else cleaned_data.get("leave_type_id")
            )
            cfd_to_encash = (
                self.instance.cfd_to_encash
                if self.instance.pk
                else cleaned_data.get("cfd_to_encash", 0)
            )
            ad_to_encash = (
                self.instance.ad_to_encash
                if self.instance.pk
                else cleaned_data.get("ad_to_encash", 0)
            )

            if not leave_type:
                self.add_error("leave_type_id", "This field is required")
            else:
                encashable = self.get_encashable_leaves(employee)
                if leave_type not in encashable:
                    self.add_error("leave_type_id", "This leave type is not encashable")
                else:
                    AvailableLeave = get_horilla_model_class("leave", "availableleave")
                    available_leave = AvailableLeave.objects.filter(
                        leave_type_id=leave_type, employee_id=employee
                    ).first()

                    if available_leave:
                        if cfd_to_encash < 0:
                            self.add_error(
                                "cfd_to_encash", _("Value can't be negative.")
                            )
                        elif cfd_to_encash > available_leave.carryforward_days:
                            self.add_error(
                                "cfd_to_encash",
                                _("Not enough carryforward days to redeem"),
                            )

                        if ad_to_encash < 0:
                            self.add_error(
                                "ad_to_encash", _("Value can't be negative.")
                            )
                        elif ad_to_encash > available_leave.available_days:
                            self.add_error(
                                "ad_to_encash", _("Not enough available days to redeem")
                            )

        return cleaned_data

    def save(self, commit: bool = True) -> Any:
        multiple_attachment_ids = []
        is_new = not self.instance.pk
        attachments = self.files.getlist("attachment")

        if attachments:
            self.instance.attachment = attachments[0]

        instance = super().save(commit=commit)

        if attachments:
            attachment_objs = [
                ReimbursementMultipleAttachment(attachment=file) for file in attachments
            ]
            created_attachments = ReimbursementMultipleAttachment.objects.bulk_create(
                attachment_objs
            )
            multiple_attachment_ids = [obj.pk for obj in created_attachments]
            instance.other_attachments.add(*multiple_attachment_ids)

        if is_new:
            try:
                manager = instance.employee_id.employee_work_info.reporting_manager_id
                if manager and manager.employee_user_id:
                    notify.send(
                        instance.employee_id,  # 816
                        recipient=manager.employee_user_id,
                        verb=f"You have a new reimbursement request to approve for {instance.employee_id}.",
                        verb_ar=f"لديك طلب استرداد نفقات جديد يتعين عليك الموافقة عليه لـ {instance.employee_id}.",
                        verb_de=f"Sie haben einen neuen Rückerstattungsantrag zur Genehmigung für {instance.employee_id}.",
                        verb_es=f"Tienes una nueva solicitud de reembolso para aprobar para {instance.employee_id}.",
                        verb_fr=f"Vous avez une nouvelle demande de remboursement à approuver pour {instance.employee_id}.",
                        icon="information",
                        redirect=f"/payroll/view-reimbursement?id={instance.id}",
                    )
            except Exception:
                pass

        return instance, attachments


# ===========================Auto payslip generate================================
class PayslipAutoGenerateForm(ModelForm):
    class Meta:
        model = PayslipAutoGenerate
        fields = ["generate_day", "company_id", "auto_generate"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active_company_id = horilla_middlewares.get_selected_company()
        if active_company_id and active_company_id != "all":
            self.fields["company_id"].queryset = Company.objects.filter(
                id=active_company_id
            )

    def as_p(self):
        """
        Render the form fields as HTML table rows with Bootstrap styling.
        """
        context = {"form": self}
        table_html = render_to_string("common_form.html", context)
        return table_html
