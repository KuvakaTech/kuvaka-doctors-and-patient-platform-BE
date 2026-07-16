from rest_framework import serializers

from apps.core.models import FinancialAuditLog


class FinancialAuditLogSerializer(serializers.ModelSerializer):
    """Read-only — this table is append-only, there is no writer here."""

    actor_email = serializers.CharField(source="actor.email", read_only=True, default=None)
    clinic_external_id = serializers.CharField(
        source="clinic.external_id", read_only=True, default=None
    )
    clinic_name = serializers.CharField(source="clinic.name", read_only=True, default=None)

    class Meta:
        model = FinancialAuditLog
        fields = (
            "id",
            "event",
            "object_type",
            "object_id",
            "amount",
            "metadata",
            "actor_email",
            "clinic_external_id",
            "clinic_name",
            "ip_address",
            "created_at",
        )
        read_only_fields = fields
