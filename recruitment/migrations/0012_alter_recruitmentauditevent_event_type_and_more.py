"""
Feature 2 schema: screening-question snapshots and per-answer rows.

HAND-EDITED -- do not regenerate. SurveyTemplateQuestion ADOPTS the existing
`recruitment_recruitmentsurvey_template_id` join table, which holds live
relationships. The autodetector instead emits CreateModel against that same
table plus an AlterField on the M2M, which drops and recreates it and loses
every row.

SeparateDatabaseAndState therefore declares the model and `through=` in state
only (table, PK, FKs and unique constraint already match), and the state
CreateModel lists only the three existing columns so the subsequent AddFields
for is_mandatory/sequence are real ALTER TABLE statements rather than
duplicates.

Reverse drops those two columns; the join table and its rows remain.
"""

import django.db.models.deletion
import horilla.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0011_recruitment_budget_max_recruitment_budget_min_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ------------------------------------------------------------------
        # 1. New audit event types (choices only -- no column change).
        # ------------------------------------------------------------------
        migrations.AlterField(
            model_name="recruitmentauditevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("JOB_OPENING_CREATED", "Job opening created"),
                    ("JOB_OPENING_UPDATED", "Job opening updated"),
                    (
                        "JOB_OPENING_SUBMITTED_FOR_REVIEW",
                        "Job opening submitted for review",
                    ),
                    (
                        "JOB_OPENING_SENT_BACK_FOR_CHANGES",
                        "Job opening sent back for changes",
                    ),
                    ("JOB_OPENING_PUBLISHED", "Job opening published"),
                    ("JOB_OPENING_CLOSED", "Job opening closed"),
                    ("JOB_OPENING_REMOVED", "Job opening removed"),
                    ("QUESTION_CREATED", "Screening question created"),
                    ("QUESTION_UPDATED", "Screening question updated"),
                    ("QUESTION_ARCHIVED", "Screening question archived"),
                    ("TEMPLATE_CREATED", "Screening template created"),
                    ("TEMPLATE_UPDATED", "Screening template updated"),
                    ("QUESTION_ADDED_TO_TEMPLATE", "Question added to template"),
                    (
                        "QUESTION_REMOVED_FROM_TEMPLATE",
                        "Question removed from template",
                    ),
                    ("QUESTION_MADE_MANDATORY", "Question made mandatory in template"),
                    ("QUESTION_MADE_OPTIONAL", "Question made optional in template"),
                    (
                        "JOB_OPENING_QUESTIONS_SNAPSHOTTED",
                        "Job opening questions snapshotted",
                    ),
                    (
                        "CANDIDATE_ANSWER_SUBMITTED",
                        "Candidate screening answers submitted",
                    ),
                    ("CANDIDATE_ANSWER_UPDATED", "Candidate screening answers updated"),
                ],
                db_index=True,
                max_length=64,
                verbose_name="Event",
            ),
        ),
        # ------------------------------------------------------------------
        # 2. The immutable snapshot table (genuinely new).
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name="JobOpeningQuestion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("wording", models.TextField(verbose_name="Question")),
                (
                    "question_type",
                    models.CharField(
                        choices=[
                            ("checkbox", "Yes/No"),
                            ("options", "Choices"),
                            ("multiple", "Multiple Choice"),
                            ("text", "Text"),
                            ("number", "Number"),
                            ("percentage", "Percentage"),
                            ("date", "Date"),
                            ("textarea", "Textarea"),
                            ("file", "File Upload"),
                            ("rating", "Rating"),
                        ],
                        max_length=15,
                        verbose_name="Type",
                    ),
                ),
                ("options", models.TextField(blank=True, default="", null=True)),
                (
                    "is_mandatory",
                    models.BooleanField(default=False, verbose_name="Is Mandatory"),
                ),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("is_reconstructed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "job_opening",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="snapshot_questions",
                        to="recruitment.recruitment",
                        verbose_name="Job Opening",
                    ),
                ),
                (
                    "source_question",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="snapshots",
                        to="recruitment.recruitmentsurvey",
                        verbose_name="Source Question",
                    ),
                ),
            ],
            options={
                "verbose_name": "Job Opening Question",
                "verbose_name_plural": "Job Opening Questions",
                "ordering": ["job_opening", "display_order", "id"],
            },
        ),
        # ------------------------------------------------------------------
        # 3. One row per answer (genuinely new). The old
        #    RecruitmentSurveyAnswer.answer_json blob is deliberately left in
        #    place -- the data migration reads it and must not destroy it.
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name="CandidateAnswer",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, null=True, verbose_name="Created At"
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="Is Active")),
                ("answer", models.TextField(blank=True, default="")),
                (
                    "attachment",
                    models.FileField(
                        blank=True, null=True, upload_to=horilla.models.upload_path
                    ),
                ),
                (
                    "candidate",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="screening_answers",
                        to="recruitment.candidate",
                        verbose_name="Candidate",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "modified_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_modified_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Modified By",
                    ),
                ),
                (
                    "job_opening_question",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="answers",
                        to="recruitment.jobopeningquestion",
                        verbose_name="Question",
                    ),
                ),
            ],
            options={
                "verbose_name": "Candidate Answer",
                "verbose_name_plural": "Candidate Answers",
                "ordering": ["job_opening_question__display_order", "id"],
            },
        ),
        # ------------------------------------------------------------------
        # 4. Adopt the existing M2M join table as an explicit through model.
        #    STATE ONLY -- see the module docstring. No database operations,
        #    because the table, PK, FKs and unique constraint already exist.
        # ------------------------------------------------------------------
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="SurveyTemplateQuestion",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        (
                            "recruitmentsurvey",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="recruitment.recruitmentsurvey",
                                verbose_name="Question",
                            ),
                        ),
                        (
                            "surveytemplate",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="recruitment.surveytemplate",
                                verbose_name="Template",
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Template Question",
                        "verbose_name_plural": "Template Questions",
                        "db_table": "recruitment_recruitmentsurvey_template_id",
                        "ordering": ["sequence", "id"],
                        # Already enforced in the database by the constraint
                        # Django created for the implicit through table.
                        "unique_together": {("recruitmentsurvey", "surveytemplate")},
                    },
                ),
                migrations.AlterField(
                    model_name="recruitmentsurvey",
                    name="template_id",
                    field=models.ManyToManyField(
                        blank=True,
                        through="recruitment.SurveyTemplateQuestion",
                        to="recruitment.surveytemplate",
                        verbose_name="Template",
                    ),
                ),
            ],
            database_operations=[],
        ),
        # ------------------------------------------------------------------
        # 5. The only real schema change to the adopted table: two new
        #    columns, both with defaults so existing rows backfill silently.
        #    Defaults match the previous behaviour, where mandatory/sequence
        #    came from RecruitmentSurvey; the data migration then copies each
        #    question's own values onto its template rows.
        # ------------------------------------------------------------------
        migrations.AddField(
            model_name="surveytemplatequestion",
            name="is_mandatory",
            field=models.BooleanField(default=False, verbose_name="Is Mandatory"),
        ),
        migrations.AddField(
            model_name="surveytemplatequestion",
            name="sequence",
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        # ------------------------------------------------------------------
        # 6. Indexes and constraints on the new tables.
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name="jobopeningquestion",
            index=models.Index(
                fields=["job_opening", "display_order"],
                name="recruitment_job_ope_7f610c_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="jobopeningquestion",
            constraint=models.UniqueConstraint(
                fields=("job_opening", "source_question"),
                name="uniq_job_opening_source_question",
            ),
        ),
        migrations.AddConstraint(
            model_name="candidateanswer",
            constraint=models.UniqueConstraint(
                fields=("candidate", "job_opening_question"),
                name="uniq_candidate_job_opening_question",
            ),
        ),
    ]
