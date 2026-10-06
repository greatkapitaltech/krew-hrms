"""
Web view for the new correction flow (#8 Regularization) --
RegularizationRequest. Distinct from the older is_validate_request-based
flow in attendance/cbv/attendance_request.py, which stays as-is for
ordinary attendance edits (see RegularizationRequest's docstring in
attendance/models.py).
"""

from typing import Any

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from attendance.forms import RegularizationRequestForm
from attendance.models import ApprovalDelegate, Attendance, RegularizationRequest
from base.methods import filtersubordinates
from horilla.decorators import login_required as fbv_login_required
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.views import HorillaFormView, HorillaListView, HorillaNavView, TemplateView


def _open_regularizable_reasons(attendance):
    """
    Which of RegularizationRequest's reasons are both (a) actually
    flagged on this attendance day and (b) don't already have a PENDING
    request raised against that specific reason -- shared by
    RegularizableAttendanceListView (which day/reason combos to
    surface) and RegularizationRequestFormView (which reason_code
    choices to offer when opened from one of those rows).

    Only two of RegularizationRequest's four reasons have anything to
    check here: TIME_CORRECTION is general-purpose, not tied to a flag
    at all (stays reachable only via the existing blank "Raise a
    Request" entry point); AUTO_CLOSE_DISPUTE has no backing flag on
    Attendance yet (see RegularizationRequestForm's own docstring), so
    there's nothing to detect. attendance_validated == False is
    deliberately not checked directly -- attendance_validate() now
    derives it entirely from the overtime buffer decision (see that
    function's docstring), so it's the exact same condition as the
    overtime check below, not a separate one.
    """
    reasons = []
    if attendance.geo_fence_violation or attendance.geo_fence_unverified:
        reasons.append(RegularizationRequest.REASON_GEO_VIOLATION_DISPUTE)
    if (attendance.overtime_second or 0) > 0 and not attendance.attendance_overtime_approve:
        reasons.append(RegularizationRequest.REASON_OVERTIME_DENIAL_DISPUTE)
    if not reasons:
        return []
    pending = set(
        RegularizationRequest.objects.filter(
            attendance=attendance,
            status=RegularizationRequest.STATUS_PENDING,
            reason_code__in=reasons,
        ).values_list("reason_code", flat=True)
    )
    return [reason for reason in reasons if reason not in pending]


@method_decorator(login_required, name="dispatch")
class RegularizationRequestPageView(TemplateView):
    """
    Page shell.
    """

    template_name = "cbv/regularization_request/regularization_request.html"


@method_decorator(login_required, name="dispatch")
class RegularizableAttendanceListView(HorillaListView):
    """
    Attendance days that are actually flagged for something a
    Regularization request can address right now -- see
    _open_regularizable_reasons() for exactly which reasons and why
    only two of the four qualify. Sits alongside the request-history
    list/generic "Raise a Request" entry point below, doesn't replace
    it -- this is the fast path for a day that's visibly flagged; the
    blank form is still there for anything else (a plain time
    correction, or disputing something this list can't detect yet).
    """

    model = Attendance
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Employee"), "employee_id", "employee_id__get_avatar"),
        (_("Date"), "attendance_date"),
        (_("Flagged For"), "flagged_reasons_display"),
        (_("Overtime"), "attendance_overtime"),
    ]
    default_columns = columns

    header_attrs = {
        # A day can have both reasons at once -- header_attrs only
        # widens the <th> (cells aren't independently stylable per
        # column), but a wider header still widens the column overall;
        # flagged_reasons_display() also uses shorter labels than
        # RegularizationRequest.REASON_CHOICES' own for the same reason.
        "flagged_reasons_display": """ style="width:180px !important" """,
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        own = queryset.filter(employee_id__employee_user_id=self.request.user)
        subordinates = filtersubordinates(
            request=self.request, perm="attendance.view_attendance", queryset=queryset,
        )
        visible = (own | subordinates).distinct()
        candidates = visible.filter(
            Q(geo_fence_violation=True)
            | Q(geo_fence_unverified=True)
            | Q(attendance_validated=False, overtime_second__gt=0)
        )
        eligible_ids = [
            attendance.pk
            for attendance in candidates
            if _open_regularizable_reasons(attendance)
        ]
        return queryset.filter(pk__in=eligible_ids).order_by("-attendance_date")

    actions = [
        {
            "action": _("Raise Request"),
            "icon": "flag-outline",
            "attrs": """
                href="#"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{raise_regularization_url}"
                hx-target="#genericModalBody"
                class="oh-btn oh-btn--secondary w-100"
            """,
        },
    ]


def flagged_reasons_display(self):
    # Shorter than RegularizationRequest.REASON_CHOICES' own labels
    # ("Geo-location Dispute", "Overtime Decision Dispute") on purpose --
    # this column has to fit both at once on a day flagged for both, and
    # list-table cells truncate with an ellipsis past a fixed width with
    # no per-column override for that (only the header, not the cells,
    # is stylable via header_attrs).
    short_labels = {
        RegularizationRequest.REASON_GEO_VIOLATION_DISPUTE: _("Geo Violation"),
        RegularizationRequest.REASON_OVERTIME_DENIAL_DISPUTE: _("Overtime"),
    }
    return ", ".join(
        str(short_labels[reason]) for reason in _open_regularizable_reasons(self)
    )


def raise_regularization_url(self):
    return f"{reverse('regularization-request-form')}?attendance={self.pk}"


Attendance.flagged_reasons_display = property(flagged_reasons_display)
Attendance.raise_regularization_url = property(raise_regularization_url)


@method_decorator(login_required, name="dispatch")
class RegularizationRequestListView(HorillaListView):
    """
    An employee's own requests, plus anything they can approve (direct
    reports and active delegations).
    """

    model = RegularizationRequest
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Employee"), "employee", "employee__get_avatar"),
        (_("Attendance Date"), "attendance__attendance_date"),
        (_("Reason"), "get_reason_code_display"),
        (_("Status"), "status"),
        (_("Overtime Decision"), "overtime_decision"),
        (_("Raised On"), "created_at"),
    ]

    default_columns = columns

    row_status_class = "regularization-{status}"

    def get_queryset(self):
        queryset = super().get_queryset()
        own = queryset.filter(employee__employee_user_id=self.request.user)
        subordinates = filtersubordinates(
            request=self.request,
            perm="attendance.view_regularizationrequest",
            queryset=queryset,
            field="employee",
        )
        return (own | subordinates).distinct()

    # action.attrs is substituted via a plain str.format()-style filter
    # (horilla_views/templatetags/generic_template_filters.py's `format`),
    # not the Django template engine -- {% trans %} tags here would not
    # be interpreted, so these stay in plain English like the rest of
    # the codebase's own actions=[...] lists (e.g. AttendanceRequestNav).
    actions = [
        {
            "action": _("Approve"),
            "icon": "checkmark-circle-outline",
            "accessibility": "attendance.cbv.regularization_request.can_resolve_accessibility",
            "attrs": """
                href="#"
                hx-post="{approve_url}"
                hx-target="#reloadMessagesButton"
                class="oh-btn oh-btn--success w-100"
                onclick="event.preventDefault(); return confirm('Approve this request?')"
            """,
        },
        {
            "action": _("Reject"),
            "icon": "close-circle-outline",
            "accessibility": "attendance.cbv.regularization_request.can_resolve_accessibility",
            "attrs": """
                href="#"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{reject_prompt_url}"
                hx-target="#genericModalBody"
                class="oh-btn oh-btn--danger w-100"
            """,
        },
    ]


def approve_url(self):
    return reverse("approve-regularization-request", kwargs={"pk": self.pk})


def reject_url(self):
    return reverse("reject-regularization-request", kwargs={"pk": self.pk})


def reject_prompt_url(self):
    return reverse("reject-regularization-request-prompt", kwargs={"pk": self.pk})


RegularizationRequest.approve_url = property(approve_url)
RegularizationRequest.reject_url = property(reject_url)
RegularizationRequest.reject_prompt_url = property(reject_prompt_url)


def can_resolve_accessibility(request, instance=None, *args, **kwargs):
    """
    Row-action visibility for Approve/Reject: only while a request is
    still PENDING, and only for someone ApprovalDelegate.can_approve()
    (or attendance.change_attendance) allows to act on it.
    """
    if instance is None or instance.status != RegularizationRequest.STATUS_PENDING:
        return False
    approver = getattr(request.user, "employee_get", None)
    if approver is None:
        return False
    return request.user.has_perm(
        "attendance.change_attendance"
    ) or ApprovalDelegate.can_approve(approver, instance.employee, target=instance)


@method_decorator(login_required, name="dispatch")
class RegularizationRequestNav(HorillaNavView):
    """
    Nav bar with the "raise a request" create button.
    """

    nav_title = _("Regularization Requests")
    search_swap_target = "#listContainer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("regularization-request-list")
        self.create_attrs = f"""
            data-toggle="oh-modal-toggle"
            data-target="#genericModal"
            hx-get="{reverse('regularization-request-form')}"
            hx-target="#genericModalBody"
        """


@method_decorator(login_required, name="dispatch")
class RegularizationRequestFormView(HorillaFormView):
    """
    Create form -- the employee raising the request is always the
    logged-in user, never chosen from a dropdown (unlike the old
    correction flow's manager-facing "raise on behalf of" form).
    """

    form_class = RegularizationRequestForm
    model = RegularizationRequest
    new_display_title = _("Raise a Regularization Request")

    def init_form(self, *args, data=None, files=None, instance=None, **kwargs):
        # Overridden (not scoped from get_context_data, which doesn't run
        # before is_valid() on a successful POST -- see
        # AttendanceRuleSetFormView's own init_form() for the same fix)
        # so both the employee-scoped queryset and, when arriving from
        # RegularizableAttendanceListView's "Raise Request" row action
        # (?attendance=<pk> in the query string -- carried onto the POST
        # too, since horilla_form.html's hx-post target includes
        # request.GET.urlencode), the attendance pre-fill/lock and
        # reason_code narrowing actually apply during validation, not
        # just on render.
        form = self.form_class(data, files, instance=instance, initial=self.get_initial())
        employee = self.request.user.employee_get
        form.fields["attendance"].queryset = Attendance.objects.filter(
            employee_id=employee
        ).order_by("-attendance_date")

        prefill_id = self.request.GET.get("attendance")
        if prefill_id:
            attendance = form.fields["attendance"].queryset.filter(pk=prefill_id).first()
            if attendance is not None:
                form.fields["attendance"].initial = attendance.pk
                form.fields["attendance"].disabled = True
                eligible_reasons = _open_regularizable_reasons(attendance)
                if eligible_reasons:
                    form.fields["reason_code"].choices = [
                        choice
                        for choice in form.fields["reason_code"].choices
                        if choice[0] in eligible_reasons
                    ]
        return form

    def form_valid(self, form: RegularizationRequestForm) -> HttpResponse:
        employee = self.request.user.employee_get
        allowed, reason = RegularizationRequest.can_raise(employee)
        if not allowed:
            messages.error(self.request, str(reason))
            return self.HttpResponse()
        form.instance.employee = employee
        form.save()
        messages.success(self.request, _("Regularization request raised."))
        return self.HttpResponse()


@fbv_login_required
@require_http_methods(["POST"])
def approve_regularization_request(request, pk):
    """
    Mirrors approve_validate_attendance_request's HTMX-refresh pattern
    for the old flow, but for RegularizationRequest.
    """
    reg_request = get_object_or_404(RegularizationRequest, pk=pk)
    approver = request.user.employee_get
    authorized = request.user.has_perm(
        "attendance.change_attendance"
    ) or ApprovalDelegate.can_approve(approver, reg_request.employee, target=reg_request)
    from attendance.cbv.payroll_readiness import is_date_locked

    if not authorized:
        messages.error(
            request, _("You do not have permission to approve this request.")
        )
    elif reg_request.status != RegularizationRequest.STATUS_PENDING:
        messages.error(request, _("This request has already been resolved."))
    elif is_date_locked(reg_request.employee, reg_request.attendance.attendance_date):
        messages.error(
            request,
            _("Payroll for this date has already been locked -- it can no longer be changed."),
        )
    else:
        reg_request.approve(approver)
        messages.success(request, _("Regularization request approved."))
    return HttpResponse(
        "<script>$('.reload-record').click(); $('#reloadMessagesButton').click();</script>"
    )


@fbv_login_required
@require_http_methods(["GET"])
def reject_regularization_prompt(request, pk):
    """
    GET-only: the remark modal itself, opened by the Reject row action
    instead of the old plain confirm()+hx-post -- the PRD requires a
    mandatory remark on a decline, which a bare confirm() dialog can
    never collect.
    """
    reg_request = get_object_or_404(RegularizationRequest, pk=pk)
    return render(
        request,
        "cbv/regularization_request/reject_prompt.html",
        {"reg_request": reg_request},
    )


@fbv_login_required
@require_http_methods(["POST"])
def reject_regularization_request(request, pk):
    reg_request = get_object_or_404(RegularizationRequest, pk=pk)
    approver = request.user.employee_get
    authorized = request.user.has_perm(
        "attendance.change_attendance"
    ) or ApprovalDelegate.can_approve(approver, reg_request.employee, target=reg_request)
    resolution_note = (request.POST.get("resolution_note") or "").strip()
    from attendance.cbv.payroll_readiness import is_date_locked

    if not authorized:
        messages.error(
            request, _("You do not have permission to reject this request.")
        )
    elif reg_request.status != RegularizationRequest.STATUS_PENDING:
        messages.error(request, _("This request has already been resolved."))
    elif not resolution_note:
        messages.error(request, _("A remark is required to decline this request."))
    elif is_date_locked(reg_request.employee, reg_request.attendance.attendance_date):
        messages.error(
            request,
            _("Payroll for this date has already been locked -- it can no longer be changed."),
        )
    else:
        reg_request.reject(approver, resolution_note=resolution_note)
        messages.success(request, _("Regularization request rejected."))
    return HttpResponse(
        "<script>$('.reload-record').click(); $('#reloadMessagesButton').click();"
        "$('.oh-modal--show').removeClass('oh-modal--show');</script>"
    )
