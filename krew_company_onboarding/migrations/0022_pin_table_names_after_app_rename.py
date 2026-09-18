# Records, in Django's migration STATE only, that every model's db_table is
# explicitly pinned to its pre-rename physical name (models.py's Meta
# classes were already updated -- see the db_table comments there).
#
# This has to be a state-only change (SeparateDatabaseAndState with empty
# database_operations), not a real AlterModelTable. Django replays migration
# history using the app's CURRENT label (krew_company_onboarding, since that's
# what apps.py says now, applied retroactively to migrations 0001-0021 too)
# -- so as far as migration state is concerned, these tables have "always"
# been named krew_company_onboarding_<model>, even though the app was
# actually named company_onboarding when 0001-0021 ran and the tables were
# really created as company_onboarding_<model>. A real AlterModelTable here
# would try `ALTER TABLE krew_company_onboarding_x RENAME TO
# company_onboarding_x` and fail outright, since no table named
# krew_company_onboarding_x has ever existed. The physical tables never
# need to move at all -- only Django's bookkeeping needs to catch up to
# what's actually there.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('krew_company_onboarding', '0021_companybankverification_failure_reason'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelTable(
                    name=model_name,
                    table=f"company_onboarding_{model_name}",
                )
                for model_name in (
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
            ],
            database_operations=[],
        ),
    ]
