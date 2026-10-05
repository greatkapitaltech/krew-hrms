from decimal import Decimal

from django import template

register = template.Library()


@register.filter(name="paid_amount")
def paid_amount(installment):
    paid = [
        Decimal(str(deduction.amount))
        for deduction in installment
        if deduction.installment_payslip()
    ]

    return round(sum(paid, Decimal(0)), 2)


@register.filter(name="balance_amount")
def balance_amount(amount, installment):
    balance = Decimal(str(amount or 0)) - paid_amount(installment)
    return round(balance, 2)
