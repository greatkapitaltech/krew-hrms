"""
models.py
Used to register models
"""

import calendar
import logging
import re
from datetime import date, datetime, timedelta

from django import forms
from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import models
from django.http import QueryDict
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.methods import get_next_month_same_date
from base.models import (
    Company,
    Department,
    EmployeeShift,
    JobPosition,
    JobRole,
    WorkType,
    validate_time_format,
)
from employee.methods.duration_methods import strtime_seconds
from employee.models import BonusPoint, Employee, EmployeeWorkInformation
from horilla import horilla_middlewares
from horilla.horilla_middlewares import _thread_locals
from horilla.models import HorillaModel, upload_path
from horilla_audit.models import HorillaAuditInfo, HorillaAuditLog
from horilla_views.cbv_methods import render_template

logger = logging.getLogger(__name__)


# Create your models here.


def min_zero(value):
    """
    The minimum value zero validation method
    """
    if value < 0:
        raise ValidationError(_("Value must be greater than zero"))


def get_date_range(start_date, end_date):
    """
    Returns a list of all dates within a given date range.

    Args:
        start_date (date): The start date of the range.
        end_date (date): The end date of the range.

    Returns:
        list: A list of date objects representing all dates within the range.

    Example:
        start_date = date(2023, 1, 1)
        end_date = date(2023, 1, 10)
        date_range = get_date_range(start_date, end_date)
    """
    date_list = []
    delta = end_date - start_date

    for i in range(delta.days + 1):
        current_date = start_date + timedelta(days=i)
        date_list.append(current_date)

    return date_list


class FilingStatus(HorillaModel):
    """
    FilingStatus model
    """

    based_on_choice = [
        ("basic_pay", _("Basic Pay")),
        ("gross_pay", _("Gross Pay")),
        ("taxable_gross_pay", _("Taxable Gross Pay")),
    ]
    filing_status = models.CharField(
        max_length=30,
        blank=False,
        verbose_name=_("Filing status"),
    )
    based_on = models.CharField(
        max_length=255,
        choices=based_on_choice,
        null=False,
        blank=False,
        default="taxable_gross_pay",
        verbose_name=_("Based on"),
    )
    use_py = models.BooleanField(verbose_name=_("Python Code"), default=False)
    python_code = models.TextField(null=True)
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
        max_length=255,
    )
    company_id = models.ForeignKey(
        Company, null=True, editable=False, on_delete=models.PROTECT
    )
    objects = HorillaCompanyManager()

    def __str__(self) -> str:
        return str(self.filing_status)

    def get_update_url(self):
        """
        Returns the URL for updating the filing status instance.
        """
        return reverse("filing-status-update", kwargs={"pk": self.pk})

    def get_create_url(self):
        """
        Returns the URL for updating the filing status instance.
        """
        return reverse("tax-bracket-create", kwargs={"filing_status_id": self.pk})

    def get_delete_url(self):
        """
        Returns the URL for updating the filing status instance.
        """
        return f"{reverse('generic-delete')}?model=krew_payroll.FilingStatus&pk={self.pk}"

    def tax_brackets_col(self):
        """
        Renders the tax brackets belonging to this filing status as a table.
        """
        return render_template(
            path="cbv/federal_tax/tax_brackets_col.html",
            context={
                "instance": self,
                "tax_brackets": self.taxbracket_set.all().order_by("min_income"),
            },
        )

    class Meta:
        ordering = ["-id"]
        verbose_name = _("Filing Status")
        verbose_name_plural = _("Filing Statuses")


class Contract(HorillaModel):
    """
    Contract Model
    """

    COMPENSATION_CHOICES = (
        ("salary", _("Salary")),
        ("hourly", _("Hourly")),
        ("commission", _("Commission")),
    )

    PAY_FREQUENCY_CHOICES = (
        ("weekly", _("Weekly")),
        ("monthly", _("Monthly")),
        ("semi_monthly", _("Semi-Monthly")),
    )
    WAGE_CHOICES = [
        ("daily", _("Daily")),
        ("monthly", _("Monthly")),
    ]

    if apps.is_installed("attendance"):
        WAGE_CHOICES.append(("hourly", _("Hourly")))

    CONTRACT_STATUS_CHOICES = (
        ("draft", _("Draft")),
        ("active", _("Active")),
        ("expired", _("Expired")),
        ("terminated", _("Terminated")),
    )

    contract_name = models.CharField(
        max_length=250, help_text=_("Contract Title."), verbose_name=_("Contract")
    )
    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="contract_set",
        verbose_name=_("Employee"),
    )
    contract_start_date = models.DateField(verbose_name=_("Start Date"))
    contract_end_date = models.DateField(
        null=True, blank=True, verbose_name=_("End Date")
    )
    wage_type = models.CharField(
        choices=WAGE_CHOICES,
        max_length=250,
        default="monthly",
        verbose_name=_("Wage Type"),
    )
    pay_frequency = models.CharField(
        max_length=20,
        null=True,
        choices=PAY_FREQUENCY_CHOICES,
        default="monthly",
        verbose_name=_("Pay Frequency"),
    )
    wage = models.FloatField(verbose_name=_("Basic Salary"), null=True, default=0)
    filing_status = models.ForeignKey(
        FilingStatus,
        on_delete=models.PROTECT,
        related_name="contracts",
        null=True,
        blank=True,
        verbose_name=_("Filing Status"),
    )
    contract_status = models.CharField(
        choices=CONTRACT_STATUS_CHOICES,
        max_length=250,
        default="draft",
        verbose_name=_("Status"),
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Department"),
    )
    job_position = models.ForeignKey(
        JobPosition,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Job Position"),
    )
    job_role = models.ForeignKey(
        JobRole,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Job Role"),
    )
    shift = models.ForeignKey(
        EmployeeShift,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Shift"),
    )
    work_type = models.ForeignKey(
        WorkType,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Work Type"),
    )
    notice_period_in_days = models.IntegerField(
        default=30,
        help_text=_("Notice period in total days."),
        validators=[min_zero],
        verbose_name=_("Notice Period"),
    )
    contract_document = models.FileField(upload_to=upload_path, null=True, blank=True)
    deduct_leave_from_basic_pay = models.BooleanField(
        default=True,
        verbose_name=_("Deduct From Basic Pay"),
        help_text=_("Deduct the leave amount from basic pay."),
    )
    calculate_daily_leave_amount = models.BooleanField(
        default=True,
        verbose_name=_("Calculate Daily Leave Amount"),
        help_text=_(
            "Leave amount will be calculated by dividing the basic pay by number of working days."
        ),
    )
    deduction_for_one_leave_amount = models.FloatField(
        null=True,
        blank=True,
        default=0,
        verbose_name=_("Deduction For One Leave Amount"),
    )

    note = models.TextField(null=True, blank=True)
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )

    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    def get_wage_type_display(self):
        """
        Display wage type
        """
        return dict(self.WAGE_CHOICES).get(self.wage_type)

    def get_pay_frequency_display(self):
        """
        Display pay frequency
        """
        return dict(self.PAY_FREQUENCY_CHOICES).get(self.pay_frequency)

    def get_status_display(self):
        """
        Display status
        """
        return dict(self.CONTRACT_STATUS_CHOICES).get(self.contract_status)

    def status_col(self):
        """
        status column
        """
        return render_template(
            path="cbv/contracts/status.html",
            context={"instance": self},
        )

    def detail_action(self):
        """
        Detail actions
        """
        return render_template(
            path="cbv/contracts/detail_action.html",
            context={"instance": self},
        )

    def note_col(self):
        """
        Note column
        """
        return render_template(
            path="cbv/contracts/note.html",
            context={"instance": self},
        )

    def document_col(self):
        """
        Document column
        """
        return render_template(
            path="cbv/contracts/document.html",
            context={"instance": self},
        )

    def actions_col(self):
        """
        actions column
        """
        return render_template(
            path="cbv/contracts/actions.html",
            context={"instance": self},
        )

    def cal_leave_amount(self):
        """
        Action column for Calculate Leave Amount
        """
        return render_template(
            path="cbv/contracts/cal_leave_amount.html",
            context={"instance": self},
        )

    def conract_subtitle(self):
        """
        Detail view subtitle
        """

        return f"{self.employee_id.get_department()} / {self.employee_id.get_job_position()}"

    def contracts_detail(self):
        """
        detail view
        """

        url = reverse("contracts-detail-view", kwargs={"pk": self.pk})

        return url

    def deduct_leave_from_basic_pay_col(self):
        """
        Deduct leave from basic pay column
        """
        if self.deduct_leave_from_basic_pay:
            return _("Yes")
        else:
            return _("No")

    def __str__(self) -> str:
        return f"{self.contract_name} -{self.contract_start_date} - {self.contract_end_date}"

    def clean(self):
        if self.contract_end_date is not None:
            if self.contract_end_date < self.contract_start_date:
                raise ValidationError(
                    {"contract_end_date": _("End date must be greater than start date")}
                )
        if (
            self.contract_status == "active"
            and Contract.objects.filter(
                employee_id=self.employee_id, contract_status="active"
            )
            .exclude(id=self.pk)
            .count()
            >= 1
        ):
            raise forms.ValidationError(
                _("An active contract already exists for this employee.")
            )
        if (
            self.contract_status == "draft"
            and Contract.objects.filter(
                employee_id=self.employee_id, contract_status="draft"
            )
            .exclude(id=self.pk)
            .count()
            >= 1
        ):
            raise forms.ValidationError(
                _("A draft contract already exists for this employee.")
            )

        if self.wage_type in ["daily", "monthly"]:
            if not self.calculate_daily_leave_amount:
                if self.deduction_for_one_leave_amount is None:
                    raise ValidationError(
                        {"deduction_for_one_leave_amount": _("This field is required")}
                    )

    def save(self, *args, **kwargs):
        if EmployeeWorkInformation.objects.filter(
            employee_id=self.employee_id
        ).exists():
            if self.department is None:
                self.department = self.employee_id.employee_work_info.department_id

            if self.job_position is None:
                self.job_position = self.employee_id.employee_work_info.job_position_id

            if self.job_role is None:
                self.job_role = self.employee_id.employee_work_info.job_role_id

            if self.work_type is None:
                self.work_type = self.employee_id.employee_work_info.work_type_id

            if self.shift is None:
                self.shift = self.employee_id.employee_work_info.shift_id
        if self.contract_end_date is not None and self.contract_end_date < date.today():
            self.contract_status = "expired"
        if (
            self.contract_status == "active"
            and Contract.objects.filter(
                employee_id=self.employee_id, contract_status="active"
            )
            .exclude(id=self.id)
            .count()
            >= 1
        ):
            raise forms.ValidationError(
                _("An active contract already exists for this employee.")
            )

        if (
            self.contract_status == "draft"
            and Contract.objects.filter(
                employee_id=self.employee_id, contract_status="draft"
            )
            .exclude(id=self.pk)
            .count()
            >= 1
        ):
            raise forms.ValidationError(
                _("A draft contract already exists for this employee.")
            )
        super().save(*args, **kwargs)
        if self.contract_status == "active" and self.wage is not None:
            try:
                wage_int = int(self.wage)
                work_info = self.employee_id.employee_work_info
                work_info.basic_salary = wage_int
                work_info.save()
            except ValueError:
                logger.error((f"Failed to convert wage '{self.wage}' to an integer."))
            except Exception as e:
                logger.error(f"An unexpected error occurred: {e}")
        return self

    class Meta:
        """
        Meta class to add additional options
        """

        unique_together = ["employee_id", "contract_start_date", "contract_end_date"]


class WorkRecord(models.Model):
    """
    WorkRecord Model
    """

    choices = [
        ("FDP", _("Present")),
        ("HDP", _("Half Day Present")),
        ("ABS", _("Absent")),
        ("HD", _("Holiday / Weekly Off")),
        ("CONF", _("Conflict")),
        ("DFT", _("Draft")),
    ]

    record_name = models.CharField(max_length=250, null=True, blank=True)
    work_record_type = models.CharField(max_length=5, null=True, choices=choices)
    employee_id = models.ForeignKey(
        Employee, on_delete=models.PROTECT, verbose_name=_("Employee")
    )
    date = models.DateField(null=True, blank=True)
    at_work = models.CharField(
        null=True,
        blank=True,
        validators=[
            validate_time_format,
        ],
        default="00:00",
        max_length=5,
    )
    min_hour = models.CharField(
        null=True,
        blank=True,
        validators=[
            validate_time_format,
        ],
        default="00:00",
        max_length=5,
    )
    at_work_second = models.IntegerField(null=True, blank=True, default=0)
    min_hour_second = models.IntegerField(null=True, blank=True, default=0)
    note = models.TextField(max_length=255)
    message = models.CharField(max_length=30, null=True, blank=True)
    is_attendance_record = models.BooleanField(default=False)
    is_leave_record = models.BooleanField(default=False)
    day_percentage = models.FloatField(default=0)
    last_update = models.DateTimeField(null=True, blank=True)
    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    def save(self, *args, **kwargs):
        self.last_update = timezone.now()

        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if not 0.0 <= self.day_percentage <= 1.0:
            raise ValidationError(_("Day percentage must be between 0.0 and 1.0"))

    def __str__(self):
        return (
            self.record_name
            if self.record_name is not None
            else f"{self.work_record_type}-{self.date}"
        )


if apps.is_installed("attendance"):
    from attendance.models import Attendance

    # class OverrideAttendance(Attendance):
    #     """
    #     Class to override Attendance model save method
    #     """
    #     pass
    # Additional fields and methods specific to AnotherModel
    # @receiver(post_save, sender=Attendance)
    # def attendance_post_save(sender, instance, **kwargs):
    #     """
    #     Overriding Attendance model save method
    #     """
    #     if instance.first_save:
    #         min_hour_second = strtime_seconds(instance.minimum_hour)
    #         at_work_second = strtime_seconds(instance.attendance_worked_hour)
    #         status = "FDP" if instance.at_work_second >= min_hour_second else "HDP"
    #         status = "CONF" if instance.attendance_validated is False else status
    #         message = (
    #             _("Validate the attendance") if status == "CONF" else _("Validated")
    #         )
    #         message = (
    #             _("Incomplete minimum hour")
    #             if status == "HDP" and min_hour_second > at_work_second
    #             else message
    #         )
    #         work_record = WorkRecord.objects.filter(
    #             date=instance.attendance_date,
    #             is_attendance_record=True,
    #             employee_id=instance.employee_id,
    #         )
    #         work_record = (
    #             WorkRecord()
    #             if not WorkRecord.objects.filter(
    #                 date=instance.attendance_date,
    #                 employee_id=instance.employee_id,
    #             ).exists()
    #             else WorkRecord.objects.filter(
    #                 date=instance.attendance_date,
    #                 employee_id=instance.employee_id,
    #             ).first()
    #         )
    #         work_record.employee_id = instance.employee_id
    #         work_record.date = instance.attendance_date
    #         work_record.at_work = instance.attendance_worked_hour
    #         work_record.min_hour = instance.minimum_hour
    #         work_record.min_hour_second = min_hour_second
    #         work_record.at_work_second = at_work_second
    #         work_record.work_record_type = status
    #         work_record.message = message
    #         work_record.is_attendance_record = True
    #         if instance.attendance_validated:
    #             work_record.day_percentage = (
    #                 1.00 if at_work_second > min_hour_second / 2 else 0.50
    #             )
    #         work_record.save()
    #         if status == "HDP" and work_record.is_leave_record:
    #             message = _("Half day leave")
    #         if status == "FDP":
    #             message = _("Present")
    #         work_record.message = message
    #         work_record.save()
    #         message = work_record.message
    #         status = work_record.work_record_type
    #         if not instance.attendance_clock_out:
    #             status = "FDP"
    #             message = _("Currently working")
    #         work_record.message = message
    #         work_record.work_record_type = status
    #         work_record.save()
    # @receiver(pre_delete, sender=Attendance)
    # def attendance_pre_delete(sender, instance, **_kwargs):
    #     """
    #     Overriding Attendance model delete method
    #     """
    #     # Perform any actions before deleting the instance
    #     # ...
    #     WorkRecord.objects.filter(
    #         employee_id=instance.employee_id,
    #         is_attendance_record=True,
    #         date=instance.attendance_date,
    #     ).delete()


if apps.is_installed("leave"):
    from leave.models import LeaveRequest

    class OverrideLeaveRequest(LeaveRequest):
        """
        Class to override Attendance model save method
        """

        pass
        # Additional fields and methods specific to AnotherModel
        # @receiver(pre_save, sender=LeaveRequest)
        # def leaverequest_pre_save(sender, instance, **_kwargs):
        #     """
        #     Overriding LeaveRequest model save method
        #     """
        #     if (
        #         instance.start_date == instance.end_date
        #         and instance.end_date_breakdown != instance.start_date_breakdown
        #     ):
        #         instance.end_date_breakdown = instance.start_date_breakdown
        #         super(LeaveRequest, instance).save()

        #     period_dates = get_date_range(instance.start_date, instance.end_date)
        #     if instance.status == "approved":
        #         for date in period_dates:
        #             try:
        #                 work_entry = (
        #                     WorkRecord.objects.filter(
        #                         date=date,
        #                         employee_id=instance.employee_id,
        #                     )
        #                     if WorkRecord.objects.filter(
        #                         date=date,
        #                         employee_id=instance.employee_id,
        #                     ).exists()
        #                     else WorkRecord()
        #                 )
        #                 work_entry.employee_id = instance.employee_id
        #                 work_entry.is_leave_record = True
        #                 work_entry.day_percentage = (
        #                     0.50
        #                     if instance.start_date == date
        #                     and instance.start_date_breakdown == "first_half"
        #                     or instance.end_date == date
        #                     and instance.end_date_breakdown == "second_half"
        #                     else 0.00
        #                 )
        #                 # scheduler task to validate the conflict entry for half day if they
        #                 # take half day leave is when they mark the attendance.
        #                 status = (
        #                     "CONF"
        #                     if instance.start_date == date
        #                     and instance.start_date_breakdown == "first_half"
        #                     or instance.end_date == date
        #                     and instance.end_date_breakdown == "second_half"
        #                     else "ABS"
        #                 )
        #                 work_entry.work_record_type = status
        #                 work_entry.date = date
        #                 work_entry.message = (
        #                     "Absent"
        #                     if status == "ABS"
        #                     else _("Half day Attendance need to validate")
        #                 )
        #                 work_entry.save()
        #             except:
        #                 pass

        #     else:
        #         for date in period_dates:
        #             WorkRecord.objects.filter(
        #                 is_leave_record=True,
        #                 date=date,
        #                 employee_id=instance.employee_id,
        #             ).delete()


# class OverrideWorkInfo(EmployeeWorkInformation):
#     """
#     This class is to override the Model default methods
#     """

# @receiver(pre_save, sender=EmployeeWorkInformation)
# def employeeworkinformation_pre_save(sender, instance, **_kwargs):
#     """
#     This method is used to override the save method for EmployeeWorkInformation Model
#     """
#     active_employee = (
#         instance.employee_id if instance.employee_id.is_active == True else None
#     )
#     if active_employee is not None:
#         contract_exists = active_employee.contract_set.exists()
#         if not contract_exists:
#             contract = Contract()
#             contract.contract_name = f"{active_employee}'s Contract"
#             contract.employee_id = active_employee
#             contract.contract_start_date = (
#                 instance.date_joining if instance.date_joining else datetime.today()
#             )
#             contract.wage = (
#                 instance.basic_salary if instance.basic_salary is not None else 0
#             )
#             contract.save()


# Create your models here.
def rate_validator(value):
    """
    Percentage validator
    """
    if value < 0:
        raise ValidationError(_("Rate must be greater than 0"))
    if value > 100:
        raise ValidationError(_("Rate must be less than 100"))


CONDITION_CHOICE = [
    ("equal", _("Equal (==)")),
    ("notequal", _("Not Equal (!=)")),
    ("lt", _("Less Than (<)")),
    ("gt", _("Greater Than (>)")),
    ("le", _("Less Than or Equal To (<=)")),
    ("ge", _("Greater Than or Equal To (>=)")),
    ("icontains", _("Contains")),
]
IF_CONDITION_CHOICE = [
    ("equal", _("Equal (==)")),
    ("notequal", _("Not Equal (!=)")),
    ("lt", _("Less Than (<)")),
    ("gt", _("Greater Than (>)")),
    ("le", _("Less Than or Equal To (<=)")),
    ("ge", _("Greater Than or Equal To (>=)")),
    ("range", _("Range")),
]
FIELD_CHOICE = [
    ("children", _("Children")),
    ("marital_status", _("Marital Status")),
    ("experience", _("Experience")),
    ("employee_work_info__experience", _("Company Experience")),
    ("gender", _("Gender")),
    ("country", _("Country")),
    ("state", _("State")),
    ("contract_set__pay_frequency", _("Pay Frequency")),
    ("contract_set__wage_type", _("Wage Type")),
    ("contract_set__department__department", _("Department on Contract")),
]








class Payslip(HorillaModel):
    """
    Payslip model
    """

    status_choices = [
        ("draft", _("Draft")),
        ("review_ongoing", _("Review Ongoing")),
        ("confirmed", _("Confirmed")),
        ("paid", _("Paid")),
    ]
    group_name = models.CharField(
        max_length=50, null=True, blank=True, verbose_name=_("Batch name")
    )
    reference = models.CharField(max_length=255, unique=False, null=True, blank=True)
    employee_id = models.ForeignKey(
        Employee, on_delete=models.PROTECT, verbose_name=_("Employee")
    )
    start_date = models.DateField()
    end_date = models.DateField()
    pay_head_data = models.JSONField()
    contract_wage = models.FloatField(null=True, default=0)
    basic_pay = models.FloatField(null=True, default=0)
    gross_pay = models.FloatField(null=True, default=0)
    deduction = models.FloatField(null=True, default=0)
    net_pay = models.FloatField(null=True, default=0)
    status = models.CharField(
        max_length=20, null=True, default="draft", choices=status_choices
    )
    sent_to_employee = models.BooleanField(null=True, default=False)
    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )

    def __str__(self) -> str:
        return f"Payslip for {self.employee_id} - Period: {self.start_date} to {self.end_date}"

    def get_status(self):
        """
        Display status
        """
        return dict(self.status_choices).get(self.status)

    def get_download_url(self):
        """
        This method to get download url
        """
        return render_template(
            path="cbv/payslip/payslip_download_tab.html",
            context={"instance": self},
        )

    def gross_pay_display(self):
        """
        gross pay
        """
        gross_pay = self.gross_pay

        return render_template(
            path="cbv/payslip/pay_display.html",
            context={"amount": gross_pay},
        )

    def deduction_display(self):
        """
        deduction
        """
        deduction = self.deduction

        return render_template(
            path="cbv/payslip/pay_display.html",
            context={"amount": deduction},
        )

    def net_pay_display(self):
        """
        net pay
        """
        net_pay = self.net_pay

        return render_template(
            path="cbv/payslip/pay_display.html",
            context={"amount": net_pay},
        )

    def custom_status_col(self):
        """
        custom status coloumn
        """

        return render_template(
            path="cbv/payslip/payslip_status_col.html",
            context={"instance": self},
        )

    def custom_actions_col(self):
        """
        custom actions coloumn
        """

        return render_template(
            path="cbv/payslip/payslip_actions.html",
            context={"instance": self},
        )

    def get_individual_payslip(self):
        """
        This method to get individual payslip
        """

        url = reverse_lazy("view-created-payslip", kwargs={"payslip_id": self.pk})
        return url

    def clean(self):
        super().clean()
        today = date.today()
        if self.end_date < self.start_date:
            raise ValidationError(
                {
                    "end_date": _(
                        "The end date must be greater than or equal to the start date"
                    )
                }
            )
        if self.end_date > today:
            raise ValidationError(_("The end date cannot be in the future."))
        if self.start_date > today:
            raise ValidationError(_("The start date cannot be in the future."))

    def save(self, *args, **kwargs):
        if (
            Payslip.objects.filter(
                employee_id=self.employee_id,
                start_date=self.start_date,
                end_date=self.end_date,
            )
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError(_("Employee ,start and end date must be unique"))

        if not isinstance(self.pay_head_data, (QueryDict, dict)):
            raise ValidationError(_("The data must be in dictionary or querydict type"))

        super().save(*args, **kwargs)

    def get_name(self):
        """
        Method is used to get the full name of the owner
        """
        return self.employee_id.get_full_name()

    def get_company(self):
        """
        Method is used to get the full name of the owner
        """
        return getattr(
            getattr(
                getattr(getattr(self, "employee_id", None), "employee_work_info", None),
                "company_id",
                None,
            ),
            "company",
            None,
        )

    def get_payslip_title(self):
        """
        Method to generate the title for a payslip.
        Returns:
            str: The title for the payslip.
        """
        if self.group_name:
            return self.group_name
        return (
            f"Payslip {self.start_date} to {self.end_date} for {self.employee_id}"
            if self.start_date != self.end_date
            else f"Payslip for {self.start_date} for {self.employee_id}"
        )

    def get_days_in_month(self):
        year = self.start_date.year
        month = self.start_date.month
        return calendar.monthrange(year, month)[1]

    class Meta:
        """
        Meta class for additional options
        """

        ordering = [
            "-end_date",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee_id", "start_date", "end_date"],
                name="unique_payslip_per_employee_period",
            )
        ]


class LoanAccount(HorillaModel):
    """
    This modal is used to store the loan Account details
    """

    loan_type = [
        ("loan", _("Loan")),
        ("advanced_salary", _("Salary Advance")),
        ("fine", _("Penalty / Fine")),
    ]
    title = models.CharField(max_length=100)
    employee_id = models.ForeignKey(
        Employee, on_delete=models.PROTECT, verbose_name=_("Employee")
    )
    type = models.CharField(default="loan", choices=loan_type, max_length=15)
    loan_amount = models.FloatField(default=0, verbose_name=_("Amount"))
    provided_date = models.DateField()
    description = models.TextField(null=True)
    is_fixed = models.BooleanField(default=True, editable=False)
    rate = models.FloatField(default=0, editable=False)
    installment_amount = models.FloatField(
        verbose_name=_("installment Amount"), blank=True, null=True
    )
    installments = models.IntegerField(verbose_name=_("Total installments"))
    installment_start_date = models.DateField(
        help_text=_("From the start date deduction will apply"),
        verbose_name=_("Installment start date"),
    )
    apply_on = models.CharField(default="end_of_month", max_length=20, editable=False)
    settled = models.BooleanField(default=False, verbose_name=_("Settled"))
    settled_date = models.DateTimeField(null=True)

    if apps.is_installed("asset"):
        asset_id = models.ForeignKey(
            "asset.Asset",
            on_delete=models.PROTECT,
            blank=True,
            null=True,
            editable=False,
        )
    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    def __str__(self):
        return f"{self.title} - {self.employee_id}"

    def installment_lines(self):
        """The loan's installment deduction lines (EmployeePayAdjustment), by date."""
        from krew_payroll.models.pay_components import EmployeePayAdjustment

        return EmployeePayAdjustment.objects.entire().filter(
            source="LOAN", source_id=self.pk, type="DEDUCTION"
        ).order_by("pay_date", "id")

    def disbursement_line(self):
        """The earning line that pays the loan out, if any."""
        from krew_payroll.models.pay_components import EmployeePayAdjustment

        return EmployeePayAdjustment.objects.entire().filter(
            source="LOAN", source_id=self.pk, type="EARNING"
        ).first()

    def has_paid_installments(self):
        return self.installment_lines().filter(payslip__isnull=False).exists()

    def installment_paid(self):
        return self.installment_lines().filter(payslip__isnull=False).count()

    def total_installments(self):
        return self.installments

    def loan_actions(self):
        """
        This method for get loan actions.
        """

        return render_template(
            path="cbv/loan/loan_actions.html",
            context={"instance": self},
        )

    def get_delete_url(self):
        """
        This method to get delete url
        """
        base_url = reverse_lazy("delete-loan")
        message = "Do you want to delete this record?"
        loan_id = self.pk
        url = f"{base_url}?ids={loan_id}"
        return f"'{url}'" + "," + f"'{message}'"

    # def delete_url(self):
    #     """
    #     Edit url
    #     """

    #     return reverse("delete-loan", kwargs={"pk": self.pk})

    def edit_url(self):
        """
        Edit url
        """
        return reverse("loan-edit-form", kwargs={"pk": self.pk})

    def progress_bar_col(self):
        """
        This method for get progress bar col.
        """

        return render_template(
            path="cbv/loan/loan_card.html",
            context={
                "instance": self,
                "total_installments": self.total_installments,
                "installment_paid": self.installment_paid,
            },
        )

    def loan_detail_view(self):
        """
        for detail view of page
        """
        url = reverse("loan-detail-view", kwargs={"pk": self.pk})
        return url

    def detail_subtitle(self):
        """
        Return subtitle containing both department and job position information.
        """
        return f"{self.employee_id.get_department()} / {self.employee_id.get_job_position()}"

    def get_installments(self):
        """
        Method to calculate installment schedule for the loan.

        Returns:
            dict: A dictionary representing the installment schedule with installment dates as keys
            and corresponding installment amounts as values.
        """
        loan_amount = self.loan_amount
        total_installments = self.installments
        installment_amount = loan_amount / total_installments
        installment_start_date = self.installment_start_date

        installment_schedule = {}

        installment_date = installment_start_date
        installment_schedule = {}
        for _ in range(total_installments):
            installment_schedule[str(installment_date)] = installment_amount
            installment_date = get_next_month_same_date(installment_date)

        return installment_schedule

    def delete(self, *args, **kwargs):
        """
        Method to delete the instance and associated objects.
        """
        from krew_payroll.models.pay_components import EmployeePayAdjustment

        # Lines already on a payslip stay there; the rest go with the loan.
        EmployeePayAdjustment.objects.entire().filter(
            source="LOAN", source_id=self.pk, payslip__isnull=True
        ).delete()
        if not Payslip.objects.filter(
            pay_adjustments__source="LOAN", pay_adjustments__source_id=self.pk
        ).exists():
            super().delete(*args, **kwargs)
        return

    def installment_ratio(self):
        """
        Method to calculate the ratio of paid installments to total installments in loan account.
        """
        total_installments = self.installments
        installment_paid = self.installment_paid()
        if not installment_paid:
            return 0
        ratio = (installment_paid / total_installments) * 100

        return ratio

    def save(self, *args, **kwargs):

        if self.settled:
            self.settled_date = timezone.now()
        else:
            self.settled_date = None

        super().save(*args, **kwargs)


class ReimbursementMultipleAttachment(models.Model):
    """
    ReimbursementMultipleAttachement Model
    """

    attachment = models.FileField(upload_to=upload_path)
    objects = models.Manager()


class Reimbursement(HorillaModel):
    """
    Reimbursement Model
    """

    reimbursement_types = [
        ("reimbursement", _("Reimbursement")),
        ("bonus_encashment", _("Bonus Point Encashment")),
    ]

    if apps.is_installed("leave"):
        reimbursement_types.append(("leave_encashment", _("Leave Encashment")))

    status_types = [
        ("requested", _("Requested")),
        ("approved", _("Approved")),
        ("rejected", _("Rejected")),
    ]
    title = models.CharField(max_length=50)
    type = models.CharField(
        choices=reimbursement_types, max_length=16, default="reimbursement"
    )
    employee_id = models.ForeignKey(
        Employee, on_delete=models.PROTECT, verbose_name="Employee"
    )
    allowance_on = models.DateField()
    attachment = models.FileField(upload_to=upload_path, null=True)
    other_attachments = models.ManyToManyField(
        ReimbursementMultipleAttachment, blank=True, editable=False
    )
    if apps.is_installed("leave"):
        leave_type_id = models.ForeignKey(
            "leave.LeaveType",
            on_delete=models.PROTECT,
            blank=True,
            null=True,
            verbose_name=_("Leave type"),
        )
    ad_to_encash = models.FloatField(
        default=0,
        help_text=_("Available Days to encash"),
        verbose_name=_("Available days"),
    )
    cfd_to_encash = models.FloatField(
        default=0,
        help_text=_("Carry Forward Days to encash"),
        verbose_name=_("Carry forward days"),
    )
    bonus_to_encash = models.IntegerField(
        default=0,
        help_text=_("Bonus points to encash"),
        verbose_name=_("Bonus points"),
    )
    amount = models.FloatField(default=0)
    status = models.CharField(
        max_length=10,
        choices=status_types,
        default="requested",
    )
    approved_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        related_name="approved_by",
        editable=False,
    )
    description = models.TextField(null=True)
    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    class Meta:
        ordering = ["-id"]

    # Approved requests are paid through a one-off earning line on the payslip.
    ADJUSTMENT_SOURCES = {
        "reimbursement": "REIMBURSEMENT",
        "leave_encashment": "ENCASHMENT",
        "bonus_encashment": "BONUS",
    }

    def pay_adjustments(self):
        from krew_payroll.models.pay_components import EmployeePayAdjustment

        return EmployeePayAdjustment.objects.entire().filter(
            source=self.ADJUSTMENT_SOURCES.get(self.type, "REIMBURSEMENT"), source_id=self.pk
        )

    def save(self, *args, **kwargs) -> None:
        request = getattr(horilla_middlewares._thread_locals, "request", None)
        amount_for_leave = (
            EncashmentGeneralSettings.objects.first().leave_amount
            if EncashmentGeneralSettings.objects.first()
            else 1
        )
        amount_for_bonus = (
            EncashmentGeneralSettings.objects.first().bonus_amount
            if EncashmentGeneralSettings.objects.first()
            else 1
        )

        # Setting the created use if the used dont have the permission
        has_perm = request.user.has_perm("krew_payroll.change_reimbursement")
        if not has_perm:
            self.employee_id = request.user.employee_get
        if self.type == "reimbursement" and self.attachment is None:
            raise ValidationError({"attachment": "This field is required"})
        if self.type == "leave_encashment" and self.leave_type_id is None:
            raise ValidationError({"leave_type_id": "This field is required"})
        if self.type == "leave_encashment":
            if self.status == "requested":
                self.amount = (
                    self.cfd_to_encash + self.ad_to_encash
                ) * amount_for_leave
            self.cfd_to_encash = max((round(self.cfd_to_encash * 2) / 2), 0)
            self.ad_to_encash = max((round(self.ad_to_encash * 2) / 2), 0)
            assigned_leave = self.leave_type_id.employee_available_leave.filter(
                employee_id=self.employee_id
            ).first()
        if self.type == "bonus_encashment":
            if self.status == "requested":
                self.amount = (self.bonus_to_encash) * amount_for_bonus
        has_line = bool(self.pk) and self.pay_adjustments().exists()
        if self.status != "approved" or not has_line:
            super().save(*args, **kwargs)
            if self.status == "approved" and not has_line:
                if self.type == "reimbursement":
                    proceed = True
                elif self.type == "bonus_encashment":
                    proceed = False
                    bonus_points = BonusPoint.objects.get(employee_id=self.employee_id)
                    if bonus_points.points >= self.bonus_to_encash:
                        proceed = True
                        bonus_points.points -= self.bonus_to_encash
                        bonus_points.reason = "bonus points has been redeemed."
                        bonus_points.save()
                    else:
                        request = getattr(
                            horilla_middlewares._thread_locals, "request", None
                        )
                        if request:
                            messages.info(
                                request,
                                "The employee don't have that much bonus points to encash.",
                            )
                else:
                    proceed = False
                    if assigned_leave:
                        available_days = assigned_leave.available_days
                        carryforward_days = assigned_leave.carryforward_days
                        if (
                            available_days >= self.ad_to_encash
                            and carryforward_days >= self.cfd_to_encash
                        ):
                            proceed = True
                            assigned_leave.available_days = (
                                available_days - self.ad_to_encash
                            )
                            assigned_leave.carryforward_days = (
                                carryforward_days - self.cfd_to_encash
                            )
                            assigned_leave.save()
                        else:
                            request = getattr(
                                horilla_middlewares._thread_locals, "request", None
                            )
                            if request:
                                messages.info(
                                    request,
                                    _(
                                        "The employee don't have that much leaves \
                                        to encash in CFD / Available days"
                                    ),
                                )

                if proceed:
                    from krew_payroll.methods.pay_components.adjustments import add_adjustment

                    add_adjustment(
                        self.employee_id,
                        "EARNING",
                        self.title,
                        self.amount,
                        self.allowance_on,
                        source=self.ADJUSTMENT_SOURCES.get(self.type, "REIMBURSEMENT"),
                        source_id=self.pk,
                        # Reimbursed expenses aren't income; encashments and bonus are.
                        is_taxable=self.type != "reimbursement",
                    )
                    if request:
                        self.approved_by = request.user.employee_get
                else:
                    self.status = "requested"
                super().save(*args, **kwargs)
            elif self.status == "rejected" and has_line:
                cfd_days = self.cfd_to_encash
                available_days = self.ad_to_encash
                if self.type == "leave_encashment" and assigned_leave:
                    assigned_leave.available_days = (
                        assigned_leave.available_days + available_days
                    )
                    assigned_leave.carryforward_days = (
                        assigned_leave.carryforward_days + cfd_days
                    )
                    assigned_leave.save()
                self.pay_adjustments().filter(payslip__isnull=True).delete()

    def delete(self, *args, **kwargs):
        request = getattr(horilla_middlewares._thread_locals, "request", None)
        if self.status == "approved":
            message = messages.info(
                request,
                _(
                    f"{self.title} is in approved state,\
                    it cannot be deleted"
                ),
            )
        else:
            self.pay_adjustments().filter(payslip__isnull=True).delete()
            super().delete(*args, **kwargs)
            message = messages.success(request, _("Reimbursement deleted"))

        return message

    def __str__(self):
        return f"{self.title}"

    def get_status_display(self):
        """
        Display status types
        """
        return dict(self.status_types).get(self.status)

    def comment_col(self):
        """
        This method for get custom coloumn .
        """

        return render_template(
            path="cbv/reimbursements/comment.html",
            context={"instance": self},
        )

    def options_col(self):
        """
        This method for get custom coloumn .
        """

        return render_template(
            path="cbv/reimbursements/options.html",
            context={"instance": self},
        )

    def actions_col(self):
        """
        This method for get custom coloumn .
        """

        return render_template(
            path="cbv/reimbursements/actions.html",
            context={"instance": self},
        )

    def amount_col(self):
        """
        This method for get custom column for amount .
        """

        return render_template(
            path="cbv/reimbursements/amount.html",
            context={"instance": self},
        )

    def attachments_col(self):
        """
        This method for get custom column for attachment .
        """

        return render_template(
            path="cbv/reimbursements/attachments.html",
            context={"instance": self},
        )

    def detail_action_col(self):
        """
        This method for get custom column for actions in detail .
        """

        return render_template(
            path="cbv/reimbursements/detail_actions.html",
            context={"instance": self},
        )

    def reimbursements_detail_view(self):
        """
        for detail view of reimbursements
        """
        url = reverse("detail-view-reimbursement", kwargs={"pk": self.pk})
        return url

    def leave_encash_detail_view(self):
        """
        for detail view of leave encashments.
        """
        url = reverse("detail-view-leave-encashment", kwargs={"pk": self.pk})
        return url

    def bonus_encash_detail_view(self):
        """
        for detail view of bonus encashments.
        """
        url = reverse("detail-view-bonus-encashment", kwargs={"pk": self.pk})
        return url


class ReimbursementFile(models.Model):
    file = models.FileField(upload_to=upload_path)
    objects = models.Manager()


class ReimbursementrequestComment(HorillaModel):
    """
    ReimbursementRequestComment Model
    """

    request_id = models.ForeignKey(Reimbursement, on_delete=models.CASCADE)
    employee_id = models.ForeignKey(Employee, on_delete=models.CASCADE)
    comment = models.TextField(null=True, verbose_name=_("Comment"), max_length=255)
    files = models.ManyToManyField(ReimbursementFile, blank=True)
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Created At"),
        null=True,
    )

    def __str__(self) -> str:
        return f"{self.comment}"


class PayrollGeneralSetting(models.Model):
    """
    PayrollGeneralSetting
    """

    notice_period = models.IntegerField(
        help_text=_("Notice period in days"),
        validators=[min_zero],
        default=30,
    )
    company_id = models.ForeignKey(Company, on_delete=models.CASCADE, null=True)


class EncashmentGeneralSettings(models.Model):
    """
    BonusPointGeneralSettings model
    """

    bonus_amount = models.IntegerField(default=1)
    leave_amount = models.IntegerField(blank=True, null=True, verbose_name="Amount")
    objects = models.Manager()


DAYS = [
    ("last day", _("Last Day")),
    ("1", "1st"),
    ("2", "2nd"),
    ("3", "3rd"),
    ("4", "4th"),
    ("5", "5th"),
    ("6", "6th"),
    ("7", "7th"),
    ("8", "8th"),
    ("9", "9th"),
    ("10", "10th"),
    ("11", "11th"),
    ("12", "12th"),
    ("13", "13th"),
    ("14", "14th"),
    ("15", "15th"),
    ("16", "16th"),
    ("17", "17th"),
    ("18", "18th"),
    ("19", "19th"),
    ("20", "20th"),
    ("21", "21th"),
    ("22", "22th"),
    ("23", "23th"),
    ("24", "24th"),
    ("25", "25th"),
    ("26", "26th"),
    ("27", "27th"),
    ("28", "28th"),
    ("29", "29th"),
    ("30", "30th"),
    ("31", "31th"),
]


class PayslipAutoGenerate(models.Model):
    """
    Model for generating payslip automatically
    """

    generate_day = models.CharField(
        max_length=30,
        choices=DAYS,
        default=("1"),
        verbose_name=_("Payslip Generate Day"),
        help_text=_("On this day of every month,Payslip will auto generate"),
    )
    auto_generate = models.BooleanField(default=False, verbose_name=_("Auto Generate"))
    company_id = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Company"),
    )
    objects = HorillaCompanyManager(related_company_field="company_id")

    def get_generate_day_display(self):
        """
        Display work type
        """
        return dict(DAYS).get(self.generate_day)

    def get_company(self):
        if self.company_id:
            return self.company_id
        return "All company"

    def is_active_col(self):
        """
        is active column
        """
        return render_template(
            path="cbv/settings/is_active_col.html", context={"instance": self}
        )

    def get_update_url(self):
        """
        This method to get update url
        """
        url = reverse_lazy("pay-slip-automation-update", kwargs={"pk": self.pk})
        return url

    def get_delete_url(self):
        """
        This method to get delete url
        """
        url = reverse_lazy("delete-auto-payslip", kwargs={"auto_id": self.pk})
        return url

    def get_instance_id(self):
        return self.id

    def clean(self):
        # Unique condition checking for all company
        if (
            not self.company_id
            and PayslipAutoGenerate.objects.filter(company_id=None).exists()
        ):
            if not self.id:
                raise ValidationError(
                    {
                        "company_id": "Auto payslip generation for all company is already exists"
                    }
                )
            all_company_auto_payslip = PayslipAutoGenerate.objects.filter(
                company_id=None
            ).first()
            if all_company_auto_payslip.id != self.id:
                raise ValidationError(
                    {
                        "company_id": "Auto payslip generation for all company is already exists"
                    }
                )

    def save(self, *args, **kwargs):
        from krew_payroll.scheduler import auto_payslip_generate

        if self.auto_generate:
            auto_payslip_generate()

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.generate_day} | {self.company_id} "


# Earnings & deductions (pay components) live in their own module; importing it
# here registers its models wherever krew_payroll.models.models is loaded.
from krew_payroll.models.pay_components import (  # noqa: E402,F401
    Calculation,
    CalculationSlab,
    EligibilityCondition,
    EligibilityEmployee,
    EmployeePayAdjustment,
    EmployeePayProfile,
    PayComponent,
    PayComponentDetails,
    WorkSite,
)
