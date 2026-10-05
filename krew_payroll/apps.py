"""
App configuration for the 'krew_payroll' app.
"""

from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_migrate


class PayrollConfig(AppConfig):
    """
    AppConfig for the 'krew_payroll' app.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "krew_payroll"
    verbose_name = "Payroll"

    def ready(self) -> None:
        ready = super().ready()
        from django.urls import include, path

        from horilla.urls import urlpatterns
        from krew_payroll import scheduler, signals

        settings.APPS.append("krew_payroll")
        urlpatterns.append(
            path("payroll/", include("krew_payroll.urls.urls")),
        )
        return ready
