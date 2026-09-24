"""
Web view for the new correction flow (#8 Regularization) --
RegularizationRequest. Distinct from the older is_validate_request-based
flow in attendance/cbv/attendance_request.py, which stays as-is for
ordinary attendance edits (see RegularizationRequest's docstring in
attendance/models.py).
"""

from typing import Any

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
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


@method_decorator(login_required, name="dispatch")
class RegularizationRequestPageView(TemplateView):
    """
    Page shell.
    """

    template_name = "cbv/regularization_request/regularization_request.html"


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
                hx-post="{reject_url}"
                hx-target="#reloadMessagesButton"
                class="oh-btn oh-btn--danger w-100"
                onclick="event.preventDefault(); return confirm('Reject this request?')"
            """,
        },
    ]


def approve_url(self):
    return reverse("approve-regularization-request", kwargs={"pk": self.pk})


def reject_url(self):
    return reverse("reject-regularization-request", kwargs={"pk": self.pk})


RegularizationRequest.approve_url = property(approve_url)
RegularizationRequest.reject_url = property(reject_url)


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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        employee = self.request.user.employee_get
        self.form.fields["attendance"].queryset = Attendance.objects.filter(
            employee_id=employee
        ).order_by("-attendance_date")
        return context

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
    if not authorized:
        messages.error(
            request, _("You do not have permission to approve this request.")
        )
    elif reg_request.status != RegularizationRequest.STATUS_PENDING:
        messages.error(request, _("This request has already been resolved."))
    else:
        reg_request.approve(approver)
        messages.success(request, _("Regularization request approved."))
    return HttpResponse(
        "<script>$('.reload-record').click(); $('#reloadMessagesButton').click();</script>"
    )


@fbv_login_required
@require_http_methods(["POST"])
def reject_regularization_request(request, pk):
    reg_request = get_object_or_404(RegularizationRequest, pk=pk)
    approver = request.user.employee_get
    authorized = request.user.has_perm(
        "attendance.change_attendance"
    ) or ApprovalDelegate.can_approve(approver, reg_request.employee, target=reg_request)
    if not authorized:
        messages.error(
            request, _("You do not have permission to reject this request.")
        )
    elif reg_request.status != RegularizationRequest.STATUS_PENDING:
        messages.error(request, _("This request has already been resolved."))
    else:
        reg_request.reject(approver)
        messages.success(request, _("Regularization request rejected."))
    return HttpResponse(
        "<script>$('.reload-record').click(); $('#reloadMessagesButton').click();</script>"
    )
