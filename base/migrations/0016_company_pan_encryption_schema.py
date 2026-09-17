# Step 1 of 3 (schema) -- widen `pan` to hold Fernet ciphertext and add the
# new `pan_hash` column (nullable for now, backfilled by the next
# migration). The OLD unique_company_pan constraint (on plaintext `pan`) is
# dropped here too -- once `pan` holds ciphertext, a constraint on it would
# be meaningless (Fernet ciphertext is never the same twice for the same
# plaintext), and it needs to be gone before 0018 adds the replacement
# constraint on `pan_hash` under the same name. Pure schema changes, no data
# touched -- kept separate from the data migration (0017) to avoid
# Postgres's "cannot ALTER TABLE because it has pending trigger events" when
# DDL and DML share one transaction.

import django.core.validators
from django.db import migrations, models

import krew_company_onboarding.model_fields


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0015_alter_company_invoice_cycle_alter_company_status_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='company',
            name='unique_company_pan',
        ),
        migrations.AlterField(
            model_name='company',
            name='pan',
            field=krew_company_onboarding.model_fields.EncryptedCharField(
                blank=True,
                max_length=500,
                null=True,
                plain_max_length=10,
                validators=[django.core.validators.RegexValidator(message='Enter a valid PAN (format: AAAAA9999A).', regex='^[A-Z]{5}[0-9]{4}[A-Z]$')],
                verbose_name='PAN',
            ),
        ),
        migrations.AddField(
            model_name='company',
            name='pan_hash',
            field=models.CharField(blank=True, editable=False, max_length=64, null=True, verbose_name='PAN Hash'),
        ),
    ]
