"""
Track every pay-component table in Horilla's audit (django-auditlog via
horilla_audit.AuditModelConfig).

AuditModelConfig rows fully override the built-in default set, so when the
table is still empty the defaults (Employee, EmployeeWorkInformation,
EmployeeBankDetails) are added too; otherwise they would stop being tracked.
"""

from django.db import migrations

DEFAULTS = [
    ("employee", "Employee"),
    ("employee", "EmployeeWorkInformation"),
    ("employee", "EmployeeBankDetails"),
]
PAY_MODELS = [
    ("base", "WorkerClass"),
    ("base", "Grade"),
    ("krew_payroll", "PayComponent"),
    ("krew_payroll", "PayComponentDetails"),
    ("krew_payroll", "Calculation"),
    ("krew_payroll", "CalculationSlab"),
    ("krew_payroll", "EligibilityCondition"),
    ("krew_payroll", "EligibilityEmployee"),
    ("krew_payroll", "WorkSite"),
    ("krew_payroll", "EmployeePayProfile"),
    ("krew_payroll", "EmployeePayAdjustment"),
]


def add_audit_rows(apps, schema_editor):
    AuditModelConfig = apps.get_model("horilla_audit", "AuditModelConfig")
    wanted = PAY_MODELS if AuditModelConfig.objects.exists() else DEFAULTS + PAY_MODELS
    for app_label, model_name in wanted:
        AuditModelConfig.objects.get_or_create(
            app_label=app_label,
            model_name=model_name,
            defaults={"is_enabled": True, "tracked_fields": []},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("krew_payroll", "0007_paycomponent_paycomponentdetails_eligibilityemployee_and_more"),
        ("base", "0019_grade_workerclass"),
        ("horilla_audit", "0003_alter_historytrackingfields_options_and_more"),
    ]

    operations = [migrations.RunPython(add_audit_rows, migrations.RunPython.noop)]
