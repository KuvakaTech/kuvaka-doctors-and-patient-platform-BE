from apps.core.services.email import send_transactional_email
from apps.users.models import EmailOTP, OTPPurpose

_SUBJECTS = {
    OTPPurpose.EMAIL_VERIFICATION: "Verify your email – Kuvaka",
    OTPPurpose.PASSWORD_RESET: "Reset your password – Kuvaka",
    OTPPurpose.LOGIN: "Your login code – Kuvaka",
}

_HEADINGS = {
    OTPPurpose.EMAIL_VERIFICATION: "Verify your email address",
    OTPPurpose.PASSWORD_RESET: "Reset your password",
    OTPPurpose.LOGIN: "Your one-time login code",
}

_BODY_LINES = {
    OTPPurpose.EMAIL_VERIFICATION: (
        "Thanks for signing up. Use the code below to verify your email address "
        "and activate your Kuvaka account."
    ),
    OTPPurpose.PASSWORD_RESET: (
        "We received a request to reset your password. Enter the code below to "
        "choose a new one. If you didn't request this, you can safely ignore this email."
    ),
    OTPPurpose.LOGIN: (
        "Use the code below to sign in to your Kuvaka account. It's valid for a single use only."
    ),
}


def _render_otp_email(*, name: str, code: str, purpose: str, expires_at) -> str:
    heading = _HEADINGS.get(purpose, "Your one-time code")
    body = _BODY_LINES.get(purpose, "Use the code below to continue.")
    greeting = f"Hi {name}," if name else "Hi there,"
    expiry = f"{expires_at:%H:%M} UTC"

    # Digits spaced out so they're readable at a glance even on mobile.
    spaced_code = " &nbsp; ".join(list(code))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{heading}</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:40px 16px;">
    <tr>
      <td align="center">
        <table width="100%" style="max-width:520px;background-color:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">

          <!-- Header -->
          <tr>
            <td style="background-color:#0f6fff;padding:32px 40px;text-align:center;">
              <span style="font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-0.3px;">Kuvaka</span>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:40px 40px 24px;">
              <p style="margin:0 0 8px;font-size:15px;color:#374151;">{greeting}</p>
              <p style="margin:0 0 28px;font-size:15px;color:#374151;line-height:1.6;">{body}</p>

              <!-- Code block -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" style="background-color:#f0f5ff;border:1px solid #c7d9ff;border-radius:6px;padding:20px;">
                    <span style="font-size:32px;font-weight:700;letter-spacing:6px;color:#0f6fff;font-family:'Courier New',Courier,monospace;">{spaced_code}</span>
                  </td>
                </tr>
              </table>

              <p style="margin:20px 0 0;font-size:13px;color:#6b7280;text-align:center;">
                Expires at&nbsp;<strong>{expiry}</strong> &nbsp;·&nbsp; Do not share this code with anyone
              </p>
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding:0 40px;">
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:0;" />
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:20px 40px 32px;text-align:center;">
              <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.6;">
                This email was sent by Kuvaka. If you didn't request this, no action is needed.<br />
                &copy; 2025 Kuvaka. All rights reserved.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def issue_and_send_otp(user, purpose: str) -> EmailOTP:
    otp, code = EmailOTP.issue(user, purpose)
    send_transactional_email(
        to_email=user.email,
        to_name=user.full_name,
        subject=_SUBJECTS.get(purpose, "Your one-time code – Kuvaka"),
        html_content=_render_otp_email(
            name=user.full_name,
            code=code,
            purpose=purpose,
            expires_at=otp.expires_at,
        ),
    )
    return otp


def verify_otp(user, purpose: str, code: str) -> bool:
    otp = (
        EmailOTP.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True)
        .order_by("-created_date")
        .first()
    )
    if otp is None:
        return False
    return otp.verify_code(code)
