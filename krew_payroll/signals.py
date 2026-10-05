from datetime import datetime

from django.apps import apps
from django.db.models.signals import post_migrate as post_migrate_signal
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from employee.models import EmployeeWorkInformation
from krew_payroll.methods.deductions import create_deductions
from krew_payroll.models.models import Contract, LoanAccount, Payslip


@receiver(post_save, sender=EmployeeWorkInformation)
def employeeworkinformation_post_save(sender, instance, **_kwargs):
    """
    This method is used to override the save method for EmployeeWorkInformation Model
    """
    # Skip during fixture load — demo contracts come from payroll_data.json
    if _kwargs.get("raw"):
        return

    active_employee = (
        instance.employee_id
        if instance.employee_id and instance.employee_id.is_active == True
        else None
    )
    if active_employee is not None:
        all_contracts = Contract.objects.entire()
        contract_exists = all_contracts.filter(employee_id_id=active_employee).exists()
        if not contract_exists:
            contract = Contract()
            contract.contract_name = f"{active_employee}'s Contract"
            contract.employee_id = active_employee
            contract.contract_start_date = (
                instance.date_joining if instance.date_joining else datetime.today()
            )
            contract.wage = (
                instance.basic_salary if instance.basic_salary is not None else 0
            )
            contract.contract_status = "active"
            contract.save()


@receiver(post_save, sender=LoanAccount)
def create_installments(sender, instance, created, **kwargs):
    """
    A loan / salary advance / fine becomes payslip adjustment lines:
    an earning that pays the loan out (not for fines or asset loans) and one
    deduction per installment month. Installments already on a payslip are
    never changed; unpaid ones follow the loan's current schedule.
    """
    # Demo fixtures carry pre-built schedules; don't rebuild them during loaddata.
    if kwargs.get("raw"):
        return
    from krew_payroll.methods.pay_components.adjustments import add_adjustment

    pays_out = instance.type != "fine"
    if apps.is_installed("asset") and getattr(instance, "asset_id", None) is not None:
        pays_out = False
    disbursement = instance.disbursement_line()
    if pays_out:
        if disbursement is None:
            add_adjustment(
                instance.employee_id,
                "EARNING",
                instance.title,
                instance.loan_amount,
                instance.provided_date,
                source="LOAN",
                source_id=instance.pk,
                # A loan paid out isn't income.
                is_taxable=False,
            )
        elif disbursement.payslip_id is None:
            disbursement.title = instance.title
            disbursement.amount = instance.loan_amount
            disbursement.pay_date = instance.provided_date
            disbursement.save()

    lines = instance.installment_lines()
    unpaid = lines.filter(payslip__isnull=True)
    if instance.settled:
        unpaid.delete()
        return
    if not lines.filter(payslip__isnull=False).exists():
        # Nothing deducted yet: rebuild the whole schedule from the loan.
        unpaid.delete()
        for installment_date, installment_amount in instance.get_installments().items():
            create_deductions(instance, installment_amount, installment_date)
        return
    # Some installments are paid: add only months that have no line yet.
    have = {str(d) for d in lines.values_list("pay_date", flat=True)}
    for installment_date, installment_amount in instance.get_installments().items():
        if str(installment_date) not in have:
            create_deductions(instance, installment_amount, installment_date)


@receiver(post_migrate_signal)
def grant_payroll_manager_pay_masters(sender, **kwargs):
    """
    Payroll Manager configures earnings and deductions, whose eligibility rules
    use the worker class and grade masters that live in base. The app-level
    group config can't grant two base models only, so grant them here.
    Additive and idempotent: safe on every migrate.
    """
    if getattr(sender, "label", None) != "krew_payroll":
        return
    from django.contrib.auth.models import Group, Permission

    group = Group.objects.filter(name="Payroll Manager").first()
    if not group:
        return
    perms = Permission.objects.filter(
        content_type__app_label="base",
        content_type__model__in=["workerclass", "grade"],
    )
    if perms.exists():
        group.permissions.add(*perms)
