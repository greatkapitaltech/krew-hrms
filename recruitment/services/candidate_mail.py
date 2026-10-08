"""
Candidate-facing automatic email.

Two messages, both required by the PRD: the rejection notice every rejection
sends, and the application link sent to a candidate entered by hand so they
fill in their own details and answers on Form 1 rather than having a recruiter
type them. Neither is a new mail subsystem --

  * the body is a ``base.models.HorillaMailTemplate``, the same editable
    template model the Settings > Mail Templates screen and the manual "Send
    Mail" action already use, so HR edits the wording in the place they
    already know;
  * the template is created on demand with a default body, following the
    pattern upstream already uses for "Candidate Portal Login"
    (recruitment/views/views.py). It is an app-provided default HR can rewrite,
    not seed data: nothing is created until the first rejection actually needs
    it, and an edited body is never overwritten;
  * placeholders are rendered through
    ``base.methods.sanitize_mail_template_placeholders`` against the existing
    allowlist from ``MailTemplateForm.get_template_language()``. An HR-authored
    body is still user input, and the Django template engine would otherwise
    expose attribute traversal on everything in the context;
  * delivery goes through ``ConfiguredEmailBackend``, so it honours the
    tenant's own Mail Server configuration like every other Horilla mail.

A failure here never hides and never lies: it is logged with the candidate id
(no message body, no address) and reported back to the caller, which records
the outcome in the rejection's audit event.
"""

import logging

from django import template
from django.core.mail import EmailMessage
from django.utils.translation import gettext_lazy as _

from base.backends import ConfiguredEmailBackend
from base.methods import sanitize_mail_template_placeholders

logger = logging.getLogger(__name__)

#: Titles of the editable templates. Matched on title because
#: HorillaMailTemplate.title is unique across the installation.
REJECTION_TEMPLATE_TITLE = "Candidate Rejection"
APPLICATION_LINK_TEMPLATE_TITLE = "Candidate Application Link"

#: Default body, used only when the template does not exist yet. Written with
#: placeholders from the allowlist (see get_template_language()) so it survives
#: sanitising unchanged. Intentionally gives no reason: the internal remark is
#: an accountability record for the hiring team, not something to send out.
REJECTION_DEFAULT_BODY = (
    '<div style="font-family: \'Segoe UI\', Arial, sans-serif; background-color:'
    ' #f4f6f9; padding: 24px;">'
    '<div style="max-width: 640px; margin: auto; background: #ffffff;'
    " border-radius: 12px; padding: 28px; border: 1px solid #eceff3;\">"
    '<p style="font-size: 14px; color: #374151; line-height: 1.7;">'
    "Dear {{instance.get_full_name}},</p>"
    '<p style="font-size: 14px; color: #374151; line-height: 1.7;">'
    "Thank you for taking the time to apply for"
    " {{instance.get_job_position}} and for speaking with our team."
    " After careful consideration we will not be moving forward with your"
    " application at this stage.</p>"
    '<p style="font-size: 14px; color: #374151; line-height: 1.7;">'
    "We appreciate the interest you have shown in"
    " {{instance.get_company}}, and we will keep your details on file for"
    " roles that may suit you better in future.</p>"
    '<p style="font-size: 14px; color: #374151; line-height: 1.7;">'
    "We wish you the very best with your search.</p>"
    '<hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">'
    '<p style="font-size: 13px; color: #6b7280; margin: 0;">Regards,</p>'
    '<p style="font-size: 13px; color: #111827; margin: 6px 0 0 0;">'
    "<strong>{{instance.get_company}}</strong></p>"
    "</div></div>"
)

REJECTION_SUBJECT = _("Update on your application")

#: Default body for the application-link message. ``{{application_link}}`` is
#: this message's own placeholder, supplied by render_application_link_body()
#: and added to the allowlist there -- it is a system-generated URL, not an
#: attribute path, so it cannot be used to traverse the context.
APPLICATION_LINK_DEFAULT_BODY = (
    '<div style="font-family: \'Segoe UI\', Arial, sans-serif; background-color:'
    ' #f4f6f9; padding: 24px;">'
    '<div style="max-width: 640px; margin: auto; background: #ffffff;'
    " border-radius: 12px; padding: 28px; border: 1px solid #eceff3;\">"
    '<p style="font-size: 14px; color: #374151; line-height: 1.7;">'
    "Dear {{instance.get_full_name}},</p>"
    '<p style="font-size: 14px; color: #374151; line-height: 1.7;">'
    "Thank you for your interest in {{instance.get_job_position}} at"
    " {{instance.get_company}}. Please complete your application using the"
    " link below so we have your details and documents on file.</p>"
    '<p style="margin: 20px 0;">'
    '<a href="{{application_link}}"'
    ' style="background: #4f46e5; color: #ffffff; padding: 12px 20px;'
    ' border-radius: 8px; text-decoration: none; font-size: 14px;">'
    "Complete your application</a></p>"
    '<p style="font-size: 13px; color: #6b7280;">'
    "If the button does not work, copy this link into your browser:<br>"
    "{{application_link}}</p>"
    '<hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">'
    '<p style="font-size: 13px; color: #6b7280; margin: 0;">Regards,</p>'
    '<p style="font-size: 13px; color: #111827; margin: 6px 0 0 0;">'
    "<strong>{{instance.get_company}}</strong></p>"
    "</div></div>"
)

APPLICATION_LINK_SUBJECT = _("Complete your application")


def _template(title, default_body):
    """
    An editable mail template, created with its default body if absent.

    ``objects`` is company-scoped, so ``.entire()`` is used for the existence
    check: the title is unique installation-wide, and a tenant-filtered miss
    would try to create a duplicate and raise IntegrityError.
    """
    from base.models import HorillaMailTemplate

    existing = (
        HorillaMailTemplate.objects.entire().filter(title=title).first()
    )
    if existing is not None:
        return existing
    return HorillaMailTemplate.objects.create(title=title, body=default_body)


def rejection_template():
    """The editable rejection template."""
    return _template(REJECTION_TEMPLATE_TITLE, REJECTION_DEFAULT_BODY)


def application_link_template():
    """The editable application-link template."""
    return _template(
        APPLICATION_LINK_TEMPLATE_TITLE, APPLICATION_LINK_DEFAULT_BODY
    )


def _render(body, context, extra_allowed=()):
    """Render an HR-authored body with the placeholder allowlist applied."""
    from base.forms import MailTemplateForm

    allowed = set(MailTemplateForm().get_template_language().values())
    allowed.update(extra_allowed)
    safe_body = sanitize_mail_template_placeholders(body, allowed)
    return template.Template(safe_body).render(template.Context(context))


def render_rejection_body(candidate, actor_employee=None):
    """Render the template for one candidate, placeholders allowlisted."""
    return _render(
        rejection_template().body,
        {"instance": candidate, "self": actor_employee},
    )


#: Signed-invite settings for the emailed Form 1 link (PRD Form 3).
INVITE_SALT = "recruitment.application-invite"
INVITE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def make_invite_token(candidate):
    """A tamper-proof token naming this candidate and their job opening."""
    from django.core import signing

    return signing.dumps(
        {"c": candidate.pk, "r": candidate.recruitment_id_id}, salt=INVITE_SALT
    )


def invited_candidate(token, job_opening):
    """
    The candidate an emailed link was issued for, or None.

    Valid only for the opening it was issued under, unexpired, and for a
    candidate who is still active (not rejected, not hired).
    """
    from django.core import signing

    from recruitment.models import Candidate

    if not token or job_opening is None:
        return None
    try:
        data = signing.loads(token, salt=INVITE_SALT, max_age=INVITE_MAX_AGE)
    except signing.BadSignature:
        return None
    if data.get("r") != job_opening.pk:
        return None
    candidate = (
        Candidate.objects.entire()
        .filter(pk=data.get("c"), recruitment_id=job_opening)
        .first()
    )
    if candidate is None or candidate.canceled or candidate.hired:
        return None
    return candidate


def application_link_for(candidate, request=None):
    """
    The public Form 1 URL for this candidate's job opening.

    Absolute, because it is going into an email. Built from the request when
    there is one; otherwise from the site's configured host, so the scheduler
    and the shell produce a usable link too.
    """
    from django.urls import reverse

    opening = candidate.recruitment_id
    if opening is None:
        return None
    path = (
        f"{reverse('application-form')}?recruitmentId={opening.pk}"
        f"&invite={make_invite_token(candidate)}"
    )
    if request is not None:
        return request.build_absolute_uri(path)

    from django.conf import settings

    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else path


def render_application_link_body(candidate, link, actor_employee=None):
    """Render the application-link template for one candidate."""
    return _render(
        application_link_template().body,
        {"instance": candidate, "self": actor_employee, "application_link": link},
        extra_allowed=("application_link",),
    )


def _send(candidate, subject, body, *, kind):
    """
    Send one HTML message to a candidate.

    Returns True only when the backend accepted it. Never raises: a mail
    problem must not undo the business action that triggered it, and must never
    be reported as a success either.
    """
    address = (candidate.email or "").strip()
    if not address:
        logger.warning(
            "No %s email sent for candidate %s: no email address on record",
            kind,
            candidate.pk,
        )
        return False

    try:
        backend = ConfiguredEmailBackend()
        message = EmailMessage(
            subject=str(subject),
            body=body,
            from_email=backend.dynamic_from_email_with_display_name,
            to=[address],
        )
        message.content_subtype = "html"
        delivered = message.send(fail_silently=False)
    except Exception:
        # Never log the rendered body or the address -- the exception text is
        # enough to diagnose a misconfigured mail server.
        logger.exception(
            "%s email raised while sending for candidate %s", kind, candidate.pk
        )
        return False

    if not delivered:
        logger.error(
            "%s email was not delivered for candidate %s "
            "(check the Mail Server configuration)",
            kind,
            candidate.pk,
        )
        return False
    return True


def send_rejection_email(candidate, actor=None):
    """Email the candidate that their application has not moved forward."""
    actor_employee = getattr(actor, "employee_get", None) if actor else None
    try:
        body = render_rejection_body(candidate, actor_employee)
    except Exception:
        logger.exception(
            "Rejection email could not be rendered for candidate %s", candidate.pk
        )
        return False
    return _send(candidate, REJECTION_SUBJECT, body, kind="Rejection")


def send_application_link(candidate, actor=None, request=None):
    """
    Email the candidate the public Form 1 link for their job opening.

    Sent when a candidate is entered by hand (PRD Form 3): the person fills in
    their own details, answers and documents on the application form rather
    than having a recruiter transcribe them.

    Returns False -- without sending -- when there is no opening to apply to,
    since the public form is per job opening.
    """
    link = application_link_for(candidate, request=request)
    if not link:
        logger.warning(
            "No application link sent for candidate %s: not attached to a job "
            "opening",
            candidate.pk,
        )
        return False

    actor_employee = getattr(actor, "employee_get", None) if actor else None
    try:
        body = render_application_link_body(candidate, link, actor_employee)
    except Exception:
        logger.exception(
            "Application-link email could not be rendered for candidate %s",
            candidate.pk,
        )
        return False
    sent = _send(candidate, APPLICATION_LINK_SUBJECT, body, kind="Application link")
    # Audited whatever the outcome; the link and its token are never stored.
    from recruitment.models import RecruitmentAuditEvent
    from recruitment.services.audit import RecruitmentAuditService

    RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.APPLICATION_LINK_SENT,
        actor=actor,
        company=candidate.company_id,
        job_opening=candidate.recruitment_id,
        candidate=candidate,
        details={"email_sent": bool(sent)},
    )
    return sent
