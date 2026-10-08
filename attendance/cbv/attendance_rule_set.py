"""
attendance/cbv/attendance_rule_set.py

Settings screen for AttendanceRuleSet -- the combined, three-tier
(Company Default / Employee-Type Override / Department Override) record
covering #1 Attendance Type, Validation Threshold, the Overtime cluster,
and Regularization's enable/cap (see AttendanceRuleSet's own docstring in
attendance/models.py, and Part 1 of the attendance plan doc).

Every save here goes through PendingConfigChange.schedule() -- never a
direct field edit on the row -- so "changes apply from the 1st of next
month" is the one mechanism used everywhere, including for a brand-new
override's very first activation (see AttendanceRuleSetFormView.
form_valid below, and AttendanceRuleSet.is_active's field comment for why
a new row starts inactive rather than skipping the delay).
"""

from typing import Any

from django.contrib import messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.forms import ATTENDANCE_RULE_SET_EDITABLE_FIELDS, AttendanceRuleSetForm
from attendance.models import AttendanceRuleSet, PendingConfigChange
from base.config_tiers import TIER_COMPANY, TIER_DEPARTMENT, TIER_EMPLOYEE_TYPE
from base.models import Company
from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)


def _current_company(request):
    """
    The one company this screen configures at a time -- AttendanceRuleSet.
    company is a required FK (unlike, say, TrackLateComeEarlyOut's
    nullable one), so there's no meaningful "all companies" row here; a
    superuser with "all companies" selected sees the picked-a-company
    prompt instead (see AttendanceRuleSetPageView).
    """
    selected = request.session.get("selected_company")
    if not selected or selected == "all":
        return None
    return Company.objects.filter(pk=selected).first()


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.view_attendanceruleset"), name="dispatch"
)
class AttendanceRuleSetPageView(TemplateView):
    """
    Page shell.
    """

    template_name = "cbv/attendance_rule_set/attendance_rule_set.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_company"] = _current_company(self.request)
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.view_attendanceruleset"), name="dispatch"
)
class AttendanceRuleSetListView(HorillaListView):
    """
    Every tier's row for the current company -- Company Default plus
    whatever Employee-Type/Department overrides have been created.
    """

    model = AttendanceRuleSet
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Tier"), "get_tier_display"),
        (_("Scope"), "scope_display"),
        (_("Attendance Type"), "get_mode_display"),
        (_("Overtime Tracked"), "track_overtime"),
        (_("Regularization"), "regularization_enabled"),
        (_("Active"), "is_active"),
        (_("Version"), "version"),
        (_("Pending Change"), "pending_change_display"),
    ]
    default_columns = columns

    def get_queryset(self):
        queryset = super().get_queryset()
        company = _current_company(self.request)
        if company is None:
            return queryset.none()
        return queryset.filter(company=company).order_by(
            "tier", "employee_type_category", "department__department"
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


def scope_display(self):
    if self.tier == TIER_COMPANY:
        return _("All employees (default)")
    if self.tier == TIER_EMPLOYEE_TYPE:
        return self.get_employee_type_category_display()
    if self.tier == TIER_DEPARTMENT:
        return str(self.department) if self.department_id else "-"
    return "-"


def pending_change_display(self):
    pending = self.get_pending_change()
    if pending is None:
        return "-"
    return _("Scheduled for %(date)s") % {"date": pending.effective_date}


def edit_url(self):
    return reverse("attendance-rule-set-update", kwargs={"pk": self.pk})


AttendanceRuleSet.scope_display = property(scope_display)
AttendanceRuleSet.pending_change_display = property(pending_change_display)
AttendanceRuleSet.edit_url = property(edit_url)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.view_attendanceruleset"), name="dispatch"
)
class AttendanceRuleSetNav(HorillaNavView):
    """
    Nav bar with the single "Create" button -- tier/scope are chosen
    inside the form itself (see AttendanceRuleSetForm), not via separate
    entry points, since HorillaNavView's create button only ever renders
    one.
    """

    nav_title = _("Attendance Rule Sets")
    search_swap_target = "#listContainer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("attendance-rule-set-list")
        self.create_attrs = f"""
            data-toggle="oh-modal-toggle"
            data-target="#genericModal"
            hx-get="{reverse('attendance-rule-set-create')}"
            hx-target="#genericModalBody"
        """


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="attendance.change_attendanceruleset"), name="dispatch"
)
class AttendanceRuleSetFormView(HorillaFormView):
    """
    Create-or-edit form, same class for both (pk absent vs present),
    matching the grace_time.py precedent (`-create/` and `-update/<pk>/`
    routed at the same view).
    """

    form_class = AttendanceRuleSetForm
    model = AttendanceRuleSet
    new_display_title = _("Add Attendance Rule Set Override")
    template_name = "cbv/attendance_rule_set/attendance_rule_set_form.html"

    def init_form(self, *args, data=None, files=None, instance=None, **kwargs):
        # Overridden (rather than scoping fields from get_context_data,
        # the RegularizationRequestFormView precedent) because `company`
        # has to be on the instance BEFORE is_valid() runs its clean() --
        # get_context_data only runs on GET / a failed POST, too late to
        # gate a successful one. See AttendanceRuleSetForm.__init__.
        company = _current_company(self.request)
        return self.form_class(
            data,
            files,
            instance=instance,
            initial=self.get_initial(),
            company=company,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.form.instance.pk:
            self.display_title = _("Edit Attendance Rule Set Override")
        return context

    def form_valid(self, form: AttendanceRuleSetForm) -> HttpResponse:
        company = _current_company(self.request)
        if company is None:
            messages.error(
                self.request, _("Select a single company before configuring this.")
            )
            return self.HttpResponse()

        changes = {
            field: form.cleaned_data[field]
            for field in ATTENDANCE_RULE_SET_EDITABLE_FIELDS
        }
        requested_by = self.request.user

        if form.instance.pk is None:
            tier = form.cleaned_data["tier"]
            employee_type_category = form.cleaned_data.get("employee_type_category")
            department = form.cleaned_data.get("department")

            # Only the field(s) that actually identify this tier's scope
            # matter for the duplicate check -- company alone for Company
            # Default, +employee_type_category for Employee-Type, +department
            # for Department (mirrors the model's own UniqueConstraints,
            # which the form can't rely on Django to validate automatically
            # since `company` is never a rendered field -- see
            # AttendanceRuleSetForm's docstring).
            if tier == TIER_COMPANY:
                duplicate = AttendanceRuleSet.objects.filter(
                    company=company, tier=TIER_COMPANY
                )
            elif tier == TIER_EMPLOYEE_TYPE:
                duplicate = AttendanceRuleSet.objects.filter(
                    company=company,
                    tier=TIER_EMPLOYEE_TYPE,
                    employee_type_category=employee_type_category,
                )
            else:
                duplicate = AttendanceRuleSet.objects.filter(
                    company=company, tier=TIER_DEPARTMENT, department=department
                )
            if duplicate.exists():
                messages.error(
                    self.request,
                    _(
                        "An override for this scope already exists -- edit "
                        "it instead of creating a second one."
                    ),
                )
                return self.HttpResponse()

            # A brand-new override starts inactive and blank; its first
            # real values only take effect on the scheduled date, exactly
            # like an edit to an existing row -- one code path for both
            # (see PendingConfigChange.apply(), which flips is_active
            # True as part of `changes` here).
            rule_set = AttendanceRuleSet(
                tier=tier,
                company=company,
                employee_type_category=employee_type_category,
                department=department,
                is_active=False,
            )
            rule_set.save()
            changes["is_active"] = True
            pending = PendingConfigChange.schedule(
                rule_set, changes=changes, requested_by=requested_by
            )
            messages.success(
                self.request,
                _("Override created -- takes effect %(date)s.")
                % {"date": pending.effective_date},
            )
            return self.HttpResponse()

        if not form.instance.is_active:
            # This row was created but never actually went live yet --
            # schedule() cancels its still-pending first-activation change
            # (schedule()'s own "never stacked" rule) before creating this
            # one, so without carrying is_active forward here, editing a
            # not-yet-active override before its first apply() would leave
            # it permanently inactive: the replacement change would carry
            # the edited rule values but drop the activation itself.
            changes["is_active"] = True
        pending = PendingConfigChange.schedule(
            form.instance, changes=changes, requested_by=requested_by
        )
        messages.success(
            self.request,
            _("Change scheduled -- takes effect %(date)s.")
            % {"date": pending.effective_date},
        )
        return self.HttpResponse()
