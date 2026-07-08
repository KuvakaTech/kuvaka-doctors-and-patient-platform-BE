import uuid

from django.db import models


class BaseModel(models.Model):
    """
    Abstract base for all domain models in both the doctors and patients apps.

    Records are never hard-deleted (see `deleted`) and every row is traceable
    to when it was created/modified, matching auditability conventions
    expected of a healthcare-grade backend.
    """

    id = models.BigAutoField(primary_key=True)
    external_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    created_date = models.DateTimeField(auto_now_add=True, db_index=True)
    modified_date = models.DateTimeField(auto_now=True, db_index=True)
    deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        abstract = True
        ordering = ("-created_date",)
