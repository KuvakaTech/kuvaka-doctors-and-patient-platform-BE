from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.core.models import BaseModel


class UserType(models.TextChoices):
    """
    Discriminates which side of the platform an account belongs to.

    `PATIENT` accounts are extended by `apps.patients.models.PatientProfile`;
    every other type is extended by `apps.doctors.models.DoctorProfile`. One
    auth model with domain apps owning their own profile/role data, rather
    than separate user tables per app.
    """

    DOCTOR = "doctor", "Doctor"
    NURSE = "nurse", "Nurse"
    RECEPTIONIST = "receptionist", "Receptionist"
    LAB_TECHNICIAN = "lab_technician", "Lab Technician"
    PHARMACIST = "pharmacist", "Pharmacist"
    CLINIC_ADMIN = "clinic_admin", "Clinic Admin"
    PATIENT = "patient", "Patient"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email=None, phone_number=None, password=None, **extra_fields):
        if not email and not phone_number:
            raise ValueError("Users must have either an email or a phone number")
        if email:
            email = self.normalize_email(email)
        user = self.model(email=email, phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email=None, phone_number=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, phone_number, password, **extra_fields)

    def create_superuser(self, email=None, phone_number=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("user_type", UserType.CLINIC_ADMIN)
        return self._create_user(email, phone_number, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, BaseModel):
    email = models.EmailField(unique=True, null=True, blank=True)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=255, blank=True)
    user_type = models.CharField(max_length=20, choices=UserType.choices)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ("-created_date",)

    def __str__(self):
        return self.email or self.phone_number or str(self.external_id)
