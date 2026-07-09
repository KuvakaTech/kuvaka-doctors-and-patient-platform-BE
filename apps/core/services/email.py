"""
Transactional email via Brevo (https://developers.brevo.com/reference/sendtransacemail).

Kept as a thin wrapper around the plain REST API (rather than the `sib-api-v3-sdk`/
`brevo-python` client) so the dependency footprint stays small and the call is trivial
to mock in tests. If we ever need a second provider, swap the implementation behind
`send_transactional_email` without touching call sites.
"""

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


class EmailDeliveryError(Exception):
    """Raised when the Brevo API rejects or fails to send an email."""


def send_transactional_email(
    *, to_email: str, to_name: str, subject: str, html_content: str
) -> None:
    """
    Send one transactional email via Brevo.

    In local/test environments without a configured API key, the email is
    logged instead of sent so the OTP/verification flows stay usable without
    a Brevo account.
    """
    if not settings.BREVO_API_KEY:
        logger.info(
            "BREVO_API_KEY not set; skipping send. to=%s subject=%r",
            to_email,
            subject,
        )
        return

    response = httpx.post(
        BREVO_API_URL,
        headers={
            "api-key": settings.BREVO_API_KEY,
            "content-type": "application/json",
            "accept": "application/json",
        },
        json={
            "sender": {"email": settings.BREVO_SENDER_EMAIL, "name": settings.BREVO_SENDER_NAME},
            "to": [{"email": to_email, "name": to_name or to_email}],
            "subject": subject,
            "htmlContent": html_content,
        },
        timeout=10.0,
    )

    if response.status_code >= 400:
        logger.error("Brevo send failed: %s %s", response.status_code, response.text)
        raise EmailDeliveryError(f"Brevo API returned {response.status_code}: {response.text}")
