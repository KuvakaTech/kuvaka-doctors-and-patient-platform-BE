import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0002_user_email_verified_emailotp"),
    ]

    operations = [
        # Lockout fields on User
        migrations.AddField(
            model_name="user",
            name="failed_login_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="user",
            name="locked_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # PasswordHistory table
        migrations.CreateModel(
            name="PasswordHistory",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="password_history",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("password_hash", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="passwordhistory",
            index=models.Index(
                fields=["user", "created_at"], name="users_pwdhist_user_idx"
            ),
        ),
    ]
