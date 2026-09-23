from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from geopy.geocoders import Nominatim

from base.config_tiers import (
    TIER_CHOICES,
    TIER_COMPANY,
    TIER_DEPARTMENT,
    TIER_EMPLOYEE_TYPE,
    TieredConfigResolutionMixin,
)
from base.models import COLLAR_CATEGORY_CHOICES, Company, Department


class GeoFencing(TieredConfigResolutionMixin, models.Model):
    """
    Company boundary (Geo-mark) config -- tiered exactly like
    AttendanceRuleSet (see base/config_tiers.py), with one deliberate
    asymmetry: the Employee-Type tier here isn't an alternate boundary,
    it's a flat EXEMPTION from enforcement entirely for that
    classification (see is_exemption()) -- latitude/longitude/radius are
    meaningless on those rows and must stay blank. Department always
    wins over an Employee-Type exemption if both could apply to the same
    employee (the shared mixin's own precedence), so a department that's
    configured its own boundary still enforces it even for an otherwise-
    exempt collar category -- "most specific wins," same as everywhere
    else this pattern is used.
    """

    ENFORCEMENT_REJECT = "REJECT"
    ENFORCEMENT_FLAG = "FLAG"
    ENFORCEMENT_CHOICES = (
        (ENFORCEMENT_REJECT, _("Reject the punch")),
        (ENFORCEMENT_FLAG, _("Allow the punch and flag for review")),
    )

    RULE_FIELDS = (
        "latitude",
        "longitude",
        "radius_in_meters",
        "start",
        "enforcement_mode",
    )
    # A Department override is a full replacement boundary, not a partial
    # one -- nothing here is inherited piecemeal from Company Default.
    INHERITED_FIELDS = ()

    tier = models.CharField(
        max_length=15, choices=TIER_CHOICES, default=TIER_COMPANY,
        verbose_name=_("Tier"),
    )
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="geo_fencing_rules",
        verbose_name=_("Company"),
    )
    # Not a FK to a specific EmployeeType row, same reasoning as
    # AttendanceRuleSet.employee_type_category: EmployeeType names stay
    # free-form, but this tier only ever has three possible exemptions.
    employee_type_category = models.CharField(
        max_length=15, choices=COLLAR_CATEGORY_CHOICES, null=True, blank=True,
        verbose_name=_("Employee Type (exempt)"),
    )
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, null=True, blank=True,
        related_name="geo_fencing_rules", verbose_name=_("Department"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Is Active"))

    # Boundary fields -- required for Company Default / Department tiers,
    # must stay blank for the Employee-Type exemption tier (see clean()).
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    radius_in_meters = models.IntegerField(null=True, blank=True)
    start = models.BooleanField(default=False, verbose_name=_("Enforcement Started"))
    enforcement_mode = models.CharField(
        max_length=10, choices=ENFORCEMENT_CHOICES, default=ENFORCEMENT_REJECT,
        verbose_name=_("On Violation"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=Q(tier=TIER_COMPANY),
                name="uniq_geofencing_company_tier",
            ),
            models.UniqueConstraint(
                fields=["company", "employee_type_category"],
                condition=Q(tier=TIER_EMPLOYEE_TYPE),
                name="uniq_geofencing_employee_type_tier",
            ),
            models.UniqueConstraint(
                fields=["company", "department"],
                condition=Q(tier=TIER_DEPARTMENT),
                name="uniq_geofencing_department_tier",
            ),
        ]

    def is_exemption(self):
        """True for the Employee-Type tier: skip enforcement entirely."""
        return self.tier == TIER_EMPLOYEE_TYPE

    def clean(self):
        if self.tier == TIER_COMPANY:
            if self.employee_type_category or self.department_id:
                raise ValidationError(
                    _(
                        "A Company Default row cannot have an employee type "
                        "or department set."
                    )
                )
            self._validate_boundary_fields_present()
        elif self.tier == TIER_EMPLOYEE_TYPE:
            if not self.employee_type_category or self.department_id:
                raise ValidationError(
                    _(
                        "An Employee-Type exemption must set an employee "
                        "type and leave department blank."
                    )
                )
            self._validate_boundary_fields_blank()
        elif self.tier == TIER_DEPARTMENT:
            if not self.department_id or self.employee_type_category:
                raise ValidationError(
                    _(
                        "A Department override must set a department and "
                        "leave employee type blank."
                    )
                )
            if (
                self.company_id
                and self.department_id
                and not self.department.company_id.filter(pk=self.company_id).exists()
            ):
                raise ValidationError(
                    _("This department is not assigned to this company.")
                )
            self._validate_boundary_fields_present()

        return super().clean()

    def _validate_boundary_fields_present(self):
        if self.latitude is None or self.longitude is None or self.radius_in_meters is None:
            raise ValidationError(
                _("A boundary row must set latitude, longitude, and radius.")
            )
        self._validate_coordinates_resolve()

    def _validate_boundary_fields_blank(self):
        if (
            self.latitude is not None
            or self.longitude is not None
            or self.radius_in_meters is not None
        ):
            raise ValidationError(
                _(
                    "An Employee-Type exemption is not a boundary -- leave "
                    "latitude, longitude, and radius blank."
                )
            )

    def _validate_coordinates_resolve(self):
        geolocator = Nominatim(
            user_agent="geo_checker_unique"
        )  # Unique user-agent is important
        try:
            location = geolocator.reverse(
                (self.latitude, self.longitude), exactly_one=True
            )
            if not location:
                raise ValidationError(_("Invalid location coordinates."))
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError(f"Geolocation error: {e}")

    def save(self, *args, **kwargs):
        self.full_clean()  # Run clean before save
        super().save(*args, **kwargs)

    def __str__(self):
        if self.tier == TIER_COMPANY:
            return f"{self.company} — Company Default boundary"
        if self.tier == TIER_EMPLOYEE_TYPE:
            return f"{self.company} — {self.get_employee_type_category_display()} exemption"
        return f"{self.company} — {self.department} boundary"
