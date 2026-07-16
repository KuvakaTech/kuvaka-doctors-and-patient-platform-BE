import pytest
from rest_framework.test import APIClient

from apps.finance.models import BusinessUnit
from apps.users.models import User
from apps.users.tokens import issue_tokens


@pytest.mark.django_db
def test_registering_a_clinic_auto_creates_its_business_unit():
    doctor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(doctor)['access']}")

    response = client.post("/api/v1/clinics/", {"name": "Sharma Clinic"})
    assert response.status_code == 201, response.data

    unit = BusinessUnit.objects.get(owner=doctor)
    assert unit.unit_type == "clinic"
    assert unit.ownership == "owned"
    assert unit.name == "Sharma Clinic"
    assert str(unit.clinic.external_id) == response.data["external_id"]
