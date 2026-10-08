"""
apps.py
"""

from django.apps import AppConfig
from django.conf import settings


class RecruitmentConfig(AppConfig):
    """
    AppConfig for the 'recruitment' app.

    This class represents the configuration for the 'recruitment' app. It provides
    the necessary settings and metadata for the app.

    Attributes:
        default_auto_field (str): The default auto field to use for model field IDs.
        name (str): The name of the app.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "recruitment"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns
        from recruitment import signals
        from recruitment import audit_signals  # noqa: F401  (set-up audit trail)
        # Start the background jobs (auto-close at End Date) in every server
        # process, like payroll/attendance. It was only imported from
        # recruitment/migrations/__init__.py, which gunicorn never loads, so in
        # production the scheduler never started.
        from recruitment import scheduler  # noqa: F401

        settings.APPS.append("recruitment")
        urlpatterns.append(
            path("recruitment/", include("recruitment.urls")),
        )
        super().ready()
