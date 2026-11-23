
"""
core/migrations/0006_auth_enhancements.py
------------------------------------------
Adds new User fields and creates UserProfile,
AccountEvent, EmailLog models.
"""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def generate_public_ids(apps, schema_editor):
    User = apps.get_model("core", "User")
    database = schema_editor.connection.alias

    user_ids = (
        User.objects.using(database)
        .filter(public_id__isnull=True)
        .values_list("pk", flat=True)
        .iterator(chunk_size=500)
    )

    for user_id in user_ids:
        User.objects.using(database).filter(pk=user_id).update(
            public_id=uuid.uuid4()
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_organization_last_assigned_user_and_more"),
    ]

    operations = [
        # --- New fields on User ---

        migrations.AddField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(
                null=True,
                editable=False,
                help_text="Public-facing opaque identifier for URLs",
            ),
        ),

        migrations.RunPython(
            generate_public_ids,
            migrations.RunPython.noop,
        ),

        migrations.AlterField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(
                default=uuid.uuid4,
                unique=True,
                editable=False,
                db_index=True,
                help_text="Public-facing opaque identifier for URLs",
            ),
        ),

        migrations.AddField(
            model_name="user",
            name="status",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("active", "Active"),
                    ("suspended", "Suspended"),
                    ("deleted", "Deleted"),
                ],
                default="active",
                db_index=True,
            ),
        ),

        migrations.AddField(
            model_name="user",
            name="signup_method",
            field=models.CharField(
                max_length=10,
                choices=[("email", "Email"), ("google", "Google")],
                default="email",
            ),
        ),

        migrations.AddField(
            model_name="user",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),

        migrations.AddField(
            model_name="user",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, null=True),
        ),

        migrations.AddField(
            model_name="user",
            name="email_verified_at",
            field=models.DateTimeField(null=True, blank=True),
        ),

        migrations.AddField(
            model_name="user",
            name="password_set_at",
            field=models.DateTimeField(null=True, blank=True),
        ),

        migrations.AddField(
            model_name="user",
            name="welcome_email_sent_at",
            field=models.DateTimeField(null=True, blank=True),
        ),

        migrations.AddField(
            model_name="user",
            name="last_login_ip",
            field=models.GenericIPAddressField(null=True, blank=True),
        ),

        migrations.AddField(
            model_name="user",
            name="suspended_at",
            field=models.DateTimeField(null=True, blank=True),
        ),

        migrations.AddField(
            model_name="user",
            name="deleted_at",
            field=models.DateTimeField(null=True, blank=True),
        ),

        migrations.AddField(
            model_name="user",
            name="terms_version",
            field=models.CharField(
                max_length=20,
                blank=True,
                default="1.0",
            ),
        ),

        migrations.AddField(
            model_name="user",
            name="marketing_opt_in",
            field=models.BooleanField(default=False),
        ),

        # --- UserProfile ---

        migrations.CreateModel(
            name="UserProfile",
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
                ("full_name", models.CharField(max_length=200, blank=True)),
                ("phone", models.CharField(max_length=30, blank=True)),
                ("company", models.CharField(max_length=200, blank=True)),
                ("avatar_url", models.URLField(blank=True)),
                ("timezone", models.CharField(max_length=64, default="Asia/Kolkata")),
                ("language", models.CharField(max_length=10, default="en")),
                ("onboarding_completed_at", models.DateTimeField(null=True, blank=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),

        # --- AccountEvent ---

        migrations.CreateModel(
            name="AccountEvent",
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
                (
                    "user_public_id",
                    models.UUIDField(null=True, blank=True, db_index=True),
                ),
                (
                    "event_type",
                    models.CharField(
                        max_length=50,
                        choices=[
                            ("account_created", "Account Created"),
                            ("email_verification_sent", "Email Verification Sent"),
                            ("email_verified", "Email Verified"),
                            ("password_set", "Password Set"),
                            ("password_changed", "Password Changed"),
                            ("password_reset_requested", "Password Reset Requested"),
                            ("password_reset", "Password Reset"),
                            ("login", "Login"),
                            ("login_failed", "Login Failed"),
                            ("logout", "Logout"),
                            ("social_linked", "Social Account Linked"),
                            ("welcome_email_sent", "Welcome Email Sent"),
                            ("profile_updated", "Profile Updated"),
                            ("onboarding_completed", "Onboarding Completed"),
                            ("account_suspended", "Account Suspended"),
                            ("account_deleted", "Account Deleted"),
                            ("account_recovered", "Account Recovered"),
                        ],
                        db_index=True,
                    ),
                ),
                ("metadata", models.JSONField(default=dict)),
                ("ip_address", models.GenericIPAddressField(null=True, blank=True)),
                ("user_agent", models.TextField(blank=True)),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                (
                    "user",
                    models.ForeignKey(
                        null=True,
                        blank=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),

        # --- EmailLog ---

        migrations.CreateModel(
            name="EmailLog",
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
                ("email_type", models.CharField(max_length=50, db_index=True)),
                ("to_address", models.EmailField()),
                (
                    "state",
                    models.CharField(
                        max_length=15,
                        choices=[
                            ("queued", "Queued"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("delivered", "Delivered"),
                            ("bounced", "Bounced"),
                        ],
                        default="queued",
                    ),
                ),
                ("provider_message_id", models.CharField(max_length=200, blank=True)),
                ("error", models.TextField(blank=True)),
                ("queued_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(null=True, blank=True)),
                (
                    "user",
                    models.ForeignKey(
                        null=True,
                        blank=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="email_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-queued_at"]},
        ),
    ]
