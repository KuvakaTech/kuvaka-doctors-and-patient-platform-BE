import json

from django.db import models as django_models
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import (
    ChargeItem,
    ChargeItemDefinition,
    Invoice,
    PatientAccount,
    Payment,
    PaymentKind,
)
from apps.billing.permissions import (
    require_billing_access,
    require_patient_owner,
    require_refund_access,
    require_view_billing,
)
from apps.billing.serializers import (
    ApplyAdvanceSerializer,
    ChargeItemCreateSerializer,
    ChargeItemDefinitionCreateSerializer,
    ChargeItemDefinitionSerializer,
    ChargeItemSerializer,
    InvoiceCancelSerializer,
    InvoiceCreateSerializer,
    InvoiceSerializer,
    PatientAccountSerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
)
from apps.billing.services import (
    apply_advance,
    cancel_charge,
    cancel_invoice,
    capture_charge,
    create_draft_invoice,
    get_idempotent_response,
    hash_request_body,
    issue_invoice,
    post_payment,
    revise_definition,
    store_idempotent_response,
    update_definition_metadata,
)
from apps.clinics.models import Clinic
from apps.clinics.permissions import require_admin, require_membership
from apps.users.models import UserType


def _get_clinic(external_id) -> Clinic:
    return get_object_or_404(Clinic, external_id=external_id, deleted=False)


def _get_account(clinic, external_id) -> PatientAccount:
    return get_object_or_404(PatientAccount, clinic=clinic, external_id=external_id, deleted=False)


def _json_safe(data):
    """Round-trip through DRF's renderer so a stored idempotency response
    body is genuinely JSON-native (Decimal/UUID/datetime become strings),
    matching exactly what a real replay would return."""
    return json.loads(JSONRenderer().render(data).decode())


# ---------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------


class ChargeItemDefinitionListCreateView(generics.ListAPIView):
    serializer_class = ChargeItemDefinitionSerializer
    permission_classes = [IsAuthenticated]

    def get_clinic(self):
        return _get_clinic(self.kwargs["clinic_external_id"])

    def get_queryset(self):
        clinic = self.get_clinic()
        require_view_billing(self.request.user, clinic)
        qs = ChargeItemDefinition.objects.filter(clinic=clinic, deleted=False)
        params = self.request.query_params
        if params.get("category"):
            qs = qs.filter(category=params["category"])
        if params.get("active") is not None:
            qs = qs.filter(is_active=params["active"] == "true")
        if params.get("doctor"):
            qs = qs.filter(doctor__external_id=params["doctor"])
        if params.get("search"):
            qs = qs.filter(title__icontains=params["search"])
        return qs

    def post(self, request, clinic_external_id):
        clinic = self.get_clinic()
        require_membership(request.user, clinic)
        serializer = ChargeItemDefinitionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        doctor_scoped = data.pop("doctor_scoped")

        if doctor_scoped:
            if request.user.user_type != UserType.DOCTOR:
                raise PermissionDenied("Only a doctor account can create a doctor-scoped fee.")
            require_membership(request.user, clinic)
            doctor = request.user
        else:
            require_admin(request.user, clinic)
            doctor = None

        from apps.billing.services import create_definition

        definition = create_definition(
            clinic=clinic, doctor=doctor, created_by=request.user, **data
        )
        return Response(
            ChargeItemDefinitionSerializer(definition).data, status=status.HTTP_201_CREATED
        )


class ChargeItemDefinitionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_definition(self, request, clinic_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        definition = get_object_or_404(
            ChargeItemDefinition, clinic=clinic, external_id=external_id, deleted=False
        )
        if definition.doctor_id is not None:
            if definition.doctor_id != request.user.id:
                raise PermissionDenied(
                    "Only the doctor themselves can manage their own consultation fee."
                )
        else:
            require_admin(request.user, clinic)
        return definition

    def patch(self, request, clinic_external_id, external_id):
        definition = self._get_definition(request, clinic_external_id, external_id)

        if "price_components" in request.data:
            serializer = ChargeItemDefinitionSerializer(
                definition,
                data={"price_components": request.data["price_components"]},
                partial=True,
            )
            serializer.is_valid(raise_exception=True)
            updated = revise_definition(
                definition,
                price_components=serializer.validated_data["price_components"],
                updated_by=request.user,
            )
        else:
            allowed = {"title", "medicine"}
            fields = {k: v for k, v in request.data.items() if k in allowed}
            updated = update_definition_metadata(definition, **fields) if fields else definition

        return Response(ChargeItemDefinitionSerializer(updated).data)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class PatientAccountListView(generics.ListAPIView):
    serializer_class = PatientAccountSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        clinic = _get_clinic(self.kwargs["clinic_external_id"])
        require_view_billing(self.request.user, clinic)
        qs = PatientAccount.objects.filter(clinic=clinic, deleted=False)
        if self.request.query_params.get("patient"):
            qs = qs.filter(patient__external_id=self.request.query_params["patient"])
        if self.request.query_params.get("balance_due__gt") is not None:
            threshold = self.request.query_params["balance_due__gt"]
            qs = qs.filter(balance_due__gt=threshold)
        return qs


class PatientAccountDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, clinic_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        require_view_billing(request.user, clinic)
        account = _get_account(clinic, external_id)
        data = PatientAccountSerializer(account).data
        data["recent_charge_items"] = ChargeItemSerializer(
            account.charge_items.filter(deleted=False).order_by("-created_date")[:10], many=True
        ).data
        data["recent_invoices"] = InvoiceSerializer(
            account.invoices.filter(deleted=False).order_by("-created_date")[:5], many=True
        ).data
        data["recent_payments"] = PaymentSerializer(
            account.payments.filter(deleted=False).order_by("-payment_datetime")[:5], many=True
        ).data
        return Response(data)


# ---------------------------------------------------------------------------
# Charge items
# ---------------------------------------------------------------------------


class ChargeItemListCreateView(generics.ListAPIView):
    serializer_class = ChargeItemSerializer
    permission_classes = [IsAuthenticated]

    def get_account(self):
        clinic = _get_clinic(self.kwargs["clinic_external_id"])
        return _get_account(clinic, self.kwargs["account_external_id"])

    def get_queryset(self):
        account = self.get_account()
        require_view_billing(self.request.user, account.clinic)
        qs = ChargeItem.objects.filter(account=account, deleted=False)
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"])
        return qs.order_by("-service_date")

    def post(self, request, clinic_external_id, account_external_id):
        account = self.get_account()
        require_billing_access(request.user, account.clinic, patient=account.patient)
        serializer = ChargeItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        definition = data.get("definition")
        price_components = data.get("price_components")
        if (
            definition is not None
            and price_components is not None
            and not data.get("override_reason")
        ):
            raise ValidationError(
                {"override_reason": "Required when price_components diverges from the definition."}
            )

        charge = capture_charge(
            account=account,
            category=data["category"],
            title=data["title"],
            quantity=data.get("quantity", 1),
            definition=definition,
            price_components=price_components,
            override_reason=data.get("override_reason", ""),
            prescription=data.get("prescription"),
            inventory_item=data.get("inventory_item"),
            performer=data.get("performer"),
            recorded_by=request.user,
            notes=data.get("notes", ""),
            request=request,
        )
        return Response(ChargeItemSerializer(charge).data, status=status.HTTP_201_CREATED)


class ChargeItemCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, clinic_external_id, account_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        account = _get_account(clinic, account_external_id)
        require_billing_access(request.user, clinic, patient=account.patient)
        charge = get_object_or_404(
            ChargeItem, account=account, external_id=external_id, deleted=False
        )
        cancel_charge(charge, cancelled_by=request.user, request=request)
        return Response(ChargeItemSerializer(charge).data)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


class InvoiceListCreateView(generics.ListAPIView):
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]

    def get_account(self):
        clinic = _get_clinic(self.kwargs["clinic_external_id"])
        return _get_account(clinic, self.kwargs["account_external_id"])

    def get_queryset(self):
        account = self.get_account()
        require_view_billing(self.request.user, account.clinic)
        return Invoice.objects.filter(account=account, deleted=False).order_by("-created_date")

    def post(self, request, clinic_external_id, account_external_id):
        account = self.get_account()
        require_billing_access(request.user, account.clinic, patient=account.patient)
        serializer = InvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = create_draft_invoice(
            account=account, charge_items=list(serializer.validated_data["charge_items"])
        )
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


class InvoiceDetailView(APIView):
    """GET is available to billing staff or the owning patient — the printable payload."""

    permission_classes = [IsAuthenticated]

    def get(self, request, clinic_external_id, account_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        account = _get_account(clinic, account_external_id)
        invoice = get_object_or_404(
            Invoice, account=account, external_id=external_id, deleted=False
        )
        if request.user.user_type == UserType.PATIENT:
            require_patient_owner(request.user, account)
        else:
            require_view_billing(request.user, clinic)
        return Response(InvoiceSerializer(invoice).data)


class InvoiceIssueView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, clinic_external_id, account_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        account = _get_account(clinic, account_external_id)
        require_billing_access(request.user, clinic, patient=account.patient)
        invoice = get_object_or_404(
            Invoice, account=account, external_id=external_id, deleted=False
        )
        issue_invoice(invoice, issued_by=request.user, request=request)
        return Response(InvoiceSerializer(invoice).data)


class InvoiceCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, clinic_external_id, account_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        account = _get_account(clinic, account_external_id)
        require_refund_access(request.user, clinic, patient=account.patient)
        invoice = get_object_or_404(
            Invoice, account=account, external_id=external_id, deleted=False
        )
        serializer = InvoiceCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cancel_invoice(
            invoice,
            reason=serializer.validated_data["reason"],
            cancelled_by=request.user,
            request=request,
        )
        return Response(InvoiceSerializer(invoice).data)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class PaymentListCreateView(generics.ListAPIView):
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]

    def get_account(self):
        clinic = _get_clinic(self.kwargs["clinic_external_id"])
        return _get_account(clinic, self.kwargs["account_external_id"])

    def get_queryset(self):
        account = self.get_account()
        require_view_billing(self.request.user, account.clinic)
        return Payment.objects.filter(account=account, deleted=False).order_by("-payment_datetime")

    def post(self, request, clinic_external_id, account_external_id):
        account = self.get_account()
        clinic = account.clinic

        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            raise ValidationError(
                {"Idempotency-Key": "This header is required to post a payment."}
            )
        request_hash = hash_request_body(request.data)
        replay = get_idempotent_response(account, idempotency_key, request_hash)
        if replay is not None:
            replay_status, replay_body = replay
            return Response(replay_body, status=replay_status)

        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        kind = data["kind"]

        if kind == PaymentKind.REFUND:
            require_refund_access(request.user, clinic, patient=account.patient)
        else:
            require_billing_access(request.user, clinic, patient=account.patient)

        payment = post_payment(
            account=account,
            kind=kind,
            amount=data["amount"],
            invoice=data.get("invoice"),
            method=data.get("method", ""),
            tendered_amount=data.get("tendered_amount"),
            returned_amount=data.get("returned_amount"),
            reference_number=data.get("reference_number", ""),
            received_by=request.user,
            notes=data.get("notes", ""),
            request=request,
        )
        body = _json_safe(PaymentSerializer(payment).data)
        store_idempotent_response(
            account, idempotency_key, request_hash, status.HTTP_201_CREATED, body
        )
        return Response(body, status=status.HTTP_201_CREATED)


class ApplyAdvanceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, clinic_external_id, account_external_id):
        clinic = _get_clinic(clinic_external_id)
        account = _get_account(clinic, account_external_id)
        require_billing_access(request.user, clinic, patient=account.patient)
        serializer = ApplyAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = apply_advance(
            account=account,
            invoice=serializer.validated_data["invoice"],
            amount=serializer.validated_data["amount"],
            applied_by=request.user,
            request=request,
        )
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Reconciliation day-book
# ---------------------------------------------------------------------------


class ReconciliationView(APIView):
    """The cash-drawer close report: payments by date/method/receiver, with totals."""

    permission_classes = [IsAuthenticated]

    def get(self, request, clinic_external_id):
        clinic = _get_clinic(clinic_external_id)
        require_view_billing(request.user, clinic)

        qs = Payment.objects.filter(account__clinic=clinic, deleted=False)
        params = request.query_params
        if params.get("from"):
            qs = qs.filter(payment_datetime__date__gte=params["from"])
        if params.get("to"):
            qs = qs.filter(payment_datetime__date__lte=params["to"])

        by_method = list(
            qs.values("method")
            .annotate(total=django_models.Sum("amount"), count=django_models.Count("id"))
            .order_by("-total")
        )
        by_receiver = list(
            qs.values("received_by__external_id", "received_by__full_name")
            .annotate(total=django_models.Sum("amount"), count=django_models.Count("id"))
            .order_by("-total")
        )
        total = qs.aggregate(total=django_models.Sum("amount"))["total"] or 0

        return Response(
            {
                "total": total,
                "by_method": by_method,
                "by_receiver": [
                    {
                        "receiver": row["received_by__external_id"],
                        "name": row["received_by__full_name"],
                        "total": row["total"],
                        "count": row["count"],
                    }
                    for row in by_receiver
                ],
                "payments": PaymentSerializer(
                    qs.order_by("-payment_datetime")[:200], many=True
                ).data,
            }
        )


# ---------------------------------------------------------------------------
# Patient-facing ("my") reads
# ---------------------------------------------------------------------------


def _require_patient(user):
    if user.user_type != UserType.PATIENT:
        raise PermissionDenied("This endpoint is only available to patient accounts.")
    return user.patient_profile


class MyAccountsView(generics.ListAPIView):
    serializer_class = PatientAccountSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        profile = _require_patient(self.request.user)
        return PatientAccount.objects.filter(patient=profile, deleted=False)


class MyInvoicesView(generics.ListAPIView):
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        profile = _require_patient(self.request.user)
        return Invoice.objects.filter(patient=profile, deleted=False).order_by("-created_date")


class MyPaymentsView(generics.ListAPIView):
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        profile = _require_patient(self.request.user)
        return Payment.objects.filter(account__patient=profile, deleted=False).order_by(
            "-payment_datetime"
        )
