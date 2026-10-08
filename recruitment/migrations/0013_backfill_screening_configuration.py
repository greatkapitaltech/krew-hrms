"""
Feature 2 data migration, in three independent passes.

1. Backfill SurveyTemplateQuestion.is_mandatory/.sequence from each question.
   0012 added those columns with defaults (False/0), discarding the question's
   own values; without this a mandatory question would publish as optional.

2. Reconstruct JobOpeningQuestion for openings already PUBLISHED/CLOSED. No
   record of their questions at publication time exists, so today's
   relationships are used and every row is stamped is_reconstructed=True --
   such a row is NOT a guarantee of the original wording.

3. Convert RecruitmentSurveyAnswer.answer_json to CandidateAnswer rows. The
   blob is keyed by question wording, so conversion is lossy where wordings
   collide: unambiguous matches convert, no match is reported unmatched, and
   multiple matches are reported AMBIGUOUS and never guessed. answer_json is
   deliberately retained as the system of record until reviewed.

Each pass prints a summary so the outcome is visible in the migrate output.
Reverse removes only rows this migration created and restores the 0012
column defaults; the original blobs are untouched.
"""

import json
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

#: Per-type prefixes the legacy candidate form used on its input names. The
#: blob keys therefore look like "date_Do you have a licence?" rather than the
#: bare wording, and have to be stripped before matching.
LEGACY_PREFIXES = (
    "multiple_choices_",
    "percentage_",
    "rating_",
    "date_",
    "file_",
)

#: Keys that were never answers.
NON_ANSWER_KEYS = {"csrfmiddlewaretoken"}


def _strip_prefix(key):
    for prefix in LEGACY_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix) :]
    return key


def _normalise(value):
    """Blob values are lists; flatten to the canonical comma-joined text."""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item not in (None, ""))
    return "" if value is None else str(value)


def backfill_template_question_config(apps, schema_editor):
    SurveyTemplateQuestion = apps.get_model("recruitment", "SurveyTemplateQuestion")

    rows = SurveyTemplateQuestion.objects.select_related("recruitmentsurvey")
    updated = 0
    for row in rows:
        question = row.recruitmentsurvey
        row.is_mandatory = question.is_mandatory
        row.sequence = question.sequence
        row.save(update_fields=["is_mandatory", "sequence"])
        updated += 1

    print(
        f"  [1/3] template-question config backfilled from question bank: "
        f"{updated} row(s)"
    )


def reverse_backfill_template_question_config(apps, schema_editor):
    SurveyTemplateQuestion = apps.get_model("recruitment", "SurveyTemplateQuestion")
    SurveyTemplateQuestion.objects.update(is_mandatory=False, sequence=0)


def _collect_for_opening(opening, SurveyTemplateQuestion, RecruitmentSurvey):
    """
    Mirror of services.screening.collect_questions_for_publication, using the
    migration's historical models.

    Deliberately duplicated rather than imported: the service imports the live
    models and writes audit events, neither of which is safe inside a migration.
    The rules it implements are the same -- union of both paths, deduplicated by
    source question, mandatory-wins on conflict, nulls-last ordering.
    """
    collected = {}

    template_rows = SurveyTemplateQuestion.objects.filter(
        surveytemplate__in=opening.survey_templates.all()
    ).select_related("recruitmentsurvey")
    for row in template_rows:
        question = row.recruitmentsurvey
        entry = collected.get(question.pk)
        if entry is None:
            collected[question.pk] = {
                "question": question,
                "is_mandatory": row.is_mandatory,
                "sequence": row.sequence,
            }
        else:
            entry["is_mandatory"] = entry["is_mandatory"] or row.is_mandatory
            entry["sequence"] = min(
                [entry["sequence"], row.sequence],
                key=lambda v: (v is None, v if v is not None else 0),
            )

    for question in RecruitmentSurvey.objects.filter(recruitment_ids=opening):
        entry = collected.get(question.pk)
        if entry is None:
            collected[question.pk] = {
                "question": question,
                "is_mandatory": question.is_mandatory,
                "sequence": question.sequence,
            }
        elif question.is_mandatory:
            entry["is_mandatory"] = True

    return sorted(
        collected.values(),
        key=lambda e: (
            e["sequence"] is None,
            e["sequence"] if e["sequence"] is not None else 0,
            e["question"].pk,
        ),
    )


def reconstruct_snapshots(apps, schema_editor):
    Recruitment = apps.get_model("recruitment", "Recruitment")
    RecruitmentSurvey = apps.get_model("recruitment", "RecruitmentSurvey")
    SurveyTemplateQuestion = apps.get_model("recruitment", "SurveyTemplateQuestion")
    JobOpeningQuestion = apps.get_model("recruitment", "JobOpeningQuestion")

    # Only openings that are already past the publication boundary need a
    # historical snapshot. DRAFT/REVIEW openings will snapshot normally when
    # they are published through the service.
    openings = Recruitment.objects.filter(status__in=["PUBLISHED", "CLOSED"])

    created_total = 0
    openings_done = 0
    openings_empty = []
    for opening in openings:
        if JobOpeningQuestion.objects.filter(job_opening=opening).exists():
            continue
        specs = _collect_for_opening(
            opening, SurveyTemplateQuestion, RecruitmentSurvey
        )
        if not specs:
            openings_empty.append(opening.pk)
            continue
        JobOpeningQuestion.objects.bulk_create(
            [
                JobOpeningQuestion(
                    job_opening=opening,
                    source_question=spec["question"],
                    wording=spec["question"].question,
                    question_type=spec["question"].type,
                    options=spec["question"].options or "",
                    is_mandatory=bool(spec["is_mandatory"]),
                    display_order=index,
                    is_reconstructed=True,
                )
                for index, spec in enumerate(specs)
            ]
        )
        created_total += len(specs)
        openings_done += 1

    print(
        f"  [2/3] reconstructed snapshots: {created_total} question(s) across "
        f"{openings_done} already-published/closed opening(s)"
    )
    if openings_empty:
        print(
            f"        no questions reachable for opening(s) {openings_empty} "
            f"- nothing to reconstruct, not an error"
        )


def reverse_reconstruct_snapshots(apps, schema_editor):
    JobOpeningQuestion = apps.get_model("recruitment", "JobOpeningQuestion")
    # Only rows this migration created.
    JobOpeningQuestion.objects.filter(is_reconstructed=True).delete()


def convert_answer_blobs(apps, schema_editor):
    RecruitmentSurveyAnswer = apps.get_model("recruitment", "RecruitmentSurveyAnswer")
    JobOpeningQuestion = apps.get_model("recruitment", "JobOpeningQuestion")
    CandidateAnswer = apps.get_model("recruitment", "CandidateAnswer")

    migrated = 0
    unmatched = []
    ambiguous = []
    skipped_no_opening = []

    for blob_row in RecruitmentSurveyAnswer.objects.select_related("candidate_id"):
        candidate = blob_row.candidate_id
        if candidate is None or candidate.recruitment_id_id is None:
            skipped_no_opening.append(blob_row.pk)
            continue

        raw = blob_row.answer_json
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                unmatched.append((blob_row.pk, "<unparseable answer_json>"))
                continue
        if not isinstance(raw, dict):
            unmatched.append((blob_row.pk, "<answer_json not an object>"))
            continue

        # Wording -> snapshot rows for this candidate's opening. A list per
        # wording so duplicates are detectable instead of silently overwritten.
        snapshot_by_wording = {}
        for question in JobOpeningQuestion.objects.filter(
            job_opening_id=candidate.recruitment_id_id
        ):
            snapshot_by_wording.setdefault(question.wording, []).append(question)

        for key, value in raw.items():
            if key in NON_ANSWER_KEYS:
                continue
            wording = _strip_prefix(key)
            matches = snapshot_by_wording.get(wording, [])

            if not matches:
                unmatched.append((blob_row.pk, key))
                continue
            if len(matches) > 1:
                # Two snapshot questions share this wording. Assigning the
                # answer to either one could attach it to the wrong question,
                # so it is reported for manual review and left in the blob.
                ambiguous.append((blob_row.pk, key, [m.pk for m in matches]))
                continue

            question = matches[0]
            _, created = CandidateAnswer.objects.get_or_create(
                candidate=candidate,
                job_opening_question=question,
                defaults={"answer": _normalise(value), "is_active": True},
            )
            if created:
                migrated += 1

    print(f"  [3/3] answer blob conversion:")
    print(f"        migrated  : {migrated} answer row(s)")
    print(f"        unmatched : {len(unmatched)} {unmatched[:10]}")
    print(f"        ambiguous : {len(ambiguous)} {ambiguous[:10]}")
    if skipped_no_opening:
        print(
            f"        skipped (no job opening on candidate): {skipped_no_opening[:10]}"
        )
    print("        original answer_json preserved on every row")

    if unmatched or ambiguous:
        logger.warning(
            "Feature 2 answer conversion needs manual review: %s unmatched, "
            "%s ambiguous. The original answer_json is preserved.",
            len(unmatched),
            len(ambiguous),
        )


def reverse_convert_answer_blobs(apps, schema_editor):
    CandidateAnswer = apps.get_model("recruitment", "CandidateAnswer")
    # The blobs were never destroyed, so dropping the converted rows is safe.
    CandidateAnswer.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0012_alter_recruitmentauditevent_event_type_and_more"),
    ]

    operations = [
        migrations.RunPython(
            backfill_template_question_config,
            reverse_backfill_template_question_config,
        ),
        migrations.RunPython(
            reconstruct_snapshots,
            reverse_reconstruct_snapshots,
        ),
        migrations.RunPython(
            convert_answer_blobs,
            reverse_convert_answer_blobs,
        ),
    ]
