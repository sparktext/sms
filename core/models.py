import secrets
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


def generate_api_key():
    return f"spk_live_{secrets.token_hex(16)}"


class Organization(models.Model):
    """One customer company. Everything else (contacts, messages...) points to this."""

    class RegistrationStatus(models.TextChoices):
        UNREGISTERED = "unregistered", "Unregistered"
        IN_REVIEW = "in_review", "In Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    timezone = models.CharField(max_length=64, default="America/Los_Angeles")
    ein = models.CharField(max_length=30, blank=True, default="")
    registration_status = models.CharField(
        max_length=20,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.UNREGISTERED,
    )
    brand_sid = models.CharField(max_length=64, blank=True, default="")
    campaign_sid = models.CharField(max_length=64, blank=True, default="")
    verification_notes = models.TextField(blank=True, default="")
    api_key = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="API key for ingesting leads via webhook or REST API",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = generate_api_key()
        super().save(*args, **kwargs)

    def regenerate_api_key(self):
        self.api_key = generate_api_key()
        self.save(update_fields=["api_key"])
        return self.api_key

    def __str__(self):
        return f"{self.name} ({self.slug})"


class UserManager(BaseUserManager):
    """Creates accounts that log in with an email instead of a username."""
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email.strip().lower())
        user = self.model(email=email, **extra)
        user.set_password(password)  # hashes the password, once
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("role", "owner")
        extra.setdefault("is_staff", True) 
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    """A login account. Belongs to one Organization."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        AGENT = "agent", "Agent"

    username = None
    email = models.EmailField(unique=True)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE,
        null=True, blank=True, related_name="users",
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.AGENT)
    phone = models.CharField(max_length=20, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def __str__(self):
        return self.email
