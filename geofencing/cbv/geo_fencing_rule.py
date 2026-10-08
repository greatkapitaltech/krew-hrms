"""
geofencing/cbv/geo_fencing_rule.py

Settings screen exposing GeoFencing's tiered shape -- the model has
always supported three tiers (Company Default / Employee-Type Exemption
/ Department Override, see GeoFencing's own docstring, base/
config_tiers.py), but the only UI that ever existed for it
(geo_location_config(), embedded in the "Attendance Rules" settings
page's Geofencing accordion) hardcodes tier=COMPANY and always edits the
single Company Default row.

This is the single screen for everything that form can't reach --
deliberately scoped to two tiers, Company Default and Department
Override, by explicit request; Employee-Type Exemption stays on the
model (still reachable through the ORM/API) but isn't exposed by either
screen. geo_location_config() itself is left running exactly as it is
(same accordion, same URL) as the quickest way to edit just the Company
Default boundary -- this is additive, not a replacement -- but no longer
exposes tier/employee_type_category/department (see
GeoFencingSetupForm's own docstring for why).

Unlike AttendanceRuleSet's settings screen, saves here are direct and
immediate: GeoFencing has never gone through PendingConfigChange, and
this screen doesn't introduce that now -- see GeoFencingRuleForm's own
docstring.
"""

from typing import Any

from django.contrib import messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from base.config_tiers import TIER_COMPANY, TIER_DEPARTMENT
from base.models import Company
from geofencing.forms import GeoFencingRuleForm
from geofencing.models import GeoFencing
from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)


def _current_company(request):
    selected = request.session.get("selected_company")
    if not selected or selected == "all":
        return None
    return Company.objects.filter(pk=selected).first()


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="geofencing.view_geofencing"), name="dispatch"
)
class GeoFencingRulePageView(TemplateView):
    template_name = "cbv/geo_fencing_rule/geo_fencing_rule.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_company"] = _current_company(self.request)
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="geofencing.view_geofencing"), name="dispatch"
)
class GeoFencingRuleListView(HorillaListView):
    model = GeoFencing
    bulk_select_option = False
    quick_export = False

    columns = [
        (_("Tier"), "get_tier_display"),
        (_("Scope"), "scope_display"),
        (_("Boundary"), "boundary_display"),
        (_("Enforcement"), "get_enforcement_mode_display"),
        (_("Started"), "start"),
        (_("Active"), "is_active"),
    ]
    default_columns = columns

    def get_queryset(self):
        queryset = super().get_queryset()
        company = _current_company(self.request)
        if company is None:
            return queryset.none()
        # Company/Department only -- see module docstring. Any pre-
        # existing Employee-Type row (created before this screen scoped
        # itself down, or via the ORM/API directly) still exists and is
        # still enforced, just not listed/editable from here.
        return queryset.filter(
            company=company, tier__in=[TIER_COMPANY, TIER_DEPARTMENT]
        ).order_by("tier", "department__department")

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
    if self.tier == TIER_DEPARTMENT:
        return str(self.department) if self.department_id else "-"
    return "-"


def boundary_display(self):
    if self.is_exemption():
        return _("Exempt (no boundary)")
    if self.latitude is None or self.longitude is None:
        return "-"
    radius = self.radius_in_meters if self.radius_in_meters is not None else "?"
    return f"{self.latitude:.5f}, {self.longitude:.5f} (±{radius}m)"


def edit_url(self):
    return reverse("geo-fencing-rule-update", kwargs={"pk": self.pk})


GeoFencing.scope_display = property(scope_display)
GeoFencing.boundary_display = property(boundary_display)
GeoFencing.edit_url = property(edit_url)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="geofencing.view_geofencing"), name="dispatch"
)
class GeoFencingRuleNav(HorillaNavView):
    nav_title = _("Geo-Mark Rules")
    search_swap_target = "#geoFencingRuleListContainer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("geo-fencing-rule-list")
        if self.request.user.has_perm("geofencing.add_geofencing"):
            self.create_attrs = f"""
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{reverse('geo-fencing-rule-create')}"
                hx-target="#genericModalBody"
            """


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="geofencing.add_geofencing"), name="dispatch"
)
class GeoFencingRuleFormView(HorillaFormView):
    form_class = GeoFencingRuleForm
    model = GeoFencing
    new_display_title = _("Add Geo-Mark Rule")
    template_name = "cbv/geo_fencing_rule/geo_fencing_rule_form.html"

    def init_form(self, *args, data=None, files=None, instance=None, **kwargs):
        company = _current_company(self.request)
        return self.form_class(
            data, files, instance=instance, initial=self.get_initial(),
            company=company,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.form.instance.pk:
            self.display_title = _("Edit Geo-Mark Rule")
        return context

    def form_valid(self, form: GeoFencingRuleForm) -> HttpResponse:
        company = _current_company(self.request)
        if company is None:
            messages.error(
                self.request, _("Select a single company before configuring this.")
            )
            return self.HttpResponse()

        if form.instance.pk is None:
            tier = form.cleaned_data["tier"]
            department = form.cleaned_data.get("department")

            if tier == TIER_COMPANY:
                duplicate = GeoFencing.objects.filter(company=company, tier=TIER_COMPANY)
            else:
                duplicate = GeoFencing.objects.filter(
                    company=company, tier=TIER_DEPARTMENT, department=department
                )
            if duplicate.exists():
                messages.error(
                    self.request,
                    _(
                        "A rule for this scope already exists -- edit it "
                        "instead of creating a second one."
                    ),
                )
                return self.HttpResponse()

            rule = GeoFencing(
                tier=tier,
                company=company,
                department=department,
                latitude=form.cleaned_data.get("latitude"),
                longitude=form.cleaned_data.get("longitude"),
                radius_in_meters=form.cleaned_data.get("radius_in_meters"),
                start=form.cleaned_data.get("start") or False,
                enforcement_mode=form.cleaned_data.get("enforcement_mode"),
            )
            rule.save()
            messages.success(self.request, _("Geo-mark rule created."))
            return self.HttpResponse()

        instance = form.instance
        instance.latitude = form.cleaned_data.get("latitude")
        instance.longitude = form.cleaned_data.get("longitude")
        instance.radius_in_meters = form.cleaned_data.get("radius_in_meters")
        instance.start = form.cleaned_data.get("start") or False
        instance.enforcement_mode = form.cleaned_data.get("enforcement_mode")
        instance.save()
        messages.success(self.request, _("Geo-mark rule updated."))
        return self.HttpResponse()
