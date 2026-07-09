import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_auditlog"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EmergencyAccess",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "accessed_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="break_glass_accesses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="break_glass_accessed_records",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("justification", models.TextField()),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True)),
                ("accessed_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="break_glass_reviews",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_notes", models.TextField(blank=True)),
            ],
            options={
                "ordering": ("-accessed_at",),
            },
        ),
        migrations.AddIndex(
            model_name="emergencyaccess",
            index=models.Index(
                fields=["accessed_by", "accessed_at"],
                name="core_bg_accessor_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="emergencyaccess",
            index=models.Index(
                fields=["patient", "accessed_at"],
                name="core_bg_patient_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="emergencyaccess",
            index=models.Index(
                fields=["reviewed_by"],
                name="core_bg_reviewer_idx",
            ),
        ),
    ]
