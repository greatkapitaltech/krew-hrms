"""
rename_app_label — fix django_content_type / django_migrations after a
Django app's Python package (and therefore its app_label) has been renamed
in code.

Why this can't be a normal migration: Django's migrate command computes its
FULL plan for every app up front, before running any operation, using
whatever is currently in django_migrations. A RunPython step inside a
migration for the renamed app would only execute AFTER that plan is already
fixed — by then Django has already decided (wrongly, using the stale
app_label) that every migration for the new label is "unapplied" and will
try to re-run CreateModel etc. against tables that already exist. This has
to run as a standalone step, BEFORE `migrate`, so django_migrations already
reflects the new label by the time Django builds its plan.

Only touches django_content_type.app_label and django_migrations.app --
never touches any actual application data or table. Safe to re-run (it's a
no-op once nothing matches the old label).

Usage:
    python manage.py rename_app_label company_onboarding krew_company_onboarding
"""

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = (
        "Rewrite django_content_type/django_migrations rows from an old app "
        "label to a new one, after renaming the app's Python package. Run "
        "this BEFORE `migrate` on every environment (local, CI, ECS) that "
        "already has the app's tables from before the rename."
    )

    def add_arguments(self, parser):
        parser.add_argument("old_label")
        parser.add_argument("new_label")

    def handle(self, *args, **options):
        old_label = options["old_label"]
        new_label = options["new_label"]

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM django_content_type WHERE app_label = %s",
                [old_label],
            )
            content_type_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM django_migrations WHERE app = %s",
                [old_label],
            )
            migrations_count = cursor.fetchone()[0]

            if not content_type_count and not migrations_count:
                self.stdout.write(
                    self.style.WARNING(
                        f"Nothing to do -- no django_content_type or "
                        f"django_migrations rows found for '{old_label}' "
                        f"(already renamed, or never existed here)."
                    )
                )
                return

            cursor.execute(
                "UPDATE django_content_type SET app_label = %s WHERE app_label = %s",
                [new_label, old_label],
            )
            cursor.execute(
                "UPDATE django_migrations SET app = %s WHERE app = %s",
                [new_label, old_label],
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Renamed {content_type_count} content_type row(s) and "
                f"{migrations_count} migration record(s) from '{old_label}' "
                f"to '{new_label}'. Now safe to run `python manage.py migrate`."
            )
        )
