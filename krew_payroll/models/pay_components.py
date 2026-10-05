"""
Earnings and deductions (pay components) for Indian payroll.

Earnings and deductions share one set of tables: ``PayComponent.type`` tells
them apart. A component's rules live on dated versions
(``PayComponentDetails``), and every child table points up to the version.
Components are user-defined: nothing here keys behaviour off a component's
name or code.

Tables are named ``krew_payroll_<table>``.
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company, Grade, WorkerClass
from employee.models import Employee
from horilla.models import HorillaModel

MONEY = {"max_digits": 12, "decimal_places": 2}


# ------------------------------------------------------------------ choices
# Stored values are always CAPITALS; HR sees the labels.


class ComponentType(models.TextChoices):
    EARNING = "EARNING", _("Earning")
    DEDUCTION = "DEDUCTION", _("Deduction")


class PublishState(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    PUBLISHED = "PUBLISHED", _("Published")


class Frequency(models.TextChoices):
    RECURRING = "RECURRING", _("Recurring")
    ONE_TIME = "ONE_TIME", _("One-Time")
    ON_DEMAND = "ON_DEMAND", _("On-Demand")
    EXIT_TRIGGERED = "EXIT_TRIGGERED", _("Exit-Triggered")


class RecurringInterval(models.TextChoices):
    EVERY_CYCLE = "EVERY_CYCLE", _("Every Cycle")
    MONTHLY = "MONTHLY", _("Monthly")
    QUARTERLY = "QUARTERLY", _("Quarterly")
    HALF_YEARLY = "HALF_YEARLY", _("Half-Yearly")
    ANNUAL = "ANNUAL", _("Annual")


class DisbursementTiming(models.TextChoices):
    FOLLOW_CADENCE = "FOLLOW_CADENCE", _("Follow worker's payout cadence")
    INSTANT = "INSTANT", _("Instant / pay on accrual")


class EligibilityMode(models.TextChoices):
    ALL = "ALL", _("Include All")
    CONDITION = "CONDITION", _("Condition-Based")
    SPECIFIC = "SPECIFIC", _("Specific employees")


class TaxTreatment(models.TextChoices):
    TAXABLE = "TAXABLE", _("Taxable")
    TAX_FREE = "TAX_FREE", _("Tax-Free")


class ExemptionType(models.TextChoices):
    FULL = "FULL", _("Fully exempt")
    UP_TO_LIMIT = "UP_TO_LIMIT", _("Exempt up to a limit")


class CalculationPurpose(models.TextChoices):
    COMPONENT_VALUE = "COMPONENT_VALUE", _("What is paid / deducted")
    EXEMPTION_LIMIT = "EXEMPTION_LIMIT", _("Tax-free limit")
    EMPLOYER_SHARE = "EMPLOYER_SHARE", _("Employer share")


class CalculationMode(models.TextChoices):
    FLAT = "FLAT", _("Flat / Fixed Amount")
    PERCENTAGE = "PERCENTAGE", _("Percentage of components")
    FORMULA = "FORMULA", _("Formula")
    SLAB = "SLAB", _("Slab / Bracket")
    SYSTEM_COMPUTED = "SYSTEM_COMPUTED", _("System-Computed")


class ConditionField(models.TextChoices):
    SHIFT = "SHIFT", _("Shift")
    STATE = "STATE", _("State")
    SITE = "SITE", _("Location / Site")
    CTC = "CTC", _("Salary (CTC)")
    WAGE_BASE = "WAGE_BASE", _("Wage Base")
    GRADE = "GRADE", _("Grade")
    DESIGNATION = "DESIGNATION", _("Designation")
    WORKER_CLASS = "WORKER_CLASS", _("Worker Class")
    EMPLOYMENT_TYPE = "EMPLOYMENT_TYPE", _("Employment Type")


class ConditionOperator(models.TextChoices):
    IN = "IN", _("is in")
    EQ = "EQ", _("equals")
    GT = "GT", _("greater than")
    LT = "LT", _("less than")
    BETWEEN = "BETWEEN", _("between")


class ListType(models.TextChoices):
    INCLUDE = "INCLUDE", _("Include")
    EXCLUDE = "EXCLUDE", _("Exclude")


class EmploymentType(models.TextChoices):
    PAYROLL = "PAYROLL", _("Employee-on-payroll")
    CONTRACTOR_GIG = "CONTRACTOR_GIG", _("Contractor-Gig")


class PayoutCadence(models.TextChoices):
    WEEKLY = "WEEKLY", _("Weekly")
    BI_WEEKLY = "BI_WEEKLY", _("Bi-Weekly")
    MONTHLY = "MONTHLY", _("Monthly")


class AdjustmentSource(models.TextChoices):
    MANUAL = "MANUAL", _("Manual")
    LOAN = "LOAN", _("Loan")
    REIMBURSEMENT = "REIMBURSEMENT", _("Reimbursement")
    ENCASHMENT = "ENCASHMENT", _("Leave encashment")
    BONUS = "BONUS", _("Bonus")
    PENALTY = "PENALTY", _("Penalty")


# Fields whose condition values are ids of a table; the rest are enum codes or numbers.
LIST_CONDITION_FIELDS = [
    ConditionField.SHIFT,
    ConditionField.STATE,
    ConditionField.SITE,
    ConditionField.GRADE,
    ConditionField.DESIGNATION,
    ConditionField.WORKER_CLASS,
    ConditionField.EMPLOYMENT_TYPE,
]
NUMBER_CONDITION_FIELDS = [ConditionField.CTC, ConditionField.WAGE_BASE]


def _stamp_company(instance):
    from base.auth_backends import stamp_company_on_create

    stamp_company_on_create(instance)


# ------------------------------------------------------------------ pay components


class PayComponent(HorillaModel):
    """
    One row per earning or deduction: what never changes across versions.
    ``is_active`` (from HorillaModel) is the Active / Inactive switch.
    """

    company_id = models.ForeignKey(
        Company,
        null=True,
        editable=False,
        on_delete=models.PROTECT,
        db_column="company_id",
        verbose_name=_("Company"),
    )
    type = models.CharField(
        max_length=10, choices=ComponentType.choices, verbose_name=_("Type")
    )
    code = models.CharField(max_length=20, editable=False, verbose_name=_("Code"))
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    objects = HorillaCompanyManager(related_company_field="company_id")

    class Meta:
        db_table = "krew_payroll_pay_component"
        verbose_name = _("Pay Component")
        verbose_name_plural = _("Pay Components")
        ordering = ["type", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_id", "code"], name="uq_pay_component_company_code"
            ),
            models.UniqueConstraint(
                fields=["company_id", "type", "name"],
                name="uq_pay_component_company_type_name",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        _stamp_company(self)
        super().save(*args, **kwargs)

    # ---- helpers used by the Configuration list (columns / actions)

    @property
    def route(self):
        return "earnings" if self.type == ComponentType.EARNING else "deductions"

    def get_instance_id(self):
        return self.id

    def listed_version(self):
        """
        The version the list's columns (frequency, tax / employer share,
        calculation, live version, order) all describe: the one live today,
        else the next one that hasn't started yet. None (blank row) when every
        published version has ended, or there is only a draft.
        """
        live = self.live_version()
        if live:
            return live
        today = timezone.localdate()
        upcoming = [
            v
            for v in self.versions.all()
            if v.publish_state == PublishState.PUBLISHED and v.effective_from and v.effective_from > today
        ]
        return min(upcoming, key=lambda v: v.effective_from) if upcoming else None

    def live_version(self):
        today = timezone.localdate()
        live = [
            v
            for v in self.versions.all()
            if v.publish_state == PublishState.PUBLISHED
            and v.effective_from
            and v.effective_from <= today
            and (v.effective_to is None or v.effective_to >= today)
        ]
        return live[-1] if live else None

    def get_editor_url(self):
        from django.urls import reverse

        return reverse("pay-component-editor", kwargs={"route": self.route, "pk": self.pk})

    def get_active_toggle_url(self):
        from django.urls import reverse

        return reverse("pay-component-active-toggle", kwargs={"route": self.route, "pk": self.pk})

    def get_status_col(self):
        published = any(v.publish_state == PublishState.PUBLISHED for v in self.versions.all())
        if not published:
            text, cls = _("Draft"), "pc-pill--draft"
        elif self.is_active:
            text, cls = _("Active"), "pc-pill--active"
        else:
            text, cls = _("Inactive"), "pc-pill--inactive"
        return format_html('<span class="pc-pill {}">{}</span>', cls, text)

    def get_frequency_col(self):
        version = self.listed_version()
        if not version:
            return ""
        if version.frequency == Frequency.RECURRING and version.recurring_interval:
            return version.get_recurring_interval_display()
        return version.get_frequency_display()

    def get_tax_col(self):
        version = self.listed_version()
        if not version:
            return ""
        if self.type == ComponentType.EARNING:
            if version.tax_treatment != TaxTreatment.TAX_FREE:
                return _("Taxable")
            if version.exemption_type == ExemptionType.UP_TO_LIMIT:
                return _("Tax-free up to a limit")
            return _("Tax-free")
        employer = version.calculations.filter(purpose=CalculationPurpose.EMPLOYER_SHARE).first()
        if not employer:
            return "-"
        if employer.mode == CalculationMode.PERCENTAGE:
            return _("%(rate)s%% of the same base") % {"rate": f"{employer.rate.normalize():f}"}
        return f"₹{employer.flat_amount:,.0f}" if employer.flat_amount is not None else "-"

    def get_calculation_col(self):
        from krew_payroll.methods.pay_components.describe import describe_calculation

        version = self.listed_version()
        if not version:
            return ""
        calc = version.calculations.filter(purpose=CalculationPurpose.COMPONENT_VALUE).first()
        text = describe_calculation(calc)
        if version.max_amount is not None:
            text += f", max ₹{version.max_amount:,.0f}"
        return text

    def get_version_col(self):
        """Live Version column: the live version, else the next one with its start; blank if all ended."""
        version = self.listed_version()
        if not version:
            return ""
        if version.effective_from > timezone.localdate():
            return f"v{version.version_no} · {_('starts')} {version.effective_from:%d %b %Y}"
        span = f"{version.effective_from:%d %b %Y}"
        if version.effective_to:
            span += f" → {version.effective_to:%d %b %Y}"
        return f"v{version.version_no} · {span}"

    def get_order_col(self):
        version = self.listed_version()
        return version.display_order if version else ""


class PayComponentDetails(HorillaModel):
    """
    One row per dated version of a component: the rules.
    A pay period uses the newest published version that overlaps it.
    """

    component = models.ForeignKey(
        PayComponent,
        on_delete=models.CASCADE,
        related_name="versions",
        db_column="component_id",
        verbose_name=_("Component"),
    )
    version_no = models.PositiveIntegerField(verbose_name=_("Version"))
    publish_state = models.CharField(
        max_length=10,
        choices=PublishState.choices,
        default=PublishState.DRAFT,
        verbose_name=_("Publish State"),
    )
    effective_from = models.DateField(
        null=True, blank=True, verbose_name=_("Effective From")
    )
    effective_to = models.DateField(null=True, blank=True, verbose_name=_("Effective To"))
    frequency = models.CharField(
        max_length=20,
        choices=Frequency.choices,
        default=Frequency.RECURRING,
        verbose_name=_("Frequency"),
    )
    recurring_interval = models.CharField(
        max_length=20,
        choices=RecurringInterval.choices,
        null=True,
        blank=True,
        verbose_name=_("Recurring Interval"),
    )
    one_time_date = models.DateField(null=True, blank=True, verbose_name=_("One-Time Date"))
    eligibility_mode = models.CharField(
        max_length=10,
        choices=EligibilityMode.choices,
        default=EligibilityMode.ALL,
        verbose_name=_("Eligibility"),
    )
    min_amount = models.DecimalField(
        **MONEY, null=True, blank=True, verbose_name=_("Min Limit")
    )
    max_amount = models.DecimalField(
        **MONEY, null=True, blank=True, verbose_name=_("Max Limit")
    )
    # Earnings only
    disbursement_timing = models.CharField(
        max_length=20,
        choices=DisbursementTiming.choices,
        null=True,
        blank=True,
        verbose_name=_("Disbursement Timing"),
    )
    tax_treatment = models.CharField(
        max_length=10,
        choices=TaxTreatment.choices,
        null=True,
        blank=True,
        verbose_name=_("Tax Treatment"),
    )
    exemption_type = models.CharField(
        max_length=20,
        choices=ExemptionType.choices,
        null=True,
        blank=True,
        verbose_name=_("Exemption Type"),
    )
    requires_proof = models.BooleanField(default=False, verbose_name=_("Requires Proof"))
    # Deductions only
    reduces_taxable_income = models.BooleanField(
        default=False, verbose_name=_("Reduces Taxable Income")
    )
    show_on_payslip = models.BooleanField(default=True, verbose_name=_("Show On Payslip"))
    payslip_label = models.CharField(
        max_length=100, null=True, blank=True, verbose_name=_("Payslip Label")
    )
    display_order = models.PositiveIntegerField(default=0, verbose_name=_("Display Order"))
    # Component ids this version uses; filled by the server on every save.
    depends_on = models.JSONField(default=list, blank=True, editable=False)
    objects = HorillaCompanyManager(related_company_field="component__company_id")

    class Meta:
        db_table = "krew_payroll_pay_component_details"
        verbose_name = _("Pay Component Version")
        verbose_name_plural = _("Pay Component Versions")
        ordering = ["component_id", "version_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["component", "version_no"], name="uq_pay_component_version"
            )
        ]
        indexes = [
            models.Index(
                fields=["component", "effective_from"],
                condition=Q(publish_state="PUBLISHED"),
                name="ix_pcd_live_version",
            )
        ]

    def __str__(self):
        return f"{self.component.code} v{self.version_no}"


class Calculation(HorillaModel):
    """
    How a figure is worked out, one row per purpose: the component's value,
    the tax-free limit (earnings) or the employer share (deductions).
    """

    component_details = models.ForeignKey(
        PayComponentDetails,
        on_delete=models.CASCADE,
        related_name="calculations",
        db_column="component_details_id",
    )
    purpose = models.CharField(max_length=20, choices=CalculationPurpose.choices)
    mode = models.CharField(max_length=20, choices=CalculationMode.choices)
    flat_amount = models.DecimalField(**MONEY, null=True, blank=True)
    rate = models.DecimalField(**MONEY, null=True, blank=True)
    # Component ids whose values are ADDED UP to form the PERCENTAGE / SLAB base.
    base_component_ids = models.JSONField(default=list, blank=True)
    # Adds the monthly CTC to that sum (earnings).
    base_includes_ctc = models.BooleanField(default=False)
    formula = models.TextField(null=True, blank=True)
    objects = HorillaCompanyManager(
        related_company_field="component_details__component__company_id"
    )

    class Meta:
        db_table = "krew_payroll_calculation"
        constraints = [
            models.UniqueConstraint(
                fields=["component_details", "purpose"],
                name="uq_calculation_version_purpose",
            )
        ]

    def __str__(self):
        return f"{self.component_details} {self.purpose}"


class CalculationSlab(HorillaModel):
    """A slab row; the last regular row is open-ended (range_to empty)."""

    calculation = models.ForeignKey(
        Calculation,
        on_delete=models.CASCADE,
        related_name="slabs",
        db_column="calculation_id",
    )
    range_from = models.DecimalField(**MONEY)
    range_to = models.DecimalField(**MONEY, null=True, blank=True)
    amount = models.DecimalField(**MONEY)
    # Calendar month 1–12 (2 = February) for a month-only row, else empty.
    applies_in_month = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    objects = HorillaCompanyManager(
        related_company_field="calculation__component_details__component__company_id"
    )

    class Meta:
        db_table = "krew_payroll_calculation_slab"
        ordering = ["range_from"]
        indexes = [
            models.Index(fields=["calculation", "range_from"], name="ix_slab_calc_from")
        ]


class EligibilityCondition(HorillaModel):
    """One rule row per field; a version's rows are ANDed."""

    component_details = models.ForeignKey(
        PayComponentDetails,
        on_delete=models.CASCADE,
        related_name="conditions",
        db_column="component_details_id",
    )
    field = models.CharField(max_length=20, choices=ConditionField.choices)
    operator = models.CharField(max_length=10, choices=ConditionOperator.choices)
    # Ids for table masters, codes for enums; for WAGE_BASE the earnings to add up.
    values = models.JSONField(default=list, blank=True)
    value_from = models.DecimalField(**MONEY, null=True, blank=True)
    value_to = models.DecimalField(**MONEY, null=True, blank=True)
    objects = HorillaCompanyManager(
        related_company_field="component_details__component__company_id"
    )

    class Meta:
        db_table = "krew_payroll_eligibility_condition"
        constraints = [
            models.UniqueConstraint(
                fields=["component_details", "field"],
                name="uq_condition_version_field",
            )
        ]


class EligibilityEmployee(HorillaModel):
    """Named workers: the include list (SPECIFIC mode) or the exclude list."""

    component_details = models.ForeignKey(
        PayComponentDetails,
        on_delete=models.CASCADE,
        related_name="employee_lists",
        db_column="component_details_id",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="pay_component_lists",
        db_column="employee_id",
    )
    list_type = models.CharField(max_length=10, choices=ListType.choices)
    objects = HorillaCompanyManager(
        related_company_field="component_details__component__company_id"
    )

    class Meta:
        db_table = "krew_payroll_eligibility_employee"
        constraints = [
            models.UniqueConstraint(
                fields=["component_details", "employee"],
                name="uq_eligibility_version_employee",
            )
        ]
        indexes = [models.Index(fields=["employee"], name="ix_eligibility_employee")]


# ------------------------------------------------------------------ worker data


class WorkSite(HorillaModel):
    """
    A work site inside a state. Lives in payroll (not base) because its state
    is ``krew_company_onboarding.GSTStateConfig``, which base must not depend on.
    """

    company_id = models.ForeignKey(
        Company,
        null=True,
        editable=False,
        on_delete=models.PROTECT,
        db_column="company_id",
        verbose_name=_("Company"),
    )
    state = models.ForeignKey(
        "krew_company_onboarding.GSTStateConfig",
        on_delete=models.PROTECT,
        db_column="state_id",
        verbose_name=_("State"),
    )
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    objects = HorillaCompanyManager(related_company_field="company_id")

    class Meta:
        db_table = "krew_payroll_work_site"
        verbose_name = _("Work Site")
        verbose_name_plural = _("Work Sites")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_id", "state", "name"],
                name="uq_work_site_company_state_name",
            )
        ]
        indexes = [models.Index(fields=["state"], name="ix_work_site_state")]

    def __str__(self):
        return f"{self.name} ({self.state.name})" if self.state_id else self.name

    def save(self, *args, **kwargs):
        _stamp_company(self)
        super().save(*args, **kwargs)

    def get_update_url(self):
        from django.urls import reverse

        return reverse("work-site-update-view", kwargs={"pk": self.pk})

    def get_delete_url(self):
        from django.urls import reverse

        return reverse("generic-delete") + f"?model=krew_payroll.WorkSite&pk={self.pk}"

    def get_instance_id(self):
        return self.id


class EmployeePayProfile(HorillaModel):
    """A worker's pay attributes, one row per employee."""

    employee = models.OneToOneField(
        Employee,
        on_delete=models.CASCADE,
        related_name="pay_profile",
        db_column="employee_id",
        verbose_name=_("Employee"),
    )
    company_id = models.ForeignKey(
        Company,
        null=True,
        editable=False,
        on_delete=models.PROTECT,
        db_column="company_id",
        verbose_name=_("Company"),
    )
    ctc_annual = models.DecimalField(
        **MONEY,
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name=_("CTC (annual)"),
    )
    worker_class = models.ForeignKey(
        WorkerClass,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_column="worker_class_id",
        verbose_name=_("Worker Class"),
    )
    employment_type = models.CharField(
        max_length=20,
        choices=EmploymentType.choices,
        null=True,
        blank=True,
        verbose_name=_("Employment Type"),
    )
    grade = models.ForeignKey(
        Grade,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_column="grade_id",
        verbose_name=_("Grade"),
    )
    work_state = models.ForeignKey(
        "krew_company_onboarding.GSTStateConfig",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_column="work_state_id",
        verbose_name=_("Work State"),
    )
    work_site = models.ForeignKey(
        WorkSite,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_column="work_site_id",
        verbose_name=_("Work Site"),
    )
    payout_cadence = models.CharField(
        max_length=10,
        choices=PayoutCadence.choices,
        default=PayoutCadence.MONTHLY,
        verbose_name=_("Payout Cadence"),
    )
    objects = HorillaCompanyManager(related_company_field="company_id")

    class Meta:
        db_table = "krew_payroll_employee_pay_profile"
        verbose_name = _("Employee Pay Profile")
        verbose_name_plural = _("Employee Pay Profiles")
        indexes = [
            models.Index(fields=["company_id"], name="ix_pay_profile_company"),
            models.Index(fields=["worker_class"], name="ix_pay_profile_class"),
            models.Index(fields=["grade"], name="ix_pay_profile_grade"),
            models.Index(fields=["work_state"], name="ix_pay_profile_state"),
            models.Index(fields=["work_site"], name="ix_pay_profile_site"),
        ]

    def __str__(self):
        return f"{self.employee} pay profile"

    @property
    def ctc_monthly(self):
        return (self.ctc_annual or 0) / 12

    def save(self, *args, **kwargs):
        if not self.company_id_id:
            work_info = getattr(self.employee, "employee_work_info", None)
            if work_info and work_info.company_id_id:
                self.company_id_id = work_info.company_id_id
        _stamp_company(self)
        super().save(*args, **kwargs)


class EmployeePayAdjustment(HorillaModel):
    """
    A one-off line for one employee in one pay period: loan disbursement and
    installments, reimbursement, leave encashment, bonus, penalty, or a manual
    payslip addition. The payslip run adds these after the configured components.
    """

    company_id = models.ForeignKey(
        Company,
        null=True,
        editable=False,
        on_delete=models.PROTECT,
        db_column="company_id",
        verbose_name=_("Company"),
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="pay_adjustments",
        db_column="employee_id",
        verbose_name=_("Employee"),
    )
    type = models.CharField(
        max_length=10, choices=ComponentType.choices, verbose_name=_("Type")
    )
    title = models.CharField(max_length=200, verbose_name=_("Title"))
    amount = models.DecimalField(
        **MONEY, validators=[MinValueValidator(0)], verbose_name=_("Amount")
    )
    # Any date inside the pay period it belongs to.
    pay_date = models.DateField(verbose_name=_("Pay Date"))
    source = models.CharField(
        max_length=20,
        choices=AdjustmentSource.choices,
        default=AdjustmentSource.MANUAL,
        verbose_name=_("Source"),
    )
    source_id = models.PositiveIntegerField(null=True, blank=True)
    # Earnings: counts toward taxable income. Deductions: reduces taxable income.
    is_taxable = models.BooleanField(default=True, verbose_name=_("Taxable"))
    reduces_taxable_income = models.BooleanField(
        default=False, verbose_name=_("Reduces Taxable Income")
    )
    # Set when a payslip that includes this line is saved.
    payslip = models.ForeignKey(
        "krew_payroll.Payslip",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pay_adjustments",
        db_column="payslip_id",
    )
    objects = HorillaCompanyManager(related_company_field="company_id")

    class Meta:
        db_table = "krew_payroll_employee_pay_adjustment"
        verbose_name = _("Pay Adjustment")
        verbose_name_plural = _("Pay Adjustments")
        ordering = ["pay_date", "id"]
        indexes = [
            models.Index(fields=["employee", "pay_date"], name="ix_adjustment_emp_date"),
            models.Index(fields=["source", "source_id"], name="ix_adjustment_source"),
        ]

    def __str__(self):
        return f"{self.title} ({self.amount})"

    # Names the loan installment templates use.
    @property
    def one_time_date(self):
        return self.pay_date

    def installment_payslip(self):
        return self.payslip

    def save(self, *args, **kwargs):
        if not self.company_id_id:
            work_info = getattr(self.employee, "employee_work_info", None)
            if work_info and work_info.company_id_id:
                self.company_id_id = work_info.company_id_id
        _stamp_company(self)
        super().save(*args, **kwargs)
