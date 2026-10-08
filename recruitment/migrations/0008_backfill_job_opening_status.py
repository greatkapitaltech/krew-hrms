"""
Backfill Recruitment.status for job openings that existed before the
lifecycle was introduced.

Mapping is derived from each row's actual data rather than defaulting
everything to DRAFT, which would hide every live job opening:

    is_active = False                          -> REMOVED
    closed    = True                            -> CLOSED
    is_published = True  and closed = False     -> PUBLISHED
    is_published = False and closed = False     -> DRAFT

Deliberately does NOT rewrite is_published / closed / is_active. Those are
the values the existing screens, filters, dashboards and reports already
read, so leaving them untouched means this migration changes no visible
behaviour -- it only adds the authoritative status alongside them. From here
on the lifecycle service keeps all four in sync.

published_at / closed_at are left null for pre-existing rows because the
historical timestamps simply are not recorded anywhere; inventing them would
put false data into an audit-adjacent field.
"""

from django.db import migrations


def set_initial_status(apps, schema_editor):
    Recruitment = apps.get_model("recruitment", "Recruitment")

    # Most specific first; each filter is mutually exclusive of the others,
    # so ordering only matters for the is_active=False case which overrides
    # whatever the published/closed flags say.
    Recruitment.objects.filter(is_active=False).update(status="REMOVED")
    Recruitment.objects.filter(is_active=True, closed=True).update(status="CLOSED")
    Recruitment.objects.filter(
        is_active=True, closed=False, is_published=True
    ).update(status="PUBLISHED")
    Recruitment.objects.filter(
        is_active=True, closed=False, is_published=False
    ).update(status="DRAFT")


def reverse_initial_status(apps, schema_editor):
    """
    No-op. The status column itself is removed by reversing 0007, and the
    mirror fields were never modified, so there is nothing to restore.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0007_recruitment_closed_at_recruitment_closed_by_and_more"),
    ]

    operations = [
        migrations.RunPython(set_initial_status, reverse_initial_status),
    ]
