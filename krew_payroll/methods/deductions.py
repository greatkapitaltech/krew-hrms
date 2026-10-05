"""
deductions.py

Loan / fine installment lines on payslips.
"""


def create_deductions(instance, amount, date):
    """One loan / fine installment: a deduction line on the payslip of its month."""
    from krew_payroll.methods.pay_components.adjustments import add_adjustment

    return add_adjustment(
        instance.employee_id,
        "DEDUCTION",
        f"{instance.title} - {date}",
        round(float(amount), 2),
        date,
        source="LOAN",
        source_id=instance.pk,
    )
