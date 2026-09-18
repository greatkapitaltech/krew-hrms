from django.apps import AppConfig
from django.conf import settings


class CompanyOnboardingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "krew_company_onboarding"
    verbose_name = "Company Setup"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        from krew_company_onboarding import signals  # noqa: F401

        settings.APPS.append("krew_company_onboarding")
        urlpatterns.append(
            path("company-onboarding/", include("krew_company_onboarding.urls")),
        )
        super().ready()
