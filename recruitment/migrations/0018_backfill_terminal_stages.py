"""
Backfill the terminal pipeline stages for job openings that already exist.

Feature 4's pipeline is Applied -> custom stages -> Final HR Round -> Hired.
recruitment.signals.create_initial_stage now seeds the terminal pair for every
NEW opening. Openings created before that have neither, which breaks two things
outright: the hiring handoff is only offered at Final HR Round (so it can never
be reached), and hire_candidate() raises InvalidStageTransition because there is
no stage of type "hired" to move the candidate into.

Additive and idempotent:

  * stages are matched on stage_type, not on name. Stage.Meta.unique_together is
    ("recruitment_id", "stage"), so a name-keyed check would allow a second
    Hired stage under a different name, and nothing in the schema forbids two
    stages sharing a type;
  * existing stages -- including custom ones -- keep their names and sequences
    untouched;
  * re-running creates nothing.

The stage_type values, names and sequences are written literally here rather
than imported from recruitment.models: a migration must keep doing the same
thing even if those constants are later renamed or re-valued.
"""

from django.db import migrations

#: Mirrors recruitment.models.TERMINAL_STAGE_DEFAULTS at the time of writing.
TERMINAL_STAGES = (
    ("final_hr_round", "Final HR Round", 9998),
    ("hired", "Hired", 9999),
)


def seed_terminal_stages(apps, schema_editor):
    Recruitment = apps.get_model("recruitment", "Recruitment")
    Stage = apps.get_model("recruitment", "Stage")

    created_total = 0
    openings_touched = 0
    renamed = []

    for opening in Recruitment.objects.all().iterator():
        created_here = 0
        for stage_type, default_name, sequence in TERMINAL_STAGES:
            if Stage.objects.filter(
                recruitment_id=opening, stage_type=stage_type
            ).exists():
                continue

            # The name may already belong to a custom stage of another type,
            # and the unique key is on the name -- so fall back rather than
            # raising IntegrityError mid-migration.
            stage_name = default_name
            if Stage.objects.filter(
                recruitment_id=opening, stage=stage_name
            ).exists():
                stage_name = f"{default_name} (stage)"
                renamed.append((opening.pk, default_name, stage_name))

            Stage.objects.create(
                recruitment_id=opening,
                stage=stage_name,
                stage_type=stage_type,
                sequence=sequence,
            )
            created_here += 1

        created_total += created_here
        if created_here:
            openings_touched += 1

    print(
        f"  terminal stage backfill: {created_total} stage(s) added across "
        f"{openings_touched} job opening(s)"
    )
    if renamed:
        print(f"        name collisions resolved: {renamed}")


def unseed(apps, schema_editor):
    """
    Deliberately a no-op.

    Deleting these stages on reverse would orphan any candidate standing in
    them (Candidate.stage_id is a FK), and a reverse migration must not destroy
    business data. The forward pass is idempotent, so re-applying is safe.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0017_applicationcontactverification_otp_attempts"),
    ]

    operations = [
        migrations.RunPython(seed_terminal_stages, unseed),
    ]
