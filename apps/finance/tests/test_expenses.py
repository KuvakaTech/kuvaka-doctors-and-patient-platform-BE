"""
Expense tracking: PurchaseOrderReceiveView -> EXPENSE RevenueEntry, and
the dashboard's expenses/net totals.
"""

from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.clinics.models import (
    Clinic,
    ClinicStaffMembership,
    Medicine,
    PurchaseOrder,
    PurchaseOrderStatus,
)
from apps.finance.models import EntryDirection, EntryStatus, RevenueEntry, RevenueSource
from apps.finance.services import compute_dashboard, record_purchase_expense
from apps.users.models import User, UserType
from apps.users.tokens import issue_tokens


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user)['access']}")
    return client


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def clinic(doctor):
    return Clinic.objects.create(name="Sharma Clinic", owner=doctor)


@pytest.fixture
def medicine(doctor):
    return Medicine.objects.create(owner=doctor, name="Azithral 500")


@pytest.mark.django_db
def test_record_purchase_expense_creates_expense_entry(clinic, doctor, medicine):
    order = PurchaseOrder.objects.create(
        clinic=clinic,
        items=[{"medicine_id": str(medicine.external_id), "quantity": 10, "unit_price": "25.00"}],
        status=PurchaseOrderStatus.RECEIVED,
        ordered_by=doctor,
    )
    entry = record_purchase_expense(order)
    assert entry.direction == EntryDirection.EXPENSE
    assert entry.source_type == RevenueSource.SUPPLY_PURCHASE
    assert entry.amount == Decimal("250.00")
    assert entry.amount_received == Decimal("250.00")
    assert entry.status == EntryStatus.RECEIVED
    assert entry.doctor_id == clinic.owner_id
    assert entry.owner is None
    assert entry.split_enabled is False
    assert entry.purchase_order_id == order.pk


@pytest.mark.django_db
def test_record_purchase_expense_skips_lines_with_no_unit_price(clinic, doctor, medicine):
    order = PurchaseOrder.objects.create(
        clinic=clinic,
        items=[
            {"medicine_id": str(medicine.external_id), "quantity": 10, "unit_price": "25.00"},
            {"medicine_id": str(medicine.external_id), "quantity": 5},
        ],
        status=PurchaseOrderStatus.RECEIVED,
        ordered_by=doctor,
    )
    entry = record_purchase_expense(order)
    assert entry.amount == Decimal("250.00")


@pytest.mark.django_db
def test_purchase_order_receive_api_creates_expense_and_updates_dashboard(
    clinic, doctor, medicine
):
    ClinicStaffMembership.objects.create(clinic=clinic, user=doctor, role=UserType.DOCTOR)
    client = _authed_client(doctor)
    create_response = client.post(
        f"/api/v1/clinics/{clinic.external_id}/purchase-orders/",
        {
            "supplier_name": "MedSupply Co",
            "items": [
                {
                    "medicine_id": str(medicine.external_id),
                    "quantity": 20,
                    "unit_price": "15.00",
                }
            ],
        },
        format="json",
    )
    assert create_response.status_code == 201, create_response.data
    order_external_id = create_response.data["external_id"]

    receive_response = client.post(
        f"/api/v1/clinics/{clinic.external_id}/purchase-orders/{order_external_id}/receive/"
    )
    assert receive_response.status_code == 200, receive_response.data

    entry = RevenueEntry.objects.get(direction=EntryDirection.EXPENSE)
    assert entry.amount == Decimal("300.00")

    dashboard = compute_dashboard(
        doctor, None, date_from=date.today(), date_to=date.today(), granularity="day"
    )
    assert dashboard["totals"]["expenses"] == Decimal("300.00")
    assert dashboard["totals"]["net"] == dashboard["totals"]["received"] - Decimal("300.00")
