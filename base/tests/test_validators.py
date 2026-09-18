from django.core.exceptions import ValidationError
from django.test import TestCase

from base.validators import gstin_validator, pan_validator, phone_number_validator


class PanValidatorTests(TestCase):
    def test_accepts_valid_pan(self):
        pan_validator("ABCDE1234F")  # should not raise

    def test_rejects_invalid_pan(self):
        with self.assertRaises(ValidationError):
            pan_validator("not-a-pan")

    def test_rejects_lowercase(self):
        with self.assertRaises(ValidationError):
            pan_validator("abcde1234f")


class GstinValidatorTests(TestCase):
    def test_accepts_valid_gstin(self):
        gstin_validator("27AAAAA0000A1Z5")  # should not raise

    def test_rejects_invalid_gstin(self):
        with self.assertRaises(ValidationError):
            gstin_validator("not-a-gstin")

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValidationError):
            gstin_validator("27AAAAA0000A1Z")


class PhoneNumberValidatorTests(TestCase):
    def test_accepts_valid_10_digit_number(self):
        phone_number_validator("9876543210")  # should not raise

    def test_rejects_too_short(self):
        with self.assertRaises(ValidationError):
            phone_number_validator("987654321")

    def test_rejects_too_long(self):
        with self.assertRaises(ValidationError):
            phone_number_validator("98765432101")

    def test_rejects_non_digits(self):
        with self.assertRaises(ValidationError):
            phone_number_validator("98765-4321")

    def test_rejects_plus_prefix(self):
        with self.assertRaises(ValidationError):
            phone_number_validator("+919876543210")
