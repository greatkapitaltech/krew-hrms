"""
attendance/cbv/approval_delegate.py

Settings screen for ApprovalDelegate -- a manager handing off their
Validation/Overtime/Regularization approval authority to someone else,
either for a date range or one specific request (see ApprovalDelegate's
own docstring in attendance/models.py, and the "Delegation" section of
the attendance plan doc). Self-service, not an admin screen: every
employee manages their own delegations here, scoped to `delegator ==
request.user.employee_get` -- there's no permission gate on *using* this
screen, only on who's eligible to be picked as a delegate
(attendance.can_be_delegate, enforced by the model's own clean() and
pre-filtered in ApprovalDelegateForm).
"""

from typing import Any

from django.contrib import messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.forms import ApprovalDelegateForm
from attendance.models import ApprovalDelegate
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)


def _current_employee(request):
    return request.user.employee_get


@method_decorator(login_required, name="dispatch")
class ApprovalDelegatePageView(TemplateView):
    """
    Page shell.
    """

    template_name = "cbv/approval_delegate/approval_delegate.html"


def scope_display(self):
    if self.object_id:
        target = self.target
        if target is None:
            return _("One request (deleted)")
        # Always a RegularizationRequest today -- the only target type
        # this screen's form can create (see ApprovalDelegateForm's own
        # docstring) -- but falls back to a plain str() for anything
        # else reachable only via direct ORM/admin use.
        attendance_date = getattr(getattr(target, "attendance", None), "attendance_date", None)
        if attendance_date is not None:
            label = f"{target.employee} – {attendance_date}"
        else:
            label = str(target)
        return _("One request: %(target)s") % {"target": label}
    return _("%(start)s – %(end)s") % {"start": self.start_date, "end": self.end_date}


ApprovalDelegate.scope_display = property(scope_display)


def edit_url(self):
    return reverse("approval-delegate-update", kwargs={"pk": self.pk})


ApprovalDelegate.edit_url = property(edit_url)


@method_decorator(login_required, name="dispatch")
class ApprovalDelegateGivenListView(HorillaListView):
    """
    Delegations I've given -- the only ones I can edit/deactivate, since
    this is self-service (see module docstring).
    """

    model = ApprovalDelegate
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Delegate"), "delegate", "delegate__get_avatar"),
        (_("Scope"), "scope_display"),
        (_("Active"), "is_active"),
    ]
    default_columns = columns

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(delegator=_current_employee(self.request)).order_by(
            "-created_at"
        )

    actions = [
        {
            "action": _("Edit"),
            "icon": "create-outline",
            "attrs": """
                href="#"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{edit_url}"
                hx-target="#genericModalBody"
                class="oh-btn oh-btn--light-bkg w-100"
            """,
        },
    ]


@method_decorator(login_required, name="dispatch")
class ApprovalDelegateReceivedListView(HorillaListView):
    """
    Delegations given to me -- read-only visibility into what I'm
    currently authorized to approve on someone else's behalf. No actions:
    only the delegator manages their own delegation.
    """

    model = ApprovalDelegate
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Delegator"), "delegator", "delegator__get_avatar"),
        (_("Scope"), "scope_display"),
    ]
    default_columns = columns

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(
            delegate=_current_employee(self.request), is_active=True
        ).order_by("-created_at")


@method_decorator(login_required, name="dispatch")
class ApprovalDelegateNav(HorillaNavView):
    """
    Nav bar for the "given" list -- the only one with a create button.
    """

    nav_title = _("Delegations I've Given")
    search_swap_target = "#givenListContainer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("approval-delegate-given-list")
        self.create_attrs = f"""
            data-toggle="oh-modal-toggle"
            data-target="#genericModal"
            hx-get="{reverse('approval-delegate-create')}"
            hx-target="#genericModalBody"
        """


@method_decorator(login_required, name="dispatch")
class ApprovalDelegateFormView(HorillaFormView):
    """
    Create-or-edit form, same class for both (pk absent vs present),
    matching the AttendanceRuleSetFormView/GeoFencingRuleFormView
    precedent. Direct save -- no PendingConfigChange delay, delegation
    is meant to take effect immediately (e.g. someone just went on leave
    today).
    """

    form_class = ApprovalDelegateForm
    model = ApprovalDelegate
    new_display_title = _("Delegate My Approvals")
    template_name = "cbv/approval_delegate/approval_delegate_form.html"

    def init_form(self, *args, data=None, files=None, instance=None, **kwargs):
        # delegator has to be on the instance BEFORE is_valid() runs its
        # clean() -- get_context_data only runs on GET / a failed POST,
        # too late to gate a successful one. Same fix already applied to
        # AttendanceRuleSetFormView/RegularizationRequestFormView.
        delegator = _current_employee(self.request)
        form = self.form_class(
            data, files, instance=instance, initial=self.get_initial(), delegator=delegator,
        )
        if instance is not None and instance.pk and instance.delegator_id != delegator.pk:
            # Never reachable from this screen's own list (which only
            # ever links to the current user's own rows), but a crafted
            # GET/POST to another employee's delegation pk shouldn't be
            # editable -- fail closed rather than trust the URL alone.
            form.fields["delegate"].queryset = form.fields["delegate"].queryset.none()
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.form.instance.pk:
            self.display_title = _("Edit Delegation")
        return context

    def form_valid(self, form: ApprovalDelegateForm) -> HttpResponse:
        delegator = _current_employee(self.request)
        if form.instance.pk and form.instance.delegator_id != delegator.pk:
            messages.error(self.request, _("You can only edit your own delegations."))
            return self.HttpResponse()
        form.save()
        messages.success(self.request, _("Delegation saved."))
        return self.HttpResponse()
