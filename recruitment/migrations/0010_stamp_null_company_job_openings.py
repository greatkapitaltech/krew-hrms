"""
Stamp a company onto job openings that were created without one.

Before company was derived from the login context, a superuser creating a job
opening while viewing "All companies" produced a row with company_id = NULL.
HorillaCompanyManager passes null-company rows through to every tenant, so
those openings are visible company-wide -- and they can no longer be fixed
from the UI, because Company is not a form field.

The company is recovered from the opening's assigned managers' work-info
company. Only applied when every manager agrees on one company: zero managers
or two different companies is genuinely ambiguous, and guessing there would
silently move a job opening into the wrong tenant. Those rows are left alone
and remain blocked from publishing until someone sets them deliberately.
"""

from django.db import migrations


def stamp_company_from_managers(apps, schema_editor):
    Recruitment = apps.get_model("recruitment", "Recruitment")
    EmployeeWorkInformation = apps.get_model("employee", "EmployeeWorkInformation")

    unscoped = Recruitment.objects.filter(company_id__isnull=True)
    for recruitment in unscoped.iterator(chunk_size=200):
        manager_ids = list(recruitment.recruitment_managers.values_list("pk", flat=True))
        if not manager_ids:
            continue
        companies = set(
            EmployeeWorkInformation.objects.filter(
                employee_id__in=manager_ids, company_id__isnull=False
            ).values_list("company_id", flat=True)
        )
        if len(companies) == 1:
            recruitment.company_id_id = companies.pop()
            recruitment.save(update_fields=["company_id"])


def reverse_stamp(apps, schema_editor):
    """
    No-op. Clearing the company again would re-expose these openings to every
    tenant, which is the bug this migration exists to fix.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0009_alter_recruitment_is_published"),
        ("employee", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(stamp_company_from_managers, reverse_stamp),
    ]
