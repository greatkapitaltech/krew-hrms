"""
After the payroll -> krew_payroll rename, rewrite the rows that store the app
label or module path as plain text (content types and django_migrations are
handled by `manage.py rename_app_label`):

* horilla_audit AuditModelConfig.app_label  ("payroll" -> "krew_payroll")
* horilla_automations MailAutomation.model  ("payroll.models.models.X" -> "krew_payroll.models.models.X")
"""

from django.db import migrations
from django.db.models import Value
from django.db.models.functions import Concat, Substr

OLD, NEW = "payroll", "krew_payroll"


def relabel(apps, schema_editor):
    try:
        AuditModelConfig = apps.get_model("horilla_audit", "AuditModelConfig")
    except LookupError:
        AuditModelConfig = None
    if AuditModelConfig is not None:
        for row in AuditModelConfig.objects.filter(app_label=OLD):
            # A row for the same model may already exist under the new label.
            if AuditModelConfig.objects.filter(app_label=NEW, model_name=row.model_name).exists():
                row.delete()
            else:
                row.app_label = NEW
                row.save(update_fields=["app_label"])

    try:
        MailAutomation = apps.get_model("horilla_automations", "MailAutomation")
    except LookupError:
        return
    MailAutomation.objects.filter(model__startswith=f"{OLD}.").update(
        model=Concat(Value(f"{NEW}."), Substr("model", len(OLD) + 2))
    )


class Migration(migrations.Migration):

    dependencies = [
        ("krew_payroll", "0011_rename_tables_to_krew_payroll"),
        ("horilla_audit", "0003_alter_historytrackingfields_options_and_more"),
        ("horilla_automations", "0003_alter_mailautomation_mail_details_and_more"),
    ]

    operations = [migrations.RunPython(relabel, migrations.RunPython.noop)]
