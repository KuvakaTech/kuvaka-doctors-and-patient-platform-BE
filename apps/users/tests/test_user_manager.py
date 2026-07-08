import pytest

from apps.users.models import User


@pytest.mark.django_db
def test_create_user_with_email():
    user = User.objects.create_user(email="doc@example.com", password="pass1234")
    assert user.email == "doc@example.com"
    assert user.check_password("pass1234")
    assert not user.is_staff


@pytest.mark.django_db
def test_create_superuser_defaults_to_clinic_admin():
    admin = User.objects.create_superuser(email="admin@example.com", password="pass1234")
    assert admin.is_staff
    assert admin.is_superuser
    assert admin.user_type == "clinic_admin"
