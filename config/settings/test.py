from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-secret-key"

DATABASES["default"] = env.db(  # noqa: F405
    "DATABASE_URL", default="postgres://postgres:postgres@localhost:5432/kuvaka_test"
)

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
