"""
Feature 3 schema: candidate company ownership, permanent notes, Final HR Round.

HAND-EDITED to add the company backfill -- do not regenerate.

The backfill must run in THIS migration: Candidate.objects becomes
HorillaCompanyManager("company_id") in the same change, and that manager passes
NULL-company rows through to every tenant, so an unfilled column would leave
every existing candidate visible company-wide.

Company comes from the candidate's job opening, the only authoritative source
(Candidate.save() also forces it on every write). Rows with no opening, or an
opening with no company, keep NULL and are listed in the migrate output to be
assigned deliberately -- guessing a tenant is how cross-company leaks happen.

HistoricalCandidate gets the column but no backfill: back-dating a company onto
a history row would fabricate audit data.

Reverse drops the column, so the backfill needs no separate reversal.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_candidate_company(apps, schema_editor):
    Candidate = apps.get_model("recruitment", "Candidate")

    # One UPDATE ... FROM rather than a per-row loop: this table can be large
    # in a client database, and the value is a pure function of the join.
    migrated = 0
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE recruitment_candidate AS cd
               SET company_id_id = r.company_id_id
              FROM recruitment_recruitment AS r
             WHERE r.id = cd.recruitment_id_id
               AND r.company_id_id IS NOT NULL
               AND cd.company_id_id IS DISTINCT FROM r.company_id_id
            """
        )
        migrated = cursor.rowcount

    unresolved_no_opening = list(
        Candidate.objects.filter(recruitment_id__isnull=True).values_list(
            "id", flat=True
        )[:50]
    )
    unresolved_no_company = list(
        Candidate.objects.filter(
            recruitment_id__isnull=False, recruitment_id__company_id__isnull=True
        ).values_list("id", flat=True)[:50]
    )

    print(f"  candidate company backfill: {migrated} row(s) set from job opening")
    if unresolved_no_opening:
        print(
            f"        UNRESOLVED - no job opening (company must be assigned "
            f"deliberately): {len(unresolved_no_opening)} e.g. {unresolved_no_opening[:10]}"
        )
    if unresolved_no_company:
        print(
            f"        UNRESOLVED - job opening has no company: "
            f"{len(unresolved_no_company)} e.g. {unresolved_no_company[:10]}"
        )
    if not unresolved_no_opening and not unresolved_no_company:
        print("        every candidate resolved to a company")


def noop_reverse(apps, schema_editor):
    """The column is dropped by the reverse of AddField; nothing to undo."""


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0018_company_pan_hash_unique'),
        ('recruitment', '0013_backfill_screening_configuration'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='candidate',
            name='company_id',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='candidates', to='base.company', verbose_name='Company'),
        ),
        migrations.AddField(
            model_name='historicalcandidate',
            name='company_id',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='base.company', verbose_name='Company'),
        ),
        migrations.AlterField(
            model_name='historicalstage',
            name='stage_type',
            field=models.CharField(choices=[('initial', 'Initial'), ('applied', 'Applied'), ('test', 'Test'), ('interview', 'Interview'), ('final_hr_round', 'Final HR Round'), ('cancelled', 'Cancelled'), ('hired', 'Hired')], default='interview', max_length=20, verbose_name='Stage Type'),
        ),
        migrations.AlterField(
            model_name='recruitmentauditevent',
            name='event_type',
            field=models.CharField(choices=[('JOB_OPENING_CREATED', 'Job opening created'), ('JOB_OPENING_UPDATED', 'Job opening updated'), ('JOB_OPENING_SUBMITTED_FOR_REVIEW', 'Job opening submitted for review'), ('JOB_OPENING_SENT_BACK_FOR_CHANGES', 'Job opening sent back for changes'), ('JOB_OPENING_PUBLISHED', 'Job opening published'), ('JOB_OPENING_CLOSED', 'Job opening closed'), ('JOB_OPENING_REMOVED', 'Job opening removed'), ('QUESTION_CREATED', 'Screening question created'), ('QUESTION_UPDATED', 'Screening question updated'), ('QUESTION_ARCHIVED', 'Screening question archived'), ('TEMPLATE_CREATED', 'Screening template created'), ('TEMPLATE_UPDATED', 'Screening template updated'), ('QUESTION_ADDED_TO_TEMPLATE', 'Question added to template'), ('QUESTION_REMOVED_FROM_TEMPLATE', 'Question removed from template'), ('QUESTION_MADE_MANDATORY', 'Question made mandatory in template'), ('QUESTION_MADE_OPTIONAL', 'Question made optional in template'), ('JOB_OPENING_QUESTIONS_SNAPSHOTTED', 'Job opening questions snapshotted'), ('CANDIDATE_ANSWER_SUBMITTED', 'Candidate screening answers submitted'), ('CANDIDATE_ANSWER_UPDATED', 'Candidate screening answers updated'), ('CANDIDATE_CREATED', 'Candidate created'), ('CANDIDATE_MAPPED_TO_JOB_OPENING', 'Candidate mapped to job opening'), ('CANDIDATE_DOCUMENT_UPLOADED', 'Candidate document uploaded'), ('CANDIDATE_NOTE_ADDED', 'Candidate note added'), ('CANDIDATE_STAGE_CHANGED', 'Candidate stage changed'), ('CANDIDATE_REJECTED', 'Candidate rejected'), ('CANDIDATE_HIRED', 'Candidate hired'), ('CANDIDATE_EXPORTED', 'Candidate data exported'), ('CANDIDATE_ANONYMIZED', 'Candidate anonymized'), ('DOCUMENT_RETENTION_CLEANUP', 'Document retention cleanup')], db_index=True, max_length=64, verbose_name='Event'),
        ),
        migrations.AlterField(
            model_name='stage',
            name='stage_type',
            field=models.CharField(choices=[('initial', 'Initial'), ('applied', 'Applied'), ('test', 'Test'), ('interview', 'Interview'), ('final_hr_round', 'Final HR Round'), ('cancelled', 'Cancelled'), ('hired', 'Hired')], default='interview', max_length=20, verbose_name='Stage Type'),
        ),
        migrations.CreateModel(
            name='CandidateNote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, null=True, verbose_name='Created At')),
                ('is_active', models.BooleanField(default=True, verbose_name='Is Active')),
                ('description', models.TextField(verbose_name='Note')),
                ('candidate_id', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='candidate_notes', to='recruitment.candidate', verbose_name='Candidate')),
                ('company_id', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='candidate_notes', to='base.company', verbose_name='Company')),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='Created By')),
                ('modified_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_modified_by', to=settings.AUTH_USER_MODEL, verbose_name='Modified By')),
                ('stage_id', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='candidate_notes', to='recruitment.stage', verbose_name='Stage')),
            ],
            options={
                'verbose_name': 'Candidate Note',
                'verbose_name_plural': 'Candidate Notes',
                'ordering': ['-created_at', '-id'],
                'indexes': [models.Index(fields=['candidate_id', '-created_at'], name='recruitment_candida_df2e0e_idx')],
            },
        ),
        # Must run in this migration: the manager becomes company-scoped in the
        # same change, and NULL company would otherwise be visible to everyone.
        migrations.RunPython(backfill_candidate_company, noop_reverse),
    ]
