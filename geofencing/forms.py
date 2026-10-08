from django import forms
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

from base.config_tiers import TIER_CHOICES, TIER_COMPANY, TIER_DEPARTMENT
from base.forms import ModelForm
from base.models import Department

from .models import GeoFencing


class GeoFencingSetupForm(ModelForm):
    """
    The pre-existing single-row form embedded in the "Attendance Rules"
    settings page's Geofencing accordion -- always edits/creates the one
    Company Default row (geo_location_config() hardcodes
    `tier = TIER_COMPANY` on every save, unconditionally). tier/
    employee_type_category/department are excluded here rather than just
    hidden: on this screen they're not merely redundant, they're actively
    misleading -- whatever a user picked for them would be silently
    discarded by that hardcoded overwrite, with no indication it
    happened. The tiers this form can't reach (Employee-Type Exemption,
    Department Override) have their own separate screen now -- see
    geofencing/cbv/geo_fencing_rule.py.
    """

    verbose_name = _("Geofence Configuration")

    class Meta:
        model = GeoFencing
        exclude = ["company", "tier", "employee_type_category", "department"]


class GeoFencingRuleForm(ModelForm):
    """
    The settings screen for GeoFencing, deliberately scoped to two of
    its three tiers -- Company Default and Department Override.
    Employee-Type Exemption is excluded here by explicit request (kept
    on the model, per its own TieredConfigResolutionMixin/is_exemption()
    machinery, and still reachable through the ORM/API -- just not from
    this form): `tier`'s choices are narrowed and `employee_type_category`
    isn't a field on this form at all.

    Unlike AttendanceRuleSetForm's screen, this one saves directly:
    GeoFencing has never gone through PendingConfigChange, so there's no
    "changes apply from the 1st of next month" delay here. `tier`/
    `department` pick WHICH row is being configured and, once a row
    exists, never change again -- disabled on an edit, same reasoning as
    AttendanceRuleSetForm.

    A save here can make a real outbound geocoding request (GeoFencing.
    save() -> full_clean() -> Nominatim.reverse() whenever latitude/
    longitude are set) -- not something this form controls, just worth
    knowing why a save can be slow or fail on a network hiccup.
    """

    class Meta:
        model = GeoFencing
        fields = [
            "tier", "department",
            "latitude", "longitude", "radius_in_meters",
            "start", "enforcement_mode",
        ]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.fields["start"].help_text = _(
            "Off leaves this row configured but not yet enforced."
        )
        # NOT done via Meta.widgets -- ChoiceField.choices is a property
        # whose setter also writes through to widget.choices, and
        # ModelForm's own field construction sets that (from the model's
        # full TIER_CHOICES) *after* Meta.widgets builds the widget
        # instance, silently overwriting any choices given there. Setting
        # it here, after super().__init__(), is what actually sticks.
        self.fields["tier"].choices = [
            choice for choice in TIER_CHOICES if choice[0] in (TIER_COMPANY, TIER_DEPARTMENT)
        ]
        if self.instance.pk is None:
            self.instance.company = company
            self.fields["department"].queryset = (
                Department.objects.filter(company_id=company)
                if company is not None
                else Department.objects.none()
            )
        else:
            self.fields["tier"].disabled = True
            self.fields["department"].disabled = True
            self.fields["department"].queryset = Department.objects.filter(
                company_id=self.instance.company_id
            )

    def clean(self):
        cleaned_data = super().clean()
        instance = self.instance
        # This form never sets employee_type_category -- an existing
        # Employee-Type row can't reach this form at all (see class
        # docstring), and a new row here is always Company/Department,
        # neither of which uses it.
        instance.employee_type_category = None
        for field_name in self.Meta.fields:
            if field_name in cleaned_data:
                setattr(instance, field_name, cleaned_data[field_name])
        try:
            instance.clean()
        except ValidationError as error:
            raise forms.ValidationError(error.messages) from error
        return cleaned_data
