"""Earnings / deductions configuration (Payroll → Configuration) and its masters."""

from django.urls import path, re_path

from krew_payroll.cbv import pay_components as cbv
from krew_payroll.views import pay_component_views as views

ROUTE = r"(?P<route>earnings|deductions)"

urlpatterns = [
    # Configuration tabs
    path("configuration/earnings-tab/", cbv.EarningsTab.as_view(), name="payroll-settings-earnings-tab"),
    path("configuration/deductions-tab/", cbv.DeductionsTab.as_view(), name="payroll-settings-deductions-tab"),
    path("configuration/worker-classes-tab/", cbv.WorkerClassesTab.as_view(), name="payroll-settings-worker-classes-tab"),
    path("configuration/grades-tab/", cbv.GradesTab.as_view(), name="payroll-settings-grades-tab"),
    path("configuration/work-sites-tab/", cbv.WorkSitesTab.as_view(), name="payroll-settings-work-sites-tab"),
    # Earnings / deductions list
    re_path(rf"^pay-components/{ROUTE}/nav/$", cbv.pay_component_nav_view, name="pay-component-nav"),
    re_path(rf"^pay-components/{ROUTE}/list/$", cbv.pay_component_list_view, name="pay-component-list"),
    re_path(rf"^pay-components/{ROUTE}/create/$", views.pay_component_create, name="pay-component-create"),
    # Editor
    re_path(rf"^pay-components/{ROUTE}/(?P<pk>\d+)/$", views.pay_component_editor, name="pay-component-editor"),
    re_path(
        rf"^pay-components/{ROUTE}/(?P<pk>\d+)/v/(?P<version_no>\d+)/render/$",
        views.pay_component_render,
        name="pay-component-render",
    ),
    re_path(
        rf"^pay-components/{ROUTE}/(?P<pk>\d+)/v/(?P<version_no>\d+)/save/$",
        views.pay_component_save,
        name="pay-component-save",
    ),
    re_path(
        rf"^pay-components/{ROUTE}/(?P<pk>\d+)/v/(?P<version_no>\d+)/publish-confirm/$",
        views.pay_component_publish_confirm,
        name="pay-component-publish-confirm",
    ),
    re_path(
        rf"^pay-components/{ROUTE}/(?P<pk>\d+)/v/(?P<version_no>\d+)/publish/$",
        views.pay_component_publish,
        name="pay-component-publish",
    ),
    re_path(
        rf"^pay-components/{ROUTE}/(?P<pk>\d+)/v/(?P<version_no>\d+)/delete-draft/$",
        views.pay_component_delete_draft,
        name="pay-component-delete-draft",
    ),
    re_path(rf"^pay-components/{ROUTE}/(?P<pk>\d+)/new-version/$", views.pay_component_new_version, name="pay-component-new-version"),
    re_path(rf"^pay-components/{ROUTE}/(?P<pk>\d+)/active-toggle/$", views.pay_component_active_toggle, name="pay-component-active-toggle"),
    # Employee profile: pay profile tab (also saves)
    path("employee-pay-profile/<int:pk>/", views.pay_profile_tab, name="employee-pay-profile-tab"),
    # Work sites
    path("work-site-list/", cbv.WorkSiteListView.as_view(), name="work-site-list"),
    path("work-site-nav/", cbv.WorkSiteNavView.as_view(), name="work-site-nav"),
    path("work-site-create-view/", cbv.WorkSiteFormView.as_view(), name="work-site-create-view"),
    path("work-site-update-view/<int:pk>/", cbv.WorkSiteFormView.as_view(), name="work-site-update-view"),
]
