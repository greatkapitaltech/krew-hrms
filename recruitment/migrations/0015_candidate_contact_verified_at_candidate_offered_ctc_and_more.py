"""
Feature 4 schema: form-type separation, Bank->Template freeze, contact
verification, handoff fields.

HAND-EDITED to add the template freeze backfill.

SurveyTemplateQuestion.wording/question_type/options are added blank, so until
they are populated the Bank->Template boundary is not actually frozen for rows
that predate it. SurveyTemplateQuestion.frozen() falls back to the live question
when a value is blank, so behaviour is unchanged either way -- the backfill is
what makes an existing template stop tracking later bank edits.
"""

import django.db.models.deletion
from django.db import migrations, models


def freeze_existing_template_questions(apps, schema_editor):
    SurveyTemplateQuestion = apps.get_model("recruitment", "SurveyTemplateQuestion")

    frozen = 0
    for row in SurveyTemplateQuestion.objects.select_related(
        "recruitmentsurvey"
    ).filter(wording=""):
        question = row.recruitmentsurvey
        row.wording = question.question
        row.question_type = question.type
        row.options = question.options or ""
        row.save(update_fields=["wording", "question_type", "options"])
        frozen += 1

    print(
        f"  template-question freeze: {frozen} row(s) captured from the "
        f"current question bank"
    )


def unfreeze_template_questions(apps, schema_editor):
    """Reverse clears the frozen copies; the columns themselves are dropped."""
    SurveyTemplateQuestion = apps.get_model("recruitment", "SurveyTemplateQuestion")
    SurveyTemplateQuestion.objects.update(wording="", question_type="", options="")


class Migration(migrations.Migration):

    dependencies = [
        ('recruitment', '0014_candidate_company_id_historicalcandidate_company_id_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='candidate',
            name='contact_verified_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='candidate',
            name='offered_ctc',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='CTC'),
        ),
        migrations.AddField(
            model_name='candidatedocument',
            name='job_opening_question',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='documents', to='recruitment.jobopeningquestion', verbose_name='Question'),
        ),
        migrations.AddField(
            model_name='historicalcandidate',
            name='contact_verified_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='historicalcandidate',
            name='offered_ctc',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='CTC'),
        ),
        migrations.AddField(
            model_name='jobopeningquestion',
            name='allow_multiple_files',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='jobopeningquestion',
            name='form_type',
            field=models.CharField(choices=[('form1', 'Application Form'), ('form2', 'Hiring Handoff Form')], default='form1', max_length=10, verbose_name='Form'),
        ),
        migrations.AddField(
            model_name='recruitmentsurvey',
            name='form_type',
            field=models.CharField(choices=[('form1', 'Application Form'), ('form2', 'Hiring Handoff Form')], default='form1', max_length=10, verbose_name='Form'),
        ),
        migrations.AddField(
            model_name='surveytemplate',
            name='form_type',
            field=models.CharField(choices=[('form1', 'Application Form'), ('form2', 'Hiring Handoff Form')], default='form1', max_length=10, verbose_name='Form'),
        ),
        migrations.AddField(
            model_name='surveytemplatequestion',
            name='options',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='surveytemplatequestion',
            name='question_type',
            field=models.CharField(blank=True, choices=[('checkbox', 'Yes/No'), ('options', 'Choices'), ('multiple', 'Multiple Choice'), ('text', 'Text'), ('number', 'Number'), ('percentage', 'Percentage'), ('date', 'Date'), ('textarea', 'Textarea'), ('file', 'File Upload'), ('rating', 'Rating')], default='', max_length=15),
        ),
        migrations.AddField(
            model_name='surveytemplatequestion',
            name='wording',
            field=models.TextField(blank=True, default='', verbose_name='Question'),
        ),
        migrations.AlterField(
            model_name='recruitmentauditevent',
            name='event_type',
            field=models.CharField(choices=[('JOB_OPENING_CREATED', 'Job opening created'), ('JOB_OPENING_UPDATED', 'Job opening updated'), ('JOB_OPENING_SUBMITTED_FOR_REVIEW', 'Job opening submitted for review'), ('JOB_OPENING_SENT_BACK_FOR_CHANGES', 'Job opening sent back for changes'), ('JOB_OPENING_PUBLISHED', 'Job opening published'), ('JOB_OPENING_CLOSED', 'Job opening closed'), ('JOB_OPENING_REMOVED', 'Job opening removed'), ('QUESTION_CREATED', 'Screening question created'), ('QUESTION_UPDATED', 'Screening question updated'), ('QUESTION_ARCHIVED', 'Screening question archived'), ('TEMPLATE_CREATED', 'Screening template created'), ('TEMPLATE_UPDATED', 'Screening template updated'), ('QUESTION_ADDED_TO_TEMPLATE', 'Question added to template'), ('QUESTION_REMOVED_FROM_TEMPLATE', 'Question removed from template'), ('QUESTION_MADE_MANDATORY', 'Question made mandatory in template'), ('QUESTION_MADE_OPTIONAL', 'Question made optional in template'), ('JOB_OPENING_QUESTIONS_SNAPSHOTTED', 'Job opening questions snapshotted'), ('CANDIDATE_ANSWER_SUBMITTED', 'Candidate screening answers submitted'), ('CANDIDATE_ANSWER_UPDATED', 'Candidate screening answers updated'), ('CANDIDATE_CREATED', 'Candidate created'), ('CANDIDATE_MAPPED_TO_JOB_OPENING', 'Candidate mapped to job opening'), ('CANDIDATE_DOCUMENT_UPLOADED', 'Candidate document uploaded'), ('CANDIDATE_NOTE_ADDED', 'Candidate note added'), ('CANDIDATE_STAGE_CHANGED', 'Candidate stage changed'), ('CANDIDATE_REJECTED', 'Candidate rejected'), ('CANDIDATE_HIRED', 'Candidate hired'), ('CANDIDATE_EXPORTED', 'Candidate data exported'), ('CANDIDATE_ANONYMIZED', 'Candidate anonymized'), ('DOCUMENT_RETENTION_CLEANUP', 'Document retention cleanup'), ('APPLICATION_SUBMITTED', 'Application submitted'), ('CONTACT_VERIFICATION_STARTED', 'Contact verification started'), ('CONTACT_VERIFICATION_COMPLETED', 'Contact verification completed'), ('HANDOFF_FORM_SUBMITTED', 'Hiring handoff form submitted')], db_index=True, max_length=64, verbose_name='Event'),
        ),
        migrations.CreateModel(
            name='ApplicationContactVerification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254)),
                ('mobile', models.CharField(max_length=25)),
                ('email_token', models.CharField(db_index=True, max_length=64)),
                ('email_verified_at', models.DateTimeField(blank=True, null=True)),
                ('otp_code', models.CharField(blank=True, default='', max_length=10)),
                ('otp_sent_at', models.DateTimeField(blank=True, null=True)),
                ('mobile_verified_at', models.DateTimeField(blank=True, null=True)),
                ('expires_at', models.DateTimeField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('job_opening', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contact_verifications', to='recruitment.recruitment')),
            ],
            options={
                'verbose_name': 'Application Contact Verification',
                'verbose_name_plural': 'Application Contact Verifications',
                'indexes': [models.Index(fields=['job_opening', 'email', 'mobile'], name='recruitment_job_ope_e84505_idx')],
            },
        ),
        # Must follow the AddFields above: without it the three frozen columns
        # stay blank and existing templates keep tracking later bank edits.
        migrations.RunPython(
            freeze_existing_template_questions, unfreeze_template_questions
        ),
    ]
