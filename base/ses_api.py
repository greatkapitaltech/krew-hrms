"""
ses_api.py

Delivery through Great Kapital's internal SES HTTP API
(POST multipart: to, subject, body, is_html; auth via x-api-key).

Enabled when both SES_API_URL and SES_API_KEY are set. The API decides the
sender address itself, so the message's from_email is not forwarded.
"""

import logging

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


def is_enabled():
    return bool(
        getattr(settings, "SES_API_URL", "") and getattr(settings, "SES_API_KEY", "")
    )


def _body(message):
    """Return (body, is_html), preferring an HTML alternative when present."""
    if isinstance(message, EmailMultiAlternatives):
        for content, mimetype in message.alternatives:
            if mimetype == "text/html":
                return content, True
    return message.body, message.content_subtype == "html"


def _post(recipients, subject, body, is_html):
    response = requests.post(
        settings.SES_API_URL,
        headers={"x-api-key": settings.SES_API_KEY, "accept": "*/*"},
        files={
            "to": (None, ",".join(recipients)),
            "subject": (None, subject),
            "body": (None, body),
            "is_html": (None, "true" if is_html else "false"),
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result = response.json().get("result")
    if result != "SUCCESS":
        raise RuntimeError(f"SES API returned result={result!r}")


def send(message):
    """
    Send one EmailMessage. Returns True on success; raises on failure.

    To and Cc go in one request. The API has no Bcc field, so each Bcc
    recipient gets a separate request rather than being exposed in "to".
    """
    if message.attachments:
        logger.warning(
            "SES API has no attachment support; %d attachment(s) dropped from %r",
            len(message.attachments),
            message.subject,
        )
    body, is_html = _body(message)
    visible = [*message.to, *message.cc]
    if visible:
        _post(visible, message.subject, body, is_html)
    for recipient in message.bcc:
        _post([recipient], message.subject, body, is_html)
    return bool(visible or message.bcc)
