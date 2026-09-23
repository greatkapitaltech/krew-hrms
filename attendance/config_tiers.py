"""
attendance/config_tiers.py

Shared building blocks for "tiered" attendance config: a Company Default
row, optionally overridden for a specific Employee Type, optionally
overridden again (most specific) for a specific Department. Every
attendance feature that follows this shape (Attendance Type today;
Validation Threshold, Overtime, Regularization later) reuses the same
resolution logic here instead of re-implementing it per feature.

Scoped to `attendance` deliberately, not promoted to `base` -- there is no
second consumer outside this app yet. Promote later if one appears.
"""

from datetime import date

from django.dispatch import Signal
from django.utils.translation import gettext_lazy as _

TIER_COMPANY = "COMPANY"
TIER_EMPLOYEE_TYPE = "EMPLOYEE_TYPE"
TIER_DEPARTMENT = "DEPARTMENT"
TIER_CHOICES = (
    (TIER_COMPANY, _("Company Default")),
    (TIER_EMPLOYEE_TYPE, _("Employee Type Override")),
    (TIER_DEPARTMENT, _("Department Override")),
)


def next_month_first(today=None):
    """
    The 1st of the month AFTER `today`'s month -- always, even when called
    on the 1st itself. Deliberately not base.forms.get_next_monthly_date():
    that function returns *today* if called with rotate_every=1 on the 1st,
    which would let a same-day config change take effect same-day --
    exactly what "changes apply from the 1st of next month" must not do.
    """
    today = today or date.today()
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


# Attendance-specific signals (not added to horilla/signals.py's repo-wide
# set, since nothing outside this app needs them yet). A future Activity
# Log feature subscribes via @receiver in its own signals.py, the same way
# payroll/signals.py already does for its own concerns -- this module never
# needs to know that consumer exists.
config_override_requested = Signal()  # kwargs: instance (a PendingConfigChange)
config_override_applied = Signal()  # kwargs: instance
config_override_cancelled = Signal()  # kwargs: instance


class TieredConfigResolutionMixin:
    """
    Mixin for a concrete tiered config model. The model itself still
    defines its own fields, `Meta.constraints`, and `clean()` -- this only
    supplies the two behaviors every such model needs identically:
    resolving the effective row for an employee, and (optionally)
    inheriting specific field values from the Company Default row when a
    more specific tier doesn't set them itself.

    A concrete model declares:
      RULE_FIELDS      -- every field name that's an actual rule value
                           (not tier/company/employee_type/department/
                           is_active bookkeeping).
      INHERITED_FIELDS -- the subset of RULE_FIELDS that a non-Company-
                           Default row may leave blank to inherit from the
                           Company Default row, rather than override.
                           Empty tuple means "always full override."
    """

    RULE_FIELDS: tuple = ()
    INHERITED_FIELDS: tuple = ()

    @classmethod
    def resolve_for_employee(cls, employee):
        """
        The single effective CURRENT row for `employee`: Department
        override, else Employee-Type override, else Company Default, else
        None. Always resolved live against current data -- never cached
        inside this method itself, so an employee moved between
        departments/types picks up the correct override on the very next
        call with no extra bookkeeping.
        """
        work_info = getattr(employee, "employee_work_info", None)
        company = getattr(work_info, "company_id", None)
        if company is None:
            return None

        department = employee.get_department()
        employee_type = employee.get_employee_type()
        # Not the specific EmployeeType row -- EmployeeType names stay
        # free-form, but this tier only ever has three possible overrides
        # (see EmployeeType.collar_category's field comment).
        employee_type_category = getattr(employee_type, "collar_category", None)

        if department is not None:
            row = cls.objects.filter(
                tier=TIER_DEPARTMENT, company=company, department=department,
                is_active=True,
            ).first()
            if row is not None:
                return row

        if employee_type_category is not None:
            row = cls.objects.filter(
                tier=TIER_EMPLOYEE_TYPE, company=company,
                employee_type_category=employee_type_category, is_active=True,
            ).first()
            if row is not None:
                return row

        return cls.objects.filter(
            tier=TIER_COMPANY, company=company, is_active=True,
        ).first()

    def get_effective_values(self):
        """
        This row's rule values, with INHERITED_FIELDS null-coalesced from
        the Company Default row when this row is a non-Company-Default
        tier and leaves them blank.
        """
        values = {name: getattr(self, name) for name in self.RULE_FIELDS}
        if self.tier != TIER_COMPANY and self.INHERITED_FIELDS:
            company_default = type(self).objects.filter(
                tier=TIER_COMPANY, company=self.company, is_active=True,
            ).first()
            if company_default is not None:
                for name in self.INHERITED_FIELDS:
                    if values.get(name) in (None, ""):
                        values[name] = getattr(company_default, name)
        return values

    def get_pending_change(self):
        """
        This row's current pending (not-yet-effective) change, if any --
        for a confirmation UI's "this will change to X on <date>" text.
        Local import to avoid a circular import (PendingConfigChange lives
        in attendance/models.py, which imports this module).
        """
        from django.contrib.contenttypes.models import ContentType

        from attendance.models import PendingConfigChange

        content_type = ContentType.objects.get_for_model(type(self))
        return PendingConfigChange.objects.filter(
            content_type=content_type,
            object_id=self.pk,
            status=PendingConfigChange.STATUS_PENDING,
        ).first()
