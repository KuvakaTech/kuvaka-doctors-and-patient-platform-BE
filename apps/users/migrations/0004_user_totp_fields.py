from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0003_lockout_and_password_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="totp_secret",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="user",
            name="totp_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
