from django.urls import path, re_path

from ...api_views.payroll.pay_component_views import (
    PayComponentCreateAPIView,
    PayComponentDeactivateAPIView,
    PayComponentPublishAPIView,
    PayComponentVersionAPIView,
)
from ...api_views.payroll.views import *

urlpatterns = [
    path(
        "contract/",
        ContractView.as_view(),
    ),
    path(
        "contract/<int:id>",
        ContractView.as_view(),
    ),
    path("payslip/", PayslipView.as_view(), name=""),
    path("payslip/<int:id>", PayslipView.as_view(), name=""),
    path("payslip-download/<int:id>", PayslipPDFAPIView.as_view(), name=""),
    path("payslip-send-mail/", PayslipSendMailView.as_view(), name=""),
    path("loan-account/", LoanAccountView.as_view(), name=""),
    path("loan-account/<int:pk>", LoanAccountView.as_view(), name=""),
    path("reimbusement/", ReimbursementView.as_view(), name=""),
    path("reimbusement/<int:pk>", ReimbursementView.as_view(), name=""),
    path(
        "reimbusement-approve-reject/<int:pk>",
        ReimbusementApproveRejectView.as_view(),
        name="",
    ),
    path("tax-bracket/<int:pk>", TaxBracketView.as_view(), name=""),
    path("tax-bracket/", TaxBracketView.as_view(), name=""),
    # Earnings & deductions: create, update, publish, deactivate (route = earnings | deductions)
    re_path(
        r"^(?P<route>earnings|deductions)/$",
        PayComponentCreateAPIView.as_view(),
        name="api-pay-component-create",
    ),
    re_path(
        r"^(?P<route>earnings|deductions)/(?P<pk>\d+)/versions/(?P<version_no>\d+)/$",
        PayComponentVersionAPIView.as_view(),
        name="api-pay-component-update",
    ),
    re_path(
        r"^(?P<route>earnings|deductions)/(?P<pk>\d+)/versions/(?P<version_no>\d+)/publish/$",
        PayComponentPublishAPIView.as_view(),
        name="api-pay-component-publish",
    ),
    re_path(
        r"^(?P<route>earnings|deductions)/(?P<pk>\d+)/deactivate/$",
        PayComponentDeactivateAPIView.as_view(),
        name="api-pay-component-deactivate",
    ),
]
