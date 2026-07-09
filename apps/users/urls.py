from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

# Token *issuance* is deliberately not exposed here — it happens through
# apps.doctors (email/password + verification) and apps.patients (email OTP),
# each of which enforces its own checks (email_verified, user_type, OTP) before
# minting a token. Refresh is generic across both journeys since it doesn't
# need those checks.
urlpatterns = [
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
]
