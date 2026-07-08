import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_health_check_returns_ok():
    client = APIClient()
    response = client.get("/api/v1/core/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
