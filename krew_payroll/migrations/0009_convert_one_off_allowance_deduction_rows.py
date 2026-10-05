"""
Before the old Allowance / Deduction tables are dropped (0010), turn every
one-off employee line they hold into an EmployeePayAdjustment:

* loans / salary advances / fines: the disbursement and each installment
  (installments already on a payslip stay linked to that payslip);
* approved reimbursements, leave and bonus encashments;
* penalties;
* other employee-specific one-off rows (bonus, payslip "add deduction").

A line whose date falls inside an existing payslip of the employee is linked
to that payslip, so it isn't paid or deducted a second time. Company-wide
allowances / deductions are not converted: they are re-created as pay
components in the new Earnings / Deductions configuration.
"""

from datetime import date, datetime

from django.db import migrations

REIMBURSEMENT_SOURCES = {
    "reimbursement": "REIMBURSEMENT",
    "leave_encashment": "ENCASHMENT",
    "bonus_encashment": "BONUS",
}


def convert(apps, schema_editor):
    Allowance = apps.get_model("krew_payroll", "Allowance")
    Deduction = apps.get_model("krew_payroll", "Deduction")
    LoanAccount = apps.get_model("krew_payroll", "LoanAccount")
    Reimbursement = apps.get_model("krew_payroll", "Reimbursement")
    Payslip = apps.get_model("krew_payroll", "Payslip")
    Adjustment = apps.get_model("krew_payroll", "EmployeePayAdjustment")
    WorkInfo = apps.get_model("employee", "EmployeeWorkInformation")
    PenaltyAccounts = apps.get_model("base", "PenaltyAccounts")

    company_of = dict(WorkInfo.objects.values_list("employee_id_id", "company_id_id"))

    def covering_payslip(employee_id, day):
        if not day:
            return None
        return Payslip.objects.filter(
            employee_id_id=employee_id, start_date__lte=day, end_date__gte=day
        ).first()

    def add(employee_id, type_, title, amount, day, source, source_id=None, payslip=None, **flags):
        if not employee_id or day is None or amount is None:
            return
        Adjustment.objects.create(
            company_id_id=company_of.get(employee_id),
            employee_id=employee_id,
            type=type_,
            title=(title or "")[:200] or source.title(),
            amount=round(float(amount), 2),
            pay_date=day,
            source=source,
            source_id=source_id,
            payslip=payslip if payslip is not None else covering_payslip(employee_id, day),
            **flags,
        )

    done_allowances, done_deductions = set(), set()

    for loan in LoanAccount.objects.all():
        if loan.allowance_id_id:
            allowance = Allowance.objects.filter(pk=loan.allowance_id_id).first()
            if allowance:
                add(loan.employee_id_id, "EARNING", loan.title, loan.loan_amount, loan.provided_date,
                    "LOAN", loan.pk, is_taxable=False)
                done_allowances.add(allowance.pk)
        for installment in loan.deduction_ids.all():
            paid = Payslip.objects.filter(installment_ids=installment).first()
            add(loan.employee_id_id, "DEDUCTION", installment.title, installment.amount,
                installment.one_time_date, "LOAN", loan.pk, payslip=paid)
            done_deductions.add(installment.pk)

    for reimbursement in Reimbursement.objects.filter(status="approved", allowance_id__isnull=False):
        add(reimbursement.employee_id_id, "EARNING", reimbursement.title, reimbursement.amount,
            reimbursement.allowance_on, REIMBURSEMENT_SOURCES.get(reimbursement.type, "REIMBURSEMENT"),
            reimbursement.pk, is_taxable=reimbursement.type != "reimbursement")
        done_allowances.add(reimbursement.allowance_id_id)

    for penalty in PenaltyAccounts.objects.filter(penalty_amount__gt=0):
        day = None
        if getattr(penalty, "late_early_id_id", None):
            late = penalty.late_early_id
            day = late.attendance_id.attendance_date if late and late.attendance_id_id else None
        elif getattr(penalty, "leave_request_id_id", None):
            day = penalty.leave_request_id.end_date if penalty.leave_request_id else None
        if day is None:
            day = penalty.created_at.date() if getattr(penalty, "created_at", None) else date.today()
        add(penalty.employee_id_id, "DEDUCTION", "Penalty", penalty.penalty_amount, day,
            "PENALTY", penalty.pk)
        # The old penalty Deduction rows are only_show_under_employee one-offs on the same day.
        done_deductions.update(
            Deduction.objects.filter(
                only_show_under_employee=True, one_time_date=day, amount=penalty.penalty_amount,
                specific_employees=penalty.employee_id_id, title__icontains="penalty",
            ).values_list("pk", flat=True)
        )

    for allowance in Allowance.objects.filter(only_show_under_employee=True).exclude(pk__in=done_allowances):
        if not allowance.is_fixed or not allowance.one_time_date:
            continue
        for employee_id in allowance.specific_employees.values_list("pk", flat=True):
            add(employee_id, "EARNING", allowance.title, allowance.amount, allowance.one_time_date,
                "BONUS", is_taxable=allowance.is_taxable)
    for deduction in Deduction.objects.filter(only_show_under_employee=True).exclude(pk__in=done_deductions):
        if not deduction.is_fixed or not deduction.one_time_date:
            continue
        for employee_id in deduction.specific_employees.values_list("pk", flat=True):
            add(employee_id, "DEDUCTION", deduction.title, deduction.amount, deduction.one_time_date,
                "MANUAL", reduces_taxable_income=bool(deduction.is_pretax))


class Migration(migrations.Migration):

    dependencies = [
        ("krew_payroll", "0008_pay_components_audit_config"),
        ("base", "0019_grade_workerclass"),
        ("employee", "0005_alter_employee_phone_and_more"),
        ("attendance", "0007_attendanceconflictresolution_attendancedailyhours_and_more"),
        ("leave", "0007_alter_historicalleaverequest_reject_reason_and_more"),
    ]

    operations = [migrations.RunPython(convert, migrations.RunPython.noop)]
