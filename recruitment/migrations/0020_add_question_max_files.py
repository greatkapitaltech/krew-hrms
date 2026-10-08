"""
Give every file-upload question a numeric "Max files allowed".

The cap was previously a boolean -- multiple files, or one -- which could not
express "up to three certificates". max_files becomes the authored value and
allow_multiple_files is kept as a derived column (RecruitmentSurvey.save()
maintains it) because templates, the published snapshot and the API read it.

The backfill is a documented choice, not recovered data: the old boolean
carried no count, so a question that allowed several files is given
DEFAULT_MAX_FILES_WHEN_MULTIPLE (5) and everything else 1. The default on the
column already makes single-file rows correct, so only the multi-file rows are
touched -- and allow_multiple_files is left exactly as it was, so no question
changes from multi-file to single-file or back.
"""

import django.core.validators
from django.db import migrations, models

#: Mirrors recruitment.models.DEFAULT_MAX_FILES_WHEN_MULTIPLE at the time of
#: writing. Written literally: a migration must keep doing the same thing even
#: if the constant is later re-valued.
DEFAULT_MAX_FILES_WHEN_MULTIPLE = 5


def backfill_max_files(apps, schema_editor):
    counts = {}
    for label in ("RecruitmentSurvey", "SurveyTemplateQuestion", "JobOpeningQuestion"):
        model = apps.get_model("recruitment", label)
        counts[label] = model.objects.filter(allow_multiple_files=True).update(
            max_files=DEFAULT_MAX_FILES_WHEN_MULTIPLE
        )
    print(
        "  max_files backfilled on multi-file questions: "
        + ", ".join(f"{label}={count}" for label, count in counts.items())
    )


def unbackfill(apps, schema_editor):
    """
    Deliberately a no-op.

    The columns are dropped by the reverse of the AddFields above, so there is
    nothing to undo; and the original boolean was never modified.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('recruitment', '0019_retire_default_initial_stage'),
    ]

    operations = [
        migrations.AddField(
            model_name='jobopeningquestion',
            name='max_files',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='recruitmentsurvey',
            name='max_files',
            field=models.PositiveSmallIntegerField(default=1, help_text='File-upload questions only. 1 means a single file.', validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(20)], verbose_name='Max Files Allowed'),
        ),
        migrations.AddField(
            model_name='surveytemplatequestion',
            name='max_files',
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(backfill_max_files, unbackfill),
    ]
