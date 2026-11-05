import secrets
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


def generate_api_key():
    return f"spk_live_{secrets.token_hex(16)}"


def generate_invite_token():
    return secrets.token_urlsafe(32)


class Organization(models.Model):
    """One customer company. Everything else (contacts, messages...) points to this."""

    class RegistrationStatus(models.TextChoices):
        UNREGISTERED = "unregistered", "Unregistered"
        IN_REVIEW = "in_review", "In Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class LeadRoutingStrategy(models.TextChoices):
        DISABLED = "disabled", "Manual Team Queue (Off)"
        ROUND_ROBIN = "round_robin", "Round-Robin Rotation"
        WORKLOAD_BALANCED = "workload_balanced", "Workload-Balanced (Least Busy Rep)"

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
    # Profile & Address Details
    address_street = models.CharField(max_length=255, blank=True, default="")
    address_city = models.CharField(max_length=100, blank=True, default="")
    address_state = models.CharField(max_length=50, blank=True, default="")
    address_postal_code = models.CharField(max_length=20, blank=True, default="")
    address_country = models.CharField(max_length=50, blank=True, default="USA")
    website = models.URLField(max_length=255, blank=True, default="")
    notification_email = models.EmailField(blank=True, default="")
    support_phone = models.CharField(max_length=30, blank=True, default="")
    # Lead Distribution & Routing Configuration
    lead_routing_strategy = models.CharField(
        max_length=30,
        choices=LeadRoutingStrategy.choices,
        default=LeadRoutingStrategy.WORKLOAD_BALANCED,
    )
    max_leads_per_agent = models.PositiveIntegerField(
        default=5,
        help_text="Maximum simultaneous active conversations per agent before they are bypassed (0 = unlimited)",
    )
    route_only_available = models.BooleanField(
        default=True,
        help_text="Only route to team members currently marked Available for leads",
    )
    last_assigned_user = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Last user assigned a lead, used for round-robin rotation",
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
    is_available_for_leads = models.BooleanField(
        default=True,
        help_text="Rep is on-duty and accepting lead assignments",
    )
    last_lead_assigned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of when the user was last assigned a lead, used for fair tiebreaking",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def __str__(self):
        return self.email


class OrganizationInvite(models.Model):
    """
    Pending invitation to join an Organization.
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invites",
    )
    email = models.EmailField()
    role = models.CharField(
        max_length=10,
        choices=User.Role.choices,
        default=User.Role.AGENT,
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        default=generate_invite_token,
        db_index=True,
    )
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_invites",
    )
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*args, **kwargs)

    @property
    def is_valid(self) -> bool:
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()

    def __str__(self):
        return f"Invite for {self.email} to {self.organization.name} ({self.role})"

