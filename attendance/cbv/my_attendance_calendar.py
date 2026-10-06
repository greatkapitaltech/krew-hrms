"""
attendance/cbv/my_attendance_calendar.py

My Attendance (PRD section) -- the employee's own color-coded monthly
calendar. Replaces the plain list screen (my_attendances.py) as the
target of the existing `view-my-attendance` URL/sidebar entry; that
list view's own URLs/code are left fully intact, just unlinked from
the sidebar -- same "unlink UI, keep backend" precedent used elsewhere
in this module (grace_time, the old validation-threshold screen, etc.).

Color precedence, per day (confirmed directly against the PRD text,
not the earlier paraphrase in the plan doc):
  1. Holiday (is_holiday()) -- restricted (is_specific=True) -> pink;
     public -> blue.
  2. Else company leave/weekend (is_company_leave()), or an approved
     personal LeaveRequest covering the date -> blue.
  3. Else, for a day on or before today: an Attendance record exists?
     -> green, UNLESS an APPROVED RegularizationRequest exists against
     it (green + note icon), or _open_regularizable_reasons() is
     non-empty (red). No record at all -> red (PRD's own explicit
     default for an unmarked past day).
  4. Today -> a "Today" marker, no color (real status only resolves
     once the midnight scheduler finalizes the record).
  5. Future -> no color, no attendance data shown at all (holiday/leave
     still shown) -- the PRD's own roster/shift-projection overlay for
     future days is a forward dependency on the Employee module's
     Roster (KREW-10), which doesn't exist anywhere in this codebase
     yet; deliberately not built here, not a gap introduced by this
     screen.

"Needs attention" (red) is deliberately scoped to the exact same two
flags Regularization's own eligible-days list already checks
(_open_regularizable_reasons() -- geo-location dispute, overtime
pending approval), not Attendance Irregularities (late-come/early-out):
the PRD itself describes Irregularities as "informational-only, gating
nothing," unlike a genuine unresolved Validation/Overtime flag.
"""

import calendar
from datetime import date, timedelta

from django.db.models import Q
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.cbv.regularization_request import _open_regularizable_reasons
from attendance.models import Attendance, AttendanceLateComeEarlyOut, RegularizationRequest
from base.methods import is_company_leave, is_holiday
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.views import TemplateView
from leave.models import LeaveRequest

COLOR_GREEN = "green"
COLOR_GREEN_NOTE = "green_note"
COLOR_RED = "red"
COLOR_BLUE = "blue"
COLOR_PINK = "pink"
COLOR_TODAY = "today"
COLOR_FUTURE = "future"


def _day_status(employee, day, today):
    """
    One day's worth of calendar-face status -- see module docstring for
    the exact precedence. Returns a dict with at least `color`; `label`
    is a short human string for the day's detail view/tooltip.
    """
    if day == today:
        return {"color": COLOR_TODAY, "label": _("Today")}

    holiday = is_holiday(day, employee=employee)
    if holiday:
        if holiday.is_specific:
            return {"color": COLOR_PINK, "label": str(holiday.name), "holiday": holiday}
        return {"color": COLOR_BLUE, "label": str(holiday.name), "holiday": holiday}

    company_leave = is_company_leave(day)
    if company_leave:
        return {"color": COLOR_BLUE, "label": _("Weekly Off")}

    leave_request = (
        LeaveRequest.objects.filter(
            employee_id=employee,
            status="approved",
            start_date__lte=day,
        )
        .filter(_end_date_covers(day))
        .select_related("leave_type_id")
        .first()
    )
    if leave_request:
        return {
            "color": COLOR_BLUE,
            "label": str(leave_request.leave_type_id.name),
            "leave_request": leave_request,
        }

    if day > today:
        return {"color": COLOR_FUTURE, "label": _("Upcoming")}

    attendance = Attendance.objects.filter(
        employee_id=employee, attendance_date=day
    ).first()
    if attendance is None:
        return {"color": COLOR_RED, "label": _("Needs Attention"), "attendance": None}

    approved_request = RegularizationRequest.objects.filter(
        attendance=attendance, status=RegularizationRequest.STATUS_APPROVED
    ).first()
    if approved_request is not None:
        return {
            "color": COLOR_GREEN_NOTE,
            "label": _("Corrected via Regularization"),
            "attendance": attendance,
            "regularization_request": approved_request,
        }

    if _open_regularizable_reasons(attendance):
        return {"color": COLOR_RED, "label": _("Needs Attention"), "attendance": attendance}

    return {"color": COLOR_GREEN, "label": _("Present"), "attendance": attendance}


def _end_date_covers(day):
    """
    LeaveRequest.end_date is nullable (a single-day request may leave it
    blank) -- covers both "day falls within a date range" and "day IS
    the (end_date-less) start_date" in one filter, same two-query-shape
    precedent attendance/views/summary.py's _build_calendar_context
    already uses (qs_range/qs_single), collapsed into one Q here since
    this only ever checks a single day, not a whole month range.
    """
    return Q(end_date__gte=day) | Q(end_date__isnull=True, start_date=day)


def _month_bounds(year, month):
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _build_month_grid(employee, year, month, today):
    """
    A list of weeks, each a list of 7 day-cells (None for padding days
    outside the month) -- calendar.monthrange()'s own Monday-first
    convention, matching every other hand-built calendar in this
    project (attendance/templates/attendance/monthly_summary/
    calendar_modal.html uses the same grid shape).
    """
    first_weekday, days_in_month = calendar.monthrange(year, month)
    cells = [None] * first_weekday
    for day_num in range(1, days_in_month + 1):
        day = date(year, month, day_num)
        status = _day_status(employee, day, today)
        status["date"] = day
        status["day_num"] = day_num
        cells.append(status)
    while len(cells) % 7:
        cells.append(None)
    return [cells[i : i + 7] for i in range(0, len(cells), 7)]


def _monthly_summary(employee, start, end):
    days_present = Attendance.objects.filter(
        employee_id=employee, attendance_date__range=(start, end)
    ).count()
    late_comes = AttendanceLateComeEarlyOut.objects.filter(
        employee_id=employee,
        type="late_come",
        attendance_id__attendance_date__range=(start, end),
    ).count()
    overtime_seconds = sum(
        Attendance.objects.filter(
            employee_id=employee, attendance_date__range=(start, end)
        ).values_list("overtime_second", flat=True)
    )
    regularization_pending = RegularizationRequest.objects.filter(
        employee=employee,
        status=RegularizationRequest.STATUS_PENDING,
        attendance__attendance_date__range=(start, end),
    ).count()
    return {
        "days_present": days_present,
        "late_comes": late_comes,
        "ot_hours_accrued": round((overtime_seconds or 0) / 3600, 2),
        "regularization_pending": regularization_pending,
    }


@method_decorator(login_required, name="dispatch")
class MyAttendanceCalendarPageView(TemplateView):
    """
    The one screen this ticket builds -- a month-grid calendar for
    request.user's own employee record, with back-navigation to prior
    months (PRD: "employees can navigate back to prior months").
    """

    template_name = "cbv/my_attendance_calendar/my_attendance_calendar.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        employee = self.request.user.employee_get
        today = date.today()

        month_param = self.request.GET.get("month")
        if month_param:
            try:
                year, month = (int(part) for part in month_param.split("-"))
            except (ValueError, TypeError):
                year, month = today.year, today.month
        else:
            year, month = today.year, today.month

        start, end = _month_bounds(year, month)
        prev_month = date(year, month, 1) - timedelta(days=1)
        next_month = end + timedelta(days=1)

        context["employee"] = employee
        context["year"] = year
        context["month"] = month
        context["month_label"] = date(year, month, 1).strftime("%B %Y")
        context["weeks"] = _build_month_grid(employee, year, month, today)
        context["summary"] = _monthly_summary(employee, start, end)
        context["prev_month_value"] = f"{prev_month.year}-{prev_month.month:02d}"
        context["next_month_value"] = f"{next_month.year}-{next_month.month:02d}"
        context["raise_regularization_url"] = reverse("regularization-request-form")
        return context


@method_decorator(login_required, name="dispatch")
class MyAttendanceDayDetailView(TemplateView):
    """
    Click-through detail for one day -- the fuller record the PRD
    describes (punch times, OT, leave/holiday reason), and, only if
    flagged, the inline "Request Regularization" action reusing
    Attendance.raise_regularization_url (the exact ?attendance=<pk>
    convention already built for RegularizableAttendanceListView).
    """

    template_name = "cbv/my_attendance_calendar/day_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        employee = self.request.user.employee_get
        today = date.today()
        day = date(int(kwargs["year"]), int(kwargs["month"]), int(kwargs["day"]))

        status = _day_status(employee, day, today)
        status["date"] = day
        context["status"] = status
        context["flagged"] = status["color"] == COLOR_RED and status.get("attendance") is not None
        return context
