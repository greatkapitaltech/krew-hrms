"""
attendance/cbv/attendance_irregularities.py

Attendance Irregularities (PRD section) -- Screen 2 of that ticket's own
UI Specification ("My Team/krew Attendance, new 'Irregularities' tab:
two views toggled within the tab -- Instances ... and By Employee ...
Read-only for MVP -- no action buttons"). Screen 1 (the Enable toggle
in Attendance Configuration) was already built -- irregularities_enabled
is in ATTENDANCE_RULE_SET_EDITABLE_FIELDS, exposed on the existing
AttendanceRuleSet settings screen for all three tiers.

Built as its own standalone screen rather than extending the existing
"Late Arrival & Early Departure" screen (late_come_and_early_out.py):
that one excludes Flexible/no-shift employees entirely
(attendance_id__shift_id__isnull=False), has penalty-assignment
actions, and has no Expected-vs-Actual/Delta columns -- none of which
matches this PRD ticket's "read-only, no actions, all three types
including Hours Shortfall" spec. My Krew Attendance's own Irregularities
tab (still unbuilt) is expected to embed/link to this screen later.

Expected/Actual/Delta are computed on the fly, not stored --
AttendanceLateComeEarlyOut only ever stored the fact that a day was
late/early/short, never the numbers behind it:
  - late_come/early_out: Expected is read straight from the shift's own
    EmployeeShiftSchedule row for that attendance's day (not grace-
    adjusted -- grace is a separate, already-configured tolerance, not
    folded into what's displayed here); Actual is the real punch time.
  - flexible_shortfall: Expected is total_work_hours_reference, read
    from the Attendance row's own already-snapshotted
    attendance_rule_set_snapshot (no live rule-set lookup needed);
    Actual is the day's worked hours.
"""

from datetime import date, timedelta

from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.filters import LateComeEarlyOutFilter
from attendance.methods.utils import strtime_seconds
from attendance.models import AttendanceLateComeEarlyOut, AttendanceRuleSet
from base.decorators import manager_can_enter
from base.methods import filtersubordinates
from base.models import EmployeeShiftSchedule
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.views import HorillaListView, HorillaNavView, TemplateView

PERM = "attendance.view_attendancelatecomeearlyout"


def _seconds_of(time_obj):
    if time_obj is None:
        return None
    return time_obj.hour * 3600 + time_obj.minute * 60 + time_obj.second


def _minutes_label(delta_seconds):
    sign = "-" if delta_seconds < 0 else ""
    minutes = abs(delta_seconds) // 60
    return f"{sign}{minutes} min"


def _shift_schedule_for(attendance):
    if not attendance.shift_id or not attendance.attendance_day:
        return None
    return EmployeeShiftSchedule.objects.filter(
        shift_id=attendance.shift_id, day=attendance.attendance_day
    ).first()


def expected_display(self):
    attendance = self.attendance_id
    if self.type == "flexible_shortfall":
        resolve = AttendanceRuleSet.resolve_effective_value
        reference = resolve(attendance.attendance_rule_set_snapshot, "total_work_hours_reference")
        return f"{reference}h" if reference not in (None, "") else "-"
    schedule = _shift_schedule_for(attendance)
    if schedule is None:
        return "-"
    expected_time = schedule.start_time if self.type == "late_come" else schedule.end_time
    return expected_time.strftime("%H:%M") if expected_time else "-"


def actual_display(self):
    attendance = self.attendance_id
    if self.type == "flexible_shortfall":
        return attendance.attendance_worked_hour or "-"
    actual_time = (
        attendance.attendance_clock_in
        if self.type == "late_come"
        else attendance.attendance_clock_out
    )
    return actual_time.strftime("%H:%M") if actual_time else "-"


def delta_display(self):
    attendance = self.attendance_id
    if self.type == "flexible_shortfall":
        resolve = AttendanceRuleSet.resolve_effective_value
        reference = resolve(attendance.attendance_rule_set_snapshot, "total_work_hours_reference")
        if reference in (None, "") or not attendance.attendance_worked_hour:
            return "-"
        reference_seconds = int(float(reference) * 3600)
        worked_seconds = strtime_seconds(attendance.attendance_worked_hour)
        return _minutes_label(worked_seconds - reference_seconds)

    schedule = _shift_schedule_for(attendance)
    if schedule is None:
        return "-"
    if self.type == "late_come":
        expected_seconds = _seconds_of(schedule.start_time)
        actual_seconds = _seconds_of(attendance.attendance_clock_in)
    else:
        expected_seconds = _seconds_of(schedule.end_time)
        actual_seconds = _seconds_of(attendance.attendance_clock_out)
    if expected_seconds is None or actual_seconds is None:
        return "-"
    return _minutes_label(actual_seconds - expected_seconds)


AttendanceLateComeEarlyOut.expected_display = property(expected_display)
AttendanceLateComeEarlyOut.actual_display = property(actual_display)
AttendanceLateComeEarlyOut.delta_display = property(delta_display)


def _scoped_queryset(request):
    queryset = AttendanceLateComeEarlyOut.objects.all()
    own = queryset.filter(employee_id__employee_user_id=request.user)
    subordinates = filtersubordinates(request, queryset, PERM)
    return (own | subordinates).distinct()


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class AttendanceIrregularitiesPageView(TemplateView):
    """
    Page shell -- an Instances / By Employee tab toggle, same Alpine.js
    pattern as Create Attendance's entry screen.
    """

    template_name = "cbv/attendance_irregularities/attendance_irregularities.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class AttendanceIrregularitiesInstancesView(HorillaListView):
    """
    Read-only -- no actions, no bulk select, no export, matching the
    PRD's explicit "Read-only for MVP" instruction for this screen
    (unlike the older penalty-oriented Late Arrival & Early Departure
    screen this deliberately doesn't reuse).
    """

    model = AttendanceLateComeEarlyOut
    filter_class = LateComeEarlyOutFilter
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Employee"), "employee_id", "employee_id__get_avatar"),
        (_("Date"), "attendance_id__attendance_date"),
        (_("Type"), "get_type"),
        (_("Expected"), "expected_display"),
        (_("Actual"), "actual_display"),
        (_("Delta"), "delta_display"),
    ]
    default_columns = columns

    def get_queryset(self):
        queryset = super().get_queryset()
        scoped_ids = _scoped_queryset(self.request).values_list("pk", flat=True)
        return queryset.filter(pk__in=scoped_ids)


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class AttendanceIrregularitiesNav(HorillaNavView):
    nav_title = _("Attendance Irregularities")
    search_swap_target = "#listContainer"
    filter_instance = LateComeEarlyOutFilter()
    filter_body_template = "cbv/late_come_and_early_out/late_early_filter.html"
    filter_form_context_name = "form"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("attendance-irregularities-instances")


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class AttendanceIrregularitiesByEmployeeView(TemplateView):
    """
    Employee + count of each type over the selected period -- a plain
    GROUP BY, not a HorillaListView (that framework's columns assume
    one model instance per row; this is an aggregate).
    """

    template_name = "cbv/attendance_irregularities/by_employee.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = date.today()
        from_date = self.request.GET.get("attendance_date__gte") or (
            today.replace(day=1)
        )
        to_date = self.request.GET.get("attendance_date__lte") or today
        if isinstance(from_date, str):
            from_date = date.fromisoformat(from_date)
        if isinstance(to_date, str):
            to_date = date.fromisoformat(to_date)

        queryset = _scoped_queryset(self.request).filter(
            attendance_id__attendance_date__range=(from_date, to_date),
        )
        employee_param = self.request.GET.get("employee_id")
        if employee_param:
            queryset = queryset.filter(employee_id=employee_param)
        department_param = self.request.GET.get("department")
        if department_param:
            queryset = queryset.filter(
                employee_id__employee_work_info__department_id__department__icontains=department_param
            )

        rows = {}
        for record in queryset.select_related("employee_id"):
            row = rows.setdefault(
                record.employee_id_id,
                {"employee": record.employee_id, "late_come": 0, "early_out": 0, "flexible_shortfall": 0},
            )
            row[record.type] += 1

        context["rows"] = sorted(rows.values(), key=lambda r: str(r["employee"]))
        context["from_date"] = from_date
        context["to_date"] = to_date
        return context
