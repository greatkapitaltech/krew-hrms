# Step 2 of 3 (data) -- for every existing Company with a real PAN still
# stored as plaintext: compute pan_hash from that plaintext FIRST (before
# it's overwritten), then encrypt pan in place. Pure data migration, no
# schema touched.

from django.db import migrations


def encrypt_existing_pans(apps, schema_editor):
    # `pan` is already EncryptedCharField at this point (0016 already ran)
    # -- its get_prep_value() transparently encrypts on save(), and its
    # from_db_value() falls back to returning the raw DB value unchanged
    # when it isn't valid Fernet ciphertext yet (see
    # krew_company_onboarding/encryption.py's decrypt_value()). So reading
    # company.pan here already gives the true plaintext (nothing to decrypt
    # yet) -- computing pan_hash from it and then just re-saving the SAME
    # field is enough; save() encrypts it exactly once. Manually calling
    # encrypt_value() here too would double-encrypt it.
    from krew_company_onboarding.encryption import hash_value

    Company = apps.get_model("base", "Company")
    for company in Company.objects.exclude(pan__isnull=True).exclude(pan=""):
        company.pan_hash = hash_value(company.pan)
        company.save(update_fields=["pan", "pan_hash"])


def reverse_encrypt_existing_pans(apps, schema_editor):
    # Not meaningfully reversible in-place: `pan` here is still
    # EncryptedCharField (0016 hasn't been reversed), so re-assigning
    # company.pan to its own decrypted value and saving would just
    # re-encrypt it right back via get_prep_value(). Clearing pan_hash is
    # the only real "undo" available at this migration state.
    Company = apps.get_model("base", "Company")
    Company.objects.update(pan_hash=None)


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0016_company_pan_encryption_schema'),
    ]

    operations = [
        migrations.RunPython(encrypt_existing_pans, reverse_encrypt_existing_pans),
    ]
