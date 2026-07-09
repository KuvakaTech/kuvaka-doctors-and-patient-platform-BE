"""
TOTP (Time-based One-Time Password) service for MFA.

Uses pyotp for RFC 6238-compliant TOTP generation and verification.
Secrets are stored on User.totp_secret (base32, encrypted at the DB/disk
level via infrastructure — we store the raw secret in the column, relying
on encrypted DB volumes for protection at rest).

MFA token (mfa_token):
  A short-lived HMAC-signed token issued after successful password
  verification when MFA is enabled. The frontend exchanges it for real
  JWT tokens by supplying a valid TOTP code. This avoids any server-side
  session state — the signed token itself carries the user identity.

  Payload: {"user_id": <int pk>, "exp": <unix timestamp>}
  Signed with SECRET_KEY using HS256 via PyJWT.
  Lifetime: MFA_TOKEN_EXPIRY_SECONDS (default 180s / 3 minutes)
"""

import time

import jwt
import pyotp
from django.conf import settings


def _mfa_token_expiry() -> int:
    return getattr(settings, "MFA_TOKEN_EXPIRY_SECONDS", 180)


_ISSUER = "kuvaka"


# ---------------------------------------------------------------------------
# Secret management
# ---------------------------------------------------------------------------


def generate_totp_secret() -> str:
    """Generate a fresh base32 TOTP secret for a new MFA enrollment."""
    return pyotp.random_base32()


def get_totp_uri(user, secret: str) -> str:
    """
    Return an otpauth:// URI for QR code generation.
    The frontend encodes this into a QR code the user scans with their
    Authenticator app (Google Authenticator, Authy, 1Password, etc.).
    """
    label = user.email or str(user.external_id)
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=label,
        issuer_name=getattr(settings, "MFA_ISSUER_NAME", _ISSUER),
    )


def verify_totp_code(secret: str, code: str) -> bool:
    """
    Verify a 6-digit TOTP code against the stored secret.
    Allows ±1 time step (30s window) to tolerate clock skew.
    """
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


# ---------------------------------------------------------------------------
# MFA token (half-authenticated state)
# ---------------------------------------------------------------------------


def issue_mfa_token(user) -> str:
    """
    Issue a short-lived signed token after password passes but before TOTP
    is verified. The frontend must exchange this for real JWT tokens within
    MFA_TOKEN_EXPIRY_SECONDS seconds.
    """
    payload = {
        "user_id": user.pk,
        "exp": int(time.time()) + _mfa_token_expiry(),
        "type": "mfa_challenge",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def decode_mfa_token(token: str) -> int | None:
    """
    Decode and validate an mfa_token. Returns the user PK on success,
    None if the token is invalid or expired.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != "mfa_challenge":
            return None
        return payload["user_id"]
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
