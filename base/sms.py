"""
SMS delivery boundary.

One function, one settings-resolved provider. Business code calls send_sms()
and never imports provider-specific code, so swapping provider is a settings
change.

Configuration (environment, via horilla/settings/addons.py):

    SMS_PROVIDER        dotted path to callable(to, body, context=None)
    SMS_API_KEY         provider credential (MSG91: authkey)
    SMS_SENDER_ID       DLT-registered sender header
    MSG91_TEMPLATE_ID   MSG91 Flow template id (required in India: DLT rules
                        forbid arbitrary message text)
    MSG91_OTP_VARIABLE  name of the template's OTP variable (default "otp";
                        e.g. "var1" for a template written as ##var1##)
    MSG91_URL           endpoint override, when the account uses a
                        non-default/regional base URL

The MSG91 Flow request carries the recipient's mobile number plus the OTP as a
template variable (``{"mobiles": ..., "otp": ...}``). If a Flow template names
its variable something other than ``otp``, that name is what must be sent.

Until SMS_PROVIDER is configured, send_sms raises SMSNotConfigured -- it never
pretends a message was delivered.
"""

import logging

import requests
from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

MSG91_FLOW_URL = "https://control.msg91.com/api/v5/flow/"
MSG91_SENDSMS_URL = "https://api.msg91.com/api/v2/sendsms"
SMS_TIMEOUT = 10
DEFAULT_COUNTRY_CODE = "91"


class SMSNotConfigured(Exception):
    """No SMS provider is configured for this deployment."""


class SMSSendFailed(Exception):
    """The configured provider rejected or failed to deliver the message."""


def send_sms(to, body, context=None):
    """
    Deliver one SMS. Raises rather than failing silently.

    ``context`` carries template variables for providers that require a
    pre-registered template instead of free text.

    Message bodies and context are never logged: an OTP must not reach the
    application log.
    """
    provider_path = getattr(settings, "SMS_PROVIDER", None)
    if not provider_path:
        raise SMSNotConfigured(
            "SMS_PROVIDER is not configured; no SMS provider adapter is available."
        )

    try:
        provider = import_string(provider_path)
    except ImportError as error:
        raise SMSNotConfigured(f"SMS_PROVIDER {provider_path!r} cannot be imported.") from error

    try:
        provider(to, body, context)
    except Exception as error:
        logger.exception("SMS delivery failed via %s", provider_path)
        raise SMSSendFailed(str(error)) from error


def _flow_url(configured):
    """
    Resolve the Flow endpoint from MSG91_URL.

    MSG91 hands a deployment its account base URL (".../api/v5"), so accept
    either that or the full ".../api/v5/flow/" and normalise to the path that
    actually accepts the POST. Storing the base verbatim as the endpoint would
    send every message to the wrong URL.
    """
    if not configured:
        return MSG91_FLOW_URL
    base = configured.rstrip("/")
    if base.endswith("/flow"):
        return f"{base}/"
    return f"{base}/flow/"


def _msisdn(number):
    """
    Digits only, with a country code. MSG91 needs "91XXXXXXXXXX"; applicants
    usually type the bare 10-digit number, which MSG91 does not route.
    """
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        country = getattr(settings, "SMS_DEFAULT_COUNTRY_CODE", None)
        digits = f"{country or DEFAULT_COUNTRY_CODE}{digits}"
    return digits


def msg91(to, body, context=None):
    """
    MSG91 adapter. Set SMS_PROVIDER="base.sms.msg91" to use it.

    Prefers the v5 Flow API when MSG91_TEMPLATE_ID is set, which is what Indian
    DLT regulation requires -- arbitrary message text is rejected by the
    carriers, so the OTP travels as a template variable. Falls back to the v2
    free-text endpoint for accounts without a Flow template.
    """
    authkey = getattr(settings, "SMS_API_KEY", None)
    if not authkey:
        raise SMSNotConfigured("SMS_API_KEY is not configured for MSG91.")

    template_id = getattr(settings, "MSG91_TEMPLATE_ID", None)
    sender = getattr(settings, "SMS_SENDER_ID", None)
    headers = {"authkey": authkey, "Content-Type": "application/json"}
    to = _msisdn(to)

    if template_id:
        # Mobile number + OTP, the latter as a Flow template variable.
        # The OTP is passed as context["otp"]; templates name their variable
        # freely (e.g. ##var1##), so MSG91_OTP_VARIABLE maps it.
        otp_var = getattr(settings, "MSG91_OTP_VARIABLE", None) or "otp"
        recipient = {"mobiles": to}
        recipient.update(
            {(otp_var if k == "otp" else k): v for k, v in (context or {}).items()}
        )
        payload = {"template_id": template_id, "recipients": [recipient]}
        # MSG91_URL describes the Flow base URL only; the v2 fallback below
        # lives on a different host, so it keeps its own constant rather than
        # being rewritten onto a v5 base.
        url = _flow_url(getattr(settings, "MSG91_URL", None))
    else:
        if not sender:
            raise SMSNotConfigured(
                "MSG91 needs MSG91_TEMPLATE_ID (Flow) or SMS_SENDER_ID (v2)."
            )
        payload = {
            "sender": sender,
            "route": "4",
            "sms": [{"message": body, "to": [to]}],
        }
        url = MSG91_SENDSMS_URL

    response = requests.post(url, json=payload, headers=headers, timeout=SMS_TIMEOUT)
    # MSG91 reports most rejections (bad authkey, template, number, DLT) as
    # HTTP 200 with {"type": "error"}, so the status code alone is not enough.
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or data.get("type") == "error":
        raise SMSSendFailed(
            f"MSG91 returned HTTP {response.status_code}: {data.get('message', '')}"
        )
