from rest_framework import serializers

from apps.clinics.models import (
    Clinic,
    ClinicInventoryItem,
    ClinicStaffMembership,
    Medicine,
    PurchaseOrder,
    STAFF_ROLE_CHOICES,
    StaffTaskGrant,
)
from apps.users.models import User


class ClinicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Clinic
        fields = (
            "external_id",
            "name",
            "registration_number",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "pincode",
            "phone_number",
            "email",
            "is_active",
        )
        read_only_fields = ("external_id", "is_active")


class StaffUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("external_id", "email", "phone_number", "full_name", "user_type")
        read_only_fields = fields


class ClinicStaffMembershipSerializer(serializers.ModelSerializer):
    user = StaffUserSerializer(read_only=True)

    class Meta:
        model = ClinicStaffMembership
        fields = ("external_id", "user", "role", "permissions", "is_active")
        read_only_fields = ("external_id", "user", "is_active")


class StaffCreateSerializer(serializers.Serializer):
    """
    Creates a brand-new staff `User` + `ClinicStaffMembership` in one call.
    A random password is generated and returned once in the response — the
    admin/doctor creating the account hands it to the staff member out of
    band (the account is pre-verified since an existing staff member vouches
    for it).
    """

    email = serializers.EmailField()
    full_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=15, required=False, allow_blank=True)
    role = serializers.ChoiceField(choices=STAFF_ROLE_CHOICES)
    permissions = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, default=list
    )

    def validate_email(self, value):
        email = value.lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email


class MedicineSerializer(serializers.ModelSerializer):
    class Meta:
        model = Medicine
        fields = ("external_id", "name", "generic_name", "dosage_form", "strength", "manufacturer")
        read_only_fields = ("external_id",)


class ClinicInventoryItemSerializer(serializers.ModelSerializer):
    medicine = serializers.SlugRelatedField(
        slug_field="external_id", queryset=Medicine.objects.filter(deleted=False)
    )
    medicine_detail = MedicineSerializer(source="medicine", read_only=True)
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = ClinicInventoryItem
        fields = (
            "external_id",
            "medicine",
            "medicine_detail",
            "batch_number",
            "expiry_date",
            "quantity_in_stock",
            "reorder_threshold",
            "unit_price",
            "is_low_stock",
        )
        read_only_fields = ("external_id", "medicine_detail", "is_low_stock")


class PurchaseOrderItemSerializer(serializers.Serializer):
    """Validates one entry of `PurchaseOrder.items` — not a model, just shape checking."""

    medicine_id = serializers.CharField()  # Medicine.external_id
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True)

    class Meta:
        model = PurchaseOrder
        fields = (
            "external_id",
            "supplier_name",
            "items",
            "status",
            "ordered_at",
            "received_at",
        )
        read_only_fields = ("external_id", "status", "ordered_at", "received_at")


class _PatientProfileSlugField(serializers.SlugRelatedField):
    """
    Resolves its queryset lazily via `apps.patients.models` rather than at
    class-body time — `apps.patients.serializers` imports this module, so an
    eager top-level import here would be circular.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("slug_field", "external_id")
        super().__init__(queryset=User.objects.none(), **kwargs)

    def get_queryset(self):
        from apps.patients.models import PatientProfile

        return PatientProfile.objects.filter(deleted=False)


class StaffTaskGrantSerializer(serializers.ModelSerializer):
    grantee = serializers.SlugRelatedField(
        slug_field="external_id", queryset=User.objects.filter(deleted=False)
    )
    grantee_detail = StaffUserSerializer(source="grantee", read_only=True)
    patient = _PatientProfileSlugField(required=False, allow_null=True)

    class Meta:
        model = StaffTaskGrant
        fields = (
            "external_id",
            "grantee",
            "grantee_detail",
            "patient",
            "task_type",
            "status",
            "expires_at",
            "revoked_at",
        )
        read_only_fields = ("external_id", "status", "revoked_at")
