# Generated manually for LabVault appliance CLI control

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("connect", "0002_alter_labvaultglobalprefs_ui_column_profiles"),
    ]

    operations = [
        migrations.AddField(
            model_name="cliinvocation",
            name="reason",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="cliinvocation",
            name="target",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="cliinvocation",
            name="result_state",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="cliinvocation",
            name="result_redacted",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="cliinvocation",
            name="remote_addr",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="cliinvocation",
            name="source",
            field=models.CharField(default="web", max_length=16),
        ),
        migrations.AlterModelOptions(
            name="cliinvocation",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    (
                        "control_labvault_services",
                        "Can control LabVault appliance services via CLI",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="CliAuthThrottle",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("username", models.CharField(db_index=True, max_length=150)),
                ("remote_addr", models.CharField(db_index=True, max_length=64)),
                ("failure_count", models.PositiveIntegerField(default=0)),
                ("first_failure_at", models.DateTimeField(blank=True, null=True)),
                ("last_failure_at", models.DateTimeField(blank=True, null=True)),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-updated_at"],
                "unique_together": {("username", "remote_addr")},
            },
        ),
    ]
