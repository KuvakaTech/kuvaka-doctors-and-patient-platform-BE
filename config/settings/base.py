"""
Base settings shared by all environments. Environment-specific settings
(local, test, deployment) import from this module and override as needed.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
APPS_DIR = BASE_DIR / "apps"

env = environ.Env()

if READ_DOT_ENV_FILE := env.bool("DJANGO_READ_DOT_ENV_FILE", default=False):
    env.read_env(str(BASE_DIR / ".env"))

# GENERAL
# ------------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env.json("DJANGO_ALLOWED_HOSTS", default=["*"])
TIME_ZONE = env("DJANGO_TIME_ZONE", default="UTC")
LANGUAGE_CODE = "en-us"
SITE_ID = 1
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# DATABASES
# ------------------------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL", default="postgres:///kuvaka")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# CACHES / REDIS
# ------------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# APPS
# ------------------------------------------------------------------------------
# Domain apps are kept modular: `doctors` and `patients` own their respective
# journeys end-to-end (models, serializers, viewsets), while `core` and `users`
# hold cross-cutting concerns shared by both (base models, auth, permissions).
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
]
LOCAL_APPS = [
    "apps.core",
    "apps.users",
    "apps.clinics",
    "apps.doctors",
    "apps.patients",
    "apps.clinical",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# AUTH
# ------------------------------------------------------------------------------
AUTH_USER_MODEL = "users.User"
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "apps.users.password_validators.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
    {
        "NAME": "apps.users.password_validators.PasswordHistoryValidator",
    },
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# CORS
# ------------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env.list("DJANGO_CORS_ALLOWED_ORIGINS", default=[])

# STATIC
# ------------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = str(BASE_DIR / "staticfiles")

# DJANGO REST FRAMEWORK
# ------------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    # Always return JSON — no browsable HTML UI in any environment
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Kuvaka Platform API",
    "DESCRIPTION": (
        "Unified backend serving both the doctor/clinic-facing and patient-facing platforms."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
}

# TRANSACTIONAL EMAIL (Brevo)
# ------------------------------------------------------------------------------
# See apps/core/services/email.py. Left blank, sends are logged instead of
# delivered, so local dev works without a Brevo account.
BREVO_API_KEY = env("BREVO_API_KEY", default="")
BREVO_SENDER_EMAIL = env("BREVO_SENDER_EMAIL", default="no-reply@kuvaka.io")
BREVO_SENDER_NAME = env("BREVO_SENDER_NAME", default="Kuvaka")

# OTP
# ------------------------------------------------------------------------------
OTP_LENGTH = env.int("OTP_LENGTH", default=6)
OTP_EXPIRY_MINUTES = env.int("OTP_EXPIRY_MINUTES", default=10)
OTP_MAX_ATTEMPTS = env.int("OTP_MAX_ATTEMPTS", default=5)

# ACCOUNT LOCKOUT
# ------------------------------------------------------------------------------
# HIPAA § 164.308(a)(5)(ii)(D) addressable.
# After LOCKOUT_MAX_ATTEMPTS consecutive failures the account is locked for
# LOCKOUT_DURATION_MINS minutes. Counter resets on successful login.
LOCKOUT_MAX_ATTEMPTS = env.int("LOCKOUT_MAX_ATTEMPTS", default=5)
LOCKOUT_DURATION_MINS = env.int("LOCKOUT_DURATION_MINS", default=30)

# PASSWORD POLICY
# ------------------------------------------------------------------------------
PASSWORD_MIN_LENGTH = env.int("PASSWORD_MIN_LENGTH", default=12)
# How many previous passwords to remember and block reuse of.
PASSWORD_HISTORY_COUNT = env.int("PASSWORD_HISTORY_COUNT", default=5)

# MFA / TOTP
# ------------------------------------------------------------------------------
# Short-lived token lifetime (seconds) bridging password-OK → TOTP-verify.
MFA_TOKEN_EXPIRY_SECONDS = env.int("MFA_TOKEN_EXPIRY_SECONDS", default=180)
# Issuer name shown in Authenticator apps.
MFA_ISSUER_NAME = env("MFA_ISSUER_NAME", default="Kuvaka")
