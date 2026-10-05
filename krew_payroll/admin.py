"""
admin.py

Used to register models on admin site
"""

from django.contrib import admin

from krew_payroll.models.models import (
    Contract,
    FilingStatus,
    LoanAccount,
    Payslip,
    PayslipAutoGenerate,
    Reimbursement,
    ReimbursementrequestComment,
)
from krew_payroll.models.pay_components import (
    Calculation,
    CalculationSlab,
    EligibilityCondition,
    EligibilityEmployee,
    EmployeePayAdjustment,
    EmployeePayProfile,
    PayComponent,
    PayComponentDetails,
    WorkSite,
)
from krew_payroll.models.tax_models import PayrollSettings, TaxBracket

# Register your models here.
admin.site.register(FilingStatus)
admin.site.register(TaxBracket)
admin.site.register(Contract)
admin.site.register(Payslip)
admin.site.register(PayrollSettings)
admin.site.register(LoanAccount)
admin.site.register(Reimbursement)
admin.site.register(ReimbursementrequestComment)
admin.site.register(PayslipAutoGenerate)

for _model in (
    PayComponent,
    PayComponentDetails,
    Calculation,
    CalculationSlab,
    EligibilityCondition,
    EligibilityEmployee,
    WorkSite,
    EmployeePayProfile,
    EmployeePayAdjustment,
):
    admin.site.register(_model)
