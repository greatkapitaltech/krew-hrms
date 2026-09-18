"""
krew_company_onboarding/signals.py

Grants the one fine-grained permission slice that base/signals.py's
_DEFAULT_HRMS_GROUPS helper can't express, since that helper only grants
permissions at whole-app granularity:

- "Ventura HR" needs base.view_company (so the "Company Setup" sidebar
  entry is visible -- krew_company_onboarding/sidebar.py's
  menu_accessibility checks it -- even though HR is otherwise read-only on
  company data), plus add/change on just
  CompanyBrandedTemplate/CompanyDocument within krew_company_onboarding
  itself -- _DEFAULT_HRMS_GROUPS' app_actions is app-level, not model-level,
  so it can't isolate to just these two models.

"Ventura Admin" needs no special-case here: it gets full `base` access
(including Company) plus full krew_company_onboarding directly through
_DEFAULT_HRMS_GROUPS. "Ventura HR" gets its krew_company_onboarding
view-only baseline the same way — this module only adds the slice that
helper structurally cannot express.
"""

from django.contrib.auth.models import Group, Permission
from django.db.models.signals import post_migrate
from django.dispatch import receiver


@receiver(post_migrate)
def grant_ventura_hr_branding_perms(sender, **kwargs):
    if getattr(sender, "label", None) != "krew_company_onboarding":
        return
    group, _created = Group.objects.get_or_create(name="Ventura HR")
    perms = Permission.objects.filter(
        content_type__app_label="base",
        content_type__model="company",
        codename="view_company",
    ) | Permission.objects.filter(
        content_type__app_label="krew_company_onboarding",
        content_type__model__in=("companybrandedtemplate", "companydocument"),
        codename__startswith="add_",
    ) | Permission.objects.filter(
        content_type__app_label="krew_company_onboarding",
        content_type__model__in=("companybrandedtemplate", "companydocument"),
        codename__startswith="change_",
    )
    group.permissions.add(*perms)
