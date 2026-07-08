from .base import *  # noqa: F403

DEBUG = True
SECRET_KEY = env("DJANGO_SECRET_KEY", default="local-insecure-secret-key")  # noqa: F405
ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = True
