"""
attendance/cbv/payroll_readiness.py

Payroll Readiness (PRD section) -- maps WorkRecords'/AttendanceOverTime's
existing vocabulary onto the PRD's Day Type column, rather than
introducing parallel fields that would drift out of sync with the
records they're describing. WorkRecords is already kept live by an
existing signal (attendance_post_save() in attendance/signals.py) --
nothing here writes to it, this module only reads it.

Day Type mapping, in order: an open Exception (see _is_exception()
below -- checked FIRST, since a day can otherwise look like ordinary
worked/leave/holiday time while still carrying an unresolved decision)
-> Exception; else an approved personal leave -> Approved Leave; else
FDP/HDP -> Worked; else HD -> Holiday or Weekly-Off (is_holiday() tells
these two apart, since WorkRecords only has one combined HD choice);
else (ABS, or DFT/not-yet-processed) -> Exception, since neither has a
clean slot in the PRD's five-value vocabulary and both represent a day
that isn't cleanly resolved one way or the other.

_is_exception() checks three independent sources, not just WorkRecords'
own CONF flag -- confirmed directly that CONF only ever reflects
Attendance.attendance_validated, which the new Overtime queue
(overtime_approval.py) and Regularization's own approval flow can both
leave stale: approving/denying overtime never touches
attendance_validated, and neither does resolving a Regularization
request. A day with a still-pending Overtime decision or a still-
pending Regularization request is just as much an open exception as an
unvalidated one, even though WorkRecords itself has no way to show that.
"""

import calendar
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.methods.utils import strtime_seconds
from attendance.models import (
    Attendance,
    PayrollReadinessSnapshot,
    PayrollReadinessSnapshotRow,
    RegularizationRequest,
    WorkRecords,
)
from base.methods import get_date_range, is_holiday
from base.models import Company
from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import TemplateView

PERM = "attendance.change_attendance"


def _current_company(request):
    selected = request.session.get("selected_company")
    if not selected or selected == "all":
        return None
    return Company.objects.filter(pk=selected).first()


def _is_exception(work_record):
    """
    True if this day still carries an unresolved decision from any of
    the three sources that can leave one open -- Validation (via
    WorkRecords' own CONF, which already reflects attendance_validated
    live), Overtime (the new manager-initiated queue), or Regularization
    (an employee-raised dispute still awaiting a decision).
    """
    if work_record.work_record_type == "CONF":
        return True
    attendance = work_record.attendance_id
    if attendance is None:
        return False
    if (
        (attendance.overtime_second or 0) > 0
        and attendance.overtime_decision == Attendance.OVERTIME_DECISION_PENDING
    ):
        return True
    return RegularizationRequest.objects.filter(
        attendance=attendance, status=RegularizationRequest.STATUS_PENDING
    ).exists()


def _day_type_for(work_record, employee):
    if _is_exception(work_record):
        return PayrollReadinessSnapshotRow.DAY_EXCEPTION
    if work_record.is_leave_record:
        return PayrollReadinessSnapshotRow.DAY_APPROVED_LEAVE
    if work_record.work_record_type in ("FDP", "HDP"):
        return PayrollReadinessSnapshotRow.DAY_WORKED
    if work_record.work_record_type == "HD":
        return (
            PayrollReadinessSnapshotRow.DAY_HOLIDAY
            if is_holiday(work_record.date, employee=employee)
            else PayrollReadinessSnapshotRow.DAY_WEEKLY_OFF
        )
    # ABS (absent, no leave on file) and DFT (not yet processed by any
    # real Attendance row) both fall here -- neither has a clean slot
    # in the PRD's five-value Day Type vocabulary, and both represent a
    # day that isn't cleanly resolved one way or the other.
    return PayrollReadinessSnapshotRow.DAY_EXCEPTION


def _employee_rows_for_range(employee, start_date, end_date):
    """
    One dict per calendar day in range for `employee` -- the shared
    shape both Live Preview (computed fresh every time) and the lock
    action (frozen once into PayrollReadinessSnapshotRow) build from.
    """
    records = {
        wr.date: wr
        for wr in WorkRecords.objects.filter(
            employee_id=employee, date__range=(start_date, end_date)
        ).select_related("attendance_id")
    }
    rows = []
    for day in get_date_range(start_date, end_date):
        work_record = records.get(day)
        if work_record is None:
            continue
        attendance = work_record.attendance_id
        day_type = _day_type_for(work_record, employee)
        is_exception = day_type == PayrollReadinessSnapshotRow.DAY_EXCEPTION
        rows.append(
            {
                "date": day,
                "day_type": day_type,
                "check_in": attendance.attendance_clock_in if attendance else None,
                "check_out": attendance.attendance_clock_out if attendance else None,
                "worked_hours": "00:00" if is_exception else (work_record.at_work or "00:00"),
                "overtime_hours": (
                    "00:00"
                    if is_exception or attendance is None or not attendance.attendance_overtime_approve
                    else attendance.attendance_overtime
                ),
                "was_exception": is_exception,
            }
        )
    return rows


def _summary_for_rows(rows):
    total_working_days = sum(
        1 for r in rows if r["day_type"] != PayrollReadinessSnapshotRow.DAY_WEEKLY_OFF
        and r["day_type"] != PayrollReadinessSnapshotRow.DAY_HOLIDAY
    )
    total_present_days = sum(
        1 for r in rows if r["day_type"] == PayrollReadinessSnapshotRow.DAY_WORKED
    )
    total_ot_seconds = sum(strtime_seconds(r["overtime_hours"]) for r in rows)
    return {
        "total_working_days": total_working_days,
        "total_present_days": total_present_days,
        "total_ot_hours": round(total_ot_seconds / 3600, 2),
    }


def _active_employees(company):
    from employee.models import Employee

    return Employee.objects.filter(
        is_active=True, employee_work_info__company_id=company
    )


def is_date_locked(employee, check_date):
    """
    True if `check_date` falls inside an already-locked
    PayrollReadinessSnapshot for `employee`'s company -- the real check
    Create Attendance's own _is_payroll_locked() stub was always meant
    to grow into once a real payroll-cutoff concept existed (it never
    did, until this model). Also the shared gate for Regularization's
    and the Overtime queue's own approve/reject actions: once a period
    is locked, nothing should be able to retroactively change the
    attendance data payroll already reported against for those dates --
    a change after the fact would silently diverge from what was
    already locked in, with nothing to show it happened.
    """
    company = getattr(getattr(employee, "employee_work_info", None), "company_id", None)
    if company is None or check_date is None:
        return False
    return PayrollReadinessSnapshot.objects.filter(
        company=company, start_date__lte=check_date, end_date__gte=check_date,
    ).exists()


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm=PERM), name="dispatch")
class PayrollReadinessPageView(TemplateView):
    template_name = "cbv/payroll_readiness/payroll_readiness.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_company"] = _current_company(self.request)
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm=PERM), name="dispatch")
class PayrollReadinessLivePreviewView(TemplateView):
    """
    Always a live query against a manually-specified date range -- no
    automatic pay-group-cutoff trigger, since no pay-group/cutoff
    concept exists anywhere in payroll/ yet (confirmed, not assumed) --
    same "obvious extension point, not a blocker" treatment as Create
    Attendance's own payroll-cutoff stub.
    """

    template_name = "cbv/payroll_readiness/live_preview.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = _current_company(self.request)
        today = date.today()
        start_date = _parse_date(self.request.GET.get("start_date")) or today.replace(day=1)
        end_date = _parse_date(self.request.GET.get("end_date")) or today

        context["company"] = company
        context["start_date"] = start_date
        context["end_date"] = end_date
        context["rows"] = []
        if company is not None:
            summaries = []
            for employee in _active_employees(company):
                day_rows = _employee_rows_for_range(employee, start_date, end_date)
                if not day_rows:
                    continue
                summary = _summary_for_rows(day_rows)
                summary["employee"] = employee
                summary["days"] = day_rows
                summaries.append(summary)
            context["rows"] = summaries
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm=PERM), name="dispatch")
class PayrollReadinessFinalReportView(TemplateView):
    """
    Lists every snapshot already locked for the current company, plus
    the "Lock Forever" form for a new period. Once a snapshot exists
    for an exact (company, start_date, end_date) triple, locking it
    again is rejected outright (PayrollReadinessSnapshot's own unique
    constraint) -- never silently overwritten.
    """

    template_name = "cbv/payroll_readiness/final_report.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = _current_company(self.request)
        context["company"] = company
        context["snapshots"] = (
            PayrollReadinessSnapshot.objects.filter(company=company)
            if company is not None
            else PayrollReadinessSnapshot.objects.none()
        )
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm=PERM), name="dispatch")
class PayrollReadinessSnapshotDetailView(TemplateView):
    """
    Reads only PayrollReadinessSnapshotRow -- never live WorkRecords/
    Attendance data -- which is what actually makes a locked period
    immutable: the source data it was built from can keep changing
    after the fact, this frozen copy can't.
    """

    template_name = "cbv/payroll_readiness/snapshot_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        snapshot = PayrollReadinessSnapshot.objects.filter(pk=kwargs["pk"]).first()
        context["snapshot"] = snapshot
        if snapshot is None:
            return context

        by_employee = {}
        for row in snapshot.rows.select_related("employee").order_by("employee_id", "date"):
            entry = by_employee.setdefault(
                row.employee_id, {"employee": row.employee, "days": []}
            )
            entry["days"].append(
                {
                    "date": row.date,
                    "day_type": row.day_type,
                    "day_type_display": row.get_day_type_display(),
                    "check_in": row.check_in,
                    "check_out": row.check_out,
                    "worked_hours": row.worked_hours,
                    "overtime_hours": row.overtime_hours,
                    "was_exception": row.was_exception,
                }
            )
        for entry in by_employee.values():
            entry["summary"] = _summary_for_rows(entry["days"])
        context["rows"] = sorted(by_employee.values(), key=lambda e: str(e["employee"]))
        return context


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm=PERM), name="dispatch")
class LockPayrollReadinessPeriodView(TemplateView):
    """
    POST-only in practice (GET just re-renders the final report list
    with an error) -- creates one PayrollReadinessSnapshot plus one
    PayrollReadinessSnapshotRow per employee per day in range, zeroing
    out any day that still has an open Exception at this exact moment
    (PRD's own rule: locking proceeds regardless, the open exception's
    hours go in as zero, permanently, for this cycle -- it does not
    block the lock).
    """

    def post(self, request, *args, **kwargs):
        company = _current_company(request)
        start_date = _parse_date(request.POST.get("start_date"))
        end_date = _parse_date(request.POST.get("end_date"))

        overlapping = None
        if company is not None and start_date and end_date and end_date >= start_date:
            # Two ranges [s1,e1]/[s2,e2] overlap iff s1<=e2 and s2<=e1 --
            # catches a full duplicate (the old, too-narrow check) *and*
            # any partial overlap (e.g. locking Sept 1-30 after Sept
            # 1-Oct 31 is already locked), which the exact-match-only
            # check above used to let straight through.
            overlapping = PayrollReadinessSnapshot.objects.filter(
                company=company, start_date__lte=end_date, end_date__gte=start_date,
            ).first()

        if company is None:
            messages.error(request, _("Select a single company before locking a period."))
        elif not start_date or not end_date or end_date < start_date:
            messages.error(request, _("Enter a valid date range."))
        elif overlapping is not None:
            messages.error(
                request,
                _(
                    "This period overlaps an already-locked period (%(start)s - %(end)s) -- "
                    "it cannot be locked."
                )
                % {"start": overlapping.start_date, "end": overlapping.end_date},
            )
        else:
            locker = request.user.employee_get
            with transaction.atomic():
                snapshot = PayrollReadinessSnapshot.objects.create(
                    company=company, start_date=start_date, end_date=end_date, locked_by=locker,
                )
                snapshot_rows = []
                for employee in _active_employees(company):
                    for day_row in _employee_rows_for_range(employee, start_date, end_date):
                        snapshot_rows.append(
                            PayrollReadinessSnapshotRow(
                                snapshot=snapshot,
                                employee=employee,
                                date=day_row["date"],
                                day_type=day_row["day_type"],
                                check_in=day_row["check_in"],
                                check_out=day_row["check_out"],
                                worked_hours=day_row["worked_hours"],
                                overtime_hours=day_row["overtime_hours"],
                                was_exception=day_row["was_exception"],
                            )
                        )
                PayrollReadinessSnapshotRow.objects.bulk_create(snapshot_rows)
            messages.success(
                request,
                _("Period locked -- %(count)d day-records frozen.") % {"count": len(snapshot_rows)},
            )
        # HX-Refresh tells htmx to do a full page reload regardless of
        # hx-swap/hx-target on the triggering form -- more reliable here
        # than a swapped-in <script> tag, since hx-swap="none" (used on
        # that form so a successful lock doesn't blank out the page)
        # skips processing the response body entirely, scripts included.
        response = HttpResponse()
        response["HX-Refresh"] = "true"
        return response
