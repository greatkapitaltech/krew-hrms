"""
attendance/cbv/activity_log.py

Read-only screen for AttendanceActivityLog (see that model's own
docstring in attendance/models.py for why this is a dedicated model, not
django-auditlog/horilla_audit-based). Gated behind a dedicated
permission (attendance.view_attendanceactivitylog, Django's normal
auto-generated model permission) -- nobody creates/edits rows through
this screen, entries are only ever written by
log_attendance_activity() at the point of action.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.models import AttendanceActivityLog
from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import HorillaListView, HorillaNavView, TemplateView

ARCHIVE_WINDOW_DAYS = 90


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.view_attendanceactivitylog"), name="dispatch"
)
class AttendanceActivityLogPageView(TemplateView):
    """
    Page shell.
    """

    template_name = "cbv/activity_log/activity_log.html"


def affected_employee_display(self):
    count = self.affected_employee_count
    if count == 0:
        return "-"
    if count == 1:
        employee = self.affected_employees().first()
        return str(employee) if employee else "-"
    names = ", ".join(str(e) for e in self.affected_employees()[:3])
    if count > 3:
        return f"{names}, +{count - 3} more"
    return names


AttendanceActivityLog.affected_employee_display = property(affected_employee_display)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.view_attendanceactivitylog"), name="dispatch"
)
class AttendanceActivityLogListView(HorillaListView):
    """
    Default window is the last 90 days -- matches the PRD's "archived
    entries drop out of the default search/filter window but stay
    exportable on request" rule without needing a separate is_archived
    flag or scheduled job: ?include_archived=1 just removes the date
    filter, nothing is ever actually deleted or flagged.
    """

    model = AttendanceActivityLog
    bulk_select_option = False
    quick_export = True

    columns = [
        (_("Timestamp"), "timestamp"),
        (_("Actor"), "actor_display"),
        (_("Action Type"), "get_action_type_display"),
        (_("Affected Employee(s)"), "affected_employee_display"),
        (_("What Changed"), "what_changed"),
        (_("Source"), "source"),
    ]
    default_columns = columns

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.GET.get("include_archived"):
            return queryset
        cutoff = timezone.now() - timedelta(days=ARCHIVE_WINDOW_DAYS)
        return queryset.filter(timestamp__gte=cutoff)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.view_attendanceactivitylog"), name="dispatch"
)
class AttendanceActivityLogNav(HorillaNavView):
    """
    Search bar only -- no create button (create_attrs left unset, which
    horilla_nav.html renders nothing for), since entries are never
    created through this screen.
    """

    nav_title = _("Attendance Activity Log")
    search_swap_target = "#listContainer"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("attendance-activity-log-list")
