"""
Contact verification for a public application: email link, then SMS OTP.

PRD flow: one Verify (email and mobile both entered) sends the email link;
following the link proves the email AND sends the SMS OTP, so a single action
covers both channels. Gating the SMS on a proven email keeps a bare POST from
making the system send SMS.

Both factors share one 10-minute window. Verification never blocks submission
(PRD) -- an unverified application is still accepted, and the Candidate Pool
shows the state.
"""

import logging
import secrets

from django.core.mail import EmailMessage
from django.db.models import F
from django.urls import reverse
from django.utils import timezone as tz
from django.utils.translation import gettext_lazy as _

from base.backends import ConfiguredEmailBackend
from base.methods import generate_otp
from base.sms import send_sms
from recruitment.services.audit import RecruitmentAuditService
from recruitment.services.errors import ContactVerificationError

logger = logging.getLogger(__name__)


#: PRD: the link and the OTP expire after 10 minutes; then restart.
VERIFICATION_EXPIRED = _("Verification expired. Click Verify to start again.")


def _audit(event, verification, **details):
    """One audit line per verification step. Never the token or the code."""
    from recruitment.models import RecruitmentAuditEvent

    RecruitmentAuditService.record(
        event_type=getattr(RecruitmentAuditEvent.EventType, event),
        company=verification.job_opening.company_id,
        job_opening=verification.job_opening,
        candidate=verification.candidate,
        object_type="ApplicationContactVerification",
        object_id=verification.pk,
        details={"verification_id": verification.pk, **details},
    )


def _active(job_opening, email, mobile):
    from recruitment.models import ApplicationContactVerification

    return (
        ApplicationContactVerification.objects.filter(
            job_opening=job_opening,
            email__iexact=email,
            mobile=mobile,
            expires_at__gt=tz.now(),
        )
        .order_by("-id")
        .first()
    )


def start_verification(request, job_opening, email, mobile):
    """
    Issue a fresh email token and send the verification link.

    Any attempt for the same opening/email is superseded, so a re-request
    invalidates the previous link rather than leaving two live. The mobile
    number is stored now; the OTP goes to it once the link is followed.
    """
    from recruitment.models import (
        ApplicationContactVerification,
        RecruitmentAuditEvent,
    )

    # Only openings with the Contact Verification toggle on send anything.
    if not job_opening.contact_verification_required:
        raise ContactVerificationError(
            _("Contact verification is not enabled for this job opening.")
        )

    email = (email or "").strip()
    mobile = (mobile or "").strip()
    if not email or not mobile:
        raise ContactVerificationError(
            _("Enter both your email address and mobile number to verify.")
        )

    ApplicationContactVerification.objects.filter(
        job_opening=job_opening, email__iexact=email
    ).delete()

    verification = ApplicationContactVerification.objects.create(
        job_opening=job_opening,
        email=email,
        mobile=mobile,
        email_token=secrets.token_urlsafe(32),
        expires_at=tz.now() + ApplicationContactVerification.VALIDITY,
    )

    link = request.build_absolute_uri(
        reverse("application-verify-email", kwargs={"token": verification.email_token})
    )
    backend = ConfiguredEmailBackend()
    message = EmailMessage(
        subject=str(_("Verify your email address")),
        body=str(
            _(
                "Confirm your email address to continue your application for "
                "%(job)s:\n\n%(link)s\n\nThis link expires in 10 minutes."
            )
            % {"job": job_opening.title, "link": link}
        ),
        from_email=backend.dynamic_from_email_with_display_name,
        to=[email],
    )
    # A send that does not deliver must never look like success: the applicant
    # would sit waiting for a link that was never sent. This is the same rule
    # the SMS boundary follows.
    #
    # The failure arrives as a RETURN OF 0, not as an exception.
    # base.backends.DefaultHorillaMailBackend.__init__ accepts the caller's
    # fail_silently and then discards it, substituting its own configured value
    # -- which defaults to True when no mail server is configured. Passing
    # fail_silently=False here is therefore not enough on its own, so both
    # shapes are handled.
    try:
        delivered = message.send(fail_silently=False)
    except Exception:
        # The verification id and nothing else: never the token, the link or
        # the message body.
        logger.exception(
            "Verification email raised while sending for verification %s",
            verification.pk,
        )
        delivered = 0
    if not delivered:
        logger.error(
            "Verification email was not delivered for verification %s "
            "(check the Mail Server configuration)",
            verification.pk,
        )
        raise ContactVerificationError(
            _(
                "We could not send the verification email just now. Please try "
                "again in a moment."
            )
        )

    RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.CONTACT_VERIFICATION_STARTED,
        company=job_opening.company_id,
        job_opening=job_opening,
        object_type="ApplicationContactVerification",
        object_id=verification.pk,
        details={"verification_id": verification.pk},
    )
    return verification


def verify_email(token):
    """
    Consume an email token, then send the mobile OTP.

    The token is single-use: it is cleared here, so the link cannot be replayed.
    An SMS failure does not undo the verified email -- the applicant can use
    "Resend code" (send_otp) or submit without the mobile factor.
    """
    from recruitment.models import ApplicationContactVerification

    verification = ApplicationContactVerification.objects.filter(
        email_token=token
    ).first()
    if verification is None or verification.is_expired():
        raise ContactVerificationError()

    verification.email_verified_at = tz.now()
    verification.email_token = ""
    verification.save(update_fields=["email_verified_at", "email_token"])
    _audit("CONTACT_EMAIL_VERIFIED", verification)

    try:
        _issue_otp(verification)
    except ContactVerificationError:
        pass  # logged in _issue_otp; the email stays verified
    return verification


def _issue_otp(verification, resend=False):
    """
    SMS the OTP. Within the 10-minute OTP stage a resend sends the SAME code;
    a new code is generated only if none was delivered yet (e.g. the first SMS
    failed). It expires when the stage does. Returns True for a new code.

    Nothing is saved unless the SMS actually goes out: a failed send (gateway
    error, or no SMS provider configured) raises ContactVerificationError, keeps
    any earlier code, and does not start the 30-second resend wait.
    """
    import math

    from recruitment.models import ApplicationContactVerification

    now = tz.now()
    expires_at = verification.otp_window_ends_at()
    if now >= expires_at:
        raise ContactVerificationError(VERIFICATION_EXPIRED)
    new_code = not verification.otp_is_valid()
    code = generate_otp() if new_code else verification.otp_code
    minutes_left = max(1, math.ceil((expires_at - now).total_seconds() / 60))
    try:
        send_sms(
            verification.mobile,
            str(
                _("Your verification code is %(code)s. It expires in %(minutes)s minutes.")
                % {"code": code, "minutes": minutes_left}
            ),
            {"otp": code},
        )
    except Exception:
        # Logged without the code.
        logger.exception("OTP delivery failed for verification %s", verification.pk)
        _audit("CONTACT_OTP_SEND_FAILED", verification)
        raise ContactVerificationError(
            _(
                "We couldn't send the code by SMS just now. Please tap Resend "
                "code to try again."
            )
        )

    verification.otp_code = code
    verification.otp_expires_at = expires_at
    verification.otp_sent_at = now
    # The attempt lives at least as long as its current code.
    if verification.otp_expires_at > verification.expires_at:
        verification.expires_at = verification.otp_expires_at
    verification.save(
        update_fields=["otp_code", "otp_sent_at", "otp_expires_at", "expires_at"]
    )
    _audit("CONTACT_OTP_SENT", verification, resend=resend)
    return new_code


def send_otp(verification_pk):
    """
    "Resend code", every 30 seconds, within the 10 minutes after the email
    was verified: re-sends the same code. After that, verification must be
    restarted (PRD). The wrong-code counter is NOT reset by a resend -- only a
    restart (a new row) does that. Returns (verification, new_code).
    """
    from recruitment.models import ApplicationContactVerification

    verification = ApplicationContactVerification.objects.filter(
        pk=verification_pk
    ).first()
    if (
        verification is None
        or not verification.email_verified_at
        or verification.mobile_verified_at
    ):
        raise ContactVerificationError()
    if not verification.is_usable():
        raise ContactVerificationError(VERIFICATION_EXPIRED)

    wait = verification.resend_wait_seconds()
    if wait:
        raise ContactVerificationError(
            _("You can request the code again in %(seconds)s seconds.")
            % {"seconds": wait}
        )

    new_code = _issue_otp(verification, resend=True)
    return verification, new_code


def verify_otp(verification_pk, code):
    """Confirm the mobile factor and complete verification."""
    from recruitment.models import (
        ApplicationContactVerification,
        RecruitmentAuditEvent,
    )

    verification = ApplicationContactVerification.objects.filter(
        pk=verification_pk
    ).first()
    if verification is None or not verification.email_verified_at:
        raise ContactVerificationError()
    if not verification.otp_is_valid():
        raise ContactVerificationError(VERIFICATION_EXPIRED)

    # The attempt limit is checked BEFORE the code is compared, so a caller
    # that has spent its guesses learns nothing about whether this particular
    # code was right -- the answer is identical either way. A 6-digit code
    # valid for 10 minutes would otherwise be brute-forceable, and
    # Fail2BanMiddleware does not cover this public endpoint.
    if verification.otp_attempts >= ApplicationContactVerification.MAX_OTP_ATTEMPTS:
        logger.warning(
            "OTP attempt limit reached for verification %s", verification.pk
        )
        raise ContactVerificationError(
            _("Too many incorrect codes. Click Verify to start again.")
        )

    if not secrets.compare_digest(verification.otp_code, (code or "").strip()):
        # Counted with an F() expression so two concurrent wrong guesses both
        # register; a read-modify-write here could lose one.
        ApplicationContactVerification.objects.filter(pk=verification.pk).update(
            otp_attempts=F("otp_attempts") + 1
        )
        _audit("CONTACT_OTP_FAILED", verification, attempts=verification.otp_attempts + 1)
        raise ContactVerificationError(
            _("Incorrect code. Please check the SMS and try again.")
        )

    verification.mobile_verified_at = tz.now()
    # Cleared, so a correct code cannot be replayed: the next call fails the
    # `not verification.otp_code` guard above.
    verification.otp_code = ""
    verification.save(update_fields=["mobile_verified_at", "otp_code"])

    RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.CONTACT_VERIFICATION_COMPLETED,
        company=verification.job_opening.company_id,
        job_opening=verification.job_opening,
        object_type="ApplicationContactVerification",
        object_id=verification.pk,
        details={"verification_id": verification.pk},
    )
    return verification


def _digits(value):
    """Last 10 digits, so "+91 98765 43210" and "9876543210" match."""
    return "".join(ch for ch in (value or "") if ch.isdigit())[-10:]


def completed_verification(job_opening, email, mobile):
    """
    The finished (email + OTP) verification for this opening and these exact
    contact details, if one is still usable; otherwise None.
    """
    from recruitment.models import ApplicationContactVerification

    email = (email or "").strip()
    if not email or not _digits(mobile):
        return None
    candidates = ApplicationContactVerification.objects.filter(
        job_opening=job_opening,
        email__iexact=email,
        email_verified_at__isnull=False,
        mobile_verified_at__isnull=False,
        mobile_verified_at__gt=tz.now()
        - ApplicationContactVerification.COMPLETED_VALIDITY,
    ).order_by("-id")
    for verification in candidates:
        if _digits(verification.mobile) == _digits(mobile):
            return verification
    return None


def stamp_candidate(candidate):
    """
    Carry a completed verification onto a newly submitted application.

    Matched on the opening plus the submitted contact details, so a verification
    proven for one opening cannot vouch for an application to another.
    """
    if candidate.recruitment_id_id is None:
        return None

    verification = completed_verification(
        candidate.recruitment_id, candidate.email, candidate.mobile
    )
    if verification is None:
        return None

    candidate.contact_verified_at = verification.mobile_verified_at
    candidate.save(update_fields=["contact_verified_at"])
    # Link the attempt to the application it vouched for.
    verification.candidate = candidate
    verification.save(update_fields=["candidate"])

    from recruitment.models import RecruitmentAuditEvent

    RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.CANDIDATE_CONTACT_VERIFIED,
        company=candidate.company_id,
        job_opening=candidate.recruitment_id,
        candidate=candidate,
        details={"verification_id": verification.pk},
    )
    return verification
