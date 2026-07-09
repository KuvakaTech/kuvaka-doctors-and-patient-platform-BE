import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.CharField(
                        choices=[
                            ("login_success", "Login success"),
                            ("login_failed", "Login failed"),
                            ("logout", "Logout"),
                            ("password_reset_requested", "Password reset requested"),
                            ("password_reset_completed", "Password reset completed"),
                            ("password_changed", "Password changed"),
                            ("email_verified", "Email verified"),
                            ("otp_failed", "OTP verification failed"),
                            ("account_locked", "Account locked"),
                            ("token_blacklisted", "All tokens blacklisted"),
                        ],
                        db_index=True,
                        max_length=40,
                    ),
                ),
                ("email", models.EmailField(blank=True, db_index=True, max_length=254)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["user", "event", "created_at"], name="core_audit_user_event_idx"
            ),
        ),
    ]
