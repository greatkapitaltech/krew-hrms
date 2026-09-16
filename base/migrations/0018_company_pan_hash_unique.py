# Step 3 of 3 (schema) -- restore duplicate-PAN protection, now scoped to
# pan_hash (the deterministic blind index computed in Company.clean()/
# save()) instead of the now-encrypted pan column. Every existing row's
# pan_hash was already backfilled by 0017, so this attaches cleanly. Pure
# schema change, no data touched.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0017_company_pan_encryption_data'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='company',
            constraint=models.UniqueConstraint(
                condition=models.Q(('pan_hash__isnull', False)),
                fields=('pan_hash',),
                name='unique_company_pan',
            ),
        ),
    ]
