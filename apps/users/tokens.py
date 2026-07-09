from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken


class PlatformRefreshToken(RefreshToken):
    """
    Extends the default simplejwt refresh token with platform-specific claims
    so consumers can read role/identity from the token without a DB round-trip.

    Extra claims injected into both the refresh and access tokens:
      - public_id : public UUID (external_id) — safe to expose to clients
      - email     : user's email address
      - user_type : role value (doctor | nurse | patient | ...)

    Deliberately does NOT touch the "user_id" claim — that's SimpleJWT's own
    claim (set by the parent's for_user()) holding the integer PK, and
    JWTAuthentication.get_user() looks users up by exactly that claim/field.
    Overwriting it with the UUID breaks authentication on every endpoint:
    `User.objects.get(id=<uuid>)` raises ValueError since `id` is a
    BigAutoField, not a UUID field.
    """

    @classmethod
    def for_user(cls, user):
        token = super().for_user(user)
        token["public_id"] = str(user.external_id)
        token["email"] = user.email or ""
        token["user_type"] = user.user_type
        return token


def issue_tokens(user) -> dict:
    """Shared JWT issuance so doctor login and patient OTP-verify produce the same token shape."""
    refresh = PlatformRefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def blacklist_all_tokens(user) -> None:
    """
    Blacklist every outstanding refresh token for `user`.

    Called after a password reset or change so that sessions on other devices
    (potentially an attacker's device) are immediately invalidated. Access
    tokens are short-lived (30 min) and can't be revoked, which is acceptable.
    """
    outstanding_qs = OutstandingToken.objects.filter(user=user)
    for token in outstanding_qs:
        BlacklistedToken.objects.get_or_create(token=token)
