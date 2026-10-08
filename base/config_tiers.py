"""
base/config_tiers.py

Shared building blocks for "tiered" config: a Company Default row,
optionally overridden for a specific Employee Type, optionally overridden
again (most specific) for a specific Department. Originally built for
Attendance (Attendance Type today; Validation Threshold, Overtime,
Regularization later) but promoted here once geofencing needed the exact
same shape (Company Default boundary / Employee-Type exemption /
Department override boundary) -- any feature that follows this pattern
reuses the resolution logic here instead of re-implementing it per model.
"""

from datetime import date

from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import Signal, receiver
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


# Shared across every tiered-config consumer (not attendance-specific
# despite the name pattern) -- a future Activity Log feature subscribes
# via @receiver in its own signals.py, the same way payroll/signals.py
# already does for its own concerns; this module never needs to know
# which consumers exist.
config_override_requested = Signal()  # kwargs: instance (a PendingConfigChange)
config_override_applied = Signal()  # kwargs: instance
config_override_cancelled = Signal()  # kwargs: instance


# resolve_for_employee() is a hot-path read (every clock-in/out), and
# these rows change about as rarely as anything in the project -- only
# when an admin edits a rule set. Cached per (model, company) using a
# version counter rather than direct key deletion: LocMem has no
# pattern-delete, and bumping a small counter invalidates every
# previously-cached (department, employee_type_category) combination for
# that company at once, with no need to enumerate them.
# TTL itself comes from settings.CACHE_TTL_SECONDS (horilla/settings/
# base.py, env-overridable) -- read live via `settings.` at each use, not
# cached into a module-level constant, so @override_settings in tests
# actually takes effect.


def _version_cache_key(model_label, company_id):
    return f"tiered_config_version:{model_label}:{company_id}"


def _current_version(model_label, company_id):
    key = _version_cache_key(model_label, company_id)
    version = cache.get(key)
    if version is None:
        version = 0
        cache.set(key, version, None)
    return version


def _bump_version(model_label, company_id):
    key = _version_cache_key(model_label, company_id)
    try:
        cache.incr(key)
    except ValueError:
        # Nothing cached yet (or it expired) -- next reader starts a
        # fresh counter at 1, which is still a change from whatever
        # version any stale entry might have been cached under.
        cache.set(key, 1, None)


@receiver(post_save)
@receiver(post_delete)
def _bust_tiered_config_cache(sender, instance, **kwargs):
    """
    Deliberately unfiltered by `sender` -- new tiered-config models get
    cache invalidation for free just by inheriting the mixin, with no
    per-model signal wiring to remember. The isinstance check costs
    nothing next to an actual query, so running it on every model's
    save/delete project-wide is a fine trade for that.
    """
    if not isinstance(instance, TieredConfigResolutionMixin):
        return
    company_id = getattr(instance, "company_id", None)
    if company_id is None:
        return
    _bump_version(type(instance)._meta.label_lower, company_id)


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
                           (not tier/company/employee_type_category/
                           department/is_active bookkeeping).
      INHERITED_FIELDS -- the subset of RULE_FIELDS that a non-Company-
                           Default row may leave blank to inherit from the
                           Company Default row, rather than override.
                           Empty tuple means "always full override."

    Expects the concrete model to name its scope fields exactly `company`,
    `employee_type_category`, and `department` -- resolve_for_employee()
    filters on those names directly.
    """

    RULE_FIELDS: tuple = ()
    INHERITED_FIELDS: tuple = ()

    @classmethod
    def resolve_for_employee(cls, employee):
        """
        The single effective CURRENT row for `employee`: Department
        override, else Employee-Type override, else Company Default, else
        None. Cached per (model, company, department, employee_type_
        category) -- see the version-counter helpers above for the
        invalidation story. An employee moved between departments/types
        still picks up the correct override on the very next call: the
        cache key itself is keyed on their *current* department/type, not
        remembered from a prior call.
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

        model_label = cls._meta.label_lower
        version = _current_version(model_label, company.pk)
        cache_key = (
            f"tiered_config:{model_label}:{company.pk}:"
            f"{getattr(department, 'pk', None)}:{employee_type_category}:{version}"
        )
        cached_pk = cache.get(cache_key, "MISS")
        if cached_pk != "MISS":
            if cached_pk is None:
                return None
            row = cls.objects.filter(pk=cached_pk).first()
            if row is not None:
                return row
            # The cached pk no longer exists (deleted without going
            # through a signal-visible save/delete on this instance, or a
            # bulk .delete() that skipped signals) -- fall through to a
            # live resolve and refresh the cache below rather than trust it.

        if department is not None:
            row = cls.objects.filter(
                tier=TIER_DEPARTMENT, company=company, department=department,
                is_active=True,
            ).first()
            if row is not None:
                cache.set(cache_key, row.pk, settings.CACHE_TTL_SECONDS)
                return row

        if employee_type_category is not None:
            row = cls.objects.filter(
                tier=TIER_EMPLOYEE_TYPE, company=company,
                employee_type_category=employee_type_category, is_active=True,
            ).first()
            if row is not None:
                cache.set(cache_key, row.pk, settings.CACHE_TTL_SECONDS)
                return row

        row = cls.objects.filter(
            tier=TIER_COMPANY, company=company, is_active=True,
        ).first()
        cache.set(cache_key, row.pk if row is not None else None, settings.CACHE_TTL_SECONDS)
        return row

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
