"""
Retire the default "Initial" stage from existing job openings.

The PRD gives a new opening three stages -- Applied -> (custom stages) ->
Final HR Round -> Hired -- and Applied is the entry stage every application
lands in (recruitment.services.candidate.entry_stage).
recruitment.signals.create_initial_stage no longer seeds a fourth, upstream
"Initial" stage. Openings created before that carry one, so the pipeline shows
four stages where the PRD specifies three.

Conservative by design: this deletes a stage only when deleting it cannot lose
anything. A stage is skipped -- and left exactly as it is -- if it

  * holds any candidate (Candidate.stage_id is PROTECT, so deleting would raise
    mid-migration anyway);
  * carries any stage note (StageNote.stage_id is CASCADE -- the notes would be
    deleted with it);
  * is referenced by any candidate note or audit event (both SET_NULL -- the
    history would lose which stage it happened in);
  * is the opening's ONLY stage, which would leave a pipeline with no stages at
    all.

A client that genuinely uses an Initial stage therefore keeps it, and the
"initial" stage_type stays a valid Stage.stage_types choice.

Stage types are written literally rather than imported from recruitment.models:
a migration must keep doing the same thing even if those constants are later
renamed or re-valued.
"""

from django.db import migrations

INITIAL_STAGE_TYPE = "initial"


def retire_initial_stages(apps, schema_editor):
    Stage = apps.get_model("recruitment", "Stage")
    Candidate = apps.get_model("recruitment", "Candidate")
    CandidateNote = apps.get_model("recruitment", "CandidateNote")
    StageNote = apps.get_model("recruitment", "StageNote")
    RecruitmentAuditEvent = apps.get_model("recruitment", "RecruitmentAuditEvent")

    deleted = 0
    kept = []

    for stage in Stage.objects.filter(stage_type=INITIAL_STAGE_TYPE).iterator():
        reason = None
        if Candidate.objects.filter(stage_id=stage).exists():
            reason = "holds candidates"
        elif StageNote.objects.filter(stage_id=stage).exists():
            reason = "has stage notes"
        elif CandidateNote.objects.filter(stage_id=stage).exists():
            reason = "referenced by candidate notes"
        elif RecruitmentAuditEvent.objects.filter(stage=stage).exists():
            reason = "referenced by audit history"
        elif Stage.objects.filter(recruitment_id=stage.recruitment_id).count() <= 1:
            reason = "only stage in the pipeline"

        if reason:
            kept.append((stage.recruitment_id_id, stage.stage, reason))
            continue

        stage.delete()
        deleted += 1

    print(f"  default Initial stage retired: {deleted} stage(s) removed")
    if kept:
        print(f"        kept (still in use): {kept}")


def restore(apps, schema_editor):
    """
    Deliberately a no-op.

    Re-creating an Initial stage on reverse would put back the fourth stage the
    PRD does not have, and the sequence it originally held is not recoverable.
    The forward pass is idempotent -- once removed there is nothing to match.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0018_backfill_terminal_stages"),
    ]

    operations = [
        migrations.RunPython(retire_initial_stages, restore),
    ]
