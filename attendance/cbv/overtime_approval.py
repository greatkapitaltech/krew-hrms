"""
attendance/cbv/overtime_approval.py

The manager-initiated Overtime review queue My Krew Attendance's Need
Your Action tab needs -- confirmed nothing like this existed anywhere
before: handle_overtime_conditions() only ever sets
Attendance.attendance_overtime_approve True itself, never flips it via
a manager action, and the one existing overtime-adjacent screen
(HourAccount) operates on AttendanceOverTime, a monthly running total,
not a per-day decision. The only existing path that could change this
flag was RegularizationRequest's own OVERTIME_DENIAL_DISPUTE handling --
but that requires the *employee* to raise a dispute first; there was no
queue a manager could proactively work from.

Attendance.overtime_decision (PENDING/APPROVED/DENIED) is new --
attendance_overtime_approve alone can't distinguish "not yet reviewed"
from "actively denied," both are False. Approving sets both fields;
rejecting only sets overtime_decision=DENIED (attendance_overtime_approve
stays False) -- either way the row drops out of this queue's own
PENDING filter.

Declining requires a mandatory remark, same PRD rule and same UI
pattern (a small modal, not a bare confirm()) as the fix already
applied to Regularization's own reject action.
"""

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from attendance.activity_log import log_attendance_activity
from attendance.models import Attendance, AttendanceActivityLog
from base.decorators import manager_can_enter
from base.methods import filtersubordinates
from horilla.decorators import login_required as fbv_login_required
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.views import HorillaListView, HorillaNavView, TemplateView

PERM = "attendance.change_attendance"


def _pending_overtime_queryset(request):
    queryset = Attendance.objects.filter(
        overtime_second__gt=0, overtime_decision=Attendance.OVERTIME_DECISION_PENDING,
    )
    own = queryset.filter(employee_id__employee_user_id=request.user)
    subordinates = filtersubordinates(request, queryset, PERM)
    return (own | subordinates).distinct()


def approve_url(self):
    return reverse("approve-overtime-decision", kwargs={"pk": self.pk})


def reject_prompt_url(self):
    return reverse("reject-overtime-decision-prompt", kwargs={"pk": self.pk})


Attendance.overtime_approve_url = property(approve_url)
Attendance.overtime_reject_prompt_url = property(reject_prompt_url)


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class OvertimeApprovalPageView(TemplateView):
    template_name = "cbv/overtime_approval/overtime_approval.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class OvertimeApprovalListView(HorillaListView):
    model = Attendance
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Employee"), "employee_id", "employee_id__get_avatar"),
        (_("Date"), "attendance_date"),
        (_("Check-in"), "attendance_clock_in"),
        (_("Check-out"), "attendance_clock_out"),
        (_("Overtime"), "attendance_overtime"),
    ]
    default_columns = columns

    actions = [
        {
            "action": _("Approve"),
            "icon": "checkmark-circle-outline",
            "attrs": """
                href="#"
                hx-post="{overtime_approve_url}"
                hx-target="#reloadMessagesButton"
                class="oh-btn oh-btn--success w-100"
                onclick="event.preventDefault(); return confirm('Approve this overtime?')"
            """,
        },
        {
            "action": _("Reject"),
            "icon": "close-circle-outline",
            "attrs": """
                href="#"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{overtime_reject_prompt_url}"
                hx-target="#genericModalBody"
                class="oh-btn oh-btn--danger w-100"
            """,
        },
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        scoped_ids = _pending_overtime_queryset(self.request).values_list("pk", flat=True)
        return queryset.filter(pk__in=scoped_ids)


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm=PERM), name="dispatch")
class OvertimeApprovalNav(HorillaNavView):
    nav_title = _("Overtime Approval")
    search_swap_target = "#listContainer"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("overtime-approval-list")


def _can_decide(request, attendance):
    if request.user.has_perm(PERM):
        return True
    return filtersubordinates(
        request, Attendance.objects.filter(pk=attendance.pk), PERM
    ).exists()


@fbv_login_required
@require_http_methods(["POST"])
def approve_overtime_decision(request, pk):
    attendance = get_object_or_404(Attendance, pk=pk)
    approver = request.user.employee_get
    if not _can_decide(request, attendance):
        messages.error(request, _("You do not have permission to approve this."))
    elif attendance.overtime_decision != Attendance.OVERTIME_DECISION_PENDING:
        messages.error(request, _("This has already been decided."))
    else:
        attendance.attendance_overtime_approve = True
        attendance.overtime_decision = Attendance.OVERTIME_DECISION_APPROVED
        attendance.save()
        log_attendance_activity(
            actor=approver,
            action_type=AttendanceActivityLog.ACTION_OVERTIME_MANUAL_DECISION,
            affected_employees=attendance.employee_id,
            what_changed=f"Overtime manually approved for {attendance.attendance_date}",
            source="My Krew Attendance",
        )
        messages.success(request, _("Overtime approved."))
    return HttpResponse(
        "<script>$('.reload-record').click(); $('#reloadMessagesButton').click();</script>"
    )


@fbv_login_required
@require_http_methods(["GET"])
def reject_overtime_decision_prompt(request, pk):
    attendance = get_object_or_404(Attendance, pk=pk)
    return render(
        request, "cbv/overtime_approval/reject_prompt.html", {"attendance": attendance},
    )


@fbv_login_required
@require_http_methods(["POST"])
def reject_overtime_decision(request, pk):
    attendance = get_object_or_404(Attendance, pk=pk)
    approver = request.user.employee_get
    resolution_note = (request.POST.get("resolution_note") or "").strip()
    if not _can_decide(request, attendance):
        messages.error(request, _("You do not have permission to reject this."))
    elif attendance.overtime_decision != Attendance.OVERTIME_DECISION_PENDING:
        messages.error(request, _("This has already been decided."))
    elif not resolution_note:
        messages.error(request, _("A remark is required to decline this."))
    else:
        attendance.overtime_decision = Attendance.OVERTIME_DECISION_DENIED
        attendance.save()
        log_attendance_activity(
            actor=approver,
            action_type=AttendanceActivityLog.ACTION_OVERTIME_MANUAL_DECISION,
            affected_employees=attendance.employee_id,
            what_changed=(
                f"Overtime manually denied for {attendance.attendance_date}: {resolution_note}"
            ),
            source="My Krew Attendance",
        )
        messages.success(request, _("Overtime denied."))
    return HttpResponse(
        "<script>$('.reload-record').click(); $('#reloadMessagesButton').click();"
        "$('.oh-modal--show').removeClass('oh-modal--show');</script>"
    )
