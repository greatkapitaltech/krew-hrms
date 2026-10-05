"""
attendance/cbv/create_attendance.py

Manual single-entry attendance creation (Create Attendance PRD section)
-- lets someone with the dedicated attendance.can_create_attendance
permission log an attendance record on an employee's behalf, bypassing
Validation and Overtime approval entirely (both forced True directly,
never evaluated) since a person with access is directly vouching for
the record. Distinct from the older is_validate_request-based manual
flow (attendance/cbv/attendance_request.py), which still routes into a
manager-approval queue -- confirmed by reading it directly, that flow
does the opposite of what this feature needs.

Built as a self-contained save path, not by reusing
clock_in_attendance_and_activity()/clock_out_attendance_and_activity()
directly: those functions log PUNCH_IN/PUNCH_OUT with the employee as
actor and VALIDATION_AUTO_PASS/OVERTIME_AUTO_APPROVE with a System
actor, which would misattribute every manual entry. Late-come/early-out
(Attendance Irregularities) detection is deliberately still enqueued,
per the PRD's own note that Irregularities "is assumed to still run
against manually-created records exactly like any other record" --
only Validation and Overtime approval are bypassed, not Irregularities.

Also implements the PRD's Batch Entry mode -- CreateAttendanceEntryView
is the single screen with a Single Entry / Batch Entry toggle;
CreateAttendanceBatchByEmployeesView handles "Multiple Employees, One
Date" (a crew/roll-call pattern) and CreateAttendanceBatchByDatesView
handles "One Employee, Multiple Dates" (a backfill pattern). Both
reuse _create_or_override_attendance_row() below -- the exact same
save logic as Single Entry, just called once per row -- and write
exactly one AttendanceActivityLog entry for the whole batch (not one
per row), per the PRD's own Activity Log section ("Batch actions ...
write one entry per batch ... with Affected Employee(s) shown as a
list/count").

Simplification, deliberate: unlike Single Entry's separate Check-in
Date/Check-out Date pickers (built for the overnight-shift case), a
batch row has exactly one Date -- the PRD's own batch table columns
list only "Check-in, Check-out" per row, no second date column, so an
overnight session isn't representable through Batch Entry; use Single
Entry for that case.
"""

import datetime

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View

from attendance.activity_log import log_attendance_activity
from attendance.forms import CreateAttendanceForm
from attendance.methods.utils import format_time
from attendance.models import Attendance, AttendanceActivity, AttendanceActivityLog, AttendanceRuleSet
from attendance.views.clock_in_out import _enqueue_late_come_early_out
from base.models import EmployeeShift, EmployeeShiftDay
from employee.models import Employee
from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)


def _is_payroll_locked(employee, attendance_date):
    """
    Whether `attendance_date`'s pay period has already had payroll run
    for `employee` -- always False today. No pay-group/cutoff concept
    exists anywhere in the payroll app yet (confirmed directly, not
    assumed); this is the single, obvious place to wire in the real
    check once Payroll exposes one, documented alongside the rest of
    this decision in the companion walkthrough doc
    ("Krew Attendance - Check-In Check-Out.html", section 9).
    """
    return False


def _create_or_override_attendance_row(
    *,
    employee,
    clock_in_date,
    clock_in_time,
    clock_out_date,
    clock_out_time,
    shift,
    allow_override,
):
    """
    One record's worth of Create Attendance save logic -- shared by
    Single Entry and both Batch Entry patterns (PRD: "Batch Entry ...
    one shared Date" / "Employee picked once ... repeatable table").
    attendance_date is derived from clock_in_date, same as Single
    Entry's own form (see CreateAttendanceForm's docstring).

    Returns {"conflict": <existing Attendance>} without saving anything
    if a record already exists for this employee/date and
    `allow_override` is False -- the caller decides what "needs
    confirmation" means for its own UI (one record for Single Entry, a
    rolled-up list for Batch Entry). Otherwise returns {"attendance":
    ..., "overridden": bool, "previous_description": str|None}.
    """
    attendance_date = clock_in_date
    existing = Attendance.objects.filter(
        employee_id=employee, attendance_date=attendance_date
    ).first()
    if existing and not allow_override:
        return {"conflict": existing}

    day = EmployeeShiftDay.objects.get(day=attendance_date.strftime("%A").lower())
    clock_in_dt = datetime.datetime.combine(clock_in_date, clock_in_time)
    clock_out_dt = datetime.datetime.combine(clock_out_date, clock_out_time)
    worked_seconds = int((clock_out_dt - clock_in_dt).total_seconds())
    # Entered as local wall-clock time (the admin is picking times off a
    # clock, not a UTC offset) -- made aware the same way the live
    # clock-in/out path's own datetime_now already is
    # (timezone.localtime()), rather than storing a naive datetime
    # Django would otherwise silently treat as UTC.
    clock_in_dt = timezone.make_aware(clock_in_dt)
    clock_out_dt = timezone.make_aware(clock_out_dt)

    rule_set = AttendanceRuleSet.resolve_for_employee(employee)
    snapshot = AttendanceRuleSet.capture_snapshot(rule_set)

    previous_description = None
    if existing:
        previous_description = (
            f"previous check-in {existing.attendance_clock_in}, "
            f"check-out {existing.attendance_clock_out}"
        )
        attendance = existing
        # One Check-in/Check-out pair per record (same scope boundary as
        # the rest of this module, no multi-session tracking) -- an
        # override replaces the prior punch history for this date
        # entirely, not just the Attendance row's own summary fields.
        AttendanceActivity.objects.filter(
            employee_id=employee, attendance_date=attendance_date
        ).delete()
    else:
        attendance = Attendance(employee_id=employee, attendance_date=attendance_date)

    attendance.attendance_day = day
    attendance.shift_id = shift
    attendance.attendance_clock_in = clock_in_time
    attendance.attendance_clock_in_date = clock_in_date
    attendance.attendance_clock_out = clock_out_time
    attendance.attendance_clock_out_date = clock_out_date
    attendance.attendance_worked_hour = format_time(worked_seconds)
    attendance.attendance_rule_set = rule_set
    attendance.attendance_rule_set_snapshot = snapshot
    attendance.creation_source = Attendance.CREATION_SOURCE_MANUAL
    # Forced directly, never evaluated -- handle_overtime_conditions()
    # (called inside save() below) only ever sets
    # attendance_overtime_approve True itself, never False, so setting
    # it True here first is never undone by that call. attendance_
    # validated has no such risk either: save() only resets it when
    # is_validate_request is True, which a manually-created row never is.
    attendance.attendance_validated = True
    attendance.attendance_overtime_approve = True
    attendance.save()

    AttendanceActivity.objects.create(
        employee_id=employee,
        attendance_date=attendance_date,
        shift_day=day,
        clock_in=clock_in_time,
        clock_in_date=clock_in_date,
        in_datetime=clock_in_dt,
        clock_out=clock_out_time,
        clock_out_date=clock_out_date,
        out_datetime=clock_out_dt,
    )

    # Irregularities (late-come/early-out) still applies to a manually-
    # created record exactly like any other -- only Validation and
    # Overtime approval are bypassed, per the PRD's own note on this.
    # Flexible-mode has no late-come/early-out concept to enqueue, same
    # gating as the real clock-in/out path.
    if not attendance.is_flexible_mode():
        _enqueue_late_come_early_out(attendance)

    return {
        "attendance": attendance,
        "overridden": existing is not None,
        "previous_description": previous_description,
    }


def _resolve_shift_requirement(employee):
    rule_set = AttendanceRuleSet.resolve_for_employee(employee)
    mode = rule_set.mode if rule_set else AttendanceRuleSet.MODE_SHIFT_BASED
    return mode == AttendanceRuleSet.MODE_SHIFT_BASED


def _validate_batch_row(*, employee, attendance_date, clock_in_time, clock_out_time, shift, reason, row_label):
    """
    Plain-function equivalent of CreateAttendanceForm's own validation,
    applied per-row -- Batch Entry's rows are a dynamic, JS-driven
    table (add/remove row), not something a single Django Form/formset
    maps onto cleanly within this project's existing HorillaFormView-
    based template conventions, so rows are parsed and validated by
    hand instead. Returns a list of error strings (empty if valid).
    """
    errors = []
    if employee is None:
        errors.append(_("%(row)s: employee is required.") % {"row": row_label})
    if attendance_date is None:
        errors.append(_("%(row)s: date is required.") % {"row": row_label})
    elif attendance_date > datetime.date.today():
        errors.append(_("%(row)s: date cannot be in the future.") % {"row": row_label})
    if clock_in_time is None:
        errors.append(_("%(row)s: check-in time is required.") % {"row": row_label})
    if clock_out_time is None:
        errors.append(_("%(row)s: check-out time is required.") % {"row": row_label})
    if clock_in_time is not None and clock_out_time is not None and clock_out_time < clock_in_time:
        errors.append(
            _("%(row)s: check-out must be on or after check-in.") % {"row": row_label}
        )
    if not reason:
        errors.append(_("%(row)s: reason is required.") % {"row": row_label})
    if employee is not None and _resolve_shift_requirement(employee) and not shift:
        errors.append(
            _("%(row)s: shift is required for a Shift-based employee.") % {"row": row_label}
        )
    return errors


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.can_create_attendance"), name="dispatch"
)
class CreateAttendanceFormView(HorillaFormView):
    form_class = CreateAttendanceForm
    model = Attendance
    new_display_title = _("Create Attendance")
    template_name = "cbv/create_attendance/create_attendance_form.html"

    def init_form(self, *args, data=None, files=None, instance=None, **kwargs):
        # CreateAttendanceForm is a plain forms.Form, not a ModelForm --
        # no `instance` kwarg to pass through.
        return self.form_class(data, files, initial=self.get_initial())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["creation_perm"] = "attendance.can_create_attendance"
        return context

    def form_valid(self, form: CreateAttendanceForm) -> HttpResponse:
        employee = form.cleaned_data["employee_id"]
        clock_in_date = form.cleaned_data["attendance_clock_in_date"]
        clock_in_time = form.cleaned_data["attendance_clock_in"]
        clock_out_date = form.cleaned_data["attendance_clock_out_date"]
        clock_out_time = form.cleaned_data["attendance_clock_out"]
        shift = form.cleaned_data.get("shift_id")
        reason = form.cleaned_data["reason"]
        creator = self.request.user.employee_get

        if _is_payroll_locked(employee, clock_in_date):
            messages.error(
                self.request,
                _("This pay period is locked for payroll -- cannot create or "
                  "override attendance for this date."),
            )
            return self.HttpResponse()

        allow_override = self.request.POST.get("confirm_override") == "true"
        result = _create_or_override_attendance_row(
            employee=employee,
            clock_in_date=clock_in_date,
            clock_in_time=clock_in_time,
            clock_out_date=clock_out_date,
            clock_out_time=clock_out_time,
            shift=shift,
            allow_override=allow_override,
        )
        if "conflict" in result:
            return render(
                self.request,
                "cbv/create_attendance/override_confirmation.html",
                {
                    "existing": result["conflict"],
                    "post_url": reverse("create-attendance-create"),
                    "form_data": self.request.POST,
                },
            )

        attendance = result["attendance"]
        previous_description = result["previous_description"]

        if previous_description:
            what_changed = (
                f"Attendance overridden for {clock_in_date} ({previous_description}, "
                f"replaced with check-in {clock_in_time}, check-out {clock_out_time}). "
                f"Reason: {reason}"
            )
        else:
            what_changed = (
                f"Attendance created manually for {clock_in_date} "
                f"(check-in {clock_in_time}, check-out {clock_out_time}). Reason: {reason}"
            )
        log_attendance_activity(
            actor=creator,
            action_type=AttendanceActivityLog.ACTION_MANUAL_CREATE_OVERRIDE,
            affected_employees=employee,
            what_changed=what_changed,
            source="Create Attendance",
        )

        messages.success(
            self.request,
            _("Attendance overridden.") if previous_description else _("Attendance created."),
        )
        return self.HttpResponse()


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.can_create_attendance"), name="dispatch"
)
class CreateAttendancePageView(TemplateView):
    """
    Page shell.
    """

    template_name = "cbv/create_attendance/create_attendance.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.can_create_attendance"), name="dispatch"
)
class CreateAttendanceListView(HorillaListView):
    """
    Everything created/overridden through this screen -- the one place
    to see what's been manually entered, since Create Attendance's own
    records don't otherwise stand out in the main Attendances list.
    """

    model = Attendance
    bulk_select_option = False
    quick_export = True

    columns = [
        (_("Employee"), "employee_id"),
        (_("Date"), "attendance_date"),
        (_("Check-in"), "attendance_clock_in"),
        (_("Check-out"), "attendance_clock_out"),
        (_("Shift"), "shift_id"),
        (_("Created By"), "created_by"),
        (_("Created At"), "created_at"),
    ]
    default_columns = columns

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(
            creation_source=Attendance.CREATION_SOURCE_MANUAL
        ).order_by("-created_at")


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.can_create_attendance"), name="dispatch"
)
class CreateAttendanceNav(HorillaNavView):
    nav_title = _("Create Attendance")
    search_swap_target = "#listContainer"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("create-attendance-list")
        self.create_attrs = f"""
            data-toggle="oh-modal-toggle"
            data-target="#genericModal"
            hx-get="{reverse('create-attendance-entry')}"
            hx-target="#genericModalBody"
        """


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.can_create_attendance"), name="dispatch"
)
class CreateAttendanceEntryView(TemplateView):
    """
    The one screen the PRD describes, with a Single Entry / Batch Entry
    mode toggle -- loaded into the modal in place of what used to be a
    direct link to CreateAttendanceFormView. Each of the three panels
    lazy-loads its own existing/new endpoint (hx-trigger="load"), so
    this view itself renders nothing but the toggle shell.
    """

    template_name = "cbv/create_attendance/create_attendance_entry.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.can_create_attendance"), name="dispatch"
)
class CreateAttendanceBatchByEmployeesView(View):
    """
    Batch Entry -- "Multiple Employees, One Date" (a crew/roll-call
    pattern, e.g. logging a whole site for today): one shared Date,
    then one row per employee.
    """

    template_name = "cbv/create_attendance/batch_by_employees_form.html"

    def get(self, request, *args, **kwargs):
        return render(
            request,
            self.template_name,
            {
                "employees": Employee.objects.filter(is_active=True),
                "shifts": EmployeeShift.objects.all(),
            },
        )

    def post(self, request, *args, **kwargs):
        attendance_date = parse_date(request.POST.get("attendance_date") or "")
        employee_ids = request.POST.getlist("employee_id")
        clock_ins = request.POST.getlist("clock_in")
        clock_outs = request.POST.getlist("clock_out")
        reasons = request.POST.getlist("reason")
        shift_ids = request.POST.getlist("shift_id")

        errors = []
        if attendance_date is None:
            errors.append(_("Date is required."))
        elif attendance_date > datetime.date.today():
            errors.append(_("Date cannot be in the future."))

        rows = []
        seen_employee_ids = set()
        for index, raw_employee_id in enumerate(employee_ids):
            if not raw_employee_id and not clock_ins[index] and not clock_outs[index]:
                # A blank trailing row left over from "Add Row" -- skip
                # rather than error, so an admin doesn't have to delete
                # an unused row before submitting.
                continue
            employee = Employee.objects.filter(
                pk=raw_employee_id, is_active=True
            ).first()
            row_label = _("Row %(n)s") % {"n": index + 1}
            if employee is not None:
                if employee.pk in seen_employee_ids:
                    errors.append(
                        _("%(row)s: %(employee)s is listed more than once.")
                        % {"row": row_label, "employee": employee}
                    )
                seen_employee_ids.add(employee.pk)
                row_label = f"{row_label} ({employee})"
            clock_in_time = parse_time(clock_ins[index]) if clock_ins[index] else None
            clock_out_time = parse_time(clock_outs[index]) if clock_outs[index] else None
            shift = (
                EmployeeShift.objects.filter(pk=shift_ids[index]).first()
                if shift_ids[index]
                else None
            )
            reason = reasons[index].strip() if reasons[index] else ""
            errors.extend(
                _validate_batch_row(
                    employee=employee,
                    attendance_date=attendance_date,
                    clock_in_time=clock_in_time,
                    clock_out_time=clock_out_time,
                    shift=shift,
                    reason=reason,
                    row_label=row_label,
                )
            )
            rows.append(
                {
                    "employee": employee,
                    "clock_in_time": clock_in_time,
                    "clock_out_time": clock_out_time,
                    "shift": shift,
                    "reason": reason,
                }
            )

        if not rows and not errors:
            errors.append(_("Add at least one employee row."))

        if errors:
            return render(
                request,
                self.template_name,
                {
                    "employees": Employee.objects.filter(is_active=True),
                    "shifts": EmployeeShift.objects.all(),
                    "errors": errors,
                    "attendance_date": request.POST.get("attendance_date", ""),
                },
            )

        allow_override = request.POST.get("confirm_override") == "true"
        if not allow_override:
            conflicts = [
                {"employee": row["employee"], "existing": existing}
                for row in rows
                for existing in [
                    Attendance.objects.filter(
                        employee_id=row["employee"], attendance_date=attendance_date
                    ).first()
                ]
                if existing is not None
            ]
            if conflicts:
                return render(
                    request,
                    "cbv/create_attendance/batch_override_confirmation.html",
                    {
                        "conflicts": conflicts,
                        "attendance_date": attendance_date,
                        "post_url": reverse("create-attendance-batch-by-employees"),
                        "form_data": request.POST,
                    },
                )

        creator = request.user.employee_get
        saved = []
        overridden_count = 0
        with transaction.atomic():
            for row in rows:
                result = _create_or_override_attendance_row(
                    employee=row["employee"],
                    clock_in_date=attendance_date,
                    clock_in_time=row["clock_in_time"],
                    clock_out_date=attendance_date,
                    clock_out_time=row["clock_out_time"],
                    shift=row["shift"],
                    allow_override=True,
                )
                saved.append(result)
                if result["overridden"]:
                    overridden_count += 1

        log_attendance_activity(
            actor=creator,
            action_type=AttendanceActivityLog.ACTION_MANUAL_CREATE_OVERRIDE,
            affected_employees=[row["employee"] for row in rows],
            what_changed=(
                f"Batch created {len(saved)} attendance record(s) for "
                f"{attendance_date} ({overridden_count} overridden)."
            ),
            source="Create Attendance (Batch)",
        )
        messages.success(
            request,
            _("%(count)d attendance record(s) saved.") % {"count": len(saved)},
        )
        return HorillaFormView.HttpResponse()


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.can_create_attendance"), name="dispatch"
)
class CreateAttendanceBatchByDatesView(View):
    """
    Batch Entry -- "One Employee, Multiple Dates" (a backfill pattern,
    e.g. catching up a week of missed punches for one person): Employee
    picked once, then a repeatable table of Date/Check-in/Check-out/
    Reason/Shift rows.
    """

    template_name = "cbv/create_attendance/batch_by_dates_form.html"

    def get(self, request, *args, **kwargs):
        return render(
            request,
            self.template_name,
            {
                "employees": Employee.objects.filter(is_active=True),
                "shifts": EmployeeShift.objects.all(),
            },
        )

    def post(self, request, *args, **kwargs):
        employee = Employee.objects.filter(
            pk=request.POST.get("employee_id"), is_active=True
        ).first()
        dates_raw = request.POST.getlist("attendance_date")
        clock_ins = request.POST.getlist("clock_in")
        clock_outs = request.POST.getlist("clock_out")
        reasons = request.POST.getlist("reason")
        shift_ids = request.POST.getlist("shift_id")

        errors = []
        if employee is None:
            errors.append(_("Employee is required."))

        rows = []
        seen_dates = set()
        for index, raw_date in enumerate(dates_raw):
            if not raw_date and not clock_ins[index] and not clock_outs[index]:
                continue
            attendance_date = parse_date(raw_date) if raw_date else None
            row_label = _("Row %(n)s") % {"n": index + 1}
            if attendance_date is not None:
                if attendance_date in seen_dates:
                    errors.append(
                        _("%(row)s: %(date)s is listed more than once.")
                        % {"row": row_label, "date": attendance_date}
                    )
                seen_dates.add(attendance_date)
                row_label = f"{row_label} ({attendance_date})"
            clock_in_time = parse_time(clock_ins[index]) if clock_ins[index] else None
            clock_out_time = parse_time(clock_outs[index]) if clock_outs[index] else None
            shift = (
                EmployeeShift.objects.filter(pk=shift_ids[index]).first()
                if shift_ids[index]
                else None
            )
            reason = reasons[index].strip() if reasons[index] else ""
            errors.extend(
                _validate_batch_row(
                    employee=employee,
                    attendance_date=attendance_date,
                    clock_in_time=clock_in_time,
                    clock_out_time=clock_out_time,
                    shift=shift,
                    reason=reason,
                    row_label=row_label,
                )
            )
            rows.append(
                {
                    "attendance_date": attendance_date,
                    "clock_in_time": clock_in_time,
                    "clock_out_time": clock_out_time,
                    "shift": shift,
                    "reason": reason,
                }
            )

        if not rows and not errors:
            errors.append(_("Add at least one date row."))

        if errors:
            return render(
                request,
                self.template_name,
                {
                    "employees": Employee.objects.filter(is_active=True),
                    "shifts": EmployeeShift.objects.all(),
                    "errors": errors,
                    "employee_id": request.POST.get("employee_id", ""),
                },
            )

        allow_override = request.POST.get("confirm_override") == "true"
        if not allow_override:
            conflicts = [
                {"attendance_date": row["attendance_date"], "existing": existing}
                for row in rows
                for existing in [
                    Attendance.objects.filter(
                        employee_id=employee, attendance_date=row["attendance_date"]
                    ).first()
                ]
                if existing is not None
            ]
            if conflicts:
                return render(
                    request,
                    "cbv/create_attendance/batch_override_confirmation.html",
                    {
                        "conflicts": conflicts,
                        "employee": employee,
                        "post_url": reverse("create-attendance-batch-by-dates"),
                        "form_data": request.POST,
                    },
                )

        creator = request.user.employee_get
        saved = []
        overridden_count = 0
        with transaction.atomic():
            for row in rows:
                result = _create_or_override_attendance_row(
                    employee=employee,
                    clock_in_date=row["attendance_date"],
                    clock_in_time=row["clock_in_time"],
                    clock_out_date=row["attendance_date"],
                    clock_out_time=row["clock_out_time"],
                    shift=row["shift"],
                    allow_override=True,
                )
                saved.append(result)
                if result["overridden"]:
                    overridden_count += 1

        log_attendance_activity(
            actor=creator,
            action_type=AttendanceActivityLog.ACTION_MANUAL_CREATE_OVERRIDE,
            affected_employees=employee,
            what_changed=(
                f"Batch created {len(saved)} attendance record(s) for "
                f"{employee} ({overridden_count} overridden)."
            ),
            source="Create Attendance (Batch)",
        )
        messages.success(
            request,
            _("%(count)d attendance record(s) saved.") % {"count": len(saved)},
        )
        return HorillaFormView.HttpResponse()
