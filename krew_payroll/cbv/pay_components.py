"""
Payroll → Configuration: Earnings, Deductions and the work-site master.
List and nav views on the shared CBV framework; the editor lives in
krew_payroll/views/pay_component_views.py.
"""

from typing import Any

from django.contrib import messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from horilla_views.cbv_methods import hx_request_required, login_required, permission_required
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)
from krew_payroll.filters import PayComponentFilter, WorkSiteFilter
from krew_payroll.forms.pay_component_forms import WorkSiteForm
from krew_payroll.models.pay_components import ComponentType, PayComponent, WorkSite

ROUTES = {"earnings": ComponentType.EARNING, "deductions": ComponentType.DEDUCTION}


# ------------------------------------------------------------------ earnings / deductions


class _PayComponentListView(HorillaListView):
    component_type = None
    model = PayComponent
    filter_class = PayComponentFilter
    show_toggle_form = False
    records_per_page = 50

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        route = "earnings" if self.component_type == ComponentType.EARNING else "deductions"
        self.view_id = f"payComponentList_{route}"
        # Each Configuration tab has its own list container (see config_tab.html).
        self.selected_instances_key_id = f"selectedInstances_{route}"
        self.search_url = reverse("pay-component-list", kwargs={"route": route})
        tax_title = (
            _("Taxable / Tax-Free")
            if self.component_type == ComponentType.EARNING
            else _("Employer Share")
        )
        self.columns = [
            (_("Name"), "name"),
            (_("Code"), "code"),
            (_("Frequency"), "get_frequency_col"),
            (_("Status"), "get_status_col"),
            (tax_title, "get_tax_col"),
            (_("Calculation"), "get_calculation_col"),
            (_("Live Version"), "get_version_col"),
            (_("Order"), "get_order_col"),
        ]
        self.row_attrs = """
            class="cursor-pointer"
            onclick="window.location.href='{get_editor_url}'"
        """
        self.actions = [
            {
                "action": _("Open"),
                "icon": "create-outline",
                "attrs": """
                    class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                    href="{get_editor_url}"
                """,
            }
        ]
        if self.request.user.has_perm("krew_payroll.change_paycomponent"):
            self.actions.append(
                {
                    "action": _("Activate / Deactivate"),
                    "icon": "power-outline",
                    "attrs": """
                        class="oh-btn oh-btn--danger oh-btn--sq-sm"
                        hx-get="{get_active_toggle_url}"
                        hx-target="#genericModalBody"
                        data-toggle="oh-modal-toggle"
                        data-target="#genericModal"
                    """,
                }
            )

    def get_queryset(self, *args, **kwargs):
        queryset = super().get_queryset(*args, **kwargs)
        return queryset.filter(type=self.component_type).prefetch_related(
            "versions", "versions__calculations"
        )


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="krew_payroll.view_paycomponent"), name="dispatch")
class EarningListView(_PayComponentListView):
    component_type = ComponentType.EARNING


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="krew_payroll.view_paycomponent"), name="dispatch")
class DeductionListView(_PayComponentListView):
    component_type = ComponentType.DEDUCTION


class _PayComponentNavView(HorillaNavView):
    component_type = None
    template_name = "generic/inline_nav.html"
    filter_body_template = "payroll/pay_components/filter.html"
    filter_form_context_name = "form"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        route = "earnings" if self.component_type == ComponentType.EARNING else "deductions"
        self.nav_title = _("Earnings") if route == "earnings" else _("Deductions")
        self.search_swap_target = f"#listContainer_{route}"
        self.search_url = reverse("pay-component-list", kwargs={"route": route})
        self.filter_instance = PayComponentFilter()
        if self.request.user.has_perm("krew_payroll.add_paycomponent"):
            self.create_attrs = f"""
                onclick="event.stopPropagation();"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-target="#genericModalBody"
                hx-get="{reverse('pay-component-create', kwargs={'route': route})}"
            """


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="krew_payroll.view_paycomponent"), name="dispatch")
class EarningNavView(_PayComponentNavView):
    component_type = ComponentType.EARNING


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="krew_payroll.view_paycomponent"), name="dispatch")
class DeductionNavView(_PayComponentNavView):
    component_type = ComponentType.DEDUCTION


def pay_component_list_view(request, route, *args, **kwargs):
    view = EarningListView if ROUTES.get(route) == ComponentType.EARNING else DeductionListView
    return view.as_view()(request, *args, **kwargs)


def pay_component_nav_view(request, route, *args, **kwargs):
    view = EarningNavView if ROUTES.get(route) == ComponentType.EARNING else DeductionNavView
    return view.as_view()(request, *args, **kwargs)


# ------------------------------------------------------------------ work sites


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="krew_payroll.view_worksite"), name="dispatch")
class WorkSiteListView(HorillaListView):
    model = WorkSite
    filter_class = WorkSiteFilter
    show_toggle_form = False
    columns = [(_("Site"), "name"), (_("State"), "state")]
    selected_instances_key_id = "selectedInstances_worksite"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.view_id = "work_site_list"
        self.search_url = reverse("work-site-list")
        self.actions = []
        if self.request.user.has_perm("krew_payroll.change_worksite"):
            self.actions.append(
                {
                    "action": _("Edit"),
                    "icon": "create-outline",
                    "attrs": """
                        class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                        hx-get="{get_update_url}"
                        hx-target="#genericModalBody"
                        data-toggle="oh-modal-toggle"
                        data-target="#genericModal"
                    """,
                }
            )
        if self.request.user.has_perm("krew_payroll.delete_worksite"):
            self.actions.append(
                {
                    "action": _("Delete"),
                    "icon": "trash-outline",
                    "attrs": """
                        class="oh-btn oh-btn--danger oh-btn--sq-sm"
                        hx-get="{get_delete_url}"
                        data-toggle="oh-modal-toggle"
                        data-target="#deleteConfirmation"
                        hx-target="#deleteConfirmationBody"
                    """,
                }
            )


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="krew_payroll.view_worksite"), name="dispatch")
class WorkSiteNavView(HorillaNavView):
    template_name = "generic/inline_nav.html"
    nav_title = _("Work Sites")
    search_swap_target = "#listContainer_worksite"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("work-site-list")
        self.filter_instance = WorkSiteFilter()
        if self.request.user.has_perm("krew_payroll.add_worksite"):
            self.create_attrs = f"""
                onclick="event.stopPropagation();"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-target="#genericModalBody"
                hx-get="{reverse('work-site-create-view')}"
            """


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="krew_payroll.add_worksite"), name="dispatch")
class WorkSiteFormView(HorillaFormView):
    model = WorkSite
    form_class = WorkSiteForm
    new_display_title = _("Create Work Site")

    def form_valid(self, form) -> HttpResponse:
        if form.is_valid():
            updating = bool(form.instance.pk)
            form.save()
            messages.success(
                self.request, _("Work site updated.") if updating else _("Work site created.")
            )
            return self.HttpResponse()
        return super().form_valid(form)


# ------------------------------------------------------------------ configuration tabs


@method_decorator(login_required, name="dispatch")
@method_decorator(hx_request_required, name="dispatch")
class ConfigurationListTab(TemplateView):
    """A Configuration tab: an inline nav + list loaded by HTMX."""

    template_name = "payroll/pay_components/config_tab.html"
    nav_url_name = None
    nav_url_kwargs = None
    list_key = None  # unique per tab: the tabs share one page, so ids must differ
    description = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav_url"] = reverse(self.nav_url_name, kwargs=self.nav_url_kwargs or None)
        context["description"] = self.description
        context["list_key"] = self.list_key
        return context


class EarningsTab(ConfigurationListTab):
    nav_url_name = "pay-component-nav"
    nav_url_kwargs = {"route": "earnings"}
    list_key = "earnings"
    description = _(
        "Every pay element your company pays (Basic, HRA, allowances, bonuses). "
        "Open one to edit its rules, dates and versions."
    )


class DeductionsTab(ConfigurationListTab):
    nav_url_name = "pay-component-nav"
    nav_url_kwargs = {"route": "deductions"}
    list_key = "deductions"
    description = _(
        "Everything taken from pay (PF, ESI, Professional Tax, recoveries). "
        "Employer shares are configured on the same deduction."
    )


class WorkerClassesTab(ConfigurationListTab):
    nav_url_name = "workerclass-nav"
    list_key = "workerclass"
    description = _("Worker classes your company uses in eligibility rules.")


class GradesTab(ConfigurationListTab):
    nav_url_name = "grade-nav"
    list_key = "grade"
    description = _("Grades your company uses in eligibility rules.")


class WorkSitesTab(ConfigurationListTab):
    nav_url_name = "work-site-nav"
    list_key = "worksite"
    description = _("Sites inside each state where your workers are based.")
