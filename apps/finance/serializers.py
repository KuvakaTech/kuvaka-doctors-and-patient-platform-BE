from decimal import Decimal

from rest_framework import serializers

from apps.clinics.models import Clinic
from apps.finance.models import (
    BusinessUnit,
    FinanceAccessGrant,
    RevenueEntry,
    RevenueShareRule,
    RevenueSource,
)
from apps.patients.models import PatientProfile
from apps.users.models import User

# ---------------------------------------------------------------------------
# Business units
# ---------------------------------------------------------------------------


class BusinessUnitSerializer(serializers.ModelSerializer):
    clinic = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Clinic.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = BusinessUnit
        fields = (
            "external_id",
            "name",
            "unit_type",
            "ownership",
            "clinic",
            "commission_rate",
            "contact_name",
            "contact_phone",
            "address",
            "notes",
            "is_active",
        )
        read_only_fields = ("external_id",)

    def validate_clinic(self, value):
        # A doctor may only mirror a clinic they themselves own — this is
        # their personal income attribution, not a shared clinic setting.
        request = self.context.get("request")
        if value is not None and request is not None and value.owner_id != request.user.id:
            raise serializers.ValidationError("You can only link a clinic you own.")
        return value


# ---------------------------------------------------------------------------
# Revenue entries
# ---------------------------------------------------------------------------


class _MREngagementItemSerializer(serializers.Serializer):
    """Validates one entry of metadata["items"] for MR_ENGAGEMENT entries — not a model."""

    kind = serializers.ChoiceField(
        choices=["samples", "gift", "sponsorship", "equipment", "other"]
    )
    description = serializers.CharField(max_length=255)
    estimated_value = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=Decimal("0")
    )


class RevenueEntrySerializer(serializers.ModelSerializer):
    business_unit = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=BusinessUnit.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    clinic = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Clinic.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    patient = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=PatientProfile.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    doctor_share_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    owner_share_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    # Viewer-relative rows the dashboard will reuse this serializer's
    # shape for — populated by the view when a viewer context is known.
    your_share_amount = serializers.SerializerMethodField()

    class Meta:
        model = RevenueEntry
        fields = (
            "external_id",
            "business_unit",
            "clinic",
            "source_type",
            "direction",
            "amount",
            "amount_received",
            "currency",
            "payment_mode",
            "status",
            "occurred_on",
            "settled_on",
            "split_enabled",
            "doctor_share_percentage",
            "doctor_share_amount",
            "owner_share_amount",
            "your_share_amount",
            "patient",
            "notes",
            "metadata",
        )
        read_only_fields = (
            "external_id",
            "currency",
            "direction",
            "status",
            "settled_on",
            "split_enabled",
            "doctor_share_percentage",
            "doctor_share_amount",
            "owner_share_amount",
        )

    def get_your_share_amount(self, obj):
        viewer_id = self.context.get("viewer_id")
        if viewer_id is None:
            return None
        if viewer_id == obj.owner_id:
            return obj.owner_share_amount
        return obj.doctor_share_amount

    def validate_amount_received(self, value):
        # On a partial update the request may only carry amount_received,
        # not amount — fall back to the persisted value so this check
        # still fires (the view/service layer re-checks too, but a field-
        # level 400 here is a better error shape than an IntegrityError).
        amount = self.initial_data.get("amount")
        if amount is None and self.instance is not None:
            amount = self.instance.amount
        if amount is not None and value is not None and Decimal(str(value)) > Decimal(str(amount)):
            raise serializers.ValidationError("Cannot exceed amount.")
        return value

    def validate_metadata(self, value):
        source_type = self.initial_data.get(
            "source_type", getattr(self.instance, "source_type", None)
        )
        if source_type == RevenueSource.MR_ENGAGEMENT and value.get("items"):
            item_serializer = _MREngagementItemSerializer(data=value["items"], many=True)
            item_serializer.is_valid(raise_exception=True)
        return value


# ---------------------------------------------------------------------------
# Revenue share rules
# ---------------------------------------------------------------------------


class RevenueShareRuleSerializer(serializers.ModelSerializer):
    clinic = serializers.SlugRelatedField(
        slug_field="external_id", queryset=Clinic.objects.filter(deleted=False)
    )
    doctor = serializers.SlugRelatedField(
        slug_field="external_id", queryset=User.objects.filter(deleted=False)
    )

    class Meta:
        model = RevenueShareRule
        fields = ("external_id", "clinic", "doctor", "enabled", "doctor_share_percentage", "notes")
        read_only_fields = ("external_id",)


# ---------------------------------------------------------------------------
# Finance access grants
# ---------------------------------------------------------------------------


class _GranteeBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("external_id", "email", "phone_number", "full_name", "user_type")
        read_only_fields = fields


class FinanceAccessGrantSerializer(serializers.ModelSerializer):
    """
    Read/list shape. Creation goes through FinanceAccessGrantCreateSerializer
    instead (resolves the grantee by exact email/phone match rather than
    requiring the caller already know their external_id — see the view).
    """

    grantee_detail = _GranteeBriefSerializer(source="grantee", read_only=True)
    clinic = serializers.SlugRelatedField(slug_field="external_id", read_only=True)
    business_unit = serializers.SlugRelatedField(slug_field="external_id", read_only=True)

    class Meta:
        model = FinanceAccessGrant
        fields = (
            "external_id",
            "grantee_detail",
            "clinic",
            "business_unit",
            "status",
            "expires_at",
            "revoked_at",
            "notes",
        )
        read_only_fields = fields


class FinanceAccessGrantCreateSerializer(serializers.Serializer):
    """
    Grantee resolved by EXACT email/phone match only — deliberately no
    fuzzy user-search endpoint, so grant creation can't be used to
    enumerate platform users.
    """

    grantee_email = serializers.EmailField(required=False, allow_blank=True)
    grantee_phone_number = serializers.CharField(max_length=15, required=False, allow_blank=True)
    clinic = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Clinic.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    business_unit = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=BusinessUnit.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        email = attrs.get("grantee_email")
        phone = attrs.get("grantee_phone_number")
        if bool(email) == bool(phone):
            raise serializers.ValidationError(
                "Specify exactly one of grantee_email or grantee_phone_number."
            )
        if attrs.get("clinic") and attrs.get("business_unit"):
            raise serializers.ValidationError("Specify at most one of clinic or business_unit.")
        return attrs
