# Physically renames every table in this app from the pre-rename
# `company_onboarding_*` names to `krew_company_onboarding_*`, matching the
# app's actual current name. Unlike 0022 (which only updated Django's
# bookkeeping, deliberately leaving the physical tables untouched), this IS
# a real ALTER TABLE ... RENAME TO for each one -- a fast, metadata-only
# operation in Postgres, not a data rewrite, so it's safe to run against a
# live table.
#
# Works correctly on BOTH a fresh database (where 0001/0011/0015 first
# create the tables under their original company_onboarding_* names, and
# this then renames them) and an existing pre-rename database (where the
# tables already have those names from before) -- either way, this is the
# step that makes them krew_company_onboarding_* for real.

from django.db import migrations


_MODEL_NAMES = (
    "gststateconfig",
    "companystateregistration",
    "companypoccontact",
    "companybankdetails",
    "companybankverification",
    "cashfreeapilog",
    "companycontract",
    "companysignatory",
    "companybrandedtemplate",
    "companydocument",
    "companydeactivationrecord",
)


class Migration(migrations.Migration):

    dependencies = [
        ('krew_company_onboarding', '0022_pin_table_names_after_app_rename'),
    ]

    operations = [
        migrations.AlterModelTable(
            name=model_name,
            table=f"krew_company_onboarding_{model_name}",
        )
        for model_name in _MODEL_NAMES
    ]
