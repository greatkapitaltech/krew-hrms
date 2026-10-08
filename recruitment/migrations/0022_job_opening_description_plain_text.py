"""
Store job-opening descriptions as plain text.

The description used a rich-text editor, so existing rows hold HTML
("<p>...</p>"). Converted here with the same rules as
recruitment.models.html_to_plain_text (copied, so this migration does not
depend on model code that may change later).
"""

import html
import re

from django.db import migrations
from django.utils.html import strip_tags


def _to_text(value):
    if not value or "<" not in value:
        return value
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|ul|ol|tr)>", "\n", text)
    text = html.unescape(strip_tags(text)).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def forwards(apps, schema_editor):
    Recruitment = apps.get_model("recruitment", "Recruitment")
    for pk, description in Recruitment.objects.values_list("pk", "description"):
        converted = _to_text(description)
        if converted != description:
            # update(), not save(): no lifecycle side effects or history rows.
            Recruitment.objects.filter(pk=pk).update(description=converted)


class Migration(migrations.Migration):
    dependencies = [
        ("recruitment", "0021_candidate_handoff_budget"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
