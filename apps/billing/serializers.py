from decimal import Decimal

from rest_framework import serializers

from apps.billing.models import (
    ChargeItem,
    ChargeItemDefinition,
    Invoice,
    Payment,
    PaymentKind,
)
from apps.clinical.models import Prescription
from apps.clinics.models import ClinicInventoryItem, Medicine
from apps.users.models import User

# ---------------------------------------------------------------------------
# Shared brief representations
# ---------------------------------------------------------------------------


class _UserBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("external_id", "email", "full_name", "user_type")
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------


class ChargeItemDefinitionSerializer(serializers.ModelSerializer):
    doctor = serializers.SlugRelatedField(slug_field="external_id", read_only=True)
    medicine = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Medicine.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = ChargeItemDefinition
        fields = (
            "external_id",
            "code",
            "title",
            "category",
            "price_components",
            "currency",
            "medicine",
            "doctor",
            "version",
            "is_active",
        )
        read_only_fields = ("external_id", "doctor", "currency", "version", "is_active")

    def validate_price_components(self, value):
        from apps.billing.services import validate_price_components

        validate_price_components(value)
        return value


class ChargeItemDefinitionCreateSerializer(serializers.Serializer):
    """
    `doctor_scoped=true` requests a per-doctor consultation fee, scoped to
    the calling doctor themselves — never a client-supplied `doctor` field,
    to rule out one doctor setting another's fee. The view resolves it
    to `request.user` and checks the role.
    """

    code = serializers.SlugField(max_length=64)
    title = serializers.CharField(max_length=255)
    category = serializers.ChoiceField(choices=ChargeItem._meta.get_field("category").choices)
    price_components = serializers.ListField(child=serializers.DictField())
    medicine = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Medicine.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    doctor_scoped = serializers.BooleanField(required=False, default=False)

    def validate_price_components(self, value):
        from apps.billing.services import validate_price_components

        validate_price_components(value)
        return value


# ---------------------------------------------------------------------------
# Charge items
# ---------------------------------------------------------------------------


class ChargeItemSerializer(serializers.ModelSerializer):
    definition = serializers.SlugRelatedField(slug_field="external_id", read_only=True)
    visit = serializers.SlugRelatedField(slug_field="external_id", read_only=True)
    prescription = serializers.SlugRelatedField(slug_field="external_id", read_only=True)
    inventory_item = serializers.SlugRelatedField(slug_field="external_id", read_only=True)
    performer = _UserBriefSerializer(read_only=True)
    invoice = serializers.SlugRelatedField(slug_field="external_id", read_only=True)

    class Meta:
        model = ChargeItem
        fields = (
            "external_id",
            "title",
            "category",
            "status",
            "quantity",
            "unit_price_components",
            "total_amount",
            "currency",
            "override_reason",
            "definition",
            "visit",
            "prescription",
            "inventory_item",
            "performer",
            "invoice",
            "service_date",
            "notes",
        )
        read_only_fields = (
            "external_id",
            "status",
            "total_amount",
            "currency",
            "definition",
            "visit",
            "prescription",
            "invoice",
        )


class ChargeItemCreateSerializer(serializers.Serializer):
    """
    Capture input. Exactly one of `definition` or `price_components` is
    required (a catalog charge, or an ad-hoc one) — `override_reason` is
    expected whenever a definition is given alongside price_components
    that diverge from it (checked in the view, not enforced at the DB
    level).
    """

    title = serializers.CharField(max_length=255)
    category = serializers.ChoiceField(choices=ChargeItem._meta.get_field("category").choices)
    quantity = serializers.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1"))
    definition = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=ChargeItemDefinition.objects.filter(deleted=False, is_active=True),
        required=False,
        allow_null=True,
    )
    price_components = serializers.ListField(child=serializers.DictField(), required=False)
    override_reason = serializers.CharField(required=False, allow_blank=True)
    prescription = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Prescription.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    inventory_item = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=ClinicInventoryItem.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    performer = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=User.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("definition") and not attrs.get("price_components"):
            raise serializers.ValidationError(
                "Specify a definition (catalog price) or price_components (ad-hoc price)."
            )
        return attrs


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


class InvoiceSerializer(serializers.ModelSerializer):
    charge_items = ChargeItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = (
            "external_id",
            "number",
            "status",
            "line_items_snapshot",
            "total_gross",
            "total_discount",
            "total_tax",
            "total_net",
            "amount_paid",
            "currency",
            "issued_at",
            "due_date",
            "cancelled_reason",
            "notes",
            "charge_items",
        )
        read_only_fields = (
            "external_id",
            "number",
            "status",
            "line_items_snapshot",
            "total_gross",
            "total_discount",
            "total_tax",
            "total_net",
            "amount_paid",
            "currency",
            "issued_at",
            "cancelled_reason",
        )


class InvoiceCreateSerializer(serializers.Serializer):
    charge_items = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=ChargeItem.objects.filter(deleted=False),
        many=True,
    )


class InvoiceCancelSerializer(serializers.Serializer):
    reason = serializers.CharField()


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class PaymentSerializer(serializers.ModelSerializer):
    invoice = serializers.SlugRelatedField(slug_field="external_id", read_only=True)
    received_by = _UserBriefSerializer(read_only=True)

    class Meta:
        model = Payment
        fields = (
            "external_id",
            "kind",
            "amount",
            "currency",
            "method",
            "tendered_amount",
            "returned_amount",
            "reference_number",
            "payment_datetime",
            "invoice",
            "received_by",
            "notes",
        )
        read_only_fields = ("external_id", "currency", "invoice", "received_by")


class PaymentCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=PaymentKind.choices, default=PaymentKind.PAYMENT)
    invoice = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Invoice.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.CharField(max_length=16, required=False, allow_blank=True)
    tendered_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    returned_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    reference_number = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class ApplyAdvanceSerializer(serializers.Serializer):
    invoice = serializers.SlugRelatedField(
        slug_field="external_id", queryset=Invoice.objects.filter(deleted=False)
    )
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class PatientAccountSerializer(serializers.Serializer):
    """Read-only rollup display — accounts are never directly created/edited via API."""

    external_id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    total_gross = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total_invoiced = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total_paid = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    advance_balance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    calculated_at = serializers.DateTimeField(read_only=True)
    patient = serializers.SerializerMethodField()

    def get_patient(self, obj):
        from apps.patients.serializers import PatientBriefSerializer

        return PatientBriefSerializer(obj.patient).data
