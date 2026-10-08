"""
attendance/cbv/my_krew_attendance.py

My Krew Attendance (PRD section) -- the manager/HR-facing counterpart
to My Attendance: "the single screen where Attendance Validation,
Overtime, Regularization, and Attendance Irregularities all converge,
rather than living as four disconnected screens."

Built as a thin dashboard that reads from existing/soon-to-exist
sources, not a data-model consolidation of those four into one shared
table -- the PRD's own framing ("rather than living as four
disconnected screens") describes a UI problem, not a data-model one.
Each Need Your Action section embeds the real, already-working queue
screen directly (Validation: attendance_request.py's existing
is_validate_request queue: Regularization: RegularizationRequestListView,
unchanged; Overtime: the new queue in overtime_approval.py, built
alongside this screen specifically because nothing like it existed
before) rather than re-implementing their list/action logic here.

The per-employee calendar reuses My Attendance's own day-status
computation (attendance/cbv/my_attendance_calendar.py) -- manager-
scoped, read-only, no inline Regularization action -- rather than
computing day status a second, different way.
"""

from datetime import date

from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.cbv.my_attendance_calendar import _build_month_grid, _month_bounds, _monthly_summary
from attendance.models import Attendance, RegularizationRequest
from base.decorators import manager_can_enter
from base.methods import filtersubordinatesemployeemodel
from employee.models import Employee
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.views import TemplateView

PERM = "attendance.view_attendance"


def _team_queryset(request):
    return filtersubordinatesemployeemodel(request, Employee.objects.filter(is_active=True), PERM)


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class MyKrewAttendancePageView(TemplateView):
    """
    Page shell -- a My Krew View / Need Your Action tab toggle, same
    Alpine.js pattern as Create Attendance's entry screen and
    Attendance Irregularities' own tab toggle.
    """

    template_name = "cbv/my_krew_attendance/my_krew_attendance.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class MyKrewViewDashboardView(TemplateView):
    template_name = "cbv/my_krew_attendance/my_krew_view.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
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

        team = _team_queryset(self.request)
        team_ids = list(team.values_list("id", flat=True))

        total_working_days = Attendance.objects.filter(
            employee_id_id__in=team_ids, attendance_date__range=(start, end),
        ).values("attendance_date").distinct().count()
        total_present_days = Attendance.objects.filter(
            employee_id_id__in=team_ids, attendance_date__range=(start, end),
        ).count()

        validation_pending = Attendance.objects.filter(
            employee_id_id__in=team_ids, is_validate_request=True,
        ).count()
        overtime_pending = Attendance.objects.filter(
            employee_id_id__in=team_ids,
            overtime_second__gt=0,
            overtime_decision=Attendance.OVERTIME_DECISION_PENDING,
        ).count()
        regularization_pending = RegularizationRequest.objects.filter(
            employee_id__in=team_ids, status=RegularizationRequest.STATUS_PENDING,
        ).count()

        context["total_employees"] = len(team_ids)
        context["total_working_days"] = total_working_days
        context["total_present_days"] = total_present_days
        context["action_items"] = validation_pending + overtime_pending + regularization_pending
        context["year"] = year
        context["month"] = month
        context["month_value"] = f"{year}-{month:02d}"
        context["employees"] = team.select_related("employee_work_info")
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class MyKrewEmployeeCalendarView(TemplateView):
    """
    Read-only, manager-facing expansion of one employee's month --
    "the same underlying view as My Attendance, but manager-facing and
    read-only" (no inline Regularization action, no Request
    Regularization button).
    """

    template_name = "cbv/my_krew_attendance/employee_calendar.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        employee = Employee.objects.filter(
            pk=kwargs["employee_id"], pk__in=_team_queryset(self.request)
        ).first()
        today = date.today()
        year = int(self.request.GET.get("year", today.year))
        month = int(self.request.GET.get("month", today.month))
        start, end = _month_bounds(year, month)

        context["employee"] = employee
        context["month_label"] = date(year, month, 1).strftime("%B %Y")
        if employee is not None:
            context["weeks"] = _build_month_grid(employee, year, month, today)
            context["summary"] = _monthly_summary(employee, start, end)
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class MyKrewNeedYourActionView(TemplateView):
    """
    Four sections -- each embeds the real, already-working queue screen
    directly via its own existing list URL, rather than re-implementing
    list/action logic a second time here.
    """

    template_name = "cbv/my_krew_attendance/need_your_action.html"
