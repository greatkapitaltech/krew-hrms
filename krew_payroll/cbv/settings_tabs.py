"""
this page is handling the cbv methods for the payroll settings page,
which lists payslip auto generation as a tab
"""

from typing import Any

from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from horilla_views.cbv_methods import hx_request_required, login_required
from horilla_views.generic.cbv.views import HorillaTabView, TemplateView


@method_decorator(login_required, name="dispatch")
class PayrollSettingsView(TemplateView):
    """
    page for payroll settings (Payslip Auto Generation tab)
    """

    template_name = "cbv/payroll_settings/payroll_settings_main.html"


@method_decorator(login_required, name="dispatch")
class PayrollSettingsTabView(HorillaTabView):
    """
    tab view for payroll settings, shows payslip auto generation as a tab
    """

    # Fixed id (not a random one per load) so the remembered tab matches on the
    # next visit; ?tab=<key> opens a given tab (e.g. Back from a deduction).
    view_id = "payrollSettingsTabs"
    TAB_KEYS = ["earnings", "deductions", "worker-classes", "grades", "work-sites", "auto-payslip"]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from base.models import Grade, WorkerClass
        from krew_payroll.models.models import PayslipAutoGenerate
        from krew_payroll.models.pay_components import ComponentType, PayComponent, WorkSite

        # Badges are counted up front (same company-scoped managers as the
        # lists), so every tab shows its count before it is opened.
        components = PayComponent.objects.all()
        self.tabs = [
            {
                "title": _("Earnings"),
                "url": f"{reverse('payroll-settings-earnings-tab')}",
                "badge": components.filter(type=ComponentType.EARNING).count(),
            },
            {
                "title": _("Deductions"),
                "url": f"{reverse('payroll-settings-deductions-tab')}",
                "badge": components.filter(type=ComponentType.DEDUCTION).count(),
            },
            {
                "title": _("Worker Classes"),
                "url": f"{reverse('payroll-settings-worker-classes-tab')}",
                "badge": WorkerClass.objects.all().count(),
            },
            {
                "title": _("Grades"),
                "url": f"{reverse('payroll-settings-grades-tab')}",
                "badge": Grade.objects.all().count(),
            },
            {
                "title": _("Work Sites"),
                "url": f"{reverse('payroll-settings-work-sites-tab')}",
                "badge": WorkSite.objects.all().count(),
            },
            {
                "title": _("Payslip Auto Generation"),
                "url": f"{reverse('payroll-settings-auto-payslip-tab')}",
                "badge": PayslipAutoGenerate.objects.all().count(),
            },
        ]

    def get_context_data(self, **kwargs):
        from horilla_views.models import ActiveTab

        key = self.request.GET.get("tab")
        if key in self.TAB_KEYS:
            target = f'[data-target="#{self.view_id}{self.TAB_KEYS.index(key) + 1}"]'
            ActiveTab.objects.update_or_create(
                created_by=self.request.user, path=self.request.path, defaults={"tab_target": target}
            )
        return super().get_context_data(**kwargs)


@method_decorator(login_required, name="dispatch")
@method_decorator(hx_request_required, name="dispatch")
class PayrollSettingsAutoPayslipTab(TemplateView):
    """
    payslip auto generation tab content, embeds the existing nav + list
    """

    template_name = "cbv/payroll_settings/auto_payslip_tab.html"
